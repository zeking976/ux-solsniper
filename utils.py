import requests
import os
from dotenv import load_dotenv
from telethon.sync import TelegramClient

load_dotenv()

# --- Get Live SOL Price (Primary: Jupiter, Fallback: CoinGecko) ---
def get_sol_price_usd():
    try:
        response = requests.get("https://price.jup.ag/v4/price?ids=SOL", timeout=10)
        response.raise_for_status()
        data = response.json()
        return float(data['data']['SOL']['price'])
    except Exception as e:
        print(f"[!] Jupiter SOL price API failed: {e}")
        print("[*] Trying CoinGecko as fallback...")
        try:
            fallback = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd", timeout=10)
            fallback.raise_for_status()
            data = fallback.json()
            return float(data['solana']['usd'])
        except Exception as fallback_e:
            print(f"[!] CoinGecko fallback failed: {fallback_e}")
            return None

# --- Gas Fee Estimation (0.9% base + priority fee) ---
def calculate_total_gas_fee(amount_in_usd, congestion=False):
    try:
        sol_price = get_sol_price_usd()
        if sol_price is None:
            print("[!] Cannot calculate gas fee: SOL price unavailable.")
            return None
        base_fee = amount_in_usd * 0.009
        priority_fee_sol = 0.3 if congestion else 0.03
        priority_fee_usd = priority_fee_sol * sol_price
        total_fee = base_fee + priority_fee_usd
        return round(total_fee, 4)
    except Exception as e:
        print(f"[!] Error calculating gas fee: {e}")
        return None

# --- Get Market Cap from Dexscreener ---
def get_market_cap_from_dexscreener(contract_address):
    try:
        url = f"https://api.dexscreener.io/latest/dex/pairs/solana/{contract_address}"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        if 'pair' in data and 'fdv' in data['pair']:
            return float(data['pair']['fdv'])
        return None
    except Exception as e:
        print(f"[!] Error fetching market cap: {e}")
        return None

# --- Send Telegram Message using Telethon (User Account) ---
def send_telegram_message(text):
    try:
        api_id = int(os.getenv("TELEGRAM_API_ID"))
        api_hash = os.getenv("TELEGRAM_API_HASH")
        chat_id = int(os.getenv("TELEGRAM_CHAT_ID"))

        with TelegramClient('session_name', api_id, api_hash) as client:
            client.send_message(chat_id, text)
    except Exception as e:
        print(f"[!] Error sending Telegram message: {e}")
