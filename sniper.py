sniper.py

import os import time import random import requests import json from datetime import datetime, timedelta from solana.rpc.api import Client from solana.transaction import Transaction from solana.publickey import PublicKey from solana.keypair import Keypair from solana.system_program import transfer, TransferParams from telegram import Bot

Load ENV

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") TELEGRAM_USER_ID = os.getenv("TELEGRAM_USER_ID") PRIVATE_KEY = os.getenv("PRIVATE_KEY") SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")

client = Client(SOLANA_RPC) bot = Bot(token=TELEGRAM_BOT_TOKEN)

Constants

DAILY_USD_INVESTMENT = 910 BUY_COUNT = 7 CONGESTION_THRESHOLD_MS = 1500 NORMAL_PRIORITY_FEE = 0.03 HIGH_PRIORITY_FEE = 0.2

Stats trackers

total_profit = 0 daily_profit = 0 paid_high_priority_today = False

Helper functions

def get_current_sol_price(): url = "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd" res = requests.get(url) return res.json()["solana"]["usd"]

def get_priority_fee(): latency = client.get_health() if latency.get("result") != "ok": return HIGH_PRIORITY_FEE, True return NORMAL_PRIORITY_FEE, False

def send_report(text): try: bot.send_message(chat_id=TELEGRAM_USER_ID, text=text) except Exception as e: print("Failed to send Telegram message:", e)

def generate_random_usd_amounts(total, parts): weights = [random.uniform(0.8, 1.2) for _ in range(parts)] factor = total / sum(weights) return [round(w * factor, 2) for w in weights]

def buy_token(contract_address, sol_amount): print(f"[BUY] Token: {contract_address} | Amount: {sol_amount} SOL") # Placeholder - Replace with real Jupiter API integration return True

def sell_token(contract_address): print(f"[SELL] Token: {contract_address}") # Placeholder - Replace with real Jupiter API integration return True

def monitor_and_trade(): global daily_profit, total_profit, paid_high_priority_today

daily_profit = 0
paid_high_priority_today = False
sol_price = get_current_sol_price()
daily_amounts_usd = generate_random_usd_amounts(DAILY_USD_INVESTMENT, BUY_COUNT)
sol_amounts = [round(usd / sol_price, 4) for usd in daily_amounts_usd]

for i in range(BUY_COUNT):
    contract_address = f"FakeTokenCA_{random.randint(1000,9999)}"  # Mocked extraction
    priority_fee, high_congestion = get_priority_fee()

    if high_congestion:
        paid_high_priority_today = True

    success = buy_token(contract_address, sol_amounts[i])
    if success:
        time.sleep(random.randint(5, 10))  # Simulate delay
        sell_token(contract_address)
        profit = round(random.uniform(-5, 20), 2)  # Mocked profit
        daily_profit += profit
        total_profit += profit

report = f"\n🧾 DAILY REPORT ({datetime.utcnow().strftime('%Y-%m-%d')}):\n"
report += f"Profit/Loss: ${daily_profit}\n"
report += f"High Congestion Tipping: {'Yes (0.2 SOL paid)' if paid_high_priority_today else 'No'}\n"
send_report(report)

def monthly_report(): report = f"\n📅 MONTHLY REPORT ({datetime.utcnow().strftime('%B %Y')}):\n" report += f"Total Profit/Loss: ${total_profit}\n" send_report(report)

Main loop

last_month = datetime.utcnow().month

while True: now = datetime.utcnow() monitor_and_trade()

if now.month != last_month:
    monthly_report()
    last_month = now.month

time.sleep(86400)  # Wait until next day

