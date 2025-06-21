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

application = None  # will be set in main()

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

async def say(room, message):
    try:
        await application.bot.send_message(chat_id=room, text=message)
    except Exception as e:
        print(f"[Telegram ERROR] Failed to send message: {e}")
        traceback.print_exc()

async def notify_error(where, error):
    await say(LOG_ROOM_ID, f"❗ Error in {where}: {type(error).__name__}: {error}")

def heartbeat():
    now = datetime.now(timezone.utc)
    print(f"[HEARTBEAT] Trade Bot alive at {now.isoformat()}")
    print(f"[ENV] ALERT_ROOM_ID: {ALERT_ROOM_ID}")
    print(f"[ENV] LOG_ROOM_ID: {LOG_ROOM_ID}")
    try:
        asyncio.run(say(LOG_ROOM_ID, f"❤️ HEARTBEAT: {now.strftime('%H:%M:%S UTC')}"))
    except Exception as e:
        print(f"HEARTBEAT TELEGRAM ERROR: {e}")
    threading.Timer(60, heartbeat).start()

heartbeat()

async def on_startup(application):
    await say(LOG_ROOM_ID, "🤖 Trade Bot STARTING...")
    await say(ALERT_ROOM_ID, "🔔 Bot activated in alert room")
    await say(LOG_ROOM_ID, "🔔 Bot activated in log room")
    try:
        bot_info = await application.bot.get_me()
        await say(LOG_ROOM_ID, f"🤖 BOT INFO:\nID: {bot_info.id}\nName: {bot_info.first_name}\nUsername: @{bot_info.username}")
    except Exception as e:
        await say(LOG_ROOM_ID, f"❗ BOT INFO ERROR: {e}")
    try:
        ip = requests.get("https://api.ipify.org").text.strip()
        await say(LOG_ROOM_ID, f"[INFO] Public IP: {ip}")
    except Exception:
        pass

async def check_permissions():
    try:
        chat = await application.bot.get_chat(ALERT_ROOM_ID)
        perms = await application.bot.get_chat_member(ALERT_ROOM_ID, application.bot.id)
        await say(LOG_ROOM_ID,
            f"🔑 PERMISSIONS FOR {chat.title or 'DM'}:\n"
            f"Type: {chat.type}\n"
            f"Bot Status: {perms.status}\n"
            f"Can Send: {getattr(perms, 'can_send_messages', 'N/A')}\n"
            f"Can Read: {getattr(perms, 'can_read_messages', 'N/A')}"
        )
    except Exception as e:
        await say(LOG_ROOM_ID, f"❌ PERMISSION CHECK FAILED: {e}")

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
        # Capture raw message exactly as received
        raw_msg = update.message.text or ""
        print(f"\n\n[RAW MESSAGE RECEIVED] {raw_msg}")

        # Always mirror the raw message first
        await say(LOG_ROOM_ID, f"🛑 RAW MIRROR:\n{raw_msg}")

        # Simplified cleaning - preserve ALL content
        cleaned = raw_msg.lower().strip()
        print(f"[DEBUG] Cleaned message: '{cleaned}'")

        # Debug: Log all environment variables
        print(f"[ENV] ALERT_ROOM_ID: {ALERT_ROOM_ID}")
        print(f"[ENV] LOG_ROOM_ID: {LOG_ROOM_ID}")

        # Manual debug command
        if "/debug" in cleaned:
            await check_permissions()
            await say(LOG_ROOM_ID, "🛠️ DEBUG COMMAND EXECUTED")
            return

        # 1. Verify message source
        if update.message.chat.id != ALERT_ROOM_ID:
            await say(LOG_ROOM_ID, 
                f"⚠️ Received message from unknown chat: {update.message.chat.id}\n"
                f"Expected: {ALERT_ROOM_ID}"
            )
            return

        # 2. No-lead detection with multiple strategies
        NO_LEAD_PHRASES = [
            "no lead", "no trade", "no alert", "not found", 
            "no opportunity", "nothing found", "no setup", "❕"
        ]
        is_no_lead = any(phrase in cleaned for phrase in NO_LEAD_PHRASES)

        if is_no_lead:
            print(f"[NO LEAD DETECTED] Phrase matched in: {cleaned}")
            await say(LOG_ROOM_ID,
                f"🔄 NO TRADE PLANNED\n"
                f"⌚ {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}\n"
                f"📝 Original: {raw_msg[:100]}..."
            )
            return
        else:
            print(f"[NO LEAD NOT DETECTED] No matching phrases in: {cleaned}")
            await say(LOG_ROOM_ID, 
                f"🔍 No 'no lead' pattern found in message:\n{cleaned[:100]}..."
            )

        # 3. Alert detection and parsing
        if "alert:" in cleaned:
            print("[ALERT DETECTED] Attempting to parse...")
            alert_data = read_alert(raw_msg)
            if not alert_data:
                await say(LOG_ROOM_ID,
                    f"❗ ALERT PARSE FAILURE: Could not parse trade signal from alert room message.\n"
                    f"Message received:\n{raw_msg}\n"
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
        else:
            await say(LOG_ROOM_ID, "❌ No 'alert:' keyword found in message")
    except Exception as e:
        error_msg = f"🚨 HANDLE MESSAGE CRASH: {type(e).__name__}: {e}\n{traceback.format_exc()}"
        print(error_msg)
        await say(LOG_ROOM_ID, error_msg)

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
        handler = MessageHandler(
            filters.Chat(chat_id=ALERT_ROOM_ID) & (filters.TEXT | filters.CAPTION),
            handle_message
        )
        application.add_handler(handler)
        print(f"[INIT] Handler registered for chat ID: {ALERT_ROOM_ID}")
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
