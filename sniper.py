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
    CYCLE_LIMIT,
    TAKE_PROFIT,
    STOP_LOSS,
    logger,
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
    """
    Load last persisted USD balance from balance.json.
    If not found, return the provided default.
    """
    if os.path.exists(BALANCE_FILE):
        try:
            with open(BALANCE_FILE, "r") as f:
                data = json.load(f)
                return float(data.get("usd_balance", default))
        except Exception:
            return default
    return default

def save_balance(balance: float) -> None:
    """
    Persist the current USD balance to balance.json.
    """
    try:
        with open(BALANCE_FILE, "w") as f:
            json.dump({"usd_balance": balance}, f)
    except Exception as e:
        logger.warning(f"Failed to save balance: {e}")

# Initial capital (compounds after each trade)
INITIAL_USD_BALANCE = float(os.getenv("DAILY_CAPITAL_USD", 25))
current_usd_balance = load_balance(INITIAL_USD_BALANCE)

# Fees buffer (%) for gas in USD (configurable via t.env)
GAS_BUFFER = float(os.getenv("GAS_BUFFER", 0.009))

# Tip fees (fixed per tx, in SOL)
NORMAL_TIP_SOL = float(os.getenv("NORMAL_TIP_SOL", 0.015))
CONGESTION_TIP_SOL = float(os.getenv("CONGESTION_TIP_SOL", 0.1))

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
# Cycle plan handling (int or tuple)
# -------------------------
if isinstance(CYCLE_LIMIT, tuple):
    _cycle_plan = list(CYCLE_LIMIT)
else:
    _cycle_plan = None

current_cycle_idx = 0

def current_daily_limit() -> int:
    """
    Returns the current daily trade limit.
    If a cycle plan exists, pull the limit for the current index.
    Otherwise use static CYCLE_LIMIT.
    """
    if _cycle_plan:
        return int(_cycle_plan[current_cycle_idx])
    return int(CYCLE_LIMIT)

def advance_cycle_after_midnight() -> None:
    """
    Rotate to the next cycle after midnight UTC.
    """
    global current_cycle_idx
    if _cycle_plan:
        current_cycle_idx = (current_cycle_idx + 1) % len(_cycle_plan)

# -------------------------
# Helper: calculate next investment (compounds)
# -------------------------
def get_next_investment() -> float:
    """
    Get the USD amount for the next trade,
    subtracting a gas buffer reserve.
    """
    global current_usd_balance
    invest = save_gas_reserve_after_trade(current_usd_balance, GAS_BUFFER)
    return max(invest, 0.01)

# -------------------------
# Robust market cap fetch with retries
# -------------------------
async def fetch_market_cap_with_retry(ca: str, retries: int = 3, delay: int = 2) -> float:
    """
    Try to fetch market cap with retry logic to handle intermittent RPC/API issues.
    """
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
    """
    Execute a Jupiter swap from SOL -> token.
    Returns tx signature or None.
    """
    try:
        amount_lamports = int(sol_amount * LAMPORTS_PER_SOL)
        if amount_lamports <= 0:
            logger.warning("Buy amount too small after fees; skipping.")
            return None
        quote = fetch_jupiter_quote(WSOL_MINT, contract_mint, amount_lamports)
        if not quote:
            logger.warning("No Jupiter quote for BUY.")
            return None
        return execute_jupiter_swap_from_quote(quote, congestion=congestion_flag)
    except Exception as e:
        logger.exception(f"jupiter_buy_token error: {e}")
        return None

def jupiter_sell_token(contract_mint: str, congestion_flag: bool) -> str | None:
    """
    Execute a Jupiter swap from token -> SOL.
    Returns tx signature or None.
    """
    try:
        raw_balance = get_token_balance_lamports(contract_mint)
        if raw_balance <= 0:
            logger.warning("No token balance to sell; skipping.")
            return None
        quote = fetch_jupiter_quote(contract_mint, WSOL_MINT, raw_balance)
        if not quote:
            logger.warning("No Jupiter quote for SELL.")
            return None
        return execute_jupiter_swap_from_quote(quote, congestion=congestion_flag)
    except Exception as e:
        logger.exception(f"jupiter_sell_token error: {e}")
        return None

# -------------------------
# Daily trade counters
# -------------------------
daily_trades = 0

# -------------------------
# Helper: apply tip cost from t.env to balance (USD equivalent)
# -------------------------
def subtract_tip_cost(usd_balance: float, tip_sol: float) -> float:
    """
    Convert the given tip cost in SOL into USD equivalent and subtract it from balance.
    """
    try:
        usd_per_sol = usd_to_sol(1)  # inverse: $1 worth in SOL
        usd_cost = tip_sol / usd_per_sol
        return max(usd_balance - usd_cost, 0.01)
    except Exception:
        return usd_balance

# -------------------------
# Trade reporting
# -------------------------
def report_trade_summary(ca: str, buy_mc: float, sell_mc: float, pnl: float,
                        tx_buy: str, tx_sell: str, fee_buy: float, fee_sell: float,
                        balance: float) -> None:
    """
    Sends a formatted Telegram message with trade summary.
    """
    send_telegram_message(
        f"\ud83d\udcca Trade Complete\n"
        f"CA: {ca}\n"
        f"Buy MC: {buy_mc}\n"
        f"Sell MC: {sell_mc}\n"
        f"PnL: {pnl:.2f}%\n"
        f"Tx Buy: {tx_buy}\n"
        f"Tx Sell: {tx_sell}\n"
        f"Priority Fee Buy: {fee_buy:.6f} SOL\n"
        f"Priority Fee Sell: {fee_sell:.6f} SOL\n"
        f"Balance after compounding: ${balance:.2f}"
    )

# -------------------------
# Handle new Telegram messages
# -------------------------
async def handle_new_message(event):
    """
    Handle a new CA message from the monitored Telegram channel.
    """
    global current_usd_balance, daily_trades

    try:
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

        async with balance_lock:
            usd_invest = get_next_investment()
        sol_invest = usd_to_sol(usd_invest)

        priority_fee_buy = get_priority_fee()
        sol_after_fees = max(sol_invest - priority_fee_buy, 0.0)

        logger.info(
            f"Investing ${usd_invest:.2f} (~{sol_after_fees:.6f} SOL after fee) into {ca} | priority fee buy {priority_fee_buy:.6f} SOL"
        )

        if DRY_RUN:
            tx_buy = "SIMULATED_BUY"
        else:
            tx_buy = jupiter_buy_token(ca, sol_after_fees, congestion_flag=(priority_fee_buy > NORMAL_TIP_SOL))
            if not tx_buy:
                for _ in range(3):
                    await asyncio.sleep(2)
                    tx_buy = jupiter_buy_token(ca, sol_after_fees, congestion_flag=(priority_fee_buy > NORMAL_TIP_SOL))
                    if tx_buy:
                        break
                if not tx_buy:
                    logger.warning("Final buy failed; skipping this CA.")
                    return

        buy_market_cap = await fetch_market_cap_with_retry(ca)
        logger.info(f"Buy Market Cap: {buy_market_cap}")

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

        priority_fee_sell = get_priority_fee()
        if DRY_RUN:
            tx_sell = "SIMULATED_SELL"
        else:
            tx_sell = jupiter_sell_token(ca, congestion_flag=(priority_fee_sell > NORMAL_TIP_SOL))
            if not tx_sell:
                for _ in range(3):
                    await asyncio.sleep(2)
                    tx_sell = jupiter_sell_token(ca, congestion_flag=(priority_fee_sell > NORMAL_TIP_SOL))
                    if tx_sell:
                        break
                if not tx_sell:
                    logger.warning("Final sell failed; skipping.")
                    return

        sell_market_cap = await fetch_market_cap_with_retry(ca)
        logger.info(f"Sell Market Cap: {sell_market_cap}")

        profit_loss_pct = 0.0
        if buy_market_cap and sell_market_cap:
            profit_loss_pct = ((sell_market_cap - buy_market_cap) / buy_market_cap) * 100

        compounded = usd_invest * (1 + (profit_loss_pct / 100.0))
        compounded_after_buffer = save_gas_reserve_after_trade(compounded, GAS_BUFFER)

        # Subtract actual tip cost (both buy & sell)
        compounded_after_tips = subtract_tip_cost(compounded_after_buffer, priority_fee_buy)
        compounded_after_tips = subtract_tip_cost(compounded_after_tips, priority_fee_sell)

        async with balance_lock:
            current_usd_balance = max(compounded_after_tips, 0.01)
            save_balance(current_usd_balance)

        daily_trades += 1

        # report summary
        report_trade_summary(ca, buy_market_cap, sell_market_cap, profit_loss_pct,
                             tx_buy, tx_sell, priority_fee_buy, priority_fee_sell,
                             current_usd_balance)

        save_processed_ca(ca)

        if daily_trades >= current_daily_limit():
            now = datetime.utcnow()
            next_cycle = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            sleep_seconds = (next_cycle - now).total_seconds()
            logger.info(
                f"Daily limit ({current_daily_limit()}) reached. Sleeping {sleep_seconds/3600:.2f}h until 00:00 UTC..."
            )
            await asyncio.sleep(sleep_seconds)

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
    """
    Continuously run the Telegram client, reconnecting if disconnected.
    """
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
    """
    Entry point for sniper bot.
    """
    logger.info("Starting sniper bot...")
    asyncio.run(start_client())

if __name__ == "__main__":
    main()
