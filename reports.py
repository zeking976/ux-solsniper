import os
import json
import datetime
import requests
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv

# Load .env (Termux/VULTR friendly)
load_dotenv(dotenv_path="t.env")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DEXSCREENER_API_KEY = os.getenv("DEXSCREENER_API_KEY", "")

# Keep the log file in repo root; .gitignore can exclude it if you prefer
REPORTS_FILE = "trade_logs.json"

# -------------------------
# Telegram
# -------------------------
def send_telegram_message(text: str) -> None:
    """Send a Markdown Telegram message (works on Termux & VULTR)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[!] Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": int(TELEGRAM_CHAT_ID), "text": text, "parse_mode": "Markdown"}
        resp = requests.post(url, json=payload, timeout=15)
        if not resp.ok:
            print("[!] Telegram API error:", resp.text)
    except Exception as e:
        print("[!] Failed to send message:", e)

def md_code(s: str) -> str:
    """Wrap a string in backticks for Markdown code, with minimal escaping."""
    if s is None:
        s = ""
    return f"`{str(s).replace('`', '\\`')}`"

# -------------------------
# Dexscreener helper (optional)
# -------------------------
def resolve_token_name(contract_address: str) -> Optional[str]:
    """
    Resolve a token name from Dexscreener for better reports if coin_name wasn't provided.
    Returns None if it cannot be resolved.
    """
    try:
        base = f"https://api.dexscreener.io/latest/dex/pairs/solana/{contract_address}"
        headers = {}
        if DEXSCREENER_API_KEY:
            headers["Authorization"] = f"Bearer {DEXSCREENER_API_KEY}"
        r = requests.get(base, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()
        # Prefer first pair entry if available
        if isinstance(data, dict):
            pairs = data.get("pairs") or []
            if pairs and isinstance(pairs, list):
                p0 = pairs[0]
                # Try baseToken.name or baseToken.symbol
                base_token = p0.get("baseToken") or {}
                name = base_token.get("name") or base_token.get("symbol")
                if name:
                    return str(name)
            # Some responses use "pair"
            pair = data.get("pair") or {}
            base_token = pair.get("baseToken") or {}
            name = base_token.get("name") or base_token.get("symbol")
            if name:
                return str(name)
    except Exception:
        pass
    return None

# -------------------------
# JSON log helpers
# -------------------------
def _init_logs_file_if_missing() -> None:
    if not os.path.exists(REPORTS_FILE):
        try:
            with open(REPORTS_FILE, "w") as f:
                json.dump([], f)
        except Exception as e:
            print("[!] Failed to initialize log file:", e)

def load_logs() -> List[Dict[str, Any]]:
    _init_logs_file_if_missing()
    try:
        with open(REPORTS_FILE, "r") as f:
            return json.load(f) or []
    except Exception:
        return []

def save_logs(logs: List[Dict[str, Any]]) -> None:
    try:
        with open(REPORTS_FILE, "w") as f:
            json.dump(logs, f, indent=2)
    except Exception as e:
        print("[!] Failed to save logs:", e)

# -------------------------
# Recording + Notifications
# -------------------------
def send_buy_notification(token: str, coin_name: str, amount_usd: float, buy_mcap: Optional[float]) -> None:
    name_display = coin_name or "N/A"
    mc_text = f"${buy_mcap:,.0f}" if isinstance(buy_mcap, (int, float)) else "N/A"
    msg = (
        f"✅ *BUY EXECUTED*\n"
        f"• Coin: *{name_display}*\n"
        f"• CA: {md_code(token)}\n"
        f"• Amount: ${amount_usd:.2f}\n"
        f"• Market Cap @ Buy: {mc_text}"
    )
    send_telegram_message(msg)

def send_sell_notification(token: str, coin_name: str, sell_mcap: Optional[float], profit_usd: Optional[float]) -> None:
    name_display = coin_name or "N/A"
    mc_text = f"${sell_mcap:,.0f}" if isinstance(sell_mcap, (int, float)) else "N/A"
    profit_text = f"${float(profit_usd):.2f}" if profit_usd is not None else "N/A"
    msg = (
        f"🟣 *SELL EXECUTED*\n"
        f"• Coin: *{name_display}*\n"
        f"• CA: {md_code(token)}\n"
        f"• Market Cap @ Sell: {mc_text}\n"
        f"• Profit: {profit_text}"
    )
    send_telegram_message(msg)

def record_buy(token: str,
               coin_name: Optional[str],
               buy_market_cap: Optional[float],
               buy_time: str,
               amount_usd: float,
               priority_fee_sol: Optional[float]) -> None:
    """
    Record a buy and immediately notify Telegram with coin name + CA.
    If coin_name is missing, try to resolve it via Dexscreener.
    """
    if not coin_name:
        resolved = resolve_token_name(token)
        if resolved:
            coin_name = resolved
    entry = {
        "token": token,
        "coin_name": coin_name or "N/A",
        "buy_market_cap": buy_market_cap,
        "buy_time": buy_time,
        "amount_usd": amount_usd,
        "priority_fee": priority_fee_sol,
        "sell_market_cap": None,
        "sell_time": None,
        "profit_usd": None,
        "date": str(datetime.datetime.utcnow().date())
    }
    logs = load_logs()
    logs.append(entry)
    save_logs(logs)
    # Notify with CA + coin name (explicit requirement)
    send_buy_notification(token, entry["coin_name"], amount_usd, buy_market_cap)

def record_sell(token: str,
                sell_market_cap: Optional[float],
                sell_time: str,
                profit_usd: Optional[float]) -> None:
    """
    Record a sell and immediately notify Telegram with coin name + CA.
    Will attach to the most recent unsold entry for the given token.
    """
    logs = load_logs()
    coin_name_for_msg = "N/A"
    for entry in reversed(logs):
        if entry.get("token") == token and entry.get("sell_time") is None:
            entry["sell_market_cap"] = sell_market_cap
            entry["sell_time"] = sell_time
            entry["profit_usd"] = profit_usd
            coin_name_for_msg = entry.get("coin_name") or "N/A"
            break
    save_logs(logs)
    # If we still don't have a coin name, try resolving now
    if not coin_name_for_msg or coin_name_for_msg == "N/A":
        resolved = resolve_token_name(token)
        if resolved:
            coin_name_for_msg = resolved
    send_sell_notification(token, coin_name_for_msg, sell_market_cap, profit_usd)

# -------------------------
# Reporting
# -------------------------
def generate_report(logs: List[Dict[str, Any]]) -> str:
    total_profit = 0.0
    lines = []
    for e in logs:
        profit = float(e.get("profit_usd") or 0)
        total_profit += profit
        coin_name = e.get("coin_name") or "N/A"
        lines.append(
            f"🔹 *{coin_name}* | CA: {md_code(e.get('token', ''))} | "
            f"Buy: {e.get('buy_time') or 'N/A'} | "
            f"Sell: {e.get('sell_time') or 'N/A'} | "
            f"Profit: ${profit:.2f}"
        )
    lines.append(f"\n*Total profit:* ${round(total_profit, 2):.2f}")
    return "\n".join(lines)

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
            d = datetime.datetime.strptime(l.get("date", ""), "%Y-%m-%d")
            if d.year == now.year and d.month == now.month:
                month_logs.append(l)
        except Exception:
            continue
    if not month_logs:
        send_telegram_message("🗓️ No trades this month.")
        return
    send_telegram_message("📅 *Monthly Report*\n\n" + generate_report(month_logs))

# -------------------------
# CLI
# -------------------------
if __name__ == "__main__":
    mode = os.getenv("REPORT_MODE", "daily").lower()
    if mode == "daily":
        send_daily_report()
    else:
        send_monthly_report()