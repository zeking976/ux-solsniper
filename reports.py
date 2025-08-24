#!/usr/bin/env python3
"""
reports.py

Improved reporting utilities for the sniper bot.

- Loads a JSON trade log (trade_logs.json by default).
- Sends daily/monthly reports via Telegram.
- Provides CLI options:
    - daily (default)
    - monthly
    - export-csv <path>
    - summary
    - rebuild-index (light maintenance)
    - interactive (prints a menu)
- Adds more robust parsing, defensive checks and helpful telemetry.
- Uses utils.resolve_token_name and utils.md_code for nicer output.
- Designed to run on Termux / VULTR environments (no extra deps beyond requests and python-dotenv).

Save this file as `reports.py` and make it executable if you like:
    chmod +x reports.py
"""

import os
import sys
import csv
import json
import time
import argparse
import datetime
from typing import List, Dict, Any, Optional, Tuple

import requests
from dotenv import load_dotenv

# local helper functions from utils
try:
    from utils import resolve_token_name, md_code
except Exception:
    # If utils is not importable, provide simple fallbacks so reports.py is still useful.
    def md_code(s: str) -> str:
        if not s:
            return "`N/A`"
        return f"`{s.replace('`', \"'\")}`"

    def resolve_token_name(_addr: str) -> Optional[str]:
        return None

# -------------------------
# Load environment
# -------------------------
load_dotenv(dotenv_path=os.path.expanduser("~/t.env"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Defaults & config locations
REPORTS_FILE = os.getenv("REPORTS_FILE", "trade_logs.json")
DEFAULT_EXPORT_CSV = os.getenv("REPORT_EXPORT_CSV", "trade_logs_export.csv")
STOP_LOSS = float(os.getenv("STOP_LOSS", "-20") or -20.0)
TAKE_PROFIT = float(os.getenv("TAKE_PROFIT", "100") or 100.0)
CYCLE_LIMIT_RAW = os.getenv("CYCLE_LIMIT", "1")  # can be "5" or "5,4" etc

# Parse simple cycle config for human readable summary
def parse_cycle_limit(raw: str) -> Tuple[Optional[int], Optional[int]]:
    """
    If CYCLE_LIMIT is "5,4" -> (5,4)
    If single integer -> (n, None)
    """
    if not raw:
        return (None, None)
    if "," in raw:
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        try:
            nums = [int(p) for p in parts if p.isdigit()]
            if len(nums) >= 2:
                return (nums[0], nums[1])
            if len(nums) == 1:
                return (nums[0], None)
        except Exception:
            pass
        return (None, None)
    try:
        return (int(raw), None)
    except Exception:
        return (None, None)

CYCLE_LIMIT_PARSED = parse_cycle_limit(CYCLE_LIMIT_RAW)
MAX_BUYS, MAX_SELLS = CYCLE_LIMIT_PARSED

# -------------------------
# Telegram helper
# -------------------------
def send_telegram_message(text: str) -> None:
    """Send a Markdown Telegram message (works on Termux & VULTR)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        # fall back to stdout for local debugging
        print("[Telegram disabled] Message would be:\n", text)
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": int(TELEGRAM_CHAT_ID), "text": text, "parse_mode": "Markdown"}
        resp = requests.post(url, json=payload, timeout=15)
        if not resp.ok:
            print("[!] Telegram API error:", resp.text)
    except Exception as e:
        print("[!] Failed to send message:", e)

# -------------------------
# Log file helpers
# -------------------------
def ensure_logs_file(path: str = REPORTS_FILE) -> None:
    if not os.path.exists(path):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump([], f)
        except Exception as e:
            print("[!] Failed to create log file:", e)

def load_logs(path: str = REPORTS_FILE) -> List[Dict[str, Any]]:
    ensure_logs_file(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
            # if the file contains an object with a "logs" key, support that too
            if isinstance(data, dict) and "logs" in data and isinstance(data["logs"], list):
                return data["logs"]
            return []
    except json.JSONDecodeError as e:
        print("[!] JSON decode error in logs file:", e)
        return []
    except Exception as e:
        print("[!] Failed to load logs:", e)
        return []

def save_logs(logs: List[Dict[str, Any]], path: str = REPORTS_FILE) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(logs, f, indent=2, default=str)
    except Exception as e:
        print("[!] Failed to save logs:", e)

# -------------------------
# Notification helpers
# -------------------------
def send_buy_notification(token: str, coin_name: str, amount_usd: float, buy_mcap: Optional[float], priority_fee_sol: Optional[float]) -> None:
    name_display = coin_name or "N/A"
    mc_text = f"${buy_mcap:,.0f}" if isinstance(buy_mcap, (int, float)) else "N/A"
    tip_text = f"{priority_fee_sol:.3f} SOL" if priority_fee_sol is not None else "N/A"
    msg = (
        f"✅ *BUY EXECUTED*\n"
        f"• Coin: *{name_display}*\n"
        f"• CA: {md_code(token)}\n"
        f"• Amount: ${amount_usd:.2f}\n"
        f"• Market Cap @ Buy: {mc_text}\n"
        f"• Tip Paid: {tip_text}\n"
        f"• SL: {STOP_LOSS:.1f}% | TP: {TAKE_PROFIT:.1f}%"
    )
    send_telegram_message(msg)

def send_sell_notification(token: str, coin_name: str, sell_mcap: Optional[float], profit_usd: Optional[float], priority_fee_sol: Optional[float]) -> None:
    name_display = coin_name or "N/A"
    mc_text = f"${sell_mcap:,.0f}" if isinstance(sell_mcap, (int, float)) else "N/A"
    profit_text = f"${float(profit_usd):.2f}" if profit_usd is not None else "N/A"
    tip_text = f"{priority_fee_sol:.3f} SOL" if priority_fee_sol is not None else "N/A"
    msg = (
        f"🟣 *SELL EXECUTED*\n"
        f"• Coin: *{name_display}*\n"
        f"• CA: {md_code(token)}\n"
        f"• Market Cap @ Sell: {mc_text}\n"
        f"• Profit: {profit_text}\n"
        f"• Tip Paid: {tip_text}\n"
        f"• SL: {STOP_LOSS:.1f}% | TP: {TAKE_PROFIT:.1f}%"
    )
    send_telegram_message(msg)

# -------------------------
# Recording functions
# -------------------------
def record_buy(token: str,
               coin_name: Optional[str],
               buy_market_cap: Optional[float],
               buy_time: Optional[str],
               amount_usd: float,
               priority_fee_sol: Optional[float]) -> None:
    """
    Append a buy event into the JSON log and notify via Telegram.
    """
    if not coin_name:
        resolved = resolve_token_name(token)
        if resolved:
            coin_name = resolved
    entry = {
        "token": token,
        "coin_name": coin_name or "N/A",
        "buy_market_cap": buy_market_cap,
        "buy_time": buy_time or datetime.datetime.utcnow().isoformat(),
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
    send_buy_notification(token, entry["coin_name"], amount_usd, buy_market_cap, priority_fee_sol)

def record_sell(token: str,
                sell_market_cap: Optional[float],
                sell_time: Optional[str],
                profit_usd: Optional[float],
                priority_fee_sol: Optional[float]) -> None:
    """
    Find the latest buy without a sell for the given token and annotate it as sold.
    """
    logs = load_logs()
    coin_name_for_msg = "N/A"
    found = False
    for entry in reversed(logs):
        if entry.get("token") == token and entry.get("sell_time") is None:
            entry["sell_market_cap"] = sell_market_cap
            entry["sell_time"] = sell_time or datetime.datetime.utcnow().isoformat()
            entry["profit_usd"] = profit_usd
            entry["sell_priority_fee"] = priority_fee_sol
            coin_name_for_msg = entry.get("coin_name") or "N/A"
            found = True
            break
    if found:
        save_logs(logs)
    else:
        # If not found, create a minimal entry (helps with reconciliation)
        entry = {
            "token": token,
            "coin_name": coin_name_for_msg,
            "buy_market_cap": None,
            "buy_time": None,
            "amount_usd": None,
            "buy_priority_fee": None,
            "sell_market_cap": sell_market_cap,
            "sell_time": sell_time or datetime.datetime.utcnow().isoformat(),
            "profit_usd": profit_usd,
            "sell_priority_fee": priority_fee_sol,
            "date": str(datetime.datetime.utcnow().date())
        }
        logs.append(entry)
        save_logs(logs)

    # Send notification (resolve name if needed)
    if coin_name_for_msg == "N/A":
        resolved = resolve_token_name(token)
        if resolved:
            coin_name_for_msg = resolved
    send_sell_notification(token, coin_name_for_msg, sell_market_cap, profit_usd, priority_fee_sol)

# -------------------------
# Reporting & utilities
# -------------------------
def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default

def generate_report(logs: List[Dict[str, Any]]) -> str:
    total_profit = 0.0
    total_tips = 0.0
    normal_tips = 0.0
    congestion_tips = 0.0
    lines = []

    for e in logs:
        profit = _safe_float(e.get("profit_usd") or 0)
        total_profit += profit

        buy_fee = _safe_float(e.get("buy_priority_fee") or 0)
        sell_fee = _safe_float(e.get("sell_priority_fee") or 0)
        total_tips += buy_fee + sell_fee

        # Heuristic: fees >= 0.2 SOL considered "congestion" for report split
        for fee in [buy_fee, sell_fee]:
            if fee >= 0.2:
                congestion_tips += fee
            elif fee > 0:
                normal_tips += fee

        coin_name = e.get("coin_name") or "N/A"
        buy_time = e.get("buy_time") or "N/A"
        sell_time = e.get("sell_time") or "N/A"

        tips_sum = (buy_fee + sell_fee)
        tips_sum_formatted = f"{tips_sum:.3f} SOL"

        lines.append(
            f"🔹 *{coin_name}* | CA: {md_code(e.get('token', ''))} | "
            f"Buy: {buy_time} | Sell: {sell_time} | "
            f"Profit: ${profit:.2f} | Tips: {tips_sum_formatted}"
        )

    # Summary header
    header = [
        f"*Configured SL/TP:* {STOP_LOSS:.1f}% / {TAKE_PROFIT:.1f}%",
    ]
    if MAX_BUYS and MAX_SELLS:
        header.append(f"*Cycle Limit:* {MAX_BUYS} buys / {MAX_SELLS} sells per cycle")
    elif MAX_BUYS:
        header.append(f"*Cycle Limit:* {MAX_BUYS} trades per cycle")
    header.append(f"*Total profit:* ${round(total_profit, 2):.2f}")
    header.append(f"*Total tips paid:* {total_tips:.3f} SOL (Normal: {normal_tips:.3f} | Congestion: {congestion_tips:.3f})")

    return "\n".join(header) + "\n\n" + "\n".join(lines)

# -------------------------
# Exports & utilities
# -------------------------
def export_to_csv(logs: List[Dict[str, Any]], path: str = DEFAULT_EXPORT_CSV) -> None:
    """
    Export logs to CSV. Best-effort: flattens entries into readable columns.
    """
    if not logs:
        print("[!] No logs to export.")
        return
    fieldnames = [
        "date", "token", "coin_name", "amount_usd",
        "buy_time", "buy_market_cap", "buy_priority_fee",
        "sell_time", "sell_market_cap", "sell_priority_fee",
        "profit_usd"
    ]
    try:
        with open(path, "w", newline="", encoding="utf-8") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            for e in logs:
                row = {k: e.get(k) for k in fieldnames}
                writer.writerow(row)
        print(f"[+] Exported {len(logs)} entries to {path}")
    except Exception as e:
        print("[!] Failed to export CSV:", e)

def summarize_logs(logs: List[Dict[str, Any]]) -> str:
    """
    Small textual summary to print to stdout: counts, totals, avg profit.
    """
    total = len(logs)
    closed = sum(1 for e in logs if e.get("sell_time"))
    open_positions = total - closed
    total_profit = sum(_safe_float(e.get("profit_usd") or 0) for e in logs)
    avg_profit = (total_profit / closed) if closed else 0.0
    return (
        f"Total entries: {total}\n"
        f"Closed trades: {closed}\n"
        f"Open trades: {open_positions}\n"
        f"Total profit (USD): ${total_profit:.2f}\n"
        f"Average profit per closed trade: ${avg_profit:.2f}\n"
    )

# -------------------------
# Reporting functions: daily/monthly
# -------------------------
def send_daily_report() -> None:
    today = str(datetime.datetime.utcnow().date())
    logs = load_logs()
    today_logs = [l for l in logs if l.get("date") == today]
    if not today_logs:
        send_telegram_message("🗓️ No trades today.")
        return
    body = generate_report(today_logs)
    send_telegram_message("📅 *Daily Report*\n\n" + body)

def send_monthly_report() -> None:
    now = datetime.datetime.utcnow()
    logs = load_logs()
    month_logs = []
    for l in logs:
        try:
            d = datetime.datetime.strptime(l.get("date", ""), "%Y-%m-%d")
            if d.year == now.year and d.month == now.month:
                month_logs.append(l)
        except Exception:
            continue
    if not month_logs:
        send_telegram_message("🗓️ No trades this month.")
        return
    body = generate_report(month_logs)
    send_telegram_message("📅 *Monthly Report*\n\n" + body)

# -------------------------
# CLI / Interactive
# -------------------------
def interactive_menu() -> None:
    """
    Simple interactive menu for quick report actions.
    """
    print("Reports interactive menu")
    print("========================")
    print("1) Send today's report (Telegram)")
    print("2) Send this month's report (Telegram)")
    print("3) Print summary to stdout")
    print("4) Export all logs to CSV")
    print("5) Show last 10 logs")
    print("6) Rebuild empty log file if missing")
    print("q) Quit")
    choice = input("Choose: ").strip().lower()
    if choice == "1":
        send_daily_report()
    elif choice == "2":
        send_monthly_report()
    elif choice == "3":
        logs = load_logs()
        print(summarize_logs(logs))
    elif choice == "4":
        logs = load_logs()
        path = input(f"Export path [{DEFAULT_EXPORT_CSV}]: ").strip() or DEFAULT_EXPORT_CSV
        export_to_csv(logs, path=path)
    elif choice == "5":
        logs = load_logs()
        for e in logs[-10:]:
            print(json.dumps(e, indent=2, default=str))
    elif choice == "6":
        ensure_logs_file()
        print("Ensured log file exists:", REPORTS_FILE)
    elif choice == "q":
        print("Bye.")
        return
    else:
        print("Unknown option.")
    print("\nReturning to shell (exit).")

# -------------------------
# Maintenance helpers
# -------------------------
def rebuild_index_if_needed(path: str = REPORTS_FILE) -> None:
    """
    Basic maintenance: ensure file exists and JSON is a list. If not, attempt to coerce to list.
    """
    if not os.path.exists(path):
        ensure_logs_file(path)
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return
        # If it's a dict with "logs" key, normalize to list
        if isinstance(data, dict) and "logs" in data and isinstance(data["logs"], list):
            save_logs(data["logs"], path=path)
            print("[+] Rebuilt logs file from 'logs' key.")
            return
        # If it's an object, attempt to wrap in a list
        save_logs([data], path=path)
        print("[+] Coerced top-level object into a list (saved).")
    except Exception as e:
        print("[!] Rebuild failed:", e)

# -------------------------
# Main entry point
# -------------------------
def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Sniper bot reports utility")
    parser.add_argument("--mode", "-m", default=os.getenv("REPORT_MODE", "daily"),
                        help="Mode: daily | monthly | export-csv | summary | interactive | rebuild-index")
    parser.add_argument("--export-path", "-o", default=DEFAULT_EXPORT_CSV, help="CSV export path")
    args = parser.parse_args(argv or sys.argv[1:])

    mode = args.mode.lower()

    if mode == "daily":
        send_daily_report()
    elif mode == "monthly":
        send_monthly_report()
    elif mode == "export-csv":
        logs = load_logs()
        export_to_csv(logs, path=args.export_path)
    elif mode == "summary":
        logs = load_logs()
        print(summarize_logs(logs))
    elif mode == "interactive":
        interactive_menu()
    elif mode == "rebuild-index":
        rebuild_index_if_needed()
        print("Rebuild/check completed.")
    else:
        print("Unknown mode. Use --help for options.")

# -------------------------
# If invoked as script
# -------------------------
if __name__ == "__main__":
    # Basic safety: ensure log file exists before doing anything
    ensure_logs_file(REPORTS_FILE)
    main()