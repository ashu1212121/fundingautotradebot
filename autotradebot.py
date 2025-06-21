import sys
import os
import time
import threading
import traceback
from datetime import datetime, timedelta, timezone
import requests
import re
import asyncio
import logging

# --- Configure Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("TradeBot")

# --- ENVIRONMENT VARIABLES ---
try:
    TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
    ALERT_ROOM_ID = int(os.environ["ALERT_ROOM_ID"])
    LOG_ROOM_ID = int(os.environ["LOG_ROOM_ID"])
    BINANCE_API_KEY = os.environ["BINANCE_API_KEY"]
    BINANCE_API_SECRET = os.environ["BINANCE_API_SECRET"]
    logger.info("Environment variables loaded successfully")
except KeyError as e:
    logger.error(f"❗ ENV ERROR: Missing environment variable: {e}")
    sys.exit(1)

try:
    from binance.client import Client
    from binance.exceptions import BinanceAPIException
    from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
    logger.info("Dependencies imported successfully")
except Exception as e:
    logger.error(f"❗ PYTHON IMPORT ERROR: {e}")
    traceback.print_exc()
    sys.exit(1)

application = None

# --- Binance Setup ---
def preflight_binance_check():
    logger.info("[STEP] Checking Binance API connectivity...")
    client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
    try:
        client.futures_account_balance()
        logger.info("[STEP] Binance API connectivity: OK")
        return True
    except BinanceAPIException as e:
        logger.error(f"[Binance ERROR] {e}")
        if hasattr(e, "code") and e.code == -2015:
            try:
                ip = requests.get("https://api.ipify.org").text.strip()
                logger.error(f"[Binance ERROR] APIError -2015 (invalid API-key, IP, or permissions).")
                logger.error(f"[Binance ERROR] Your current public IP is: {ip}")
                logger.error(f"==> Go whitelist this IP on Binance, then restart the bot.")
            except Exception as ip_e:
                logger.error(f"[Binance ERROR] Could not fetch public IP: {ip_e}")
            sys.exit(1)
        else:
            logger.error(f"[Binance ERROR] Unexpected Binance error: {e}")
            sys.exit(1)
    except Exception as e:
        logger.error(f"[Binance ERROR] General error: {e}")
        sys.exit(1)

preflight_binance_check()
binance = Client(BINANCE_API_KEY, BINANCE_API_SECRET)

# --- Telegram Utilities ---
def send_telegram_message_sync(room_id, message):
    """Synchronous Telegram message sender for non-async contexts"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": room_id,
            "text": message
        }
        response = requests.post(url, json=payload)
        if response.status_code == 200:
            logger.info(f"Telegram message sent to room {room_id}")
        else:
            logger.error(f"Telegram API error: {response.status_code} - {response.text}")
    except Exception as e:
        logger.error(f"Telegram sync send error: {e}")

async def say(room, message):
    """Asynchronous Telegram message sender"""
    global application
    try:
        if application and hasattr(application, "bot"):
            await application.bot.send_message(chat_id=room, text=message)
        else:
            logger.warning("Application not initialized, using sync send")
            send_telegram_message_sync(room, message)
    except Exception as e:
        logger.error(f"Telegram async send error: {e}")
        send_telegram_message_sync(room, f"⚠️ ASYNC FAILED: {message}")

async def notify_error(where, error):
    try:
        await say(LOG_ROOM_ID, f"❗ Error in {where}: {type(error).__name__}: {error}")
    except Exception as e:
        logger.error(f"Error notification failed: {e}")
        send_telegram_message_sync(LOG_ROOM_ID, f"🚨 CRITICAL: Error notification failed: {e}")

# --- Heartbeat System ---
def heartbeat():
    """Synchronous heartbeat that doesn't depend on async, never touches async run loop"""
    try:
        now = datetime.now(timezone.utc)
        logger.info(f"[HEARTBEAT] Trade Bot is alive at {now.isoformat()}")
        send_telegram_message_sync(LOG_ROOM_ID, f"❤️ HEARTBEAT: {now.strftime('%H:%M:%S UTC')}")
    except Exception as e:
        logger.error(f"Heartbeat failed: {e}")
    finally:
        threading.Timer(60, heartbeat).start()

heartbeat()

# --- Core Functions ---
async def on_startup(app):
    global application
    application = app
    logger.info("Application startup initiated")
    await say(LOG_ROOM_ID, "🤖 Trade Bot started and ready!")

    try:
        ip = requests.get("https://api.ipify.org").text.strip()
        logger.info(f"Public IP: {ip}")
        await say(LOG_ROOM_ID, f"🌐 Public IP: {ip}")
    except Exception as e:
        logger.error(f"IP fetch error: {e}")

def count_coins(coin, leverage):
    try:
        price = float(binance.futures_symbol_ticker(symbol=coin)['price'])
        max_coins = (10 * leverage) / price
        qty = int(max_coins * 0.75)
        return qty
    except Exception as e:
        logger.error(f"Count coins error: {e}")
        return 0

def is_still_good(coin):
    try:
        fr_data = binance.futures_premium_index(symbol=coin)
        current_fr = float(fr_data['lastFundingRate'])
        abs_fr = abs(current_fr) * 100
        lev_data = binance.futures_leverage_bracket(symbol=coin)
        leverage = lev_data[0]['brackets'][0]['initialLeverage']
        value = abs_fr * leverage
        return value > 100, value, current_fr
    except Exception as e:
        logger.error(f"Funding check error: {e}")
        return False, 0, 0

def read_alert(alert_text):
    # Compatible with new alert bot format
    pat = (
        r"ALERT:\s*(\w+)\s*[\r\n]+"
        r"Funding Rate:\s*([-+]?\d*\.?\d+)%[\r\n]+"
        r"Max Leverage:\s*(\d+)x[\r\n]+"
        r"Next window:\s*([\d]{2}:[\d]{2}:[\d]{2})"
    )
    cleaned = re.sub(r'<.*?>', '', alert_text)
    match = re.search(pat, cleaned, re.DOTALL | re.IGNORECASE)
    if match:
        coin = match.group(1)
        fr = float(match.group(2))
        leverage = int(match.group(3))
        raw_time = match.group(4)
        now = datetime.now(timezone.utc)
        trade_time = datetime.strptime(raw_time, "%H:%M:%S").time()
        full_time = datetime.combine(now.date(), trade_time).replace(tzinfo=timezone.utc)
        if full_time < now:
            full_time += timedelta(days=1)
        return {
            "coin": coin,
            "fr": fr,
            "leverage": leverage,
            "time": full_time
        }
    return None

async def handle_message(update, context: ContextTypes.DEFAULT_TYPE):
    try:
        raw_msg = update.message.text or ""
        logger.info(f"Received message: {raw_msg}")

        # Mirror the raw message
        await say(LOG_ROOM_ID, f"🛑 MIRROR FROM ALERT ROOM:\n{raw_msg}")

        cleaned = raw_msg.lower()
        logger.info(f"Cleaned message: '{cleaned}'")

        if update.message.chat.id != ALERT_ROOM_ID:
            logger.warning(f"Received message from unexpected chat: {update.message.chat.id}")
            await say(LOG_ROOM_ID,
                f"⚠️ Received message from unknown chat: {update.message.chat.id}\n"
                f"Expected: {ALERT_ROOM_ID}"
            )
            return

        # --- Robust no-lead detection that ignores @mentions ---
        cleaned_content = cleaned.replace("@autofundingtradebot", "").strip()
        logger.info(f"Content after removing mention: '{cleaned_content}'")

        if "no lead found" in cleaned_content:
            logger.info("No lead pattern detected")
            response = (
                f"🔄 No trade planned.\n"
                f"Time: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}\n"
                f"Triggered by: {raw_msg[:100]}..."
            )
            await say(LOG_ROOM_ID, response)
            return

        # 2. Check for alert messages
        if "alert:" in cleaned:
            logger.info("Alert pattern detected")
            alert_data = read_alert(raw_msg)
            if not alert_data:
                logger.warning("Alert parsing failed")
                await say(LOG_ROOM_ID,
                    f"❗ ALERT PARSE FAILURE: Could not parse trade signal.\n"
                    f"Message: {raw_msg[:200]}..."
                )
                return

            coin = alert_data["coin"]
            leverage = alert_data["leverage"]
            funding_time = alert_data["time"]
            now = datetime.now(timezone.utc)

            qty = count_coins(coin, leverage)
            if qty == 0:
                logger.error(f"Coin count failed for {coin}")
                await say(LOG_ROOM_ID, f"❌ Trade failed: Could not count coins for {coin}.")
                return

            try:
                binance.futures_change_leverage(symbol=coin, leverage=leverage)
                fr_data = binance.futures_premium_index(symbol=coin)
                current_fr = float(fr_data['lastFundingRate'])
                direction = "SELL" if current_fr > 0 else "BUY"
            except Exception as e:
                logger.error(f"Pre-trade checks failed: {e}")
                await notify_error("handle_message (pre-checks)", e)
                return

            entry_time = funding_time - timedelta(seconds=1)
            check_time = entry_time - timedelta(minutes=5)
            time_remaining = (entry_time - now).total_seconds()

            if time_remaining <= 0:
                logger.warning("Entry time already passed")
                await say(LOG_ROOM_ID, "⚠️ Entry time already passed. Trade cancelled.")
                return

            await say(
                LOG_ROOM_ID,
                f"🔔 New trade planned: {coin}\n"
                f"Direction: {direction}\n"
                f"Qty: {qty}\n"
                f"Entry at {entry_time.strftime('%H:%M:%S')} UTC\n"
                f"⏳ Time remaining: {timedelta(seconds=int(time_remaining))}"
            )

            def schedule_trade():
                try:
                    time.sleep(max(0, (check_time - datetime.now(timezone.utc)).total_seconds()))
                    good, value, current_fr2 = is_still_good(coin)
                    if not good:
                        asyncio.run(say(LOG_ROOM_ID,
                            f"❌ Trade cancelled: Funding not good enough for {coin}\n"
                            f"Current value: {value:.2f} (need >100)"
                        ))
                        return
                    time.sleep(max(0, (entry_time - datetime.now(timezone.utc)).total_seconds()))
                    asyncio.run(execute_trade(coin, qty, direction, funding_time))
                except Exception as e:
                    logger.error(f"Trade scheduling failed: {e}")
                    asyncio.run(notify_error("schedule_trade", e))

            threading.Thread(target=schedule_trade, daemon=True).start()

        else:
            logger.info("No alert pattern detected")
            await say(LOG_ROOM_ID, f"ℹ️ Received non-alert message:\n{raw_msg[:100]}...")

    except Exception as e:
        logger.error(f"Message handling crashed: {e}")
        await notify_error("handle_message", e)

async def execute_trade(coin, qty, direction, funding_time):
    try:
        logger.info(f"Executing trade: {coin} {direction} {qty}")
        await say(LOG_ROOM_ID, f"⏳ Executing {direction} order for {qty} {coin}...")

        entry_order = binance.futures_create_order(
            symbol=coin,
            side=direction,
            type="MARKET",
            quantity=qty
        )
        entry_time = datetime.now(timezone.utc)
        await say(LOG_ROOM_ID, f"🚀 {coin} {direction} order placed at {entry_time.strftime('%H:%M:%S')} UTC")

        # Wait for funding fee
        found_fee = False
        money_bag = 0.0
        funding_ts_ms = int(funding_time.timestamp() * 1000)

        await say(LOG_ROOM_ID, "⏳ Waiting for funding fee...")
        for _ in range(30):  # 30 attempts with 1s delay = 30s total
            try:
                history = binance.futures_income_history(
                    symbol=coin,
                    incomeType="FUNDING_FEE",
                    limit=1
                )
                if history and int(history[0]['time']) >= funding_ts_ms:
                    found_fee = True
                    money_bag = float(history[0]['income'])
                    break
            except Exception as e:
                logger.warning(f"Funding fee check failed: {e}")
            time.sleep(1)

        close_side = "BUY" if direction == "SELL" else "SELL"
        try:
            binance.futures_create_order(
                symbol=coin,
                side=close_side,
                type="MARKET",
                quantity=qty,
                reduceOnly=True
            )
            await say(LOG_ROOM_ID, f"✅ {coin} position closed")
        except Exception as e:
            await notify_error("execute_trade (close position)", e)

        if found_fee:
            await say(LOG_ROOM_ID, f"💰 Funding fee received: {money_bag:.6f} USDT")
        else:
            await say(LOG_ROOM_ID, f"⚠️ Funding fee not detected within 30s")

    except Exception as e:
        await notify_error("execute_trade", e)

def main():
    logger.info("Starting main application")
    try:
        app = (
            ApplicationBuilder()
            .token(TELEGRAM_TOKEN)
            .post_init(on_startup)
            .build()
        )

        app.add_handler(
            MessageHandler(
                filters.Chat(chat_id=ALERT_ROOM_ID) & filters.TEXT,
                handle_message
            )
        )

        logger.info(f"Handler registered for chat ID: {ALERT_ROOM_ID}")
        logger.info("Starting polling...")
        app.run_polling()

    except Exception as e:
        logger.critical(f"Fatal error: {e}")
        traceback.print_exc()
        send_telegram_message_sync(LOG_ROOM_ID, f"🆘 BOT CRASHED: {e}")
        sys.exit(1)

if __name__ == "__main__":
    logger.info("Launching application")
    main()
