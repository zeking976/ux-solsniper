import os
import json
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Union, Any

# Telethon for scraping the channel
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, SessionPasswordNeededError, PhoneNumberInvalidError

# Local utils (all helpers, RPC, signing, pricing, reporting)
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
    BUY_FEE_PERCENT,
    SELL_FEE_PERCENT,
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

# --- Telegram client (session file portable across VPS/Termux) ---
session_name = os.path.join(os.getcwd(), os.getenv("SESSION_NAME", "sniper_session"))
client = None

async def initialize_telegram_client():
    global client
    session_path = f"{session_name}.session"
    if not os.path.exists(session_path):
        logger.error(f"Telegram session file {session_path} not found. Please provide a valid session file.")
        raise FileNotFoundError(f"Missing session file: {session_path}")
    try:
        client = TelegramClient(session_name, API_ID, API_HASH)
        await client.start()
        logger.info("Telegram client initialized successfully.")
    except (SessionPasswordNeededError, PhoneNumberInvalidError) as e:
        logger.error(f"Telegram client initialization failed: {e}. Please ensure valid session file.")
        raise
    except Exception as e:
        logger.error(f"Unexpected error initializing Telegram client: {e}")
        raise

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
    global current_usd_balance
    invest = save_gas_reserve_after_trade(current_usd_balance, 0.009)  # Default 0.9% reserve
    return max(invest, 0.01)

def calculate_gas_fee_sol_from_priority(amount_usd: float, congestion: bool) -> float:
    """Calculate fee in SOL based on percentage of transaction amount."""
    fee_percent = SELL_FEE_PERCENT if amount_usd < 0 else BUY_FEE_PERCENT
    fee_usd = abs(amount_usd) * (fee_percent / 100)
    sol_price = utils.get_sol_price_usd()
    return fee_usd / sol_price if sol_price > 0 else 0.0

def estimate_fee_and_adjust_usd(usd_amount: float, congestion: Optional[bool]) -> float:
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
    try:
        usd_per_sol = usd_to_sol(1)
        if not usd_per_sol or usd_per_sol <= 0:
            return usd_balance
        usd_cost = tip_sol / usd_per_sol
        return max(usd_balance - usd_cost, 0.01)
    except Exception:
        return usd_balance

def handle_cycle_reset() -> None:
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
    send_telegram_message(
        f"📊 Trade Complete\n"
        f"CA: {ca}\n"
        f"Buy MC: {buy_mc}\n"
        f"Sell MC: {sell_mc}\n"
        f"PnL: {pnl:.2f}%\n"
        f"Tx Buy: {tx_buy}\n"
        f"Tx Sell: {tx_sell}\n"
        f"Buy Fee: {fee_buy_sol:.6f} SOL ({BUY_FEE_PERCENT}%)\n"
        f"Sell Fee: {fee_sell_sol:.6f} SOL ({SELL_FEE_PERCENT}%)\n"
        f"Balance after compounding: ${balance:.2f}"
    )

# -------------------------
# Main message handler
# -------------------------
async def handle_new_message(event) -> None:
    global current_usd_balance, daily_trades

    try:
        message = event.message
        if not message or not hasattr(message, 'text') or not message.text:
            logger.warning("Received empty or invalid message event.")
            return

        contract_address = utils.extract_contract_address(message_text=message.text, message_obj=message)
        if not contract_address:
            logger.info("No valid contract address found in message: %s", message.text[:50])
            return

        if is_ca_processed(contract_address):
            logger.info("Contract address %s already processed, skipping.", contract_address)
            return

        logger.info("New contract address detected: %s", contract_address)

        market_cap = await fetch_market_cap_with_retry(contract_address)
        if market_cap == 0.0:
            logger.warning("Failed to fetch market cap for %s after retries, skipping.", contract_address)
            save_processed_ca(contract_address)
            return
        elif market_cap > 1_000_000:
            logger.warning("Market cap %f USD too high for sniper, skipping.", market_cap)
            save_processed_ca(contract_address)
            return

        async with balance_lock:
            invest_usd = get_next_investment()
            if invest_usd >= current_usd_balance:
                logger.warning("Insufficient balance for investment: %f vs %f", invest_usd, current_usd_balance)
                return

            congestion = utils.detect_network_congestion()
            adjusted_usd = estimate_fee_and_adjust_usd(invest_usd, congestion)
            sol_amount = usd_to_sol(adjusted_usd)
            if not sol_amount or sol_amount <= 0:
                logger.error("Invalid SOL amount calculated: %f", sol_amount)
                return

            priority_fee_sol = calculate_gas_fee_sol_from_priority(adjusted_usd, congestion)
            tx_signature = jupiter_buy_token(contract_address, sol_amount, congestion)
            if not tx_signature or (DRY_RUN and tx_signature == "SIMULATED_TX_SIGNATURE"):
                logger.error("Buy execution failed or in DRY_RUN mode: %s", tx_signature)
                if not DRY_RUN:
                    save_processed_ca(contract_address)
                return

            current_usd_balance = subtract_tip_cost(current_usd_balance, priority_fee_sol)
            save_balance(current_usd_balance)

            utils.record_buy(
                token=contract_address,
                coin_name=utils.resolve_token_name(contract_address),
                buy_market_cap=market_cap,
                amount_usd=adjusted_usd,
                priority_fee_sol=priority_fee_sol
            )
            daily_trades += 1
            logger.info("Buy executed successfully, TX: %s, Daily trades: %d/%d", tx_signature, daily_trades, current_daily_limit())

            save_processed_ca(contract_address)

            await check_sell_condition(contract_address, market_cap, tx_signature, priority_fee_sol)

    except FloodWaitError as e:
        logger.warning("Flood wait detected: %d seconds. Pausing...", e.seconds)
        await asyncio.sleep(e.seconds)
    except Exception as e:
        logger.exception("Error in handle_new_message: %s", str(e))
        return

# -------------------------
# Sell condition checker
# -------------------------
async def check_sell_condition(contract_address: str, buy_market_cap: float, tx_buy: str, fee_buy_sol: float) -> None:
    global current_usd_balance, daily_trades

    try:
        async with balance_lock:
            while True:
                token_balance = get_token_balance_lamports(contract_address)
                if token_balance <= 0:
                    logger.info("No token balance to sell for %s, exiting loop.", contract_address)
                    break

                sell_market_cap = await fetch_market_cap_with_retry(contract_address)
                if sell_market_cap == 0.0:
                    logger.warning("Failed to fetch sell market cap, retrying...")
                    await asyncio.sleep(5)
                    continue

                profit_percent = ((sell_market_cap - buy_market_cap) / buy_market_cap) * 100
                if profit_percent >= TAKE_PROFIT:
                    logger.info("Take Profit hit: %.2f%% >= %.2f%%", profit_percent, TAKE_PROFIT)
                elif profit_percent <= STOP_LOSS:
                    logger.info("Stop Loss hit: %.2f%% <= %.2f%%", profit_percent, STOP_LOSS)
                else:
                    logger.debug("Holding %s, profit: %.2f%% (TP: %.2f%%, SL: %.2f%%)", contract_address, profit_percent, TAKE_PROFIT, STOP_LOSS)
                    await asyncio.sleep(60)
                    continue

                congestion = utils.detect_network_congestion()
                priority_fee_sol = calculate_gas_fee_sol_from_priority(-sell_market_cap, congestion)
                tx_sell = jupiter_sell_token(contract_address, congestion)
                if not tx_sell or (DRY_RUN and tx_sell == "SIMULATED_TX_SIGNATURE"):
                    logger.error("Sell execution failed or in DRY_RUN mode: %s", tx_sell)
                    if not DRY_RUN:
                        break
                    continue

                profit_usd = (sell_market_cap - buy_market_cap) * (token_balance / LAMPORTS_PER_SOL) * (1 / buy_market_cap)
                current_usd_balance = subtract_tip_cost(current_usd_balance + profit_usd, priority_fee_sol)
                save_balance(current_usd_balance)

                utils.record_sell(contract_address, sell_market_cap, profit_usd, priority_fee_sol)
                report_trade_summary(
                    ca=contract_address,
                    buy_mc=buy_market_cap,
                    sell_mc=sell_market_cap,
                    pnl=profit_percent,
                    tx_buy=tx_buy,
                    tx_sell=tx_sell,
                    fee_buy_sol=fee_buy_sol,
                    fee_sell_sol=priority_fee_sol,
                    balance=current_usd_balance
                )
                daily_trades += 1
                logger.info("Sell executed successfully, TX: %s, Daily trades: %d/%d", tx_sell, daily_trades, current_daily_limit())
                break

    except Exception as e:
        logger.exception("Error in check_sell_condition for %s: %s", contract_address, str(e))

# -------------------------
# Main entry point
# -------------------------
async def main():
    try:
        await initialize_telegram_client()
        client.add_event_handler(handle_new_message, events.NewMessage(chats=TARGET_CHANNEL_ID))
        logger.info("Starting Telegram client for event handling...")
        await client.run_until_disconnected()
    except Exception as e:
        logger.error("Main loop error: %s. Reconnecting in 10 seconds...", e)
        await asyncio.sleep(10)
        await main()

if __name__ == "__main__":
    asyncio.run(main())