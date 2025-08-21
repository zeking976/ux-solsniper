import os
import time
import logging
from datetime import datetime, timedelta

from telethon.sync import TelegramClient, events

# Local utils
import utils
from utils import DRY_RUN

# Load environment
dotenv_path = os.path.expanduser("~/t.env")
utils.load_dotenv(dotenv_path=dotenv_path)  # ensure t.env loaded

# -------------------------
# Env vars
# -------------------------
API_ID = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TARGET_CHANNEL_ID = int(os.getenv("TARGET_CHANNEL_ID"))

# Daily capital and multiplier (manual in t.env)
DAILY_CAPITAL_USD = float(os.getenv("DAILY_CAPITAL_USD", 25))
INVESTMENT_MULTIPLIER = float(os.getenv("INVESTMENT_MULTIPLIER", 2))  # 2x default
GAS_BUFFER = float(os.getenv("GAS_BUFFER", 0.009))  # 0.9% buffer for gas
MAX_BUYS_PER_DAY = int(os.getenv("MAX_BUYS_PER_DAY", 5))  # max buy cycles per day

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Telegram session
session_name = "session"
client = TelegramClient(session_name, API_ID, API_HASH)


def calculate_compound_investment(base_capital: float, current_buy_index: int, max_buys: int) -> float:
    """
    Calculate compounded investment for the current buy cycle.
    Example: 3 buys/day: [33%, 33%, 34%] of total capital
    """
    percent_per_buy = base_capital / max_buys
    return percent_per_buy


async def handle_new_message(event):
    try:
        message = event.message.message
        ca = utils.extract_contract_address(message)
        if not ca:
            return

        if utils.is_ca_processed(ca):
            logger.info(f"CA already processed: {ca}")
            return

        logger.info(f"Detected new CA: {ca}")

        for buy_index in range(1, MAX_BUYS_PER_DAY + 1):
            usd_invest = calculate_compound_investment(DAILY_CAPITAL_USD, buy_index, MAX_BUYS_PER_DAY)
            sol_invest = utils.usd_to_sol(usd_invest, utils.RPC_URL)
            sol_after_fees = sol_invest * (1 - GAS_BUFFER)

            logger.info(f"Buy #{buy_index}: Investing {usd_invest}$ ({sol_after_fees} SOL) into {ca}")

            tx_buy = None
            if DRY_RUN:
                logger.info(f"[DRY RUN] Buying {usd_invest}$ worth of token {ca}")
                tx_buy = f"SIMULATED_BUY_{buy_index}"
            else:
                tx_buy = utils.jupiter_buy(ca, sol_after_fees)

                if not tx_buy:
                    logger.warning("Buy failed, retrying 3 times...")
                    for attempt in range(3):
                        time.sleep(2)
                        tx_buy = utils.jupiter_buy(ca, sol_after_fees, retries=1)
                        if tx_buy:
                            break
                    if not tx_buy:
                        logger.error("Final buy failed, skipping this buy.")
                        continue

            buy_market_cap = utils.get_market_cap(ca)
            logger.info(f"Buy Market Cap: {buy_market_cap}")

            # Wait until token reaches target multiplier
            target_cap = buy_market_cap * INVESTMENT_MULTIPLIER
            while True:
                current_cap = utils.get_market_cap(ca)
                if current_cap and current_cap >= target_cap:
                    logger.info(f"Target reached: {current_cap} >= {target_cap}")
                    break
                time.sleep(30)

            # Execute sell
            tx_sell = None
            if DRY_RUN:
                logger.info(f"[DRY RUN] Selling token {ca} at {INVESTMENT_MULTIPLIER}x market cap")
                tx_sell = f"SIMULATED_SELL_{buy_index}"
            else:
                tx_sell = utils.jupiter_sell(ca)
                if not tx_sell:
                    logger.warning("Sell failed, retrying 3 times...")
                    for attempt in range(3):
                        time.sleep(2)
                        tx_sell = utils.jupiter_sell(ca, retries=1)
                        if tx_sell:
                            break
                    if not tx_sell:
                        logger.error("Final sell failed, skipping.")
                        continue

            sell_market_cap = utils.get_market_cap(ca)
            logger.info(f"Sell Market Cap: {sell_market_cap}")

            # Telegram report
            utils.send_telegram_message(
                BOT_TOKEN,
                CHAT_ID,
                f"✅ Trade Complete (Buy #{buy_index})\n"
                f"CA: {ca}\n"
                f"Buy Market Cap: {buy_market_cap}\n"
                f"Sell Market Cap: {sell_market_cap}\n"
                f"Tx Buy: {tx_buy}\n"
                f"Tx Sell: {tx_sell}"
            )

            # Save processed CA
            utils.save_processed_ca(ca)

        # Sleep until next cycle (00:00 UTC)
        now = datetime.utcnow()
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        sleep_seconds = (tomorrow - now).total_seconds()
        logger.info(f"Sleeping {sleep_seconds / 3600:.2f} hours until next cycle...")
        time.sleep(sleep_seconds)

    except Exception as e:
        logger.exception(f"Error in handle_new_message: {e}")


def main():
    logger.info("Starting sniper bot...")

    with client:
        client.add_event_handler(
            handle_new_message,
            events.NewMessage(chats=TARGET_CHANNEL_ID)
        )
        logger.info("Listening for new messages...")
        client.run_until_disconnected()


if __name__ == "__main__":
    main()