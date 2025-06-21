import sys
import os
import time
import threading
import traceback
from datetime import datetime, timedelta, timezone
import requests
import re
import asyncio

print("=== BOT CONTAINER STARTED ===")

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
    from binance.client import Client, BinanceAPIException
    from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
except Exception as e:
    print("❗ PYTHON IMPORT ERROR:", e)
    traceback.print_exc()
    sys.exit(1)

# --- Check Binance API and Print IP before starting bot ---
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

# --- Run preflight check before ANYTHING else ---
preflight_binance_check()

# --- Now safe to import/init Telegram bot ---
binance = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
trade_notes = {}

# --- Only print minimal heartbeat to console for debugging ---
def heartbeat():
    print(f"[HEARTBEAT] Bot is alive at {datetime.now(timezone.utc).isoformat()}")
    threading.Timer(60, heartbeat).start()
heartbeat()

# --- Minimal async say, no buffer, only for Telegram group notifications ---
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

# --- On startup: only send "Bot started!" ---
async def on_startup(application):
    await say(LOG_ROOM_ID, "🤖 Bot started and ready!")
    try:
        ip = requests.get("https://api.ipify.org").text.strip()
        print(f"[INFO] Public IP: {ip}")
    except Exception:
        pass

def check_balance_sync():
    try:
        balance_list = binance.futures_account_balance()
        usdt_bal = None
        for asset in balance_list:
            if asset["asset"] == "USDT":
                usdt_bal = float(asset["balance"])
                break
        return usdt_bal if usdt_bal is not None else 0
    except Exception as e:
        print(f"[Balance ERROR] {e}")
        return 0

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

def precision_wait(target_ts_ms):
    while True:
        current = time.time() * 1000
        if current >= target_ts_ms:
            return
        remaining = target_ts_ms - current
        time.sleep(max(remaining / 2000, 0.001))

def read_alert(alert_text):
    pattern = (
        r"🚨\s*<b>ALERT:\s*(\w+)</b>\s*🚨.*?"
        r"Funding Rate:\s*<b>([\d\.\-]+)%</b>.*?"
        r"Max Leverage:\s*<b>(\d+)x</b>.*?"
        r"Next window:\s*([\d]{2}:[\d]{2}:[\d]{2})"
    )
    if match := re.search(pattern, alert_text, re.DOTALL):
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

async def make_trade_async(coin):
    try:
        data = trade_notes[coin]
        check_time = data['time'] - timedelta(minutes=5)
        check_time_ts = check_time.replace(tzinfo=timezone.utc).timestamp() * 1000
        precision_wait(check_time_ts)

        good, value, current_fr = is_still_good(coin)
        if not good:
            del trade_notes[coin]
            return

        direction = "SELL" if current_fr > 0 else "BUY"

        try:
            binance.futures_change_leverage(symbol=coin, leverage=data['leverage'])
        except Exception as e:
            await notify_error("make_trade (set leverage)", e)
            del trade_notes[coin]
            return

        entry_time = data['time'] - timedelta(seconds=1)
        entry_time_ts = entry_time.replace(tzinfo=timezone.utc).timestamp() * 1000
        precision_wait(entry_time_ts)

        try:
            binance.futures_create_order(
                symbol=coin,
                side=direction,
                type="MARKET",
                quantity=data['qty']
            )
        except Exception as e:
            await notify_error("make_trade (open position)", e)
            del trade_notes[coin]
            return

        real_entry = datetime.now(timezone.utc)

        got_money = False
        money_bag = 0.0
        wait_start = time.time()
        while not got_money and (time.time() - wait_start < 10):
            try:
                money_history = binance.futures_income_history(
                    symbol=coin,
                    incomeType="FUNDING_FEE",
                    limit=1
                )
                if money_history and money_history[0]['time'] > int(real_entry.timestamp() * 1000):
                    got_money = True
                    money_bag = float(money_history[0]['income'])
                    break
            except Exception:
                break
            time.sleep(0.1)

        close_side = "BUY" if direction == "SELL" else "SELL"
        try:
            binance.futures_create_order(
                symbol=coin,
                side=close_side,
                type="MARKET",
                quantity=data['qty'],
                reduceOnly=True
            )
        except Exception as e:
            await notify_error("make_trade (close position)", e)
            del trade_notes[coin]
            return

        await say(LOG_ROOM_ID,
            f"💰 TRADE DONE: {coin}\n"
            f"Money: {'✅' if got_money else '❌'} {money_bag:.6f} USDT\n"
            f"Time: {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC"
        )

        del trade_notes[coin]
    except Exception as e:
        await notify_error("make_trade", e)

def schedule_trade(coin):
    asyncio.run(make_trade_async(coin))

async def handle_message(update, context: ContextTypes.DEFAULT_TYPE):
    try:
        msg = update.message.text

        if msg and "no lead found" in msg.lower():
            await say(LOG_ROOM_ID,
                f"🔄 No trade planned.\n"
                f"Time: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
            )
            return

        if "🚨 ALERT:" in msg or "🚨 <b>ALERT:" in msg:
            alert_data = read_alert(msg)
            if alert_data:
                coin = alert_data["coin"]
                qty = count_coins(coin, alert_data["leverage"])
                if qty == 0:
                    await say(LOG_ROOM_ID, f"❌ Trade failed: Could not count coins for {coin}.")
                    return

                trade_notes[coin] = {
                    'qty': qty,
                    'leverage': alert_data["leverage"],
                    'time': alert_data["time"]
                }

                await say(LOG_ROOM_ID,
                    f"🔔 New trade: {coin}\n"
                    f"Qty: {qty}\n"
                    f"Check at {(alert_data['time']-timedelta(minutes=5)).strftime('%H:%M:%S')} UTC"
                )
                wait_time = (alert_data['time'] - timedelta(minutes=5) - datetime.now(timezone.utc)).total_seconds()
                if wait_time > 0:
                    threading.Timer(wait_time, schedule_trade, [coin]).start()
                else:
                    # If alert time is already within 5min, execute immediately
                    threading.Thread(target=schedule_trade, args=(coin,)).start()
    except Exception as e:
        await notify_error("handle_message", e)

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
        print(f"=== BOT CRASHED: {e} ===")
        traceback.print_exc()
        sys.exit(1)
