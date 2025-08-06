import os
import json
import datetime
import requests
from dotenv import load_dotenv

# Load from custom env file
load_dotenv(dotenv_path="t.env")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
REPORTS_FILE = "trade_logs.json"

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"[!] Failed to send message: {e}")

def load_logs():
    if not os.path.exists(REPORTS_FILE):
        with open(REPORTS_FILE, "w") as f:
            json.dump([], f)
        return []
    with open(REPORTS_FILE, "r") as f:
        return json.load(f)

def save_logs(logs):
    with open(REPORTS_FILE, "w") as f:
        json.dump(logs, f, indent=4)

# Record a new buy
def record_buy(token, coin_name, buy_market_cap, buy_time, amount_usd, priority_fee):
    logs = load_logs()
    logs.append({
        "token": token,
        "coin_name": coin_name,
        "buy_market_cap": buy_market_cap,
        "buy_time": buy_time,
        "amount_usd": amount_usd,
        "priority_fee": priority_fee,
        "sell_market_cap": None,
        "sell_time": None,
        "profit_usd": None,
        "date": str(datetime.datetime.utcnow().date())
    })
    save_logs(logs)

# Record sell for the most recent matching buy entry
def record_sell(token, sell_market_cap, sell_time, profit_usd):
    logs = load_logs()
    for entry in reversed(logs):
        if entry["token"] == token and entry["sell_time"] is None:
            entry["sell_market_cap"] = sell_market_cap
            entry["sell_time"] = sell_time
            entry["profit_usd"] = profit_usd
            break
    save_logs(logs)

# Daily / Monthly report logic
def generate_report(logs):
    report = ""
    total_profit = 0
    for entry in logs:
        buy_time = entry.get("buy_time", "N/A")
        sell_time = entry.get("sell_time", "N/A")
        token = entry.get("token", "N/A")
        name = entry.get("coin_name", "N/A")
        buy_cap = entry.get("buy_market_cap", "N/A")
        sell_cap = entry.get("sell_market_cap", "N/A")
        profit = entry.get("profit_usd", 0)
        priority_fee = entry.get("priority_fee", "N/A")

        total_profit += float(profit) if profit else 0
        report += (
            f"🔹 *{name}*\n"
            f"CA: `{token}`\n"
            f"🟢 Buy: {buy_time} (${buy_cap})\n"
            f"🔴 Sell: {sell_time} (${sell_cap})\n"
            f"💰 Profit: ${profit}\n"
            f"⚡ Fee: {priority_fee} SOL\n\n"
        )
    report += f"\n📊 *Total Profit:* ${round(total_profit, 2)}"
    return report

def send_daily_report():
    today = datetime.datetime.utcnow().date()
    logs = load_logs()
    today_logs = [log for log in logs if log.get("date") == str(today)]
    if not today_logs:
        send_telegram_message("🗓️ No trades were made today.")
    else:
        message = "📅 Daily Report:\n\n" + generate_report(today_logs)
        send_telegram_message(message)

def send_monthly_report():
    now = datetime.datetime.utcnow()
    logs = load_logs()
    month_logs = [
        log for log in logs
        if datetime.datetime.strptime(log.get("date"), "%Y-%m-%d").month == now.month
        and datetime.datetime.strptime(log.get("date"), "%Y-%m-%d").year == now.year
    ]
    if not month_logs:
        send_telegram_message("🗓️ No trades were made this month.")
    else:
        message = "📅 Monthly Report:\n\n" + generate_report(month_logs)
        send_telegram_message(message)

if __name__ == "__main__":
    mode = os.getenv("REPORT_MODE", "daily").lower()
    if mode == "daily":
        send_daily_report()
    elif mode == "monthly":
        send_monthly_report()
    else:
        print("Invalid REPORT_MODE. Use 'daily' or 'monthly'.")
