import sys
import os
import time
import threading
import traceback
from datetime import datetime, timedelta
import requests

# --- Immediate stdout/stderr flushing for Railway logs ---
sys.stdout = os.fdopen(sys.stdout.fileno(), "w", buffering=1)
sys.stderr = os.fdopen(sys.stderr.fileno(), "w", buffering=1)

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

# --- Telegram and Binance imports (import after env check for clean error logs) ---
try:
    from binance.client import Client, BinanceAPIException
    from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
except Exception as e:
    print("❗ PYTHON IMPORT ERROR:", e)
    traceback.print_exc()
    sys.exit(1)

binance = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
trade_notes = {}
application = None  # Will be set in main()
message_buffer = []

def heartbeat():
    print(f"[HEARTBEAT] Bot is alive at {datetime.utcnow().isoformat()}")
    threading.Timer(60, heartbeat).start()
heartbeat()

# --- Logging and Messaging Utilities ---

def log_step(step):
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
    msg = f"[STEP {now}] {step}"
    print(msg)
    say_sync(LOG_ROOM_ID, msg)

def log_error(where, e, extra=None):
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
    msg = f"❌ ERROR in {where}: {type(e).__name__}: {e}"
    print(msg)
    if extra:
        print("Extra context:", extra)
    traceback.print_exc()
    say_sync(LOG_ROOM_ID, msg)
    if extra:
        say_sync(LOG_ROOM_ID, f"Context: {extra}")
    say_sync(LOG_ROOM_ID, f"Traceback:\n{traceback.format_exc()}")

async def say(room, message):
    print(f"[Telegram Log to {room}] {message}")
    try:
        await application.bot.send_message(chat_id=room, text=message)
    except Exception as e:
        print(f"[Telegram ERROR] Failed to send message: {e}")
        traceback.print_exc()

def say_sync(room, message):
    print(f"[Log to {room}] {message}")
    # If the event loop isn't running, buffer the message for later
    if application is None or not hasattr(application, "loop") or not application.loop.is_running():
        message_buffer.append((room, message))
        print(f"[Buffered] Message buffered until event loop starts.")
        return
    try:
        application.create_task(say(room, message))
    except Exception as e:
        print(f"[say_sync ERROR] Failed to schedule Telegram message: {e}")
        traceback.print_exc()

async def flush_message_buffer():
    print("[FLUSH] Flushing buffered Telegram messages")
    while message_buffer:
        room, message = message_buffer.pop(0)
        await say(room, message)

# --- Balance Check ---

def check_balance_sync():
    log_step("Starting check_balance_sync")
    try:
        balance_list = binance.futures_account_balance()
        usdt_bal = None
        for asset in balance_list:
            if asset["asset"] == "USDT":
                usdt_bal = float(asset["balance"])
                break
        if usdt_bal is None:
            raise Exception("USDT balance not found in Binance response.")
        print(f"[BALANCE CHECK] USDT Balance: {usdt_bal}")
        if usdt_bal < 12:
            msg = (
                f"⚠️ Your USDT balance is ${usdt_bal:.2f}.\n"
                "Suggestion: Please deposit more funds to avoid failed trades!"
            )
            say_sync(LOG_ROOM_ID, msg)
        return usdt_bal
    except BinanceAPIException as e:
        log_error("check_balance_sync", e)
        if hasattr(e, "code") and e.code == -2015:
            say_sync(LOG_ROOM_ID, "❗ Check your Binance API key, secret, IP whitelist, and permissions.")
        return 0
    except Exception as e:
        log_error("check_balance_sync", e)
        return 0

# --- Show Railway IP Address ---

async def show_ip_startup():
    log_step("Fetching Railway Public IP")
    try:
        ip = requests.get("https://api.ipify.org").text.strip()
        await say(LOG_ROOM_ID, f"🚦 Railway Public IP: `{ip}`\nWhitelist this IP for Binance API access.")
    except Exception as e:
        await say(LOG_ROOM_ID, f"❗ Could not fetch Railway Public IP: {e}")
        log_error("show_ip_startup", e)

# --- Startup Handler ---

async def on_startup(application):
    try:
        log_step("on_startup started")
        calibrate_time_sync(binance)
        log_step("Flushing message buffer")
        await flush_message_buffer()
        await show_ip_startup()
        await say(LOG_ROOM_ID, "🤖 HELLO! I'M YOUR MONEY ROBOT (NOW FASTER, MORE PRECISE, AND SAFER)!")
        check_balance_sync()
        log_step("on_startup finished")
    except Exception as e:
        log_error("on_startup", e)

# --- LATENCY & TIME SYNC ---

time_offset = 0.0

def calibrate_time_sync(binance):
    global time_offset
    log_step("Starting time sync calibration")
    measurements = []
    for i in range(10):
        try:
            t0 = time.time() * 1000
            server_time = int(binance.futures_time()['serverTime'])
            t1 = time.time() * 1000
            offset = server_time - ((t0 + t1) / 2)
            measurements.append(offset)
            time.sleep(0.05)
        except Exception as e:
            log_error(f"calibrate_time_sync (iteration {i})", e)
    if measurements:
        time_offset = sum(measurements) / len(measurements)
    log_step(f"Time offset set to {time_offset} ms")

def get_server_time():
    return time.time() * 1000 + time_offset

def precision_wait(target_ts_ms):
    log_step(f"Entering precision_wait for target {target_ts_ms}")
    while True:
        current = get_server_time()
        if current >= target_ts_ms:
            log_step("precision_wait finished")
            return
        remaining = target_ts_ms - current
        time.sleep(max(remaining / 2000, 0.001))

import re
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
        now = datetime.utcnow()
        trade_time = datetime.strptime(raw_time, "%H:%M:%S").time()
        full_time = datetime.combine(now.date(), trade_time)
        if full_time < now:
            full_time += timedelta(days=1)
        return {
            "coin": coin,
            "fr": fr,
            "leverage": leverage,
            "time": full_time
        }
    return None

def count_coins(coin, leverage):
    log_step(f"Counting coins for {coin} at leverage {leverage}")
    try:
        price = float(binance.futures_symbol_ticker(symbol=coin)['price'])
        max_coins = (10 * leverage) / price
        qty = int(max_coins * 0.75)
        log_step(f"Calculated qty={qty} for {coin}")
        return qty
    except Exception as e:
        log_error("count_coins", e)
        return 0

def is_still_good(coin):
    log_step(f"Checking is_still_good for {coin}")
    try:
        fr_data = binance.futures_premium_index(symbol=coin)
        current_fr = float(fr_data['lastFundingRate'])
        abs_fr = abs(current_fr) * 100
        lev_data = binance.futures_leverage_bracket(symbol=coin)
        leverage = lev_data[0]['brackets'][0]['initialLeverage']
        value = abs_fr * leverage
        log_step(f"is_still_good={value > 100} (value={value}, current_fr={current_fr})")
        return value > 100, value, current_fr
    except Exception as e:
        log_error("is_still_good", e)
        return False, 0, 0

def make_trade(coin):
    try:
        log_step(f"make_trade started for {coin}")
        check_balance_sync()  # check balance before trade
        data = trade_notes[coin]

        check_time = data['time'] - timedelta(minutes=5)
        check_time_ts = check_time.replace(tzinfo=None).timestamp() * 1000
        precision_wait(check_time_ts)

        good, value, current_fr = is_still_good(coin)
        if not good:
            say_sync(LOG_ROOM_ID, f"🚫 STOP: {coin} now {value:.2f}")
            del trade_notes[coin]
            return

        direction = "SELL" if current_fr > 0 else "BUY"
        say_sync(LOG_ROOM_ID, f"✅ GO: {coin} {direction} {data['qty']} coins")

        try:
            binance.futures_change_leverage(symbol=coin, leverage=data['leverage'])
        except Exception as e:
            say_sync(LOG_ROOM_ID, f"❌ TRADE FAILED: Could not set leverage for {coin}: {e}")
            del trade_notes[coin]
            return

        entry_time = data['time'] - timedelta(seconds=1)
        entry_time_ts = entry_time.replace(tzinfo=None).timestamp() * 1000
        precision_wait(entry_time_ts)

        try:
            binance.futures_create_order(
                symbol=coin,
                side=direction,
                type="MARKET",
                quantity=data['qty']
            )
        except Exception as e:
            say_sync(LOG_ROOM_ID, f"❌ TRADE FAILED: Could not open position for {coin}: {e}")
            del trade_notes[coin]
            return

        real_entry = datetime.utcnow()

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
            except Exception as e:
                log_error("make_trade (funding fee check)", e)
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
            say_sync(LOG_ROOM_ID, f"❌ TRADE FAILED: Could not close position for {coin}: {e}")
            del trade_notes[coin]
            return

        say_sync(LOG_ROOM_ID,
            f"💰 DONE: {coin}\n"
            f"▫️ Money: {'✅' if got_money else '❌'} {money_bag:.6f} USDT\n"
            f"▫️ Time: {datetime.utcnow().strftime('%H:%M:%S')} UTC")
        del trade_notes[coin]
        check_balance_sync()  # check after trade
        log_step(f"make_trade finished for {coin}")
    except Exception as e:
        log_error("make_trade", e)

async def handle_message(update, context: ContextTypes.DEFAULT_TYPE):
    try:
        msg = update.message.text
        now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
        print(f"[DEBUG {now}] Received in alert room: {msg!r}")  # Debug log for every received message

        # Flexible matching for "no lead found"
        if msg and "no lead found" in msg.lower():
            await say(LOG_ROOM_ID,
                f"🔄 NO TRADE NOW\n"
                f"▫️ Time: {datetime.utcnow().strftime('%H:%M:%S UTC')}\n"
                f"▫️ Robot is awake!")
            log_step("NO TRADE NOW message sent")
            return

        if "🚨 ALERT:" in msg or "🚨 <b>ALERT:" in msg:
            alert_data = read_alert(msg)
            if alert_data:
                coin = alert_data["coin"]

                qty = count_coins(coin, alert_data["leverage"])
                if qty == 0:
                    await say(LOG_ROOM_ID, f"❌ TRADE FAILED: Could not count coins for {coin}.")
                    log_step(f"Could not count coins for {coin}, trade aborted")
                    return

                trade_notes[coin] = {
                    'qty': qty,
                    'leverage': alert_data["leverage"],
                    'time': alert_data["time"]
                }

                await say(LOG_ROOM_ID,
                    f"🔔 NEW: {coin}\n"
                    f"▫️ Qty: {qty}\n"
                    f"▫️ Check at {(alert_data['time']-timedelta(minutes=5)).strftime('%H:%M:%S')} UTC")
                log_step(f"Trade note set for {coin}, timer for make_trade started")
                wait_time = (alert_data['time'] - timedelta(minutes=5) - datetime.utcnow()).total_seconds()
                threading.Timer(wait_time, make_trade, [coin]).start()
    except Exception as e:
        log_error("handle_message", e, extra=f"Message: {update.message.text}")

def main():
    global application
    try:
        log_step("Building application")
        application = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(on_startup).build()
        log_step("Adding handler")
        application.add_handler(MessageHandler(filters.Chat(ALERT_ROOM_ID) & filters.TEXT, handle_message))
        log_step("Starting polling")
        application.run_polling()
    except Exception as e:
        print(f"Fatal error on startup: {e}")
        traceback.print_exc()
        say_sync(LOG_ROOM_ID, f"❌ FATAL STARTUP ERROR: {e}")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"=== BOT CRASHED: {e} ===")
        traceback.print_exc()
        sys.exit(1)
