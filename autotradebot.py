import re
import time
import threading
import os
import requests
from datetime import datetime, timedelta
from binance.client import Client
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

try:
    TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
    ALERT_ROOM_ID = int(os.environ["ALERT_ROOM_ID"])
    LOG_ROOM_ID = int(os.environ["LOG_ROOM_ID"])
    BINANCE_API_KEY = os.environ["BINANCE_API_KEY"]
    BINANCE_API_SECRET = os.environ["BINANCE_API_SECRET"]
except KeyError as e:
    print(f"❗ Environment variable missing: {e}")
    exit(1)

binance = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
trade_notes = {}
application = None  # Will be set in main()
message_buffer = []

# --- Logging and Messaging Utilities ---

async def say(room, message):
    print(f"[Telegram Log to {room}] {message}")
    try:
        await application.bot.send_message(chat_id=room, text=message)
    except Exception as e:
        print(f"[Telegram ERROR] Failed to send message: {e}")

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

async def flush_message_buffer():
    # Called after the event loop is up
    while message_buffer:
        room, message = message_buffer.pop(0)
        await say(room, message)

# --- Error Handling Helper ---

def handle_exception(where, e):
    msg = f"❌ ERROR in {where}: {e}"
    print(msg)
    say_sync(LOG_ROOM_ID, msg)

# --- Balance Check ---

def check_balance_sync():
    try:
        # Get USDT balance from Binance futures wallet
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
    except Exception as e:
        handle_exception("check_balance_sync", e)
        return 0

async def check_balance_async():
    # You can use this in async handlers if needed
    from binance import AsyncClient
    try:
        async_binance = await AsyncClient.create(BINANCE_API_KEY, BINANCE_API_SECRET)
        balance_list = await async_binance.futures_account_balance()
        await async_binance.close_connection()
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
            await say(LOG_ROOM_ID, msg)
        return usdt_bal
    except Exception as e:
        handle_exception("check_balance_async", e)
        return 0

# --- Show Railway IP Address ---

async def show_ip_startup():
    try:
        ip = requests.get("https://api.ipify.org").text.strip()
        await say(LOG_ROOM_ID, f"🚦 Railway Public IP: `{ip}`\nWhitelist this IP for Binance API access.")
    except Exception as e:
        await say(LOG_ROOM_ID, f"❗ Could not fetch Railway Public IP: {e}")
        handle_exception("show_ip_startup", e)

# --- Startup Handler ---

async def on_startup(application):
    try:
        calibrate_time_sync(binance)
        await flush_message_buffer()
        await show_ip_startup()
        await say(LOG_ROOM_ID, "🤖 HELLO! I'M YOUR MONEY ROBOT (NOW FASTER, MORE PRECISE, AND SAFER)!")
        check_balance_sync()
    except Exception as e:
        handle_exception("on_startup", e)

# --- LATENCY & TIME SYNC ---

time_offset = 0.0

def calibrate_time_sync(binance):
    global time_offset
    measurements = []
    for _ in range(10):
        try:
            t0 = time.time() * 1000
            server_time = int(binance.futures_time()['serverTime'])
            t1 = time.time() * 1000
            offset = server_time - ((t0 + t1) / 2)
            measurements.append(offset)
            time.sleep(0.05)
        except Exception as e:
            handle_exception("calibrate_time_sync", e)
    if measurements:
        time_offset = sum(measurements) / len(measurements)

def get_server_time():
    return time.time() * 1000 + time_offset

def precision_wait(target_ts_ms):
    while True:
        current = get_server_time()
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
    try:
        price = float(binance.futures_symbol_ticker(symbol=coin)['price'])
        max_coins = (10 * leverage) / price
        return int(max_coins * 0.75)
    except Exception as e:
        handle_exception("count_coins", e)
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
        handle_exception("is_still_good", e)
        return False, 0, 0

def make_trade(coin):
    try:
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
                handle_exception("make_trade (funding fee check)", e)
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
    except Exception as e:
        handle_exception("make_trade", e)

async def handle_message(update, context: ContextTypes.DEFAULT_TYPE):
    try:
        msg = update.message.text

        if "❕ No lead found" in msg:
            await say(LOG_ROOM_ID,
                f"🔄 NO TRADE NOW\n"
                f"▫️ Time: {datetime.utcnow().strftime('%H:%M:%S UTC')}\n"
                f"▫️ Robot is awake!")
            return

        if "🚨 ALERT:" in msg or "🚨 <b>ALERT:" in msg:
            alert_data = read_alert(msg)
            if alert_data:
                coin = alert_data["coin"]

                qty = count_coins(coin, alert_data["leverage"])
                if qty == 0:
                    await say(LOG_ROOM_ID, f"❌ TRADE FAILED: Could not count coins for {coin}.")
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

                wait_time = (alert_data['time'] - timedelta(minutes=5) - datetime.utcnow()).total_seconds()
                threading.Timer(wait_time, make_trade, [coin]).start()
    except Exception as e:
        handle_exception("handle_message", e)

def main():
    global application
    try:
        print("Building application...")
        application = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(on_startup).build()
        print("Adding handler...")
        application.add_handler(MessageHandler(filters.Chat(ALERT_ROOM_ID) & filters.TEXT, handle_message))
        print("Starting polling...")
        application.run_polling()
    except Exception as e:
        print(f"Fatal error on startup: {e}")
        say_sync(LOG_ROOM_ID, f"❌ FATAL STARTUP ERROR: {e}")

if __name__ == "__main__":
    main()
