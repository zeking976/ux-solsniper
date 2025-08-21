import os
import json
import time
import random
import logging
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv
from telethon.sync import TelegramClient, events

# Local imports
import utils

# Load environment
dotenv_path = os.path.expanduser("~/t.env")
load_dotenv(dotenv_path=dotenv_path)

# Env vars
API_ID = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TARGET_CHANNEL_ID = int(os.getenv("TARGET_CHANNEL_ID"))
DRY_RUN = int(os.getenv("DRY_RUN", "1"))  # 1 = simulate, 0 = real
RPC_URL = os.getenv("SOLANA_RPC")

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Globals
DAILY_START_CAPITAL = 25  # Starting capital in USD
INVESTMENT_MULTIPLIER = 2  # Target 2x sell
GAS_BUFFER = 0.009  # 0.9% reserved for gas
session_name = "session"

# Telegram client
client = TelegramClient(session_name, API_ID, API_HASH)


async def handle_new_message(event):
    """
    Triggered whenever a new message is detected in the target channel.
    Extract contract address and start buy/sell cycle.
    """
    try:
        message = event.message.message
        ca = utils.extract_contract_address(message)
        if not ca:
            return

        logger.info(f"Detected CA: {ca}")

        # Decide investment amount for the day
        usd_invest = utils.get_today_investment()
        sol_invest = utils.usd_to_sol(usd_invest, RPC_URL)

        # Deduct buffer for gas
        sol_after_fees = sol_invest * (1 - GAS_BUFFER)

        # Execute buy
        tx_buy = None
        if DRY_RUN:
            logger.info(f"[DRY RUN] Buying {usd_invest}$ worth of token at {ca}")
        else:
            tx_buy = utils.buy_token(ca, sol_after_fees, congestion=False)

        if not DRY_RUN and not tx_buy:
            logger.error("Buy transaction failed, retrying...")
            for _ in range(3):
                time.sleep(2)
                tx_buy = utils.buy_token(ca, sol_after_fees, congestion=True)
                if tx_buy:
                    break
            if not tx_buy:
                logger.error("Final buy failed after retries.")
                return

        buy_market_cap = utils.get_market_cap(ca)
        logger.info(f"Buy market cap: {buy_market_cap}")

        # Wait for 2x market cap
        while True:
            current_cap = utils.get_market_cap(ca)
            if current_cap and current_cap >= buy_market_cap * INVESTMENT_MULTIPLIER:
                break
            time.sleep(30)

        # Execute sell
        tx_sell = None
        if DRY_RUN:
            logger.info(f"[DRY RUN] Selling token {ca} at 2x market cap")
        else:
            tx_sell = utils.sell_token(ca, congestion=False)

        if not DRY_RUN and not tx_sell:
            logger.error("Sell transaction failed, retrying...")
            for _ in range(3):
                time.sleep(2)
                tx_sell = utils.sell_token(ca, congestion=True)
                if tx_sell:
                    break
            if not tx_sell:
                logger.error("Final sell failed after retries.")
                return

        sell_market_cap = utils.get_market_cap(ca)
        logger.info(f"Sell market cap: {sell_market_cap}")

        # Report
        utils.send_telegram_message(
            BOT_TOKEN,
            CHAT_ID,
            f"✅ Trade Complete\nCA: {ca}\nBuy: {buy_market_cap}\nSell: {sell_market_cap}\nTx Buy: {tx_buy}\nTx Sell: {tx_sell}"
        )

        # Sleep until next cycle 00:00 UTC
        now = datetime.utcnow()
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        sleep_seconds = (tomorrow - now).total_seconds()
        logger.info(f"Sleeping for {sleep_seconds/3600:.2f} hours until next cycle.")
        time.sleep(sleep_seconds)

    except Exception as e:
        logger.error(f"Error in handle_new_message: {e}")


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