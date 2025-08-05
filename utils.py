import requests
import os
import math
from dotenv import load_dotenv
from telethon.sync import TelegramClient

load_dotenv()

# --- SOL Price Conversion ---
def get_sol_price_usd():
    try:
        response = requests.get("https://price.jup.ag/v4/price?ids=SOL")
        response.raise_for_status()
        data = response.json()
        return float(data['data']['SOL']['price'])
    except Exception as e:
        print(f"[!] Error getting SOL price: {e}")
        return None

# --- Gas Fee Calculation (0.9% + priority fee) ---
def calculate_total_gas_fee(amount_in_usd, congestion=False):
    base_fee = amount_in_usd * 0.009  # 0.9% gas
    priority_fee_sol = 0.3 if congestion else 0.03
    sol_price = get_sol_price_usd()
    priority_fee_usd = priority_fee_sol * sol_price if sol_price else 0
    total_fee = base_fee + priority_fee_usd
    return round(total_fee, 4)

# --- Market Cap from Dexscreener ---
def get_market_cap_from_dexscreener(contract_address):
    try:
        url = f"https://api.dexscreener.io/latest/dex/pairs/solana/{contract_address}"
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        return float(data['pair']['fdv']) if 'pair' in data else None
    except Exception as e:
        print(f"[!] Error fetching market cap: {e}")
        return None

# --- Telegram Messaging (Optional) ---
def send_telegram_message(text):
    try:
        from telethon.sync import TelegramClient
        api_id = int(os.getenv("TELEGRAM_API_ID"))
        api_hash = os.getenv("TELEGRAM_API_HASH")
        chat_id = int(os.getenv("TELEGRAM_CHAT_ID"))
        with TelegramClient('session_name', api_id, api_hash) as client:
            client.send_message(chat_id, text)
    except Exception as e:
        print(f"[!] Error sending Telegram message: {e}")
