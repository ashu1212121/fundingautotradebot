import os
import asyncio
import logging
from datetime import datetime, timedelta, timezone
import time
import aiohttp
import hmac
import hashlib
import urllib.parse

from binance import AsyncClient

# Configure logging with milliseconds
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

async def async_futures_transfer(api_key, api_secret, asset, amount, type_):
    """
    Custom async function to transfer from Futures to Spot using Binance REST API.
    """
    base_url = "https://api.binance.com"
    endpoint = "/sapi/v1/futures/transfer"
    timestamp = int(time.time() * 1000)
    params = {
        "asset": asset,
        "amount": amount,
        "type": type_,
        "timestamp": timestamp
    }
    query_string = urllib.parse.urlencode(params)
    signature = hmac.new(
        api_secret.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256
    ).hexdigest()
    headers = {"X-MBX-APIKEY": api_key}
    url = f"{base_url}{endpoint}?{query_string}&signature={signature}"

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers) as resp:
            data = await resp.json()
            if resp.status != 200:
                raise Exception(f"Binance transfer failed: {data}")
            return data

class PrecisionFuturesTrader:
    def __init__(self):
        self.SYMBOL = 'HYPERUSDT'  # Trading pair
        self.FIXED_QTY = 1250      # Quantity (fixed at 1250 HYPER)
        self.LEVERAGE = 75         # Leverage fixed at 75x
        # 12:29:59.200 AM IST (00:29:59.200)
        self.ENTRY_TIME = (0, 29, 59, 200000)  
        self.time_offset = 0.0
        self.entry_time = None
        self.liquidation_price = None

    async def _verify_connection(self, async_client):
        try:
            account_info = await async_client.futures_account()
            if not account_info['canTrade']:
                raise PermissionError("Futures trading disabled")
            logging.info("API connection verified")
        except Exception as e:
            logging.error(f"Connection failed: {e}")
            raise

    async def _calibrate_time_sync(self, async_client):
        measurements = []
        for _ in range(20):
            try:
                t0 = time.time() * 1000
                server_time = (await async_client.futures_time())['serverTime']
                t1 = time.time() * 1000
                latency = t1 - t0
                offset = server_time - ((t0 + t1) / 2)
                measurements.append((latency, offset))
            except Exception as e:
                logging.warning(f"Time sync failed: {e}")
        avg_latency = sum(m[0] for m in measurements) / len(measurements) if measurements else 0
        self.time_offset = sum(m[1] for m in measurements) / len(measurements) if measurements else 0
        logging.info(f"Time synced | Offset: {self.time_offset:.2f}ms | Latency: {avg_latency:.2f}ms")

    async def _set_leverage_and_margin_type(self, async_client):
        # Set cross margin type
        try:
            await async_client.futures_change_margin_type(
                symbol=self.SYMBOL,
                marginType='CROSSED'
            )
            logging.info(f"Margin type set to CROSSED for {self.SYMBOL}")
        except Exception as e:
            if "No need to change margin type" not in str(e):
                logging.warning(f"Could not set margin type: {e}")
            else:
                logging.info(f"Margin type already CROSSED for {self.SYMBOL}")
        # Set leverage
        await async_client.futures_change_leverage(
            symbol=self.SYMBOL,
            leverage=self.LEVERAGE
        )
        logging.info(f"Leverage set to {self.LEVERAGE}x for {self.SYMBOL}")

    def _get_server_time(self):
        return time.time() * 1000 + self.time_offset  # ms

    def _calculate_target(self, hour, minute, second, microsecond):
        # Get current server time in IST, then adjust to target time
        now = datetime.fromtimestamp(self._get_server_time() / 1000, IST)
        target = now.replace(
            hour=hour,
            minute=minute,
            second=second,
            microsecond=microsecond
        )
        # If target time already passed today, schedule for tomorrow
        if target < now:
            target += timedelta(days=1)
        return target.timestamp() * 1000  # ms

    async def _precision_wait(self, target_ts):
        while True:
            current = self._get_server_time()
            if current >= target_ts:
                return
            remaining = target_ts - current
            await asyncio.sleep(max(remaining / 2000, 0.001))

    async def _get_liquidation_price(self, async_client):
        positions = await async_client.futures_position_information(symbol=self.SYMBOL)
        for pos in positions:
            if float(pos.get("positionAmt", 0)) > 0:
                return float(pos.get("liquidationPrice", 0))
        return None

    async def _get_long_position_amt(self, async_client):
        positions = await async_client.futures_position_information(symbol=self.SYMBOL)
        for pos in positions:
            amt = float(pos.get("positionAmt", 0))
            if amt > 0:
                return amt
        return 0

    async def _execute_order(self, async_client, side, quantity=None, reduce_only=False):
        try:
            qty = quantity if quantity else self.FIXED_QTY
            order = await async_client.futures_create_order(
                symbol=self.SYMBOL,
                side=side,
                type='MARKET',
                quantity=qty,
                reduceOnly=reduce_only,  # Important for safety!
                newOrderRespType='FULL'
            )
            logging.info(f"Market {side} order executed for {qty} {self.SYMBOL}: {order}")
            return order
        except Exception as e:
            logging.error(f"Failed to execute market {side} order: {e}")
            raise

    async def _transfer_funding_fee(self, api_key, api_secret, funding_income):
        try:
            income_amount = float(funding_income)
            if income_amount > 1:
                # Transfer 70% of funding fee, rounded to 2 decimals
                transfer_amount = round(income_amount * 0.70, 2)
                # type=2 means transfer from USDT-M Futures to Spot
                transfer_result = await async_futures_transfer(
                    api_key, api_secret, 'USDT', transfer_amount, 2
                )
                logging.info(f"Transferred {transfer_amount} USDT from Futures to Spot: {transfer_result}")
            else:
                logging.info("Funding fee <= 1 USDT, skipping transfer.")
        except Exception as e:
            logging.error(f"Failed to transfer funding fee to spot: {e}")

    async def _monitor_funding_fee_and_sell(self, async_client, api_key, api_secret):
        """Monitor funding fee, then attempt to transfer funding fee and close position without risk of opening a short."""
        try:
            while True:
                income_history = await async_client.futures_income_history(
                    symbol=self.SYMBOL,
                    incomeType='FUNDING_FEE',
                    limit=10
                )
                for income in income_history:
                    funding_time = income['time']
                    if funding_time > self.entry_time:
                        logging.info("\n=== FUNDING FEE DETECTED ===")
                        logging.info(f"Funding fee credited: {income}")
                        # Start transfer and exit concurrently
                        funding_income = income.get('income', '0')
                        transfer_task = asyncio.create_task(
                            self._transfer_funding_fee(api_key, api_secret, funding_income)
                        )
                        sell_task = asyncio.create_task(
                            self._execute_order(
                                async_client, 'SELL',
                                quantity=self.FIXED_QTY,
                                reduce_only=True
                            )
                        )
                        # Wait for both tasks to finish, but do not delay sell for transfer
                        done, pending = await asyncio.wait(
                            [transfer_task, sell_task],
                            return_when=asyncio.ALL_COMPLETED
                        )
                        logging.info(f"ReduceOnly Sell order executed for {self.FIXED_QTY} contracts of {self.SYMBOL} (attempt to close long).")
                        return
                await asyncio.sleep(0.01)
        except Exception as e:
            logging.error(f"Error while monitoring funding fee: {e}")
            raise

    async def execute_strategy(self):
        api_key = os.getenv('BINANCE_API_KEY')
        api_secret = os.getenv('BINANCE_API_SECRET')
        async_client = await AsyncClient.create(api_key, api_secret)
        try:
            await self._verify_connection(async_client)
            await self._set_leverage_and_margin_type(async_client)
            await self._calibrate_time_sync(async_client)
            entry_target = self._calculate_target(*self.ENTRY_TIME)
            await self._precision_wait(entry_target)
            logging.info("\n=== ENTRY TRIGGERED ===")
            buy_order = await self._execute_order(async_client, 'BUY')
            self.entry_time = self._get_server_time()
            # Fetch and log liquidation price after entry
            self.liquidation_price = await self._get_liquidation_price(async_client)
            if self.liquidation_price:
                logging.info(f"Liquidation price after entry: {self.liquidation_price}")
            else:
                logging.warning("Could not fetch liquidation price after entry.")
            await self._monitor_funding_fee_and_sell(async_client, api_key, api_secret)
        except Exception as e:
            logging.error(f"Strategy failed: {e}")
        finally:
            await async_client.close_connection()

if __name__ == "__main__":
    trader = PrecisionFuturesTrader()
    asyncio.run(trader.execute_strategy())
