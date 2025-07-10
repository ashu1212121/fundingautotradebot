from binance import AsyncClient
import os
import asyncio
import logging
from datetime import datetime, timedelta, timezone
import time

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

class PrecisionFuturesTrader:
    def __init__(self):
        self.SYMBOL = 'HYPERUSDT'  # Trading pair
        self.FIXED_QTY = 1700      # Quantity (fixed at 1700 HYPER)
        self.LEVERAGE = 75         # Leverage fixed at 75x
        self.ENTRY_TIME = (19, 29, 59, 150000)  # 07:29:59.150 PM IST (microseconds)
        self.time_offset = 0.0
        self.order_plan = []
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

    async def _set_leverage(self, async_client):
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

    async def _execute_order(self, async_client, side, quantity=None):
        try:
            qty = quantity if quantity else self.FIXED_QTY
            order = await async_client.futures_create_order(
                symbol=self.SYMBOL,
                side=side,
                type='MARKET',
                quantity=qty,
                newOrderRespType='FULL'
            )
            logging.info(f"Market {side} order executed for {qty} {self.SYMBOL}: {order}")
            return order
        except Exception as e:
            logging.error(f"Failed to execute market {side} order: {e}")
            raise

    async def _fetch_order_book(self, async_client):
        try:
            order_book = await async_client.futures_order_book(symbol=self.SYMBOL, limit=50)
            bids = order_book['bids']
            remaining_qty = self.FIXED_QTY
            self.order_plan = []
            for price, qty in bids:
                qty = float(qty)
                if remaining_qty <= 0:
                    break
                chunk_qty = min(remaining_qty, qty)
                self.order_plan.append((float(price), chunk_qty))
                remaining_qty -= chunk_qty
            logging.info(f"Sell order plan created: {self.order_plan}")
        except Exception as e:
            logging.error(f"Failed to fetch order book: {e}")
            raise

    async def _execute_sell_plan(self, async_client):
        try:
            # Before selling, confirm position is still open (not liquidated)
            positions = await async_client.futures_position_information(symbol=self.SYMBOL)
            long_amt = next((float(p["positionAmt"]) for p in positions if float(p["positionAmt"]) > 0), 0)
            if long_amt <= 0:
                logging.warning("No open LONG position to close (possibly liquidated). Sell aborted.")
                return
            qty = min(self.FIXED_QTY, long_amt)
            await self._execute_order(async_client, 'SELL', quantity=qty)
            logging.info(f"Sell order executed for {qty} contracts of {self.SYMBOL} (closing long).")
        except Exception as e:
            logging.error(f"Failed to execute sell plan: {e}")
            raise

    async def _monitor_funding_fee_and_sell(self, async_client):
        """Monitor funding fee, then attempt to close position if not liquidated."""
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
                        # Check for liquidation BEFORE selling
                        positions = await async_client.futures_position_information(symbol=self.SYMBOL)
                        long_amt = next((float(p["positionAmt"]) for p in positions if float(p["positionAmt"]) > 0), 0)
                        if long_amt > 0:
                            await self._execute_sell_plan(async_client)
                        else:
                            logging.warning("Position liquidated before exit. No new position will be opened.")
                        return
                await asyncio.sleep(0.01)
        except Exception as e:
            logging.error(f"Error while monitoring funding fee: {e}")
            raise

    async def execute_strategy(self):
        async_client = await AsyncClient.create(
            os.getenv('BINANCE_API_KEY'), os.getenv('BINANCE_API_SECRET')
        )
        try:
            await self._verify_connection(async_client)
            await self._set_leverage(async_client)
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
            await self._fetch_order_book(async_client)
            await self._monitor_funding_fee_and_sell(async_client)
        except Exception as e:
            logging.error(f"Strategy failed: {e}")
        finally:
            await async_client.close_connection()

if __name__ == "__main__":
    trader = PrecisionFuturesTrader()
    asyncio.run(trader.execute_strategy())
