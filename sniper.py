import os
import json
import random
import asyncio
import logging
from datetime import datetime, timedelta
import requests

from dotenv import load_dotenv
from telethon import TelegramClient, events
from solana.keypair import Keypair
from solana.rpc.async_api import AsyncClient
from base58 import b58decode
from jupiter_python import JupiterAggregator, TokenSwap

load_dotenv(".env")

# Env
TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
DEXSCREENER_API = os.getenv("DEXSCREENER_API", "https://api.dexscreener.com/latest/dex/pairs/solana/")
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL")

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sniper")

# Wallets & Solana
wallet = Keypair.from_secret_key(b58decode(PRIVATE_KEY))
solana_client = AsyncClient("https://api.mainnet-beta.solana.com")
jupiter = JupiterAggregator(wallet, solana_client)

# Telegram
tg_client = TelegramClient("session", TELEGRAM_API_ID, TELEGRAM_API_HASH)

def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    requests.post(url, data=data)

async def get_token_info(ca: str):
    try:
        response = requests.get(f"{DEXSCREENER_API}{ca}")
        data = response.json()
        if "pair" in data:
            return {
                "name": data["pair"].get("baseToken", {}).get("name", "Unknown"),
                "symbol": data["pair"].get("baseToken", {}).get("symbol", ""),
                "mcap": float(data["pair"].get("fdv", 0)),
                "token_address": ca
            }
        return None
    except Exception as e:
        logger.error(f"Dexscreener error: {e}")
        return None

async def get_sol_price():
    try:
        price_data = await jupiter.get_sol_price()
        return price_data
    except Exception as e:
        logger.error(f"SOL price fetch failed: {e}")
        return 0

async def perform_token_swap(amount_usd, output_token, direction):
    for _ in range(3):  # Retry up to 3x
        try:
            sol_price = await get_sol_price()
            if sol_price == 0:
                return None

            amount_sol = amount_usd / sol_price
            congestion = await jupiter.is_network_congested()
            priority_fee = 0.3 if congestion else 0.03

            swap = TokenSwap(
                wallet=wallet,
                client=solana_client,
                amount=amount_sol,
                input_token="SOL" if direction == "buy" else output_token,
                output_token=output_token if direction == "buy" else "SOL",
                slippage=1.0,
                priority_fee=priority_fee
            )

            tx_sig = await jupiter.swap(swap)
            return tx_sig
        except Exception as e:
            logger.error(f"Swap {direction} failed: {e}")
            await asyncio.sleep(5)
    return None

async def buy_and_monitor(ca):
    sol_price = await get_sol_price()
    if sol_price == 0:
        return

    gas_fee_percent = 0.009
    usd_capital = 55.0
    adjusted_usd = usd_capital * (1 - gas_fee_percent)

    info = await get_token_info(ca)
    if not info or not info["mcap"]:
        logger.warning("Market cap not found.")
        return

    buy_time = datetime.utcnow()
    buy_mcap = info["mcap"]
    target_mcap = buy_mcap * 1.5
    token_name = info["name"]
    token_symbol = info["symbol"]

    tx_buy = await perform_token_swap(adjusted_usd, ca, "buy")
    if not tx_buy:
        logger.error("Buy failed.")
        return

    send_telegram(f"🟢 BUY\nCoin: {token_name} ({token_symbol})\nCA: {ca}\n"
                  f"Time: {buy_time.strftime('%Y-%m-%d %H:%M:%S')} UTC\nMCap: ${buy_mcap:,.0f}\n"
                  f"Tx: https://solscan.io/tx/{tx_buy}")

    while True:
        await asyncio.sleep(30)
        current_info = await get_token_info(ca)
        if current_info and current_info["mcap"] >= target_mcap:
            break

    sell_time = datetime.utcnow()
    duration = (sell_time - buy_time).total_seconds()
    tx_sell = await perform_token_swap(adjusted_usd * 1.5, ca, "sell")
    if not tx_sell:
        logger.error("Sell failed.")
        return

    send_telegram(f"🔴 SELL\nCoin: {token_name} ({token_symbol})\nTime: {sell_time.strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
                  f"MCap: ${current_info['mcap']:,.0f}\nTx: https://solscan.io/tx/{tx_sell}")

    # Sleep till next 00:00 UTC
    now = datetime.utcnow()
    next_day = datetime.combine(now + timedelta(days=1), datetime.min.time())
    seconds_left = (next_day - now).total_seconds()
    remaining = max(0, seconds_left - duration)

    logger.info(f"Sleeping {int(remaining)} seconds until next cycle.")
    await asyncio.sleep(remaining)

@tg_client.on(events.NewMessage(chats=TARGET_CHANNEL))
async def handler(event):
    text = event.raw_text
    cas = [x for x in text.split() if len(x) == 44 and x.startswith("So")]
    if cas:
        logger.info(f"Detected CA: {cas[0]}")
        await buy_and_monitor(cas[0])

async def main():
    while True:
        try:
            await tg_client.start()
            logger.info("Bot started.")
            await tg_client.run_until_disconnected()
        except Exception as e:
            logger.error(f"Bot crashed: {e}")
            await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(main())
