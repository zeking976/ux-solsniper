import os
import json
import datetime
import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path="t.env")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
REPORTS_FILE = "trade_logs.json"

def send_telegram_message(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[!] Missing Telegram token or chat id")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": int(TELEGRAM_CHAT_ID), "text": text, "parse_mode": "Markdown"}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if not resp.ok:
            print("[!] Telegram API error:", resp.text)
    except Exception as e:
        print("[!] Failed to send message:", e)

def load_logs():
    if not os.path.exists(REPORTS_FILE):
        with open(REPORTS_FILE, "w") as f:
            json.dump([], f)
        return []
    with open(REPORTS_FILE, "r") as f:
        try:
            return json.load(f)
        except Exception:
            return []

def save_logs(logs):
    with open(REPORTS_FILE, "w") as f:
        json.dump(logs, f, indent=2)

def record_buy(token, coin_name, buy_market_cap, buy_time, amount_usd, priority_fee_sol):
    logs = load_logs()
    logs.append({
        "token": token,
        "coin_name": coin_name,
        "buy_market_cap": buy_market_cap,
        "buy_time": buy_time,
        "amount_usd": amount_usd,
        "priority_fee": priority_fee_sol,
        "sell_market_cap": None,
        "sell_time": None,
        "profit_usd": None,
        "date": str(datetime.datetime.utcnow().date())
    })
    save_logs(logs)

def record_sell(token, sell_market_cap, sell_time, profit_usd):
    logs = load_logs()
    for entry in reversed(logs):
        if entry["token"] == token and entry["sell_time"] is None:
            entry["sell_market_cap"] = sell_market_cap
            entry["sell_time"] = sell_time
            entry["profit_usd"] = profit_usd
            break
    save_logs(logs)

def generate_report(logs):
    total_profit = 0
    lines = []
    for e in logs:
        profit = e.get("profit_usd") or 0
        total_profit += float(profit)
        lines.append(f"🔹 {e.get('coin_name','N/A')} | CA: `{e.get('token')}` | Buy: {e.get('buy_time')} | Sell: {e.get('sell_time')} | Profit: ${profit}")
    lines.append(f"\nTotal profit: ${round(total_profit,2)}")
    return "\n".join(lines)

def send_daily_report():
    today = str(datetime.datetime.utcnow().date())
    logs = load_logs()
    today_logs = [l for l in logs if l.get("date") == today]
    if not today_logs:
        send_telegram_message("🗓️ No trades today.")
        return
    send_telegram_message("📅 Daily Report\n\n" + generate_report(today_logs))

def send_monthly_report():
    now = datetime.datetime.utcnow()
    logs = load_logs()
    month_logs = [l for l in logs if datetime.datetime.strptime(l.get("date"), "%Y-%m-%d").month == now.month]
    if not month_logs:
        send_telegram_message("🗓️ No trades this month.")
        return
    send_telegram_message("📅 Monthly Report\n\n" + generate_report(month_logs))

if __name__ == "__main__":
    mode = os.getenv("REPORT_MODE", "daily").lower()
    if mode == "daily":
        send_daily_report()
    else:
        send_monthly_report()