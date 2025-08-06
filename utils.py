import os
import requests
import math
from dotenv import load_dotenv
from telethon.sync import TelegramClient

# Load environment variables from a custom .env file (e.g., t.env)
load_dotenv(dotenv_path="t.env")

# --- Safe Environment Variable Retrieval ---
def get_env_variable(key, required=True, default=None):
    value = os.getenv(key, default)
    if required and value is None:
        raise EnvironmentError(f"[!] Missing required environment variable: {key}")
    return value

# --- Get SOL Price from Jupiter or fallback to CoinGecko ---
def get_sol_price_usd():
    try:
        response = requests.get("https://price.jup.ag/v4/price?ids=SOL", timeout=10)
        response.raise_for_status()
        data = response.json()
        return float(data['data']['SOL']['price'])
    except Exception as jupiter_error:
        print(f"[!] Jupiter API failed: {jupiter_error}")
        try:
            response = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd", timeout=10)
            response.raise_for_status()
            data = response.json()
            return float(data['solana']['usd'])
        except Exception as cg_error:
            print(f"[!] CoinGecko fallback also failed: {cg_error}")
            return 0  # Critical failure fallback

# --- Gas Fee Calculation (0.9% + priority fee) ---
def calculate_total_gas_fee(amount_in_usd, congestion=False):
    base_fee = amount_in_usd * 0.009  # 0.9% base fee
    priority_fee_sol = 0.3 if congestion else 0.03
    sol_price = get_sol_price_usd()
    if sol_price == 0:
        print("[!] Unable to fetch SOL price. Skipping fee calculation.")
        return None
    priority_fee_usd = priority_fee_sol * sol_price
    total_fee = base_fee + priority_fee_usd
    return round(total_fee, 4)

# --- Market Cap from Dexscreener ---
def get_market_cap_from_dexscreener(contract_address):
    try:
        url = f"https://api.dexscreener.io/latest/dex/pairs/solana/{contract_address}"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        return float(data['pair']['fdv']) if 'pair' in data else None
    except Exception as e:
        print(f"[!] Error fetching market cap: {e}")
        return None

# --- Telegram Messaging ---
def send_telegram_message(text):
    try:
        api_id = int(get_env_variable("TELEGRAM_API_ID"))
        api_hash = get_env_variable("TELEGRAM_API_HASH")
        chat_id = int(get_env_variable("TELEGRAM_CHAT_ID"))
        with TelegramClient('session_name', api_id, api_hash) as client:
            client.send_message(chat_id, text)
    except Exception as e:
        print(f"[!] Error sending Telegram message: {e}")

# --- Extra Math Utility (Example: Round to nearest 5) ---
def round_to_nearest_5(value):
    return int(5 * round(float(value) / 5))
