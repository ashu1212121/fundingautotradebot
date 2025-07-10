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
        self.LEVERAGE = 75         # Leverage fixed at 75x
        self.ENTRY_TIME = (1, 29, 59, 250000)  # 01:29:59:250 AM IST
        self.time_offset = 0.0
        self.entry_time = None
        self.precomputed_qty = None

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
        now = datetime.fromtimestamp(self._get_server_time() / 1000, IST)
        target = now.replace(
            hour=hour,
            minute=minute,
            second=second,
            microsecond=microsecond
        )
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

    async def _get_market_price(self, async_client):
        ticker = await async_client.futures_mark_price(symbol=self.SYMBOL)
        return float(ticker['markPrice'])

    async def _precompute_qty(self, async_client, margin_usd=5.5):
        """Calculate 95% of the qty using price 2 seconds before entry, rounded to 0 decimals."""
        market_price = await self._get_market_price(async_client)
        qty = (margin_usd * self.LEVERAGE) / market_price
        precomputed_qty = int(qty * 0.95)
        if precomputed_qty < 1:
            precomputed_qty = 1
        logging.info(f"Precomputed qty (95% of $5.5 margin at 75x, 2s before entry): {precomputed_qty} contracts at price {market_price}")
        self.precomputed_qty = precomputed_qty

    async def _execute_order(self, async_client, side, reduce_only=False):
        try:
            qty = self.precomputed_qty
            order = await async_client.futures_create_order(
                symbol=self.SYMBOL,
                side=side,
                type='MARKET',
                quantity=qty,
                reduceOnly=reduce_only,
                newOrderRespType='FULL'
            )
            logging.info(f"Market {side} order executed for {qty} {self.SYMBOL}: {order}")
            return order
        except Exception as e:
            logging.error(f"Failed to execute market {side} order: {e}")
            raise

    async def _transfer_80pct_total_usdt_futures(self, api_key, api_secret, async_client):
        try:
            # Get total available USDT in Futures
            futures_balance = await async_client.futures_account_balance()
            usdt_balance = 0
            for asset in futures_balance:
                if asset['asset'] == 'USDT':
                    usdt_balance = float(asset['balance'])
                    break
            transfer_amount = round(usdt_balance * 0.80, 2)
            if transfer_amount > 0:
                transfer_result = await async_futures_transfer(
                    api_key, api_secret, 'USDT', transfer_amount, 2
                )
                logging.info(f"Transferred {transfer_amount} USDT (80% of total futures USDT) from Futures to Spot: {transfer_result}")
            else:
                logging.info("Transfer amount is zero, skipping transfer.")
        except Exception as e:
            logging.error(f"Failed to transfer 80% of total futures USDT to spot: {e}")

    async def _monitor_funding_fee_and_sell(self, async_client, api_key, api_secret):
        """Monitor funding fee, then sell and transfer 80% of total futures USDT in parallel."""
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
                        # Start transfer and exit concurrently (no latency between them)
                        transfer_task = asyncio.create_task(
                            self._transfer_80pct_total_usdt_futures(api_key, api_secret, async_client)
                        )
                        sell_task = asyncio.create_task(
                            self._execute_order(
                                async_client, 'SELL',
                                reduce_only=True
                            )
                        )
                        done, pending = await asyncio.wait(
                            [transfer_task, sell_task],
                            return_when=asyncio.ALL_COMPLETED
                        )
                        logging.info(f"ReduceOnly Sell order executed to close long, and 80% of total available futures USDT transferred.")
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
            # --- New: Precompute qty 2 seconds before entry ---
            precompute_time = entry_target - 2000
            await self._precision_wait(precompute_time)
            await self._precompute_qty(async_client, margin_usd=5.5)
            # --- Wait for actual entry time ---
            await self._precision_wait(entry_target)
            logging.info("\n=== ENTRY TRIGGERED ===")
            buy_order = await self._execute_order(async_client, 'BUY')
            self.entry_time = self._get_server_time()
            await self._monitor_funding_fee_and_sell(async_client, api_key, api_secret)
        except Exception as e:
            logging.error(f"Strategy failed: {e}")
        finally:
            await async_client.close_connection()

if __name__ == "__main__":
    trader = PrecisionFuturesTrader()
    asyncio.run(trader.execute_strategy())
