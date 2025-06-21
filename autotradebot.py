import sys
import os
import time
import threading
import traceback
from datetime import datetime, timedelta, timezone
import requests
import re
import asyncio

print("=== TRADE BOT CONTAINER STARTED ===")

# --- ENVIRONMENT VARIABLES ---
try:
    TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
    ALERT_ROOM_ID = int(os.environ["ALERT_ROOM_ID"])
    LOG_ROOM_ID = int(os.environ["LOG_ROOM_ID"])
    BINANCE_API_KEY = os.environ["BINANCE_API_KEY"]
    BINANCE_API_SECRET = os.environ["BINANCE_API_SECRET"]
except KeyError as e:
    print(f"❗ ENV ERROR: Missing environment variable: {e}")
    sys.exit(1)

try:
    from binance.client import Client
    from binance.exceptions import BinanceAPIException
    from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
except Exception as e:
    print("❗ PYTHON IMPORT ERROR:", e)
    traceback.print_exc()
    sys.exit(1)

def preflight_binance_check():
    print("[STEP] Checking Binance API connectivity...")
    client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
    try:
        client.futures_account_balance()
        print("[STEP] Binance API connectivity: OK")
        return True
    except BinanceAPIException as e:
        print(f"[Binance ERROR] {e}")
        if hasattr(e, "code") and e.code == -2015:
            try:
                ip = requests.get("https://api.ipify.org").text.strip()
                print(f"[Binance ERROR] APIError -2015 (invalid API-key, IP, or permissions).")
                print(f"[Binance ERROR] Your current public IP is: {ip}")
                print(f"==> Go whitelist this IP on Binance, then restart the bot.")
            except Exception as ip_e:
                print(f"[Binance ERROR] Could not fetch public IP: {ip_e}")
            sys.exit(1)
        else:
            print(f"[Binance ERROR] Unexpected Binance error: {e}")
            sys.exit(1)
    except Exception as e:
        print(f"[Binance ERROR] General error: {e}")
        sys.exit(1)

preflight_binance_check()
binance = Client(BINANCE_API_KEY, BINANCE_API_SECRET)

def heartbeat():
    print(f"[HEARTBEAT] Trade Bot is alive at {datetime.now(timezone.utc).isoformat()}")
    threading.Timer(60, heartbeat).start()
heartbeat()

async def say(room, message):
    try:
        await application.bot.send_message(chat_id=room, text=message)
    except Exception as e:
        print(f"[Telegram ERROR] Failed to send message: {e}")
        traceback.print_exc()

async def notify_error(where, error):
    try:
        await say(LOG_ROOM_ID, f"❗ Error in {where}: {type(error).__name__}: {error}")
    except Exception as e:
        print(f"[Telegram ERROR] Failed to send error message: {e}")

async def on_startup(application):
    await say(LOG_ROOM_ID, "🤖 Trade Bot started and ready!")
    try:
        ip = requests.get("https://api.ipify.org").text.strip()
        print(f"[INFO] Public IP: {ip}")
    except Exception:
        pass

def count_coins(coin, leverage):
    try:
        price = float(binance.futures_symbol_ticker(symbol=coin)['price'])
        max_coins = (10 * leverage) / price
        qty = int(max_coins * 0.75)
        return qty
    except Exception as e:
        print(f"[Count coins ERROR] {e}")
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
        print(f"[Check funding ERROR] {e}")
        return False, 0, 0

def read_alert(alert_text):
    pattern = (
        r"ALERT:\s*(\w+)[^A-Za-z0-9]+"
        r"Funding Rate:\s*([-+]?\d*\.?\d+)%[^A-Za-z0-9]+"
        r"Max Leverage:\s*(\d+)x[^A-Za-z0-9]+"
        r"Next window:\s*([\d]{2}:[\d]{2}:[\d]{2})"
    )
    cleaned = re.sub(r'<.*?>', '', alert_text)
    match = re.search(pattern, cleaned, re.DOTALL | re.IGNORECASE)
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
        msg = update.message.text or ""
        await say(LOG_ROOM_ID, f"🛑 MIRROR FROM ALERT ROOM:\n{msg}")
        print(f"[ALERT MSG MIRRORED] {msg}")

        # --- FIX: Only remove HTML, then lowercase and trim ---
        cleaned = re.sub(r'<.*?>', '', msg)
        cleaned = cleaned.strip().lower()
        print(f"[DEBUG] Cleaned message: '{cleaned}'")

        no_lead_patterns = [
            "no lead found",
            "no trade found",
            "no alert found",
            "no opportunity found",
            "no lead identified",
            "no trades today",
            "no alert this check",
            "no lead this check",
            "❕ no lead found"
        ]

        matched = False
        for pattern in no_lead_patterns:
            if pattern in cleaned:
                print(f"[DEBUG] Matched pattern: '{pattern}'")
                await say(LOG_ROOM_ID,
                    f"🔄 No trade planned.\n"
                    f"Time: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}\n"
                    f"(Alert room message: {msg})"
                )
                matched = True
                return
        if not matched:
            print(f"[DEBUG] No 'no lead' pattern matched in: '{cleaned}'")

        if "alert:" in cleaned:
            alert_data = read_alert(msg)
            if not alert_data:
                await say(LOG_ROOM_ID,
                    f"❗ ALERT PARSE FAILURE: Could not parse trade signal from alert room message.\n"
                    f"Message received:\n{msg}\n"
                    f"Please check alert format or regex."
                )
                return

            coin = alert_data["coin"]
            leverage = alert_data["leverage"]
            funding_time = alert_data["time"]
            now = datetime.now(timezone.utc)

            qty = count_coins(coin, leverage)
            if qty == 0:
                await say(LOG_ROOM_ID, f"❌ Trade failed: Could not count coins for {coin}.")
                return

            try:
                binance.futures_change_leverage(symbol=coin, leverage=leverage)
                fr_data = binance.futures_premium_index(symbol=coin)
                current_fr = float(fr_data['lastFundingRate'])
                direction = "SELL" if current_fr > 0 else "BUY"
            except Exception as e:
                await notify_error("handle_message (pre-checks)", e)
                return

            entry_time = funding_time - timedelta(seconds=1)
            check_time = entry_time - timedelta(minutes=5)
            minutes_remaining = (entry_time - now).total_seconds() / 60
            if minutes_remaining > 1:
                time_str = f"{minutes_remaining:.1f} minutes"
            elif minutes_remaining > 0:
                time_str = f"{minutes_remaining*60:.0f} seconds"
            else:
                time_str = "less than 1 second"

            await say(
                LOG_ROOM_ID,
                f"🔔 New trade planned: {coin}\n"
                f"Direction: {direction}\n"
                f"Qty: {qty}\n"
                f"Entry at {entry_time.strftime('%H:%M:%S')} UTC (1s before funding)\n"
                f"⏳ Time remaining for entry: {time_str}\n"
                f"🕔 Funding rate/threshold check will occur 5 minutes before entry."
            )

            def five_min_check_and_entry():
                good, value, current_fr2 = is_still_good(coin)
                if not good:
                    asyncio.run(say(LOG_ROOM_ID, f"❌ Trade cancelled: Funding not good enough for {coin} (checked 5 min before entry)."))
                    return
                seconds_until_entry = (entry_time - datetime.now(timezone.utc)).total_seconds()
                if seconds_until_entry > 0:
                    threading.Timer(seconds_until_entry, lambda: asyncio.run(execute_trade(coin, qty, direction, funding_time))).start()
                else:
                    asyncio.run(execute_trade(coin, qty, direction, funding_time))

            seconds_until_5min = (check_time - now).total_seconds()
            if seconds_until_5min > 0:
                threading.Timer(seconds_until_5min, five_min_check_and_entry).start()
            else:
                threading.Thread(target=five_min_check_and_entry).start()
    except Exception as e:
        await notify_error("handle_message", e)

async def execute_trade(coin, qty, direction, funding_time):
    try:
        entry_order = binance.futures_create_order(
            symbol=coin,
            side=direction,
            type="MARKET",
            quantity=qty
        )
        entry_time = datetime.now(timezone.utc)
        await say(LOG_ROOM_ID, f"🚀 {coin} {direction} order placed at {entry_time.strftime('%H:%M:%S')} UTC")

        found_fee = False
        money_bag = 0.0
        poll_start = time.time()
        funding_ts_ms = int(funding_time.timestamp() * 1000)

        while time.time() - poll_start < 30:
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
            except Exception:
                pass
            time.sleep(0.1)

        close_side = "BUY" if direction == "SELL" else "SELL"
        try:
            binance.futures_create_order(
                symbol=coin,
                side=close_side,
                type="MARKET",
                quantity=qty,
                reduceOnly=True
            )
            await say(LOG_ROOM_ID, f"✅ {coin} position closed (post-funding, fee detected: {found_fee})")
        except Exception as e:
            await notify_error("execute_trade (close position)", e)

        if found_fee:
            await say(LOG_ROOM_ID, f"💰 Funding fee received: {money_bag:.6f} USDT")
        else:
            await say(LOG_ROOM_ID, f"⚠️ Funding fee not detected within 30s.")

    except Exception as e:
        await notify_error("execute_trade", e)

def main():
    global application
    try:
        application = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(on_startup).build()
        application.add_handler(MessageHandler(filters.Chat(ALERT_ROOM_ID) & filters.TEXT, handle_message))
        application.run_polling()
    except Exception as e:
        print(f"Fatal error on startup: {e}")
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"=== TRADE BOT CRASHED: {e} ===")
        traceback.print_exc()
        sys.exit(1)
