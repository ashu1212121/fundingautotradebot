import os
import asyncio
import logging
from datetime import datetime, timedelta, timezone
import time
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

class KNCUSDTTrader:
    def __init__(self):
        self.SYMBOL = 'KNCUSDT'         # Trading pair
        self.LEVERAGE = 50              # 50x leverage
        self.MARGIN_USDT = 5            # 5 USDT margin
        self.TRADE_DELAY_SEC = 4 * 60   # Trade after 4 mins
        self.PROFIT_PCT = 0.0026        # 0.26%
        self.SELL_TIMEOUT = 18          # seconds to wait for sell fill

    async def _verify_connection(self, async_client):
        try:
            account_info = await async_client.futures_account()
            if not account_info['canTrade']:
                raise PermissionError("Futures trading disabled")
            logging.info("API connection verified")
        except Exception as e:
            logging.error(f"Connection failed: {e}")
            raise

    async def _set_leverage_and_margin_type(self, async_client):
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
        await async_client.futures_change_leverage(
            symbol=self.SYMBOL,
            leverage=self.LEVERAGE
        )
        logging.info(f"Leverage set to {self.LEVERAGE}x for {self.SYMBOL}")

    async def _get_market_price(self, async_client):
        ticker = await async_client.futures_mark_price(symbol=self.SYMBOL)
        return float(ticker['markPrice'])

    async def _get_price_decimals(self, async_client):
        exchange_info = await async_client.futures_exchange_info()
        for symbol_data in exchange_info['symbols']:
            if symbol_data['symbol'] == self.SYMBOL:
                step_size = None
                for f in symbol_data['filters']:
                    if f['filterType'] == 'PRICE_FILTER':
                        tick_size = float(f['tickSize'])
                        decimals = abs(decimal.Decimal(str(tick_size)).as_tuple().exponent)
                        return decimals
        return 2  # fallback

    async def _precompute_qty(self, async_client):
        market_price = await self._get_market_price(async_client)
        qty = (self.MARGIN_USDT * self.LEVERAGE) / market_price
        precomputed_qty = int(qty)   # round down to nearest integer
        if precomputed_qty < 1:
            precomputed_qty = 1
        logging.info(f"Precomputed qty for {self.SYMBOL} at price {market_price}: {precomputed_qty}")
        return precomputed_qty, market_price

    async def _open_long(self, async_client, qty):
        try:
            order = await async_client.futures_create_order(
                symbol=self.SYMBOL,
                side='BUY',
                type='MARKET',
                quantity=qty,
                newOrderRespType='FULL'
            )
            logging.info(f"Market BUY order executed for {qty} {self.SYMBOL}: {order}")
            fill_price = float(order['avgFillPrice']) if 'avgFillPrice' in order else float(order['fills'][0]['price'])
            return order['orderId'], fill_price
        except Exception as e:
            logging.error(f"Failed to execute market BUY order: {e}")
            raise

    async def _place_sell_limit(self, async_client, qty, price, price_decimals):
        price = round(price, price_decimals)
        try:
            order = await async_client.futures_create_order(
                symbol=self.SYMBOL,
                side='SELL',
                type='LIMIT',
                price=str(price),
                quantity=qty,
                timeInForce='GTC',
                reduceOnly=True,
                newOrderRespType='FULL'
            )
            logging.info(f"Limit SELL order placed at {price} for {qty} {self.SYMBOL}: {order}")
            return order['orderId'], price
        except Exception as e:
            logging.error(f"Failed to place limit SELL order: {e}")
            raise

    async def _wait_for_order_fill(self, async_client, order_id, timeout):
        start = time.time()
        while time.time() - start < timeout:
            order = await async_client.futures_get_order(
                symbol=self.SYMBOL,
                orderId=order_id
            )
            if order['status'] == 'FILLED':
                logging.info(f"Order {order_id} filled.")
                return True
            await asyncio.sleep(0.5)
        logging.info(f"Order {order_id} not filled in {timeout} seconds.")
        return False

    async def _close_long_market(self, async_client, qty):
        try:
            order = await async_client.futures_create_order(
                symbol=self.SYMBOL,
                side='SELL',
                type='MARKET',
                quantity=qty,
                reduceOnly=True,
                newOrderRespType='FULL'
            )
            logging.info(f"Market SELL close executed for {qty} {self.SYMBOL}: {order}")
            return order
        except Exception as e:
            logging.error(f"Failed to close position at market price: {e}")
            raise

    async def execute_once(self):
        api_key = os.getenv('BINANCE_API_KEY')
        api_secret = os.getenv('BINANCE_API_SECRET')
        async_client = await AsyncClient.create(api_key, api_secret)
        try:
            await self._verify_connection(async_client)
            await self._set_leverage_and_margin_type(async_client)
            logging.info(f"Waiting {self.TRADE_DELAY_SEC} seconds before trading...")
            await asyncio.sleep(self.TRADE_DELAY_SEC)

            qty, entry_price = await self._precompute_qty(async_client)
            price_decimals = 3  # Default, you can fetch dynamically with _get_price_decimals if needed

            buy_order_id, real_entry_price = await self._open_long(async_client, qty)

            # Calculate sell price (entry + 0.26%)
            sell_price = real_entry_price * (1 + self.PROFIT_PCT)
            sell_price = round(sell_price, price_decimals)

            sell_order_id, limit_price = await self._place_sell_limit(async_client, qty, sell_price, price_decimals)

            filled = await self._wait_for_order_fill(async_client, sell_order_id, self.SELL_TIMEOUT)
            if not filled:
                # Cancel limit order
                try:
                    await async_client.futures_cancel_order(symbol=self.SYMBOL, orderId=sell_order_id)
                    logging.info(f"Limit SELL order {sell_order_id} canceled.")
                except Exception as e:
                    logging.warning(f"Could not cancel limit order: {e}")
                # Close at market
                await self._close_long_market(async_client, qty)
        except Exception as e:
            logging.error(f"Trade execution failed: {e}")
        finally:
            await async_client.close_connection()

if __name__ == "__main__":
    trader = KNCUSDTTrader()
    asyncio.run(trader.execute_once())
