#!/usr/bin/env python3
"""
reports.py

Aligned with sniper.py and utils.py.

Handles:
- Trade logs (JSON)
- Daily/monthly/weekly reports via Telegram
- Notifications for buys/sells with priority fees
- DRY_RUN compatibility
- Generates meme-based profit images using Google Gemini API
"""

import os
import sys
import csv
import json
import logging
import datetime
from typing import List, Dict, Any, Optional
import requests
import random
import matplotlib.pyplot as plt
from PIL import Image
from io import BytesIO
from dotenv import load_dotenv

# -------------------------
# Import from utils.py
# -------------------------
from utils import md_code, resolve_token_name, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DRY_RUN, logger, get_sol_price_usd

# -------------------------
# Load environment
# -------------------------
load_dotenv(dotenv_path=os.path.expanduser("~/t.env"))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
REPORTS_FILE = os.getenv("REPORTS_FILE", "trade_logs.json")
DEFAULT_EXPORT_CSV = os.getenv("REPORT_EXPORT_CSV", "trade_logs_export.csv")
STOP_LOSS = float(os.getenv("STOP_LOSS", "-20") or -20.0)
TAKE_PROFIT = float(os.getenv("TAKE_PROFIT", "100") or 100.0)

# Validate environment variables
if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    logger.warning("Telegram credentials missing. Notifications disabled.")
if not GEMINI_API_KEY:
    logger.warning("Google Gemini API key missing. Image generation disabled.")

# -------------------------
# Telegram helpers
# -------------------------
def send_telegram_message(text: str, image_path: Optional[str] = None) -> None:
    if DRY_RUN or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Telegram disabled / DRY_RUN] Message would be:\n", text)
        if image_path:
            print(f"[Image would be sent: {image_path}]")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": int(TELEGRAM_CHAT_ID), "text": text, "parse_mode": "Markdown"}
        resp = requests.post(url, json=payload, timeout=15)
        if resp.ok and image_path:
            with open(image_path, "rb") as photo:
                files = {"photo": photo}
                params = {"chat_id": TELEGRAM_CHAT_ID, "caption": text}
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto", data=params, files=files, timeout=15)
        if not resp.ok:
            logger.error(f"Telegram API error: {resp.text}")
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")

# -------------------------
# Logs helpers
# -------------------------
def ensure_logs_file(path: str = REPORTS_FILE) -> None:
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump([], f)

def load_logs(path: str = REPORTS_FILE) -> List[Dict[str, Any]]:
    ensure_logs_file(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and "logs" in data and isinstance(data["logs"], list):
                return data["logs"]
            return []
    except Exception:
        return []

def save_logs(logs: List[Dict[str, Any]], path: str = REPORTS_FILE) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(logs, f, indent=2, default=str)

# -------------------------
# Recording functions
# -------------------------
def record_buy(token: str, coin_name: Optional[str], buy_market_cap: Optional[float],
               amount_usd: float, priority_fee_sol: Optional[float]) -> None:
    if not coin_name:
        coin_name = resolve_token_name(token) or "N/A"
    entry = {
        "token": token,
        "coin_name": coin_name,
        "buy_market_cap": buy_market_cap,
        "buy_time": datetime.datetime.utcnow().isoformat(),
        "amount_usd": amount_usd,
        "buy_priority_fee": priority_fee_sol,
        "sell_market_cap": None,
        "sell_time": None,
        "profit_usd": None,
        "sell_priority_fee": None,
        "date": str(datetime.datetime.utcnow().date())
    }
    logs = load_logs()
    logs.append(entry)
    save_logs(logs)
    send_buy_notification(token, coin_name, amount_usd, buy_market_cap, priority_fee_sol)

def record_sell(token: str, sell_market_cap: Optional[float], profit_usd: Optional[float],
                priority_fee_sol: Optional[float]) -> None:
    logs = load_logs()
    coin_name_for_msg = "N/A"
    found = False
    for entry in reversed(logs):
        if entry.get("token") == token and entry.get("sell_time") is None:
            entry["sell_market_cap"] = sell_market_cap
            entry["sell_time"] = datetime.datetime.utcnow().isoformat()
            entry["profit_usd"] = profit_usd
            entry["sell_priority_fee"] = priority_fee_sol
            coin_name_for_msg = entry.get("coin_name") or "N/A"
            found = True
            break
    if not found:
        logs.append({
            "token": token,
            "coin_name": coin_name_for_msg,
            "buy_market_cap": None,
            "buy_time": None,
            "amount_usd": None,
            "buy_priority_fee": None,
            "sell_market_cap": sell_market_cap,
            "sell_time": datetime.datetime.utcnow().isoformat(),
            "profit_usd": profit_usd,
            "sell_priority_fee": priority_fee_sol,
            "date": str(datetime.datetime.utcnow().date())
        })
    save_logs(logs)
    if coin_name_for_msg == "N/A":
        coin_name_for_msg = resolve_token_name(token) or "N/A"
    send_sell_notification(token, coin_name_for_msg, sell_market_cap, profit_usd, priority_fee_sol)

# -------------------------
# Notification helpers
# -------------------------
def send_buy_notification(token: str, coin_name: str, amount_usd: float,
                          buy_mcap: Optional[float], priority_fee_sol: Optional[float]) -> None:
    mc_text = f"${buy_mcap:,.0f}" if buy_mcap else "N/A"
    tip_text = f"{priority_fee_sol:.3f} SOL" if priority_fee_sol else "N/A"
    msg = (
        f"✅ *BUY EXECUTED*\n"
        f"• Coin: *{coin_name}*\n"
        f"• CA: {md_code(token)}\n"
        f"• Amount: ${amount_usd:.2f}\n"
        f"• Market Cap @ Buy: {mc_text}\n"
        f"• Tip Paid: {tip_text}\n"
        f"• SL: {STOP_LOSS:.1f}% | TP: {TAKE_PROFIT:.1f}%"
    )
    send_telegram_message(msg)

def send_sell_notification(token: str, coin_name: str, sell_mcap: Optional[float],
                           profit_usd: Optional[float], priority_fee_sol: Optional[float]) -> None:
    mc_text = f"${sell_mcap:,.0f}" if sell_mcap else "N/A"
    profit_text = f"${profit_usd:.2f}" if profit_usd else "N/A"
    tip_text = f"{priority_fee_sol:.3f} SOL" if priority_fee_sol else "N/A"
    msg = (
        f"🟣 *SELL EXECUTED*\n"
        f"• Coin: *{coin_name}*\n"
        f"• CA: {md_code(token)}\n"
        f"• Market Cap @ Sell: {mc_text}\n"
        f"• Profit: {profit_text}\n"
        f"• Tip Paid: {tip_text}\n"
        f"• SL: {STOP_LOSS:.1f}% | TP: {TAKE_PROFIT:.1f}%"
    )
    send_telegram_message(msg)

# -------------------------
# Reporting and Image Generation
# -------------------------
def _safe_float(v: Any) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0

def calculate_period_data(logs: List[Dict[str, Any]], period: str) -> Dict[str, Any]:
    now = datetime.datetime.utcnow()
    sol_price = get_sol_price_usd()
    total_profit_usd = 0.0
    initial_capital_usd = 0.0
    coins_bought = set()
    daily_profits = {}  # For monthly chart: date -> profit_usd
    for entry in logs:
        try:
            trade_date = datetime.datetime.strptime(entry.get("date", ""), "%Y-%m-%d").date()
            if period == "daily" and trade_date == now.date():
                total_profit_usd += _safe_float(entry.get("profit_usd"))
                initial_capital_usd += _safe_float(entry.get("amount_usd"))
                if entry.get("coin_name"):
                    coins_bought.add(entry["coin_name"])
            elif period == "weekly" and (now.date() - trade_date).days < 7:
                total_profit_usd += _safe_float(entry.get("profit_usd"))
            elif period == "monthly" and trade_date.month == now.month and trade_date.year == now.year:
                total_profit_usd += _safe_float(entry.get("profit_usd"))
                date_str = str(trade_date)
                daily_profits[date_str] = daily_profits.get(date_str, 0.0) + _safe_float(entry.get("profit_usd"))
        except Exception:
            continue
    total_profit_sol = total_profit_usd / sol_price if sol_price else 0.0
    data = {
        "total_profit_usd": total_profit_usd,
        "total_profit_sol": total_profit_sol,
        "date": now.strftime("%Y-%m-%d"),
        "month": now.strftime("%B %Y") if period == "monthly" else None,
        "coins_bought": list(coins_bought) if period == "daily" else None,
        "initial_capital_usd": initial_capital_usd if period == "daily" else None,
        "daily_profits": daily_profits if period == "monthly" else None
    }
    return data

def generate_chart(daily_profits: Dict[str, float]) -> str:
    if not daily_profits:
        return None
    dates = sorted(daily_profits.keys())
    profits = [daily_profits[d] for d in dates]
    cumulative = [sum(profits[:i+1]) for i in range(len(profits))]
    plt.figure(figsize=(4, 3))
    plt.plot(dates, cumulative, marker='o', color='g')
    plt.title("Monthly Profit Growth")
    plt.xlabel("Date")
    plt.ylabel("Cumulative Profit (USD)")
    plt.xticks(rotation=45)
    plt.tight_layout()
    chart_path = "chart.png"
    plt.savefig(chart_path)
    plt.close()
    return chart_path

def generate_meme_image(period_data: Dict[str, Any], period: str, telegram_username: str) -> str:
    if not GEMINI_API_KEY:
        return None
    meme_themes = [
        "crypto moon", "doge to the moon", "rich uncle pennybags", "breaking bad money",
        "game of thrones gold", "spongebob profit", "stonks meme", "elon musk crypto",
        "trump take egg", "crypto market dump", "ai scream", "anxiety doechii",
        "barbershop quarter", "little french fish", "duke white lotus"
    ]
    theme = random.choice(meme_themes)
    profit_sol_text = f"{period_data['total_profit_sol']:.2f} SOL"
    profit_usd_text = f"${period_data['total_profit_usd']:.2f}"
    date_text = period_data['date']
    
    if period == "daily":
        coins = ", ".join(period_data['coins_bought']) if period_data['coins_bought'] else "N/A"
        initial_cap = f"${period_data['initial_capital_usd']:.2f}"
        card_type = "Daily Profit Card"
        style = "cartoon style with dynamic background, neon glow on text, 1080x1080 resolution"
        prompt_text = f"Profit: {profit_sol_text} ({profit_usd_text}), Coins: {coins}, Initial: {initial_cap}, User: @{telegram_username}, Date: {date_text}"
    elif period == "weekly":
        card_type = "Weekly Profit Card"
        style = "meme style with vibrant colors, neon borders, 1080x1080 resolution"
        prompt_text = f"Profit: {profit_sol_text} ({profit_usd_text}), Date: {date_text}"
    else:  # monthly
        card_type = "Monthly Profit Card"
        style = "highly attractive style with glowing effects, neon background, 1080x1080 resolution"
        prompt_text = f"Profit: {profit_sol_text} ({profit_usd_text}), Month: {period_data['month']}, Date: {date_text}"
    
    prompt = f"Generate a {card_type} based on trending meme '{theme}', {style}, with catchy bold font for text: {prompt_text}, neon glow where attractive"
    
    try:
        url = "https://api.google.ai/gemini/v1/images:generate"
        headers = {
            "Authorization": f"Bearer {GEMINI_API_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "prompt": prompt,
            "width": 1080,
            "height": 1080,
            "num_images": 1
        }
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        image_url = response.json().get("images")[0].get("url")  # Adjust based on actual Gemini API response structure
        
        # Download image
        img_response = requests.get(image_url, timeout=30)
        img_response.raise_for_status()
        image_path = f"profit_{period}_{int(datetime.datetime.now().timestamp())}.png"
        
        if period == "monthly" and period_data["daily_profits"]:
            chart_path = generate_chart(period_data["daily_profits"])
            if chart_path:
                base_img = Image.open(BytesIO(img_response.content))
                chart_img = Image.open(chart_path)
                chart_img = chart_img.resize((300, 225))  # Small chart
                base_img.paste(chart_img, (base_img.width - 320, base_img.height - 245))  # Bottom right
                base_img.save(image_path)
                os.remove(chart_path)
            else:
                with open(image_path, "wb") as f:
                    f.write(img_response.content)
        else:
            with open(image_path, "wb") as f:
                f.write(img_response.content)
        
        logger.info(f"Generated meme image for {period} with theme {theme}")
        return image_path
    except Exception as e:
        logger.error(f"Failed to generate image with Google Gemini API: {e}")
        return None

def generate_report(logs: List[Dict[str, Any]], period: str) -> str:
    period_data = calculate_period_data(logs, period)
    telegram_username = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getChat?chat_id={TELEGRAM_CHAT_ID}").json().get("result", {}).get("username", "UnknownUser")
    image_path = generate_meme_image(period_data, period, telegram_username)
    total_profit_sol = period_data["total_profit_sol"]
    total_profit_usd = period_data["total_profit_usd"]
    header = [
        f"*Configured SL/TP:* {STOP_LOSS:.1f}% / {TAKE_PROFIT:.1f}%",
        f"*Total profit ({period.capitalize()}):* {total_profit_sol:.2f} SOL (${total_profit_usd:.2f})"
    ]
    lines = []
    if period == "daily":
        coins = period_data["coins_bought"]
        if coins:
            lines.append(f"*Coins Bought:* {', '.join(coins)}")
        lines.append(f"*Initial Capital:* ${period_data['initial_capital_usd']:.2f}")
    elif period == "monthly":
        lines.append(f"*Month:* {period_data['month']}")
    return "\n".join(header) + "\n\n" + "\n".join(lines), image_path

def send_daily_report() -> None:
    logs = load_logs()
    if not any(l.get("date") == str(datetime.datetime.utcnow().date()) for l in logs):
        send_telegram_message("🗓️ No trades today.")
        return
    report_text, image_path = generate_report(logs, "daily")
    send_telegram_message("📅 *Daily Report*\n\n" + report_text, image_path)

def send_weekly_report() -> None:
    logs = load_logs()
    if not any((datetime.datetime.utcnow() - datetime.datetime.strptime(l.get("date", ""), "%Y-%m-%d")).days < 7 for l in logs):
        send_telegram_message("🗓️ No trades this week.")
        return
    report_text, image_path = generate_report(logs, "weekly")
    send_telegram_message("📅 *Weekly Report*\n\n" + report_text, image_path)

def send_monthly_report() -> None:
    now = datetime.datetime.utcnow()
    logs = load_logs()
    month_logs = [l for l in logs if datetime.datetime.strptime(l.get("date",""), "%Y-%m-%d").month == now.month]
    if not month_logs:
        send_telegram_message("🗓️ No trades this month.")
        return
    report_text, image_path = generate_report(logs, "monthly")
    send_telegram_message("📅 *Monthly Report*\n\n" + report_text, image_path)

# -------------------------
# CLI / CSV / summary
# -------------------------
def export_to_csv(logs: List[Dict[str, Any]], path: str = DEFAULT_EXPORT_CSV) -> None:
    fieldnames = [
        "date","token","coin_name","amount_usd",
        "buy_time","buy_market_cap","buy_priority_fee",
        "sell_time","sell_market_cap","sell_priority_fee",
        "profit_usd"
    ]
    with open(path,"w",newline="",encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile,fieldnames=fieldnames)
        writer.writeheader()
        for e in logs:
            row = {k: e.get(k) for k in fieldnames}
            writer.writerow(row)
    print(f"[+] Exported {len(logs)} entries to {path}")

def summarize_logs(logs: List[Dict[str, Any]]) -> str:
    total = len(logs)
    closed = sum(1 for e in logs if e.get("sell_time"))
    open_positions = total - closed
    total_profit = sum(_safe_float(e.get("profit_usd")) for e in logs)
    avg_profit = (total_profit/closed) if closed else 0.0
    return (
        f"Total entries: {total}\n"
        f"Closed trades: {closed}\n"
        f"Open trades: {open_positions}\n"
        f"Total profit (USD): ${total_profit:.2f}\n"
        f"Average profit per closed trade: ${avg_profit:.2f}\n"
    )

def rebuild_index_if_needed(path: str = REPORTS_FILE) -> None:
    if not os.path.exists(path):
        ensure_logs_file(path)
        return
    try:
        with open(path,"r",encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data,list):
            return
        if isinstance(data,dict) and "logs" in data and isinstance(data["logs"],list):
            save_logs(data["logs"],path=path)
            print("[+] Rebuilt logs from 'logs' key.")
            return
        save_logs([data],path=path)
        print("[+] Wrapped single object into list.")
    except Exception as e:
        print("[!] Rebuild failed:", e)

def interactive_menu() -> None:
    print("Reports menu:\n1) Daily\n2) Weekly\n3) Monthly\n4) Summary\n5) Export CSV\n6) Rebuild logs\nq) Quit")
    choice = input("Choose: ").strip().lower()
    if choice=="1": send_daily_report()
    elif choice=="2": send_weekly_report()
    elif choice=="3": send_monthly_report()
    elif choice=="4": print(summarize_logs(load_logs()))
    elif choice=="5":
        path=input(f"CSV path [{DEFAULT_EXPORT_CSV}]: ").strip() or DEFAULT_EXPORT_CSV
        export_to_csv(load_logs(), path)
    elif choice=="6": rebuild_index_if_needed()
    elif choice=="q": return
    else: print("Unknown option.")

# -------------------------
# Main
# -------------------------
def main(argv: Optional[List[str]]=None) -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Sniper bot reports")
    parser.add_argument("--mode","-m",default=os.getenv("REPORT_MODE","daily"),
                        help="daily|weekly|monthly|export-csv|summary|interactive|rebuild-index")
    parser.add_argument("--export-path","-o",default=DEFAULT_EXPORT_CSV)
    args = parser.parse_args(argv or sys.argv[1:])
    mode=args.mode.lower()
    if mode=="daily": send_daily_report()
    elif mode=="weekly": send_weekly_report()
    elif mode=="monthly": send_monthly_report()
    elif mode=="export-csv": export_to_csv(load_logs(), path=args.export_path)
    elif mode=="summary": print(summarize_logs(load_logs()))
    elif mode=="interactive": interactive_menu()
    elif mode=="rebuild-index": rebuild_index_if_needed()
    else: print("Unknown mode. Use --help.")

if __name__=="__main__":
    ensure_logs_file()
    main()