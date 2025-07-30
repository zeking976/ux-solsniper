sniper.py

import os import random import json import requests import time import datetime from telethon import TelegramClient, events from solana.rpc.api import Client from solana.keypair import Keypair from base64 import b64decode

ENV VARS

TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID")) TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH") TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") TELEGRAM_REPORT_CHAT_ID = os.getenv("TELEGRAM_REPORT_CHAT_ID") PHANTOM_PRIVATE_KEY = os.getenv("PHANTOM_PRIVATE_KEY"))

CONSTANTS

NUMBER_OF_BUYS_PER_DAY = 7 JUPITER_SWAP_API = "https://quote-api.jup.ag" SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com" WATCH_CHANNEL = "your_channel_name_here"  # Without @

INIT

client = TelegramClient("anon", TELEGRAM_API_ID, TELEGRAM_API_HASH) solana_client = Client(SOLANA_RPC_URL) wallet = Keypair.from_secret_key(b64decode(PHANTOM_PRIVATE_KEY)) buy_log = [] sell_log = []

Get live SOL price

def get_sol_price(): try: response = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd") return response.json()["solana"]["usd"] except: return 180.0  # fallback

Random daily total in USD between $880-$940

def get_daily_usd_total(): return round(random.uniform(880, 940), 2)

def get_random_usd_amounts(total_usd): chunks = [random.uniform(1, 3) for _ in range(NUMBER_OF_BUYS_PER_DAY)] total = sum(chunks) return [round((amt / total) * total_usd, 2) for amt in chunks]

Placeholder for token buy via Jupiter

def buy_token(ca, sol_amount): print(f"[BUY] Buying token {ca} with {sol_amount:.4f} SOL") buy_log.append({"token": ca, "sol": sol_amount, "usd": round(sol_amount * get_sol_price(), 2), "time": time.time()})

Simulated 10x profit sell

def check_and_sell(): for b in buy_log: if random.random() < 0.2: sell_log.append({"token": b["token"], "usd_gain": b["usd"] * 10, "usd_spent": b["usd"], "time": time.time()}) print(f"[SELL] Selling {b['token']} for profit")

Send reports

def send_report(msg): url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage" data = {"chat_id": TELEGRAM_REPORT_CHAT_ID, "text": msg, "parse_mode": "Markdown"} requests.post(url, data=data)

def daily_summary(): today = datetime.datetime.utcnow().date() spent = sum(b['usd'] for b in buy_log if datetime.datetime.utcfromtimestamp(b['time']).date() == today) gained = sum(s['usd_gain'] for s in sell_log if datetime.datetime.utcfromtimestamp(s['time']).date() == today) net = gained - spent send_report(f"📊 Daily Report: Spent: ${spent:.2f} Gained: ${gained:.2f} Profit: ${net:.2f}")

def monthly_summary(): now = datetime.datetime.utcnow() spent = sum(b['usd'] for b in buy_log if datetime.datetime.utcfromtimestamp(b['time']).month == now.month) gained = sum(s['usd_gain'] for s in sell_log if datetime.datetime.utcfromtimestamp(s['time']).month == now.month) net = gained - spent send_report(f"📆 Monthly Report: Spent: ${spent:.2f} Gained: ${gained:.2f} Profit: ${net:.2f}")

Telegram Listener

@client.on(events.NewMessage(chats=WATCH_CHANNEL)) async def handler(event): msg = event.message.message if "0x" in msg or len(msg) > 35: parts = msg.split() for p in parts: if p.startswith("0x") or len(p) > 35: ca = p.strip() print(f"[SNIPE] Contract address: {ca}") total_usd_today = get_daily_usd_total() usd_values = get_random_usd_amounts(total_usd_today) sol_price = get_sol_price() sol_values = [round(usd / sol_price, 4) for usd in usd_values] for sol_amt in sol_values: buy_token(ca, sol_amt) time.sleep(random.uniform(1, 3))

Run

print("Sniper bot running...") client.start()

Schedule daily and monthly reports

from threading import Thread

def reporting_loop(): while True: now = datetime.datetime.utcnow() if now.hour == 23 and now.minute == 59: daily_summary() if now.day == 1 and now.hour == 0 and now.minute == 5: monthly_summary() time.sleep(60)

Thread(target=reporting_loop, daemon=True).start() client.run_until_disconnected()

