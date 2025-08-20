import os
import time
import traceback
import threading
from datetime import datetime, timedelta

from dotenv import load_dotenv
from telethon import TelegramClient, events, errors

from utils import (
    get_env_variable,
    get_market_cap_from_dexscreener,
    get_sol_amount_for_usd,
    send_telegram_message,
    is_ca_processed,
    save_processed_ca,
    jupiter_buy,
    jupiter_sell,
    calculate_total_gas_fee,
    save_gas_reserve_after_trade,
    # get_current_daily_limit  # note: implemented below if missing
)

# Load environment
load_dotenv(dotenv_path="t.env")

# Required envs (will raise if missing)
API_ID = int(get_env_variable("TELEGRAM_API_ID"))
API_HASH = get_env_variable("TELEGRAM_API_HASH")
TARGET_CHANNEL_ID = int(get_env_variable("TARGET_CHANNEL_ID"))
INVESTMENT_USD = float(get_env_variable("INVESTMENT_USD"))
REQUIRED_MULTIPLIER = float(get_env_variable("REQUIRED_MULTIPLIER"))
CYCLE_LIMIT = int(get_env_variable("CYCLE_LIMIT"))
# DRY_RUN stored as string "0" or "1" in t.env — use get_env_variable so missing var is noticed
DRY_RUN = get_env_variable("DRY_RUN", required=False, default="0") == "1"

# Telethon client (non-interactive) - uses session file 'telethon.session'
client = TelegramClient("telethon", API_ID, API_HASH, device_model="ux-solsniper")

# Helper: parse daily limits env (fallback to 5,4)
def get_current_daily_limit():
    try:
        daily_limits = os.getenv("DAILY_LIMITS", "5,4").split(",")
        daily_limits = [int(x.strip()) for x in daily_limits if x.strip()]
        if not daily_limits:
            daily_limits = [5, 4]
    except Exception:
        daily_limits = [5, 4]
    if len(daily_limits) == 1:
        return daily_limits[0]
    day_index = datetime.utcnow().toordinal() % len(daily_limits)
    return int(daily_limits[day_index])

# Run state
cycle_count = 0
capital_usd = INVESTMENT_USD

# Event handler collects new contract addresses as they show
found_contracts = []

# Safe sender wrapper so Telegram errors do not crash the bot
def safe_send_telegram(text: str):
    try:
        send_telegram_message(text)
    except Exception as e:
        # fallback to printing; don't let messaging failures crash the bot
        print(f"[!] safe_send_telegram failed: {e} | msg: {text}")

def read_from_target_channel(limit: int = 5):
    """
    Local helper to fetch latest messages from TARGET_CHANNEL_ID.
    Returns a list of message texts (most recent first).
    Works in Termux/VPS because we call Telethon via the running client loop.
    """
    try:
        msgs = client.loop.run_until_complete(client.get_messages(TARGET_CHANNEL_ID, limit=limit))
        out = []
        for m in msgs:
            try:
                out.append(m.message or "")
            except Exception:
                out.append("")
        return out
    except Exception as e:
        safe_send_telegram(f"[!] read_from_target_channel error: {e}")
        return []

@client.on(events.NewMessage(chats=TARGET_CHANNEL_ID))
async def new_message_handler(event):
    try:
        text = event.message.message or ""
        if "CA:" in text:
            ca = text.split("CA:")[1].strip().split()[0]
        else:
            parts = text.strip().split()
            ca = None
            for p in parts:
                if 32 <= len(p) <= 44:
                    ca = p.strip()
                    break
        if ca:
            from utils import is_valid_solana_address
            if is_valid_solana_address(ca) and not is_ca_processed(ca):
                found_contracts.append(ca)
                safe_send_telegram(f"[+] Detected new CA: `{ca}`")
    except Exception as e:
        safe_send_telegram(f"[!] Error parsing new message: {e}")

def pick_next_contract(limit_checks=5):
    while found_contracts:
        candidate = found_contracts.pop(0)
        if not is_ca_processed(candidate):
            return candidate
    from utils import is_valid_solana_address
    msgs = read_from_target_channel(limit=limit_checks)
    for m in msgs:
        if not m:
            continue
        if "CA:" in m:
            ca_cand = m.split("CA:")[1].strip().split()[0]
        else:
            parts = m.split()
            ca_cand = None
            for p in parts:
                if 32 <= len(p) <= 44:
                    ca_cand = p
                    break
        if ca_cand and is_valid_solana_address(ca_cand) and not is_ca_processed(ca_cand):
            return ca_cand
    return None

def main_loop():
    global cycle_count, capital_usd
    while cycle_count < CYCLE_LIMIT:
        daily_limit = get_current_daily_limit()
        current_invests = 0
        safe_send_telegram(f"[*] Starting cycle {cycle_count+1}/{CYCLE_LIMIT} | Daily limit: {daily_limit}")
        while current_invests < daily_limit:
            try:
                ca = None
                pick_waits = 0
                while not ca:
                    ca = pick_next_contract()
                    if not ca:
                        pick_waits += 1
                        if pick_waits > 30:
                            safe_send_telegram("[*] No CA found after many attempts — sleeping 2 minutes")
                            time.sleep(120)
                            pick_waits = 0
                        else:
                            time.sleep(10)
                save_processed_ca(ca)

                mcap = None
                for _ in range(3):
                    mcap = get_market_cap_from_dexscreener(ca)
                    if mcap:
                        break
                    time.sleep(5)
                if not mcap:
                    safe_send_telegram(f"[!] Could not get market cap for {ca}, skipping")
                    continue

                amount_sol = get_sol_amount_for_usd(capital_usd)
                if amount_sol <= 0:
                    safe_send_telegram("[!] Invalid SOL amount, skipping")
                    continue

                if DRY_RUN:
                    safe_send_telegram(f"[DRY RUN] Would buy {ca} for {amount_sol} SOL (${capital_usd})")
                    buy_sig = None
                else:
                    buy_sig = jupiter_buy(ca, amount_sol)
                if not DRY_RUN and not buy_sig:
                    safe_send_telegram(f"[!] Buy failed for {ca}; skipping sell monitor")
                    continue

                safe_send_telegram(f"[BUY] CA `{ca}` invested ${capital_usd:.2f} ({amount_sol} SOL) | tx: {buy_sig or 'DRY'} | MC at buy: ${mcap:,.0f}")

                target_mc = mcap * REQUIRED_MULTIPLIER
                stop_loss_mc = mcap * 0.8
                sold = False
                while not sold:
                    try:
                        cur_mc = get_market_cap_from_dexscreener(ca)
                        if cur_mc is None:
                            time.sleep(60)
                            continue
                        if cur_mc >= target_mc:
                            if DRY_RUN:
                                safe_send_telegram(f"[DRY RUN] Would sell {ca} now at MC ${cur_mc:,.0f} (take profit)")
                                sell_sig = None
                                sold = True
                            else:
                                sell_sig = jupiter_sell(ca)
                                if sell_sig:
                                    safe_send_telegram(f"[SELL] CA `{ca}` sold at MC ${cur_mc:,.0f} | tx: {sell_sig} (take profit)")
                                    sold = True
                                else:
                                    safe_send_telegram(f"[!] Sell failed for {ca}; retrying in 2 minutes")
                                    time.sleep(120)
                        elif cur_mc <= stop_loss_mc:
                            if DRY_RUN:
                                safe_send_telegram(f"[DRY RUN] Would sell {ca} now at MC ${cur_mc:,.0f} (stop loss)")
                                sell_sig = None
                                sold = True
                            else:
                                sell_sig = jupiter_sell(ca)
                                if sell_sig:
                                    safe_send_telegram(f"[STOP-LOSS] CA `{ca}` sold at MC ${cur_mc:,.0f} | tx: {sell_sig}")
                                    sold = True
                                else:
                                    safe_send_telegram(f"[!] Stop-loss sell failed for {ca}; retrying in 2 minutes")
                                    time.sleep(120)
                        else:
                            time.sleep(60)
                    except Exception as e:
                        safe_send_telegram(f"[!] Monitoring loop exception: {e}")
                        time.sleep(60)

                gross_return = capital_usd * REQUIRED_MULTIPLIER
                fee_est = calculate_total_gas_fee(gross_return) or 0
                capital_usd = round(gross_return - fee_est, 6)
                capital_usd = save_gas_reserve_after_trade(capital_usd, reserve_pct=0.0009)
                safe_send_telegram(f"[INFO] Updated capital after fees & reserve: ${capital_usd:.6f} (fees est ${fee_est:.6f})")

                current_invests += 1
            except Exception as e:
                safe_send_telegram(f"[!] Main loop exception: {e}\n{traceback.format_exc()}")
                time.sleep(10)

        cycle_count += 1
        now = datetime.utcnow()
        next_midnight = datetime(now.year, now.month, now.day) + timedelta(days=1)
        sleep_seconds = (next_midnight - now).total_seconds()
        safe_send_telegram(f"[*] Day finished. Sleeping {int(sleep_seconds)} seconds until next UTC midnight.")
        time.sleep(sleep_seconds)

def _telethon_runner():
    try:
        with client:
            client.run_until_disconnected()
    except Exception as e:
        safe_send_telegram(f"[!] Telethon runner crashed: {e}")

def start_bot():
    while True:
        try:
            client.connect()
            if not client.is_user_authorized():
                client.start()
            t = threading.Thread(target=_telethon_runner, daemon=True)
            t.start()
            main_loop()
            time.sleep(3)
        except (errors.RPCError, ConnectionResetError, Exception) as e:
            safe_send_telegram(f"[!] Bot disconnected / crashed: {e}. Restarting in 15s.")
            try:
                client.disconnect()
            except Exception:
                pass
            time.sleep(15)
            continue

if __name__ == "__main__":
    start_bot()