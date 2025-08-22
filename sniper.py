import os
import json
import asyncio
from datetime import datetime, timedelta

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, RpcError

# Local utils
import utils
from utils import (
    DRY_RUN,
    usd_to_sol,
    get_market_cap,
    save_gas_reserve_after_trade,
    send_telegram_message,
    save_processed_ca,
    is_ca_processed,
    get_priority_fee,
    get_token_balance_lamports,
    fetch_jupiter_quote,
    execute_jupiter_swap_from_quote,
    CYCLE_LIMIT,      # may be int or tuple per utils.py
    TAKE_PROFIT,
    STOP_LOSS,
    logger,           # use utils logger for consistency
)

# -------------------------
# Load environment
# -------------------------
dotenv_path = os.path.expanduser("~/t.env")
utils.load_env(dotenv_path=dotenv_path)

API_ID = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TARGET_CHANNEL_ID = int(os.getenv("TARGET_CHANNEL_ID"))

# -------------------------
# State persistence for balance
# -------------------------
BALANCE_FILE = "balance.json"

def load_balance(default: float) -> float:
    if os.path.exists(BALANCE_FILE):
        try:
            with open(BALANCE_FILE, "r") as f:
                data = json.load(f)
                return float(data.get("usd_balance", default))
        except Exception:
            return default
    return default

def save_balance(balance: float) -> None:
    try:
        with open(BALANCE_FILE, "w") as f:
            json.dump({"usd_balance": balance}, f)
    except Exception as e:
        logger.warning(f"Failed to save balance: {e}")

# Initial capital (compounds after each trade)
INITIAL_USD_BALANCE = float(os.getenv("DAILY_CAPITAL_USD", 25))
current_usd_balance = load_balance(INITIAL_USD_BALANCE)

# Fees buffer (%) for gas in USD (configurable via t.env)
GAS_BUFFER = float(os.getenv("GAS_BUFFER", 0.009))  # 0.9% reserve

# -------------------------
# Telegram session
# -------------------------
session_name = "session"
client = TelegramClient(session_name, API_ID, API_HASH)

# -------------------------
# Constants
# -------------------------
WSOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000

# -------------------------
# Concurrency guard for balance compounding
# -------------------------
balance_lock = asyncio.Lock()

# -------------------------
# Cycle plan handling (int or tuple from utils.CYCLE_LIMIT)
# -------------------------
# If CYCLE_LIMIT is an int: one fixed limit per day.
# If it's a tuple (e.g., (5, 4)): rotate daily through those limits.
if isinstance(CYCLE_LIMIT, tuple):
    _cycle_plan = list(CYCLE_LIMIT)
else:
    _cycle_plan = None  # single fixed limit mode

current_cycle_idx = 0  # rotates if tuple plan is used

def current_daily_limit() -> int:
    if _cycle_plan:
        return int(_cycle_plan[current_cycle_idx])
    return int(CYCLE_LIMIT)

def advance_cycle_after_midnight() -> None:
    global current_cycle_idx
    if _cycle_plan:
        # move to next bucket (wrap-around)
        current_cycle_idx = (current_cycle_idx + 1) % len(_cycle_plan)

# -------------------------
# Helper: calculate next investment (compounds)
# -------------------------
def get_next_investment() -> float:
    global current_usd_balance
    # Reserve GAS_BUFFER% for gas; fees/tips are handled via utils.get_priority_fee()
    invest = save_gas_reserve_after_trade(current_usd_balance, GAS_BUFFER)
    return max(invest, 0.01)

# -------------------------
# Robust market cap fetch with retries
# -------------------------
async def fetch_market_cap_with_retry(ca: str, retries: int = 3, delay: int = 2) -> float:
    for attempt in range(1, retries + 1):
        mcap = get_market_cap(ca)
        if mcap:
            return mcap
        logger.warning(f"Attempt {attempt} failed to fetch market cap for {ca}. Retrying in {delay}s...")
        await asyncio.sleep(delay)
    return 0.0

# -------------------------
# Jupiter helpers
# -------------------------
def jupiter_buy_token(contract_mint: str, sol_amount: float, congestion_flag: bool) -> str | None:
    try:
        amount_lamports = int(sol_amount * LAMPORTS_PER_SOL)
        if amount_lamports <= 0:
            logger.warning("Buy amount too small after fees; skipping.")
            return None
        quote = fetch_jupiter_quote(WSOL_MINT, contract_mint, amount_lamports)
        if not quote:
            logger.warning("No Jupiter quote for BUY.")
            return None
        # MEV protection is applied inside utils via asymmetric slippage on both quote & swap
        return execute_jupiter_swap_from_quote(quote, congestion=congestion_flag)
    except Exception as e:
        logger.exception(f"jupiter_buy_token error: {e}")
        return None

def jupiter_sell_token(contract_mint: str, congestion_flag: bool) -> str | None:
    try:
        raw_balance = get_token_balance_lamports(contract_mint)
        if raw_balance <= 0:
            logger.warning("No token balance to sell; skipping.")
            return None
        quote = fetch_jupiter_quote(contract_mint, WSOL_MINT, raw_balance)
        if not quote:
            logger.warning("No Jupiter quote for SELL.")
            return None
        # MEV protection is applied inside utils via asymmetric slippage on both quote & swap
        return execute_jupiter_swap_from_quote(quote, congestion=congestion_flag)
    except Exception as e:
        logger.exception(f"jupiter_sell_token error: {e}")
        return None

# -------------------------
# Daily trade counters
# -------------------------
daily_trades = 0  # counts completed buy+sell pairs for the current day

# -------------------------
# Handle new Telegram messages
# -------------------------
async def handle_new_message(event):
    global current_usd_balance, daily_trades

    try:
        # Respect the daily limit (works for both int and tuple plan)
        if daily_trades >= current_daily_limit():
            return

        text = getattr(event.message, "text", None)
        ca = utils.extract_contract_address(text, message_obj=event.message)
        if not ca:
            return

        if is_ca_processed(ca):
            logger.info(f"CA already processed: {ca}")
            return

        logger.info(f"Detected new CA: {ca}")

        # Determine investment (compounded)
        async with balance_lock:
            usd_invest = get_next_investment()
        sol_invest = usd_to_sol(usd_invest)

        # Priority fee (env-configurable; detects congestion automatically unless MANUAL_CONGESTION set)
        priority_fee_buy = get_priority_fee()
        # Convert to net SOL after priority-fee tip (Jupiter prioritization lamports handled in utils as well)
        sol_after_fees = max(sol_invest - priority_fee_buy, 0.0)

        logger.info(
            f"Investing ${usd_invest:.2f} (~{sol_after_fees:.6f} SOL after priority fee) "
            f"into {ca} | priority fee buy {priority_fee_buy:.6f} SOL"
        )

        # -------------------------
        # BUY
        # -------------------------
        if DRY_RUN:
            tx_buy = "SIMULATED_BUY"
        else:
            tx_buy = jupiter_buy_token(ca, sol_after_fees, congestion_flag=(priority_fee_buy > utils.NORMAL_PRIORITY_FEE))
            if not tx_buy:
                for _ in range(3):
                    await asyncio.sleep(2)
                    tx_buy = jupiter_buy_token(ca, sol_after_fees, congestion_flag=(priority_fee_buy > utils.NORMAL_PRIORITY_FEE))
                    if tx_buy:
                        break
                if not tx_buy:
                    logger.warning("Final buy failed; skipping this CA.")
                    return

        buy_market_cap = await fetch_market_cap_with_retry(ca)
        logger.info(f"Buy Market Cap: {buy_market_cap}")

        # -------------------------
        # Monitor for TP/SL
        # -------------------------
        while True:
            current_cap = await fetch_market_cap_with_retry(ca)
            if not current_cap or not buy_market_cap:
                await asyncio.sleep(20)
                continue

            profit_loss_pct = ((current_cap - buy_market_cap) / buy_market_cap) * 100

            if profit_loss_pct >= TAKE_PROFIT:
                logger.info(f"✅ Take Profit triggered: {profit_loss_pct:.2f}%")
                break

            if profit_loss_pct <= STOP_LOSS:
                logger.info(f"🛑 Stop Loss triggered: {profit_loss_pct:.2f}%")
                break

            await asyncio.sleep(20)

        # -------------------------
        # SELL
        # -------------------------
        priority_fee_sell = get_priority_fee()
        if DRY_RUN:
            tx_sell = "SIMULATED_SELL"
        else:
            tx_sell = jupiter_sell_token(ca, congestion_flag=(priority_fee_sell > utils.NORMAL_PRIORITY_FEE))
            if not tx_sell:
                for _ in range(3):
                    await asyncio.sleep(2)
                    tx_sell = jupiter_sell_token(ca, congestion_flag=(priority_fee_sell > utils.NORMAL_PRIORITY_FEE))
                    if tx_sell:
                        break
                if not tx_sell:
                    logger.warning("Final sell failed; skipping.")
                    return

        sell_market_cap = await fetch_market_cap_with_retry(ca)
        logger.info(f"Sell Market Cap: {sell_market_cap}")

        # -------------------------
        # Update balance (compounding)
        # -------------------------
        profit_loss_pct = 0.0
        if buy_market_cap and sell_market_cap:
            profit_loss_pct = ((sell_market_cap - buy_market_cap) / buy_market_cap) * 100

        compounded = usd_invest * (1 + (profit_loss_pct / 100.0))
        compounded_after_buffer = save_gas_reserve_after_trade(compounded, GAS_BUFFER)

        async with balance_lock:
            current_usd_balance = max(compounded_after_buffer, 0.01)
            save_balance(current_usd_balance)

        daily_trades += 1

        # -------------------------
        # Report
        # -------------------------
        send_telegram_message(
            f"📊 Trade Complete\n"
            f"CA: {ca}\n"
            f"Buy MC: {buy_market_cap}\n"
            f"Sell MC: {sell_market_cap}\n"
            f"PnL: {profit_loss_pct:.2f}%\n"
            f"Tx Buy: {tx_buy}\n"
            f"Tx Sell: {tx_sell}\n"
            f"Priority Fee Buy: {priority_fee_buy:.6f} SOL\n"
            f"Priority Fee Sell: {priority_fee_sell:.6f} SOL"
        )

        save_processed_ca(ca)

        # -------------------------
        # If we've hit today's limit, sleep until 00:00 UTC,
        # then rotate to the next cycle bucket (if tuple) and continue compounding.
        # -------------------------
        if daily_trades >= current_daily_limit():
            now = datetime.utcnow()
            next_cycle = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            sleep_seconds = (next_cycle - now).total_seconds()
            logger.info(
                f"Daily limit ({current_daily_limit()}) reached. "
                f"Sleeping {sleep_seconds/3600:.2f}h until 00:00 UTC..."
            )
            await asyncio.sleep(sleep_seconds)

            # New UTC day: reset counters, advance cycle if tuple, clear CA memory
            daily_trades = 0
            advance_cycle_after_midnight()
            utils.clear_processed_ca()
            logger.info(f"New cycle started. Today's limit = {current_daily_limit()} trades.")

    except (RpcError, FloodWaitError) as rpc_e:
        logger.warning(f"Telethon RPC error: {rpc_e}.")
    except Exception as e:
        logger.exception(f"Error in handle_new_message: {e}")

# -------------------------
# Auto reconnect wrapper
# -------------------------
async def start_client():
    while True:
        try:
            async with client:
                client.add_event_handler(handle_new_message, events.NewMessage(chats=TARGET_CHANNEL_ID))
                logger.info("Listening for new messages...")
                await client.run_until_disconnected()
        except Exception as e:
            logger.warning(f"Client disconnected: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)

# -------------------------
# Main
# -------------------------
def main():
    logger.info("Starting sniper bot...")
    asyncio.run(start_client())

if __name__ == "__main__":
    main()