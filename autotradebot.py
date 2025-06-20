import re
import time
import threading
import os
import requests
from datetime import datetime, timedelta
from binance.um_futures import UMFutures
from telegram import Bot
from telegram.ext import Updater, MessageHandler, Filters

# ===== GET SECRETS FROM ENVIRONMENT VARIABLES =====
try:
    TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
    ALERT_ROOM_ID = os.environ["ALERT_ROOM_ID"]
    LOG_ROOM_ID = os.environ["LOG_ROOM_ID"]
    BINANCE_API_KEY = os.environ["BINANCE_API_KEY"]
    BINANCE_API_SECRET = os.environ["BINANCE_API_SECRET"]
except KeyError as e:
    print(f"❗ Environment variable missing: {e}")
    exit(1)
# ==================================================

money_bot = Bot(token=TELEGRAM_TOKEN)
binance = UMFutures(BINANCE_API_KEY, BINANCE_API_SECRET)
trade_notes = {}

# --- Show Railway IP Address ---
def show_ip():
    try:
        ip = requests.get("https://api.ipify.org").text.strip()
        print(f"🚦 Railway Public IP: {ip}")
        money_bot.send_message(chat_id=LOG_ROOM_ID, text=f"🚦 Railway Public IP: `{ip}`\nWhitelist this IP for Binance API access.", parse_mode="Markdown")
    except Exception as e:
        print("Could not fetch public IP:", e)
        money_bot.send_message(chat_id=LOG_ROOM_ID, text=f"❗ Could not fetch Railway Public IP: {e}")

# --- LATENCY & TIME SYNC ---

time_offset = 0.0

def calibrate_time_sync(binance):
    global time_offset
    measurements = []
    for _ in range(10):
        try:
            t0 = time.time() * 1000
            server_time = int(binance.time()['serverTime'])
            t1 = time.time() * 1000
            offset = server_time - ((t0 + t1) / 2)
            measurements.append(offset)
            time.sleep(0.05)
        except Exception:
            pass
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

def say(room, message):
    money_bot.send_message(chat_id=room, text=message)

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
    price = float(binance.ticker_price(coin)['price'])
    max_coins = (10 * leverage) / price
    return int(max_coins * 0.75)

def is_still_good(coin):
    try:
        fr_data = binance.premium_index(symbol=coin)
        current_fr = float(fr_data['lastFundingRate'])
        abs_fr = abs(current_fr) * 100
        lev_data = binance.leverage_bracket(symbol=coin)
        leverage = lev_data[0]['brackets'][0]['initialLeverage']
        value = abs_fr * leverage
        return value > 100, value, current_fr
    except Exception as e:
        say(LOG_ROOM_ID, f"❌ ERROR: Pre-check for {coin} failed: {e}")
        return False, 0, 0

def make_trade(coin):
    data = trade_notes[coin]

    check_time = data['time'] - timedelta(minutes=5)
    check_time_ts = check_time.replace(tzinfo=None).timestamp() * 1000
    precision_wait(check_time_ts)

    good, value, current_fr = is_still_good(coin)
    if not good:
        say(LOG_ROOM_ID, f"🚫 STOP: {coin} now {value:.2f}")
        del trade_notes[coin]
        return

    direction = "SELL" if current_fr > 0 else "BUY"
    say(LOG_ROOM_ID, f"✅ GO: {coin} {direction} {data['qty']} coins")

    try:
        binance.change_leverage(symbol=coin, leverage=data['leverage'], isIsolated="TRUE")
    except Exception as e:
        say(LOG_ROOM_ID, f"❌ TRADE FAILED: Could not set leverage for {coin}: {e}")
        del trade_notes[coin]
        return

    entry_time = data['time'] - timedelta(seconds=1)
    entry_time_ts = entry_time.replace(tzinfo=None).timestamp() * 1000
    precision_wait(entry_time_ts)

    try:
        binance.new_order(
            symbol=coin,
            side=direction,
            type="MARKET",
            quantity=data['qty'],
            isIsolated="TRUE"
        )
    except Exception as e:
        say(LOG_ROOM_ID, f"❌ TRADE FAILED: Could not open position for {coin}: {e}")
        del trade_notes[coin]
        return

    real_entry = datetime.utcnow()

    got_money = False
    money_bag = 0.0
    wait_start = time.time()
    while not got_money and (time.time() - wait_start < 10):
        try:
            money_history = binance.get_income_history(
                symbol=coin,
                incomeType="FUNDING_FEE",
                limit=1
            )
            if money_history and money_history[0]['time'] > int(real_entry.timestamp() * 1000):
                got_money = True
                money_bag = float(money_history[0]['income'])
                break
        except Exception as e:
            say(LOG_ROOM_ID, f"❌ ERROR: Funding fee check for {coin} failed: {e}")
            break
        time.sleep(0.1)

    close_side = "BUY" if direction == "SELL" else "SELL"
    try:
        binance.new_order(
            symbol=coin,
            side=close_side,
            type="MARKET",
            quantity=data['qty'],
            reduceOnly="TRUE"
        )
    except Exception as e:
        say(LOG_ROOM_ID, f"❌ TRADE FAILED: Could not close position for {coin}: {e}")
        del trade_notes[coin]
        return

    say(LOG_ROOM_ID,
        f"💰 DONE: {coin}\n"
        f"▫️ Money: {'✅' if got_money else '❌'} {money_bag:.6f} USDT\n"
        f"▫️ Time: {datetime.utcnow().strftime('%H:%M:%S')} UTC")
    del trade_notes[coin]

def handle_message(update, context):
    msg = update.message.text

    if "❕ No lead found" in msg:
        say(LOG_ROOM_ID,
            f"🔄 NO TRADE NOW\n"
            f"▫️ Time: {datetime.utcnow().strftime('%H:%M:%S UTC')}\n"
            f"▫️ Robot is awake!")
        return

    if "🚨 ALERT:" in msg or "🚨 <b>ALERT:" in msg:
        if alert_data := read_alert(msg):
            coin = alert_data["coin"]

            try:
                qty = count_coins(coin, alert_data["leverage"])
            except Exception as e:
                say(LOG_ROOM_ID, f"❌ TRADE FAILED: Could not count coins for {coin}: {e}")
                return

            trade_notes[coin] = {
                'qty': qty,
                'leverage': alert_data["leverage"],
                'time': alert_data["time"]
            }

            say(LOG_ROOM_ID,
                f"🔔 NEW: {coin}\n"
                f"▫️ Qty: {qty}\n"
                f"▫️ Check at {(alert_data['time']-timedelta(minutes=5)).strftime('%H:%M:%S')} UTC")

            wait_time = (alert_data['time'] - timedelta(minutes=5) - datetime.utcnow()).total_seconds()
            threading.Timer(wait_time, make_trade, [coin]).start()

calibrate_time_sync(binance)
show_ip()
say(LOG_ROOM_ID, "🤖 HELLO! I'M YOUR MONEY ROBOT (NOW FASTER, MORE PRECISE, AND SAFER)!")

updater = Updater(TELEGRAM_TOKEN)
dp = updater.dispatcher
dp.add_handler(MessageHandler(Filters.chat(int(ALERT_ROOM_ID)) & Filters.text, handle_message))
updater.start_polling()
updater.idle()