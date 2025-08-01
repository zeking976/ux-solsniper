import os import json import random import asyncio import logging from datetime import datetime

import requests from dotenv import load_dotenv from telethon import TelegramClient, events from solana.keypair import Keypair from solana.rpc.async_api import AsyncClient from base58 import b58decode from jupiter_python import JupiterAggregator, TokenSwap

Load environment variables

load_dotenv(".env")

Env variables

TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID")) TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH") TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID")) TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") PRIVATE_KEY = os.getenv("PRIVATE_KEY") DEXSCREENER_API = os.getenv("DEXSCREENER_API", "https://api.dexscreener.com/latest/dex/pairs/solana/") TARGET_CHANNEL = os.getenv("TARGET_CHANNEL")

Bot tracking variables

starting_balance = 10.0  # USD current_balance = starting_balance

Setup logging

logging.basicConfig(level=logging.INFO) logger = logging.getLogger(name)

Init wallet and Solana connection

wallet = Keypair.from_secret_key(b58decode(PRIVATE_KEY)) solana_client = AsyncClient("https://api.mainnet-beta.solana.com") jupiter = JupiterAggregator(wallet, solana_client)

Telegram client

tg_client = TelegramClient("session", TELEGRAM_API_ID, TELEGRAM_API_HASH)

Send Telegram message

def send_telegram_message(text): url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage" data = {"chat_id": TELEGRAM_CHAT_ID, "text": text} requests.post(url, data=data)

async def get_token_info(ca: str): try: response = requests.get(f"{DEXSCREENER_API}{ca}") data = response.json() if "pair" in data: return { "name": data["pair"].get("baseToken", {}).get("name", "Unknown"), "symbol": data["pair"].get("baseToken", {}).get("symbol", ""), "mcap": float(data["pair"].get("fdv", 0)), "token_address": ca } return None except Exception as e: logger.error(f"Error fetching Dexscreener info: {e}") return None

async def perform_token_swap(input_amount_usd: float, output_token: str, direction: str): try: sol_price = await jupiter.get_sol_price() input_amount = input_amount_usd / sol_price

swap = TokenSwap(
        wallet=wallet,
        client=solana_client,
        amount=input_amount,
        input_token="SOL",
        output_token=output_token if direction == "buy" else "SOL",
        slippage=1.0
    )
    tx_sig = await jupiter.swap(swap)
    return tx_sig
except Exception as e:
    logger.error(f"Error performing token swap: {e}")
    return None

async def buy_and_monitor(ca: str): global current_balance

info = await get_token_info(ca)
if not info or not info["mcap"]:
    logger.warning("Invalid market cap info. Skipping.")
    return

buy_time = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
buy_mcap = info["mcap"]
target_mcap = buy_mcap * 1.5
token_name = info["name"]
token_symbol = info["symbol"]

tx_buy = await perform_token_swap(current_balance, info["token_address"], direction="buy")
if not tx_buy:
    logger.error("Buy transaction failed.")
    return

send_telegram_message(f"\ud83d\udcc8 BUY\nCoin: {token_name} ({token_symbol})\nCA: {ca}\nBuy Time: {buy_time} UTC\n"
                      f"Market Cap: ${buy_mcap:,.0f}\nAmount: ${current_balance:.2f}\n"
                      f"Tx: https://solscan.io/tx/{tx_buy}")

while True:
    await asyncio.sleep(30)
    current_info = await get_token_info(ca)
    if current_info and current_info["mcap"] >= target_mcap:
        break

sell_time = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
sell_mcap = current_info["mcap"]

tx_sell = await perform_token_swap(current_balance, ca, direction="sell")
if not tx_sell:
    logger.error("Sell transaction failed.")
    return

current_balance *= 1.5

send_telegram_message(f"\ud83d\udcc9 SELL\nSell Time: {sell_time} UTC\nMarket Cap: ${sell_mcap:,.0f}\n"
                      f"Amount: ${current_balance:.2f}\nTx: https://solscan.io/tx/{tx_sell}\n\u27f3 Reinvesting tomorrow.")

@tg_client.on(events.NewMessage(chats=TARGET_CHANNEL)) async def handle_message(event): global current_balance

text = event.raw_text
cas = [x for x in text.split() if len(x) == 44 and x.startswith("So")]
if cas:
    logger.info(f"Found contract address: {cas[0]}")
    await buy_and_monitor(cas[0])
    logger.info("Sleeping 23.5 hours before next reinvestment...")
    await asyncio.sleep(84600)

async def main(): while True: try: await tg_client.start() logger.info("Bot started.") await tg_client.run_until_disconnected() except Exception as e: logger.error(f"Bot crashed: {e}") await asyncio.sleep(10)

if name == "main": asyncio.run(main())

