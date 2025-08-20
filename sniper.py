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
# Store tuples: (mint, posted_ts_epoch)
found_contracts = []

# Track whether the bot currently holds a position
holding_position = False
# Whether the bot has performed the first buy in this process run
first_buy_done = False
# Timestamp (epoch seconds) of the last successful sell (TP or SL). Initialized to 0 -> allows first buy.
last_sell_time = 0.0

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
    Returns a list of (message_text, message_date) tuples (most recent first).
    Works in Termux/VPS because we call Telethon via the running client loop.
    """
    try:
        msgs = client.loop.run_until_complete(client.get_messages(TARGET_CHANNEL_ID, limit=limit))
        out = []
        for m in msgs:
            try:
                txt = m.message or ""
                # Telethon returns a datetime in m.date
                dt = getattr(m, "date", None)
                # Convert to utc epoch seconds if datetime provided
                ts = None
                if dt is not None:
                    try:
                        ts = dt.timestamp()
                    except Exception:
                        ts = time.time()
                else:
                    ts = time.time()
                out.append((txt, ts))
            except Exception:
                out.append(("", time.time()))
        return out
    except Exception as e:
        safe_send_telegram(f"[!] read_from_target_channel error: {e}")
        return []

def extract_mint_from_start_link(text: str):
    """
    The channel posts contain a '⚡Buy Now' which leads to a link like:
    https://t.me/Soul_Sniper_Bot?start=15_3tz3HtW9LogbBBV...
    We must extract the portion after the last underscore as the mint (32-44 chars).
    Returns the mint string or None.
    """
    try:
        # find '?start=' occurrences
        if "?start=" in text:
            parts = text.split("?start=")
            for p in parts[1:]:
                # p may be like "15_3tz3H... moretext" or "15_3tz3H..."
                # take up to first whitespace / newline
                token = p.split()[0].strip()
                if "_" in token:
                    candidate = token.split("_")[-1]
                else:
                    # fallback: take token if length matches mint
                    candidate = token
                # cleanup punctuation
                candidate = candidate.strip().rstrip(".,;:)")
                if 32 <= len(candidate) <= 44:
                    return candidate
        # fallback: check raw message for a 32-44 char base58-like token
        parts = text.strip().split()
        for p in parts:
            p2 = p.strip().rstrip(".,;:)")
            if 32 <= len(p2) <= 44:
                return p2
    except Exception as e:
        safe_send_telegram(f"[!] extract_mint_from_start_link error: {e}")
    return None

@client.on(events.NewMessage(chats=TARGET_CHANNEL_ID))
async def new_message_handler(event):
    try:
        text = event.message.message or ""
        # try to extract mint via the start link pattern
        ca = extract_mint_from_start_link(text)
        # Also capture the posted time (epoch)
        post_ts = None
        try:
            post_dt = getattr(event.message, "date", None)
            post_ts = post_dt.timestamp() if post_dt else time.time()
        except Exception:
            post_ts = time.time()

        if ca:
            from utils import is_valid_solana_address
            if is_valid_solana_address(ca) and not is_ca_processed(ca):
                # Append so pick_next_contract can evaluate it
                found_contracts.append((ca, post_ts))
                safe_send_telegram(f"[+] Detected new CA: `{ca}` posted at {datetime.utcfromtimestamp(post_ts).isoformat()}Z")
    except Exception as e:
        safe_send_telegram(f"[!] Error parsing new message: {e}")

def pick_next_contract(limit_checks=5):
    """
    Picks the next contract to attempt.  Behavior rules:
    - Skip contracts already processed (is_ca_processed)
    - Skip contracts that were posted while bot was holding (posted_ts <= last_sell_time)
    - If bot is currently holding (holding_position==True) and the first buy already happened,
      do NOT pick any contract (return None)
    - Returns a mint string or None
    """
    global found_contracts, holding_position, last_sell_time, first_buy_done

    # If holding and we've already done the first buy, do not pick new contracts
    if holding_position and first_buy_done:
        return None

    # First check the in-memory queue
    while found_contracts:
        candidate, posted_ts = found_contracts.pop(0)
        # skip if processed
        if is_ca_processed(candidate):
            continue
        # Only consider if posted after last sell time (skip coins posted while we were holding previously)
        if posted_ts and posted_ts <= last_sell_time:
            continue
        # Candidate passes checks
        return candidate

    # fallback: read latest messages from the channel (text, ts tuples)
    from utils import is_valid_solana_address
    msgs = read_from_target_channel(limit=limit_checks)
    for m, posted_ts in msgs:
        if not m:
            continue
        ca_cand = extract_mint_from_start_link(m)
        if not ca_cand:
            # fallback detection as before
            parts = m.split()
            ca_cand = None
            for p in parts:
                if 32 <= len(p) <= 44:
                    ca_cand = p
                    break
        if ca_cand and is_valid_solana_address(ca_cand) and not is_ca_processed(ca_cand):
            # Skip if posted while we were holding (posted_ts <= last_sell_time)
            if posted_ts and posted_ts <= last_sell_time:
                continue
            return ca_cand
    return None

def main_loop():
    global cycle_count, capital_usd, holding_position, last_sell_time, first_buy_done
    while cycle_count < CYCLE_LIMIT:
        daily_limit = get_current_daily_limit()
        current_invests = 0
        safe_send_telegram(f"[*] Starting cycle {cycle_count+1}/{CYCLE_LIMIT} | Daily limit: {daily_limit}")
        while current_invests < daily_limit:
            try:
                # If currently holding and first buy already happened, wait for sell
                if holding_position and first_buy_done:
                    safe_send_telegram("[*] Currently holding a position; waiting for sell to complete before scanning new coins.")
                    time.sleep(30)
                    continue

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

                # At this point we're about to attempt a buy; mark as processed to avoid duplicates
                save_processed_ca(ca)

                # fetch market cap
                mcap = None
                for _ in range(3):
                    mcap = get_market_cap_from_dexscreener(ca)
                    if mcap:
                        break
                    time.sleep(5)
                if not mcap:
                    safe_send_telegram(f"[!] Could not get market cap for {ca}, skipping")
                    continue

                # compute amount (in SOL)
                amount_sol = get_sol_amount_for_usd(capital_usd)
                if amount_sol <= 0:
                    safe_send_telegram("[!] Invalid SOL amount, skipping")
                    continue

                # perform buy (real vs DRY)
                if DRY_RUN:
                    safe_send_telegram(f"[DRY RUN] Would buy {ca} for {amount_sol} SOL (${capital_usd})")
                    buy_sig = None
                else:
                    buy_sig = jupiter_buy(ca, amount_sol)
                if not DRY_RUN and not buy_sig:
                    safe_send_telegram(f"[!] Buy failed for {ca}; skipping sell monitor")
                    # If buy failed, we leave it marked processed to avoid immediate re-buy (keeps previous behavior)
                    continue

                # mark we are holding now
                if not DRY_RUN:
                    holding_position = True
                    # record that we have now completed the first buy in this run
                    first_buy_done = True

                safe_send_telegram(f"[BUY] CA `{ca}` invested ${capital_usd:.2f} ({amount_sol} SOL) | tx: {buy_sig or 'DRY'} | MC at buy: ${mcap:,.0f}")

                # monitor for target MC and stop loss
                target_mc = mcap * REQUIRED_MULTIPLIER
                stop_loss_mc = mcap * 0.8
                sold = False
                while not sold:
                    try:
                        cur_mc = get_market_cap_from_dexscreener(ca)
                        if cur_mc is None:
                            # wait and retry
                            time.sleep(60)
                            continue
                        # Take profit
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
                        # Stop loss
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
                            # wait
                            time.sleep(60)
                    except Exception as e:
                        safe_send_telegram(f"[!] Monitoring loop exception: {e}")
                        time.sleep(60)

                # after sell, clear holding flag and update last_sell_time
                if not DRY_RUN:
                    holding_position = False
                    last_sell_time = time.time()

                # update capital after fees & save reserve
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
        # Sleep until next UTC midnight (calculate remaining)
        now = datetime.utcnow()
        next_midnight = datetime(now.year, now.month, now.day) + timedelta(days=1)
        sleep_seconds = (next_midnight - now).total_seconds()
        safe_send_telegram(f"[*] Day finished. Sleeping {int(sleep_seconds)} seconds until next UTC midnight.")
        time.sleep(sleep_seconds)

def _telethon_runner():
    """
    Runs Telethon's event loop so @client.on handlers fire while main_loop() runs.
    Uses a daemon thread so it works in Termux and on VULTR.
    """
    try:
        with client:
            client.run_until_disconnected()
    except Exception as e:
        safe_send_telegram(f"[!] Telethon runner crashed: {e}")

# resilient runner that auto reconnects Telethon if needed
def start_bot():
    while True:
        try:
            # Ensure session connects before spawning runner thread
            client.connect()
            if not client.is_user_authorized():
                # Will prompt device login on first-ever run; for headless, use code sent to your TG
                client.start()

            # Start Telethon event loop in background so handlers work
            t = threading.Thread(target=_telethon_runner, daemon=True)
            t.start()

            # Run main trading loop in the main thread
            main_loop()

            # If main_loop exits naturally, pause briefly then loop (or break)
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