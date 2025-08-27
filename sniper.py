import os
import json
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Union, Any

# Telethon for scraping the channel
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError

# Local utils (all helpers, RPC, signing, pricing, reporting)
import utils
from utils import (
    DRY_RUN,
    usd_to_sol,
    sol_to_usd,
    get_market_cap,  # returns market cap (dexscreener -> birdeye)
    save_gas_reserve_after_trade,
    send_telegram_message,
    save_processed_ca,
    is_ca_processed,
    get_priority_fee,
    get_token_balance_lamports,
    fetch_jupiter_quote,
    execute_jupiter_swap_from_quote,
    CYCLE_LIMIT,
    TAKE_PROFIT,
    STOP_LOSS,
    logger,
    GAS_BUFFER as UTILS_GAS_BUFFER,
    NORMAL_PRIORITY_FEE,
    HIGH_PRIORITY_FEE,
)

# --- Load environment (explicit) ---
dotenv_path = os.path.expanduser("~/t.env")
utils.load_env(dotenv_path=dotenv_path)

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TARGET_CHANNEL_ID = int(os.getenv("TARGET_CHANNEL_ID", "0"))

# --- Persistence for balance ---
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
        logger.warning("Failed to save balance: %s", e)


# --- Initial capital & runtime vars ---
INITIAL_USD_BALANCE = float(os.getenv("DAILY_CAPITAL_USD", "25"))
current_usd_balance = load_balance(INITIAL_USD_BALANCE)

# --- GAS buffer and tip settings from ENV ---
GAS_BUFFER = float(os.getenv("GAS_BUFFER_USD", str(UTILS_GAS_BUFFER)))
NORMAL_TIP_SOL = usd_to_sol(float(os.getenv("NORMAL_TIP_USD", str(NORMAL_PRIORITY_FEE))))
CONGESTION_TIP_SOL = usd_to_sol(float(os.getenv("CONGESTION_TIP_USD", str(HIGH_PRIORITY_FEE))))

# --- Telethon client (session file name from t.env) ---
session_name = os.getenv("SESSION_NAME", "sniper_session")
client = TelegramClient(session_name, API_ID, API_HASH)

# Constants
WSOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000

# Concurrency guard
balance_lock = asyncio.Lock()

# Cycle state
current_cycle_day = 1
daily_trades = 0


# -------------------------
# Helpers / cycle logic
# -------------------------
def current_daily_limit() -> int:
    """
    Determine today's allowed number of trades using utils.get_daily_reinvestment_plan.
    Fallback to CYCLE_LIMIT if parsing fails.
    """
    try:
        return int(utils.get_daily_reinvestment_plan(current_cycle_day))
    except Exception as e:
        logger.warning("get_daily_reinvestment_plan failed: %s - falling back to CYCLE_LIMIT", e)
        if isinstance(CYCLE_LIMIT, (list, tuple)):
            idx = (current_cycle_day - 1) % len(CYCLE_LIMIT)
            return int(CYCLE_LIMIT[idx])
        return int(CYCLE_LIMIT)


def advance_cycle_after_midnight() -> None:
    global current_cycle_day
    current_cycle_day += 1
    if current_cycle_day > 7:
        current_cycle_day = 1


def get_next_investment() -> float:
    """
    Reserve a small gas runway from current balance and return amount we will invest.
    """
    global current_usd_balance
    invest = save_gas_reserve_after_trade(current_usd_balance, GAS_BUFFER)
    return max(invest, 0.01)


def calculate_gas_fee_sol_from_priority(congestion: bool) -> float:
    """Return SOL tip amount depending on congestion flag (uses derived SOL tip env)."""
    return CONGESTION_TIP_SOL if congestion else NORMAL_TIP_SOL


def estimate_fee_and_adjust_usd(usd_amount: float, congestion: Optional[bool]) -> float:
    """
    Use utils.calculate_total_gas_fee to get USD fee estimate and subtract from amount.
    Falls back to returning original amount if estimator unavailable.
    """
    try:
        est = utils.calculate_total_gas_fee(usd_amount, congestion=congestion, num_priority_txs=1)
        if est is None:
            return usd_amount
        adjusted = max(usd_amount - float(est), 0.01)
        logger.debug("Estimated USD gas cost %.6f for $%.2f -> using invest $%.6f", est, usd_amount, adjusted)
        return adjusted
    except Exception as e:
        logger.debug("calculate_total_gas_fee failed: %s - falling back.", e)
        return usd_amount


# -------------------------
# Market cap helper with retry
# -------------------------
async def fetch_market_cap_with_retry(ca: str, retries: int = 3, delay: int = 2) -> float:
    for attempt in range(1, retries + 1):
        try:
            mcap = get_market_cap(ca)
            if mcap:
                return mcap
        except Exception as e:
            logger.warning("get_market_cap error attempt %d: %s", attempt, e)
        logger.warning("Attempt %d failed to fetch market cap for %s. Retrying in %ds...", attempt, ca, delay)
        await asyncio.sleep(delay)
    return 0.0


# -------------------------
# Jupiter wrappers
# -------------------------
def jupiter_buy_token(contract_mint: str, sol_amount: float, congestion_flag: bool) -> Optional[str]:
    try:
        amount_lamports = int(sol_amount * LAMPORTS_PER_SOL)
        if amount_lamports <= 0:
            logger.warning("Buy amount too small after fees; skipping.")
            return None
        quote = fetch_jupiter_quote(WSOL_MINT, contract_mint, amount_lamports)
        if not quote:
            logger.warning("No Jupiter quote for BUY (mint=%s, lamports=%d).", contract_mint, amount_lamports)
            return None
        return execute_jupiter_swap_from_quote(quote, congestion=congestion_flag)
    except Exception as e:
        logger.exception("jupiter_buy_token error: %s", e)
        return None


def jupiter_sell_token(contract_mint: str, congestion_flag: bool) -> Optional[str]:
    try:
        raw_balance = get_token_balance_lamports(contract_mint)
        if raw_balance <= 0:
            logger.warning("No token balance to sell; skipping.")
            return None
        quote = fetch_jupiter_quote(contract_mint, WSOL_MINT, raw_balance)
        if not quote:
            logger.warning("No Jupiter quote for SELL (mint=%s, amount=%d).", contract_mint, raw_balance)
            return None
        return execute_jupiter_swap_from_quote(quote, congestion=congestion_flag)
    except Exception as e:
        logger.exception("jupiter_sell_token error: %s", e)
        return None


# -------------------------
# Simple utils used by sniper
# -------------------------
def subtract_tip_cost(usd_balance: float, tip_sol: float) -> float:
    """
    Convert a tip (in SOL) to USD and subtract from the USD balance.
    Uses utils.sol_to_usd indirectly (via sol_to_usd) and safe-fallbacks.
    """
    try:
        usd_per_sol = usd_to_sol(1)
        if not usd_per_sol or usd_per_sol <= 0:
            return usd_balance
        usd_cost = tip_sol / usd_per_sol
        return max(usd_balance - usd_cost, 0.01)
    except Exception:
        return usd_balance


def handle_cycle_reset() -> None:
    """
    Reset cycle state either via utils.reset_cycle_state (if implemented) or local fallback.
    """
    global daily_trades, current_cycle_day
    try:
        if isinstance(CYCLE_LIMIT, int) and CYCLE_LIMIT == 0:
            logger.info("Cycle reset skipped (CYCLE_LIMIT=0, cycles disabled).")
            return
    except Exception:
        pass

    try:
        if hasattr(utils, "reset_cycle_state") and callable(utils.reset_cycle_state):
            try:
                changed = utils.reset_cycle_state()
                if changed:
                    logger.info("utils.reset_cycle_state() returned True - cycle marked reset.")
                else:
                    logger.info("utils.reset_cycle_state() returned False - already reset.")
            except Exception as e:
                logger.warning("utils.reset_cycle_state() raised: %s - continuing with local reset.", e)

        daily_trades = 0
        advance_cycle_after_midnight()
        utils.clear_processed_ca()
        logger.info("Local cycle reset performed. New cycle day = %s, today's limit = %s", current_cycle_day,
                    current_daily_limit())
    except Exception as e:
        logger.exception("Error during handle_cycle_reset: %s", e)


def report_trade_summary(
    ca: str,
    buy_mc: float,
    sell_mc: float,
    pnl: float,
    tx_buy: str,
    tx_sell: str,
    fee_buy_sol: float,
    fee_sell_sol: float,
    balance: float,
) -> None:
    """Send a short summary to the bot chat."""
    send_telegram_message(
        f"📊 Trade Complete\n"
        f"CA: {ca}\n"
        f"Buy MC: {buy_mc}\n"
        f"Sell MC: {sell_mc}\n"
        f"PnL: {pnl:.2f}%\n"
        f"Tx Buy: {tx_buy}\n"
        f"Tx Sell: {tx_sell}\n"
        f"Priority Fee Buy: {fee_buy_sol:.6f} SOL\n"
        f"Priority Fee Sell: {fee_sell_sol:.6f} SOL\n"
        f"Balance after compounding: ${balance:.2f}"
    )


# -------------------------
# Main message handler
# -------------------------
async def handle_new_message(event) -> None:
    """
    Processes new messages from target channel.
    - Extract CA using utils.extract_contract_address
    -