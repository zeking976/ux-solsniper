#!/usr/bin/env python3
"""
reports.py

Aligned with sniper.py and utils.py.

Handles:
- Trade logs (JSON)
- Daily/monthly reports via Telegram
- Notifications for buys/sells with priority fees
- DRY_RUN compatibility
"""

import os
import sys
import csv
import json
import datetime
from typing import List, Dict, Any, Optional

import requests
from dotenv import load_dotenv

# -------------------------
# Load environment
# -------------------------
load_dotenv(dotenv_path=os.path.expanduser("~/t.env"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DRY_RUN = os.getenv("DRY_RUN", "False").lower() in ("1", "true", "yes")

REPORTS_FILE = os.getenv("REPORTS_FILE", "trade_logs.json")
DEFAULT_EXPORT_CSV = os.getenv("REPORT_EXPORT_CSV", "trade_logs_export.csv")
STOP_LOSS = float(os.getenv("STOP_LOSS", "-20") or -20.0)
TAKE_PROFIT = float(os.getenv("TAKE_PROFIT", "100") or 100.0)

# -------------------------
# utils.py hooks
# -------------------------
try:
    from utils import resolve_token_name, md_code
except ImportError:
    def md_code(s: str) -> str:
        if not s:
            return "`N/A`"
        return f"`{s.replace('`','\'')}`"

    def resolve_token_name(_addr: str) -> Optional[str]:
        return None

# -------------------------
# Telegram helpers
# -------------------------
def send_telegram_message(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or DRY_RUN:
        print("[Telegram disabled / DRY_RUN] Message would be:\n", text)
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": int(TELEGRAM_CHAT_ID), "text": text, "parse_mode": "Markdown"}
        resp = requests.post(url, json=payload, timeout=15)
        if not resp.ok:
            print("[!] Telegram API error:", resp.text)
    except Exception as e:
        print("[!] Failed to send Telegram message:", e)

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
# Reporting
# -------------------------
def _safe_float(v: Any) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0

def generate_report(logs: List[Dict[str, Any]]) -> str:
    total_profit = 0.0
    total_tips = 0.0
    lines = []
    for e in logs:
        profit = _safe_float(e.get("profit_usd"))
        total_profit += profit
        buy_fee = _safe_float(e.get("buy_priority_fee"))
        sell_fee = _safe_float(e.get("sell_priority_fee"))
        total_tips += buy_fee + sell_fee
        coin_name = e.get("coin_name") or "N/A"
        buy_time = e.get("buy_time") or "N/A"
        sell_time = e.get("sell_time") or "N/A"
        tips_sum_formatted = f"{buy_fee+sell_fee:.3f} SOL"
        lines.append(
            f"🔹 *{coin_name}* | CA: {md_code(e.get('token',''))} | "
            f"Buy: {buy_time} | Sell: {sell_time} | "
            f"Profit: ${profit:.2f} | Tips: {tips_sum_formatted}"
        )
    header = [
        f"*Configured SL/TP:* {STOP_LOSS:.1f}% / {TAKE_PROFIT:.1f}%",
        f"*Total profit:* ${round(total_profit,2):.2f}",
        f"*Total tips paid:* {total_tips:.3f} SOL"
    ]
    return "\n".join(header) + "\n\n" + "\n".join(lines)

def send_daily_report() -> None:
    today = str(datetime.datetime.utcnow().date())
    logs = load_logs()
    today_logs = [l for l in logs if l.get("date") == today]
    if not today_logs:
        send_telegram_message("🗓️ No trades today.")
        return
    send_telegram_message("📅 *Daily Report*\n\n" + generate_report(today_logs))

def send_monthly_report() -> None:
    now = datetime.datetime.utcnow()
    logs = load_logs()
    month_logs = []
    for l in logs:
        try:
            d = datetime.datetime.strptime(l.get("date",""), "%Y-%m-%d")
            if d.year == now.year and d.month == now.month:
                month_logs.append(l)
        except Exception:
            continue
    if not month_logs:
        send_telegram_message("🗓️ No trades this month.")
        return
    send_telegram_message("📅 *Monthly Report*\n\n" + generate_report(month_logs))

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
    print("Reports menu:\n1) Daily\n2) Monthly\n3) Summary\n4) Export CSV\n5) Rebuild logs\nq) Quit")
    choice = input("Choose: ").strip().lower()
    if choice=="1": send_daily_report()
    elif choice=="2": send_monthly_report()
    elif choice=="3": print(summarize_logs(load_logs()))
    elif choice=="4":
        path=input(f"CSV path [{DEFAULT_EXPORT_CSV}]: ").strip() or DEFAULT_EXPORT_CSV
        export_to_csv(load_logs(), path)
    elif choice=="5": rebuild_index_if_needed()
    elif choice=="q": return
    else: print("Unknown option.")

# -------------------------
# Main
# -------------------------
def main(argv: Optional[List[str]]=None) -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Sniper bot reports")
    parser.add_argument("--mode","-m",default=os.getenv("REPORT_MODE","daily"),
                        help="daily|monthly|export-csv|summary|interactive|rebuild-index")
    parser.add_argument("--export-path","-o",default=DEFAULT_EXPORT_CSV)
    args = parser.parse_args(argv or sys.argv[1:])
    mode=args.mode.lower()
    if mode=="daily": send_daily_report()
    elif mode=="monthly": send_monthly_report()
    elif mode=="export-csv": export_to_csv(load_logs(), path=args.export_path)
    elif mode=="summary": print(summarize_logs(load_logs()))
    elif mode=="interactive": interactive_menu()
    elif mode=="rebuild-index": rebuild_index_if_needed()
    else: print("Unknown mode. Use --help.")

if __name__=="__main__":
    ensure_logs_file()
    main()