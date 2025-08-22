import os
import time
import logging
from datetime import datetime, timedelta

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, RpcError

# Local utils
import utils
from utils import (
    DRY_RUN, usd_to_sol, get_market_cap, save_gas_reserve_after_trade,
    send_telegram_message, save_processed_ca, is_ca_processed,
    get_priority_fee
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

# Initial capital
INITIAL_USD_BALANCE = float(os.getenv("DAILY_CAPITAL_USD", 25))
current_usd_balance = INITIAL_USD_BALANCE

# Fees buffer
GAS_BUFFER = float(os.getenv("GAS_BUFFER", 0.009))  # 0.9% reserve

# SL & TP
STOP_LOSS = float(os.getenv("STOP_LOSS", "-20"))     # -20%
TAKE_PROFIT = float(os.getenv("TAKE_PROFIT", "100")) # +100%

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# -------------------------
# Telegram session
# -------------------------
session_name = "session"
client = TelegramClient(session_name, API_ID, API_HASH)

# -------------------------
# Helper: calculate next investment
# -------------------------
def get_next_investment() -> float:
    global current_usd_balance
    invest = save_gas_reserve_after_trade(current_usd_balance, GAS_BUFFER)
    return max(invest, 0.01)  # minimum $0.01

# -------------------------
# Robust market cap fetch with retries
# -------------------------
def fetch_market_cap_with_retry(ca: str, retries: int = 3, delay: int = 2) -> float:
    for attempt in range(1, retries + 1):
        mcap = get_market_cap(ca)
        if mcap:
            return mcap
        logger.warning(f"Attempt {attempt} failed to fetch market cap for {ca}. Retrying in {delay}s...")
        time.sleep(delay)
    return 0.0

# -------------------------
# Handle new Telegram messages
# -------------------------
async def handle_new_message(event):
    global current_usd_balance
    try:
        ca = utils.extract_contract_address(event.message.text if hasattr(event.message, "text") else None,
                                            message_obj=event.message)
        if not ca:
            return

        if is_ca_processed(ca):
            logger.info(f"CA already processed: {ca}")
            return

        logger.info(f"Detected new CA: {ca}")

        # Determine investment
        usd_invest = get_next_investment()
        sol_invest = usd_to_sol(usd_invest)
        priority_fee = get_priority_fee()
        sol_after_fees = max(sol_invest - priority_fee, 0)
        logger.info(f"Investing ${usd_invest:.2f} (~{sol_after_fees:.6f} SOL after fees) into {ca} with priority fee {priority_fee:.6f} SOL")

        # -------------------------
        # Buy
        # -------------------------
        tx_buy = None
        if DRY_RUN:
            logger.info(f"[DRY RUN] Buying ${usd_invest} worth of token {ca}")
            tx_buy = "SIMULATED_BUY"
        else:
            tx_buy = utils.jupiter_buy(ca, sol_after_fees, congestion=priority_fee > utils.NORMAL_PRIORITY_FEE)
            if not tx_buy:
                logger.warning("Buy failed, retrying 3 times...")
                for attempt in range(3):
                    time.sleep(2)
                    tx_buy = utils.jupiter_buy(ca, sol_after_fees, congestion=priority_fee > utils.NORMAL_PRIORITY_FEE)
                    if tx_buy:
                        break
                if not tx_buy:
                    logger.error("Final buy failed, skipping this CA.")
                    return

        buy_market_cap = fetch_market_cap_with_retry(ca)
        logger.info(f"Buy Market Cap: {buy_market_cap}")

        # -------------------------
        # Monitor for TP or SL
        # -------------------------
        while True:
            current_cap = fetch_market_cap_with_retry(ca)
            if not current_cap:
                time.sleep(20)
                continue

            profit_loss_pct = ((current_cap - buy_market_cap) / buy_market_cap) * 100

            if profit_loss_pct >= TAKE_PROFIT:
                logger.info(f"✅ Take Profit triggered: {profit_loss_pct:.2f}%")
                break

            if profit_loss_pct <= STOP_LOSS:
                logger.info(f"🛑 Stop Loss triggered: {profit_loss_pct:.2f}%")
                break

            time.sleep(20)

        # -------------------------
        # Sell
        # -------------------------
        tx_sell = None
        priority_fee_sell = get_priority_fee()
        sol_after_fees_sell = sol_after_fees - priority_fee_sell

        if DRY_RUN:
            logger.info(f"[DRY RUN] Selling token {ca}")
            tx_sell = "SIMULATED_SELL"
        else:
            tx_sell = utils.jupiter_sell(ca, congestion=priority_fee_sell > utils.NORMAL_PRIORITY_FEE)
            if not tx_sell:
                logger.warning("Sell failed, retrying 3 times...")
                for attempt in range(3):
                    time.sleep(2)
                    tx_sell = utils.jupiter_sell(ca, congestion=priority_fee_sell > utils.NORMAL_PRIORITY_FEE)
                    if tx_sell:
                        break
                if not tx_sell:
                    logger.error("Final sell failed, skipping.")
                    return

        sell_market_cap = fetch_market_cap_with_retry(ca)
        logger.info(f"Sell Market Cap: {sell_market_cap}")

        # -------------------------
        # Update balance for compounding
        # -------------------------
        delta_usd = usd_invest * (1 + profit_loss_pct / 100)
        delta_usd = save_gas_reserve_after_trade(delta_usd, GAS_BUFFER)
        current_usd_balance = max(delta_usd, 0.01)
        logger.info(f"Updated USD balance for next buy: {current_usd_balance:.2f}")

        # -------------------------
        # Telegram report
        # -------------------------
        send_telegram_message(
            f"📊 Trade Complete\nCA: {ca}\nBuy Market Cap: {buy_market_cap}\nSell Market Cap: {sell_market_cap}\nPnL: {profit_loss_pct:.2f}%\nTx Buy: {tx_buy}\nTx Sell: {tx_sell}\nPriority Fee Buy: {priority_fee:.6f} SOL\nPriority Fee Sell: {priority_fee_sell:.6f} SOL"
        )

        # -------------------------
        # Save processed CA
        # -------------------------
        save_processed_ca(ca)

        # -------------------------
        # Sleep until next 00:00 UTC
        # -------------------------
        now = datetime.utcnow()
        next_cycle = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        sleep_seconds = (next_cycle - now).total_seconds()
        logger.info(f"Sleeping {sleep_seconds/3600:.2f} hours until next cycle (00:00 UTC)...")
        time.sleep(sleep_seconds)

    except (RpcError, FloodWaitError) as rpc_e:
        logger.warning(f"Telethon RPC error: {rpc_e}. Reconnecting in 5s...")
        time.sleep(5)
        await handle_new_message(event)
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
            time.sleep(5)

# -------------------------
# Main
# -------------------------
def main():
    import asyncio
    logger.info("Starting sniper bot...")
    asyncio.run(start_client())

if __name__ == "__main__":
    main()