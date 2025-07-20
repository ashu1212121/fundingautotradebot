import os
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from binance import AsyncClient

# Fast logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s,%(msecs)03d - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler('bot_debug.log'),
        logging.StreamHandler()
    ]
)

IST = timezone(timedelta(hours=5, minutes=30))  # Indian Standard Time

class DIAUSDTTrader:
    def __init__(self):
        self.SYMBOL = 'DIAUSDT'
        self.LEVERAGE = 35
        self.FIXED_QTY = 610
        self.ENTRY_IST = "21:29:59.200" # hh:mm:ss.mmm IST
        self.MARGIN_TYPE = 'CROSSED'
        self.FUNDING_POLL_INTERVAL = 0.05  # 50 ms for ultra-fast polling
        self.FUNDING_POLL_TIMEOUT = 10     # 10 seconds safety timeout

    async def _verify_connection(self, async_client):
        account_info = await async_client.futures_account()
        if not account_info['canTrade']:
            raise PermissionError("Futures trading disabled")
        logging.info("API connection verified")

    async def _set_leverage_and_margin_type(self, async_client):
        try:
            await async_client.futures_change_margin_type(
                symbol=self.SYMBOL,
                marginType=self.MARGIN_TYPE
            )
            logging.info(f"Margin type set to {self.MARGIN_TYPE} for {self.SYMBOL}")
        except Exception as e:
            if "No need to change margin type" not in str(e):
                logging.warning(f"Could not set margin type: {e}")
            else:
                logging.info(f"Margin type already {self.MARGIN_TYPE} for {self.SYMBOL}")
        await async_client.futures_change_leverage(
            symbol=self.SYMBOL,
            leverage=self.LEVERAGE
        )
        logging.info(f"Leverage set to {self.LEVERAGE}x for {self.SYMBOL}")

    async def _wait_until_entry_time(self, async_client):
        ist_now = datetime.now(IST)
        entry_h, entry_m, entry_s_ms = self.ENTRY_IST.split(":")
        entry_s, entry_ms = entry_s_ms.split(".")
        entry_dt_ist = ist_now.replace(hour=int(entry_h), minute=int(entry_m), second=int(entry_s), microsecond=int(entry_ms)*1000)
        if entry_dt_ist < ist_now:
            entry_dt_ist += timedelta(days=1)
        entry_dt_utc = entry_dt_ist.astimezone(timezone.utc)
        server_time_obj = await async_client.futures_time()
        # Use timezone-aware UTC datetime
        binance_now_utc = datetime.fromtimestamp(server_time_obj['serverTime']/1000.0, timezone.utc)
        wait_sec = (entry_dt_utc - binance_now_utc).total_seconds()
        logging.info(f"Waiting {wait_sec:.3f} seconds until {self.ENTRY_IST} IST (Binance UTC: {entry_dt_utc.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]})")
        if wait_sec > 0:
            await asyncio.sleep(wait_sec)
        else:
            logging.warning("Entry time already passed, executing immediately.")

    async def _open_long(self, async_client):
        order = await async_client.futures_create_order(
            symbol=self.SYMBOL,
            side='BUY',
            type='MARKET',
            quantity=self.FIXED_QTY,
            newOrderRespType='RESULT'
        )
        logging.info(f"Market BUY order executed for {self.FIXED_QTY} {self.SYMBOL}: {order}")
        fill_price = float(order.get('avgFillPrice', 0)) or None
        if not fill_price:
            position = await async_client.futures_position_information(symbol=self.SYMBOL)
            for pos in position:
                if pos['symbol'] == self.SYMBOL and float(pos['positionAmt']) > 0:
                    fill_price = float(pos['entryPrice'])
                    break
        if fill_price is None:
            raise Exception("Could not determine fill price for DIAUSDT")
        return order['orderId'], fill_price

    async def _poll_funding_fee(self, async_client, entry_time_ms, poll_interval=0.05, timeout=10):
        logging.info("Polling for funding fee after entry time %s...", entry_time_ms)
        start_time = datetime.utcnow()
        deadline = start_time + timedelta(seconds=timeout)
        seen_funding_time = None
        while datetime.utcnow() < deadline:
            try:
                income_records = await async_client.futures_income_history(symbol=self.SYMBOL, incomeType="FUNDING_FEE", limit=1)
            except Exception as e:
                logging.warning(f"Error fetching funding fee history: {e}")
                await asyncio.sleep(poll_interval)
                continue
            for fee in income_records:
                if int(fee['time']) > entry_time_ms and float(fee['income']) != 0:
                    if seen_funding_time is None or int(fee['time']) > seen_funding_time:
                        seen_funding_time = int(fee['time'])
                        logging.info(f"Funding fee received! {fee}")
                        return fee
            await asyncio.sleep(poll_interval)
        logging.warning("Timeout waiting for funding fee.")
        return None

    async def _close_long_market(self, async_client):
        qty = self.FIXED_QTY
        order = await async_client.futures_create_order(
            symbol=self.SYMBOL,
            side='SELL',
            type='MARKET',
            quantity=qty,
            reduceOnly=True,
            newOrderRespType='RESULT'
        )
        logging.info(f"Market SELL close executed for {qty} {self.SYMBOL}: {order}")
        return order

    async def execute_once(self):
        api_key = os.getenv('BINANCE_API_KEY')
        api_secret = os.getenv('BINANCE_API_SECRET')
        async_client = await AsyncClient.create(api_key, api_secret)
        try:
            await self._verify_connection(async_client)
            await self._set_leverage_and_margin_type(async_client)
            await self._wait_until_entry_time(async_client)
            entry_time_ms = (await async_client.futures_time())['serverTime']
            buy_order_id, entry_price = await self._open_long(async_client)
            funding_fee_record = await self._poll_funding_fee(
                async_client,
                entry_time_ms,
                poll_interval=self.FUNDING_POLL_INTERVAL,
                timeout=self.FUNDING_POLL_TIMEOUT
            )
            if funding_fee_record:
                logging.info("Funding fee confirmed, exiting position immediately.")
                await self._close_long_market(async_client)
            else:
                logging.warning("Funding fee not confirmed, but exiting position for safety.")
                await self._close_long_market(async_client)
        except Exception as e:
            logging.error(f"Trade execution failed: {e}")
        finally:
            await async_client.close_connection()

if __name__ == "__main__":
    trader = DIAUSDTTrader()
    asyncio.run(trader.execute_once())
