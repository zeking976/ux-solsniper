# sniper.py
import os
import json
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Union, Any

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, RpcError

# Local utils (centralized helpers and env)
import utils
from utils import (
    DRY_RUN,
    usd_to_sol,
    sol_to_usd,
    get_market_cap,
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

# -------------------------
# Load environment (already supports termux/Vultr style t.env)
# -------------------------
dotenv_path = os.path.expanduser("~/t.env")
utils.load_env(dotenv_path=dotenv_path)

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TARGET_CHANNEL_ID = int(os.getenv("TARGET_CHANNEL_ID", "0"))

# -------------------------
# Persistence / balance
# -------------------------
BALANCE_FILE = "balance.json"


def load_balance(default: float) -> float:
    """Load last persisted USD balance from balance.json, fallback to default."""
    if os.path.exists(BALANCE_FILE):
        try:
            with open(BALANCE_FILE, "r") as f:
                data = json.load(f)
                return float(data.get("usd_balance", default))
        except Exception:
            # If parsing fails, return default to avoid crash
            return default
    return default


def save_balance(balance: float) -> None:
    """Persist the current USD balance to balance.json."""
    try:
        with open(BALANCE_FILE, "w") as f:
            json.dump({"usd_balance": balance}, f)
    except Exception as e:
        logger.warning("Failed to save balance: %s", e)


# Initial capital (the starting daily bankroll, compounding stored to balance.json)
INITIAL_USD_BALANCE = float(os.getenv("DAILY_CAPITAL_USD", "25"))
current_usd_balance = load_balance(INITIAL_USD_BALANCE)

# GAS_BUFFER read from utils (preferred). Local fallback kept for safety
try:
    GAS_BUFFER = float(os.getenv("GAS_BUFFER", str(UTILS_GAS_BUFFER)))
except Exception:
    GAS_BUFFER = float(os.getenv("GAS_BUFFER", "0.009"))

# Tip fees (fixed per tx, in SOL) - used as fallback / quick check.
# Prefer values from utils (NORMAL_PRIORITY_FEE, HIGH_PRIORITY_FEE) which are driven by t.env.
try:
    NORMAL_TIP_SOL = float(os.getenv("NORMAL_TIP_SOL", str(NORMAL_PRIORITY_FEE)))
except Exception:
    NORMAL_TIP_SOL = 0.015
try:
    CONGESTION_TIP_SOL = float(os.getenv("CONGESTION_TIP_SOL", str(HIGH_PRIORITY_FEE)))
except Exception:
    CONGESTION_TIP_SOL = 0.1

# -------------------------
# Telegram session
# -------------------------
session_name = os.getenv("SESSION_NAME", "sniper_session")
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
# Cycle plan handling using utils.get_daily_reinvestment_plan
# -------------------------
# We'll track a simple day index (1..N) for your reinvestment plan. utils handles plan mapping.
current_cycle_day = 1  # starts at Day 1; gets advanced after daily reset


def current_daily_limit() -> int:
    """
    Return the number of trades allowed today according to reinvestment plan.
    utils.get_daily_reinvestment_plan(day) is expected to return an int.
    """
    try:
        # We pass the cycle day to utils helper; utils will default to Day 1 if needed.
        return int(utils.get_daily_reinvestment_plan(current_cycle_day))
    except Exception as e:
        logger.warning("get_daily_reinvestment_plan failed: %s - falling back to CYCLE_LIMIT", e)
        # fallback: if CYCLE_LIMIT is a tuple/list, pick element; else return int
        if isinstance(CYCLE_LIMIT, (list, tuple)):
            idx = (current_cycle_day - 1) % len(CYCLE_LIMIT)
            return int(CYCLE_LIMIT[idx])
        return int(CYCLE_LIMIT)


def advance_cycle_after_midnight() -> None:
    """
    Advance cycle to next day. If utils plan is 2-day, this cycles 1 -> 2 -> 1.
    This function only advances our local day index; utils.get_daily_reinvestment_plan
    defines the actual numbers for each day.
    """
    global current_cycle_day
    current_cycle_day += 1
    # If utils has a plan beyond 7 days it's fine; this ensures we don't grow unbounded.
    if current_cycle_day > 7:
        current_cycle_day = 1


# -------------------------
# Helper: calculate next investment (compounds)
# -------------------------
def get_next_investment() -> float:
    """
    Compute next USD investment by reserving a gas buffer off the current balance.
    Uses save_gas_reserve_after_trade from utils to compute the available amount after reserving.
    """
    global current_usd_balance
    invest = save_gas_reserve_after_trade(current_usd_balance, GAS_BUFFER)
    return max(invest, 0.01)


# -------------------------
# Gas fee helpers
# -------------------------
def calculate_gas_fee_sol_from_priority(congestion: bool) -> float:
    """Return SOL gas fee depending on congestion flag (simple tip values)."""
    # Prefer utils constants when available
    try:
        return CONGESTION_TIP_SOL if congestion else NORMAL_TIP_SOL
    except Exception:
        return 0.015 if not congestion else 0.1


def estimate_fee_and_adjust_usd(usd_amount: float, congestion: Optional[bool]) -> float:
    """
    Attempt to use utils.calculate_total_gas_fee to estimate USD fee, subtract it,
    and return the adjusted USD amount that will be converted to SOL.

    Fallback: if utils.calculate_total_gas_fee returns None or raises, return the original usd_amount.
    """
    try:
        # Prefer the utils estimator which returns USD amount (priority fees + base reserve)
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
# Robust market cap fetch with retries (async)
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
# Jupiter helpers (thin wrappers)
# -------------------------
def jupiter_buy_token(contract_mint: str, sol_amount: float, congestion_flag: bool) -> Optional[str]:
    """
    Execute SOL -> token swap using Jupiter. Returns signature string or None.
    """
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
    """
    Execute token -> SOL swap using Jupiter. Returns signature string or None.
    """
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
# Daily trade counters
# -------------------------
daily_trades = 0


# -------------------------
# Tip cost handling (USD)
# -------------------------
def subtract_tip_cost(usd_balance: float, tip_sol: float) -> float:
    """
    Convert tip in SOL to USD via usd_to_sol and subtract from balance.
    usd_to_sol(1) returns SOL equivalent of $1, so tip USD = tip_sol / (SOL per $1).
    """
    try:
        usd_per_sol = usd_to_sol(1)  # returns SOL equivalent of $1
        if not usd_per_sol or usd_per_sol <= 0:
            return usd_balance
        usd_cost = tip_sol / usd_per_sol
        return max(usd_balance - usd_cost, 0.01)
    except Exception:
        return usd_balance


# -------------------------
# Cycle reset handling
# -------------------------
def handle_cycle_reset() -> None:
    """
    Reset the reinvestment cycle using utils.reset_cycle_state() if present.
    Observes CYCLE_LIMIT == 0 meaning "cycles disabled" (no resets).
    Ensures daily_trades and processed list are cleared appropriately.
    """
    global daily_trades, current_cycle_day

    # If cycles disabled (CYCLE_LIMIT set to 0 in t.env), do not reset
    try:
        if isinstance(CYCLE_LIMIT, int) and CYCLE_LIMIT == 0:
            logger.info("Cycle reset skipped (CYCLE_LIMIT=0, cycles disabled).")
            return
    except Exception:
        # if CYCLE_LIMIT malformed, continue with reset behavior
        pass

    # Try to call utils.reset_cycle_state (user-supplied). If not present, do a best-effort reset.
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
        # Local housekeeping:
        daily_trades = 0
        # rotate the cycle day forward (advance_cycle_after_midnight already handles wrap)
        advance_cycle_after_midnight()
        utils.clear_processed_ca()
        logger.info("Local cycle reset performed. New cycle day = %s, today's limit = %s", current_cycle_day,
                    current_daily_limit())
    except Exception as e:
        logger.exception("Error during handle_cycle_reset: %s", e)


# -------------------------
# Trade summary reporter
# -------------------------
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
    """
    Friendly trade summary delivered to Telegram.
    """
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
    Called for new messages in the monitored Telegram channel.
    Processes token mint announcements and runs the buy/sell cycle.
    """
    global current_usd_balance, daily_trades

    try:
        # If daily limit already reached — skip processing
        if daily_trades >= current_daily_limit():
            return

        text = getattr(event.message, "text", None)
        ca = utils.extract_contract_address(text, message_obj=event.message)
        if not ca:
            return

        if is_ca_processed(ca):
            logger.info("CA already processed: %s", ca)
            return

        logger.info("Detected new CA: %s", ca)

        # Calculate next investment (USD)
        async with balance_lock:
            usd_invest = get_next_investment()

        # Estimate / reserve USD fees using utils.calculate_total_gas_fee when possible
        # First decide congestion flag via get_priority_fee() (in SOL)
        priority_fee_sol_buy = get_priority_fee()
        # Prefer reading priority fee from utils; fallback to fallback env constants for threshold detection
        try:
            congestion_flag_buy = priority_fee_sol_buy > (NORMAL_TIP_SOL or NORMAL_PRIORITY_FEE)
        except Exception:
            congestion_flag_buy = bool(priority_fee_sol_buy > NORMAL_PRIORITY_FEE)

        # Use USD estimator to adjust the investable USD down by estimated fees (prefer this)
        usd_to_invest_after_est_fee = estimate_fee_and_adjust_usd(usd_invest, congestion_flag_buy)

        # Convert USD to SOL amount to send to Jupiter for buy
        sol_invest = usd_to_sol(usd_to_invest_after_est_fee)

        # As a fallback — ensure we subtract priority tip (SOL) from sol_invest
        # This prevents sending exactly the tip amount + zero to JP
        sol_after_tips = max(sol_invest - calculate_gas_fee_sol_from_priority(congestion_flag_buy), 0.0)

        logger.info(
            "Investing $%.2f (est fee-adjusted $%.2f -> ~%.6f SOL, after tip reserve %.6f SOL) into %s",
            usd_invest,
            usd_to_invest_after_est_fee,
            sol_invest,
            sol_after_tips,
            ca,
        )

        # Execute buy (with retries)
        if DRY_RUN:
            tx_buy = "SIMULATED_BUY"
        else:
            tx_buy = jupiter_buy_token(ca, sol_after_tips, congestion_flag=congestion_flag_buy)
            if not tx_buy:
                # Retry a few times
                for _ in range(3):
                    await asyncio.sleep(2)
                    tx_buy = jupiter_buy_token(ca, sol_after_tips, congestion_flag=congestion_flag_buy)
                    if tx_buy:
                        break
                if not tx_buy:
                    logger.warning("Final buy failed for %s; skipping this CA.", ca)
                    return

        # Record buy market cap (for PnL)
        buy_market_cap = await fetch_market_cap_with_retry(ca)
        logger.info("Buy Market Cap: %s", buy_market_cap)

        # Wait for take-profit / stop-loss
        while True:
            current_cap = await fetch_market_cap_with_retry(ca)
            if not current_cap or not buy_market_cap:
                await asyncio.sleep(20)
                continue

            # NOTE: protect division by zero
            try:
                profit_loss_pct = ((current_cap - buy_market_cap) / buy_market_cap) * 100
            except Exception:
                profit_loss_pct = 0.0

            if profit_loss_pct >= TAKE_PROFIT:
                logger.info("✅ Take Profit triggered: %.2f%%", profit_loss_pct)
                break

            if profit_loss_pct <= STOP_LOSS:
                logger.info("🛑 Stop Loss triggered: %.2f%%", profit_loss_pct)
                break

            await asyncio.sleep(20)

        # Prepare sell: determine updated congestion & fees
        priority_fee_sol_sell = get_priority_fee()
        try:
            congestion_flag_sell = priority_fee_sol_sell > (NORMAL_TIP_SOL or NORMAL_PRIORITY_FEE)
        except Exception:
            congestion_flag_sell = bool(priority_fee_sol_sell > NORMAL_PRIORITY_FEE)

        # Execute sell (with retries)
        if DRY_RUN:
            tx_sell = "SIMULATED_SELL"
        else:
            tx_sell = jupiter_sell_token(ca, congestion_flag=congestion_flag_sell)
            if not tx_sell:
                for _ in range(3):
                    await asyncio.sleep(2)
                    tx_sell = jupiter_sell_token(ca, congestion_flag=congestion_flag_sell)
                    if tx_sell:
                        break
                if not tx_sell:
                    logger.warning("Final sell failed for %s; skipping PnL handling.", ca)
                    return

        sell_market_cap = await fetch_market_cap_with_retry(ca)
        logger.info("Sell Market Cap: %s", sell_market_cap)

        # Compute PnL % using market caps (best-effort)
        profit_loss_pct = 0.0
        try:
            if buy_market_cap and sell_market_cap:
                profit_loss_pct = ((sell_market_cap - buy_market_cap) / buy_market_cap) * 100
        except Exception:
            profit_loss_pct = 0.0

        # Compound the USD investment by PnL % (positive or negative)
        compounded = usd_invest * (1 + (profit_loss_pct / 100.0))

        # Reserve a gas runway from compounded results (small pct)
        compounded_after_buffer = save_gas_reserve_after_trade(compounded, GAS_BUFFER)

        # Convert actual tip SOL costs to USD and subtract (we used USD estimator earlier, but subtract actual tip SOLs too)
        # For priority fees we will use the SOL tip values (priority_fee_sol_buy/sell) if estimator not present.
        compounded_after_tips = subtract_tip_cost(compounded_after_buffer, priority_fee_sol_buy)
        compounded_after_tips = subtract_tip_cost(compounded_after_tips, priority_fee_sol_sell)

        # Persist the new balance after compounding
        async with balance_lock:
            current_usd_balance = max(compounded_after_tips, 0.01)
            save_balance(current_usd_balance)

        # Increment daily trade counter
        daily_trades += 1

        # Report trade summary
        # Use SOL tip values for human readability. In future you may prefer to compute actual SOL spent from RPC.
        report_trade_summary(
            ca,
            buy_market_cap,
            sell_market_cap,
            profit_loss_pct,
            tx_buy,
            tx_sell,
            priority_fee_sol_buy,
            priority_fee_sol_sell,
            current_usd_balance,
        )

        # Mark CA processed for today and persist
        save_processed_ca(ca)

        # If we reached daily limit, wait until next cycle / midnight and then run reset logic
        if daily_trades >= current_daily_limit():
            now = datetime.utcnow()
            # sleep until next 00:00 UTC
            next_cycle = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            sleep_seconds = (next_cycle - now).total_seconds()
            logger.info(
                "Daily limit (%s) reached. Sleeping %.2fh until 00:00 UTC...",
                current_daily_limit(),
                sleep_seconds / 3600.0,
            )
            # Sleep (non-blocking)
            await asyncio.sleep(sleep_seconds)

            # Reset using centralized handler (will respect CYCLE_LIMIT==0)
            handle_cycle_reset()

    except (RpcError, FloodWaitError) as rpc_e:
        