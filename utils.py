import os
import requests
import fcntl
import base58
from dotenv import load_dotenv
from telethon.sync import TelegramClient
from datetime import datetime

# Load environment variables from t.env
load_dotenv(dotenv_path="t.env")

# -------------------------
# ENV VAR LOADER
# -------------------------
def get_env_variable(key, required=True, default=None):
    value = os.getenv(key, default)
    if required and (value is None or value == ""):
        raise EnvironmentError(f"[!] Missing required environment variable: {key}")
    return value

# -------------------------
# SOLANA CONTRACT ADDRESS VALIDATION
# -------------------------
def is_valid_solana_address(address):
    try:
        decoded = base58.b58decode(address)
        return len(decoded) == 32
    except Exception:
        return False

# -------------------------
# SOL PRICE FETCHING (Only Jupiter, no CoinGecko fallback)
# -------------------------
def get_sol_price_usd():
    jupiter_price_api = get_env_variable("JUPITER_PRICE_API", required=False, default="https://price.jup.ag/v4/price")

    try:
        resp = requests.get(f"{jupiter_price_api}?ids=SOL", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if "data" in data and "SOL" in data["data"]:
            return float(data['data']['SOL']['price'])
        raise ValueError("Invalid Jupiter API response")
    except Exception as jupiter_error:
        print(f"[!] Jupiter API failed: {jupiter_error}")
        return 0

# -------------------------
# GAS FEE CALCULATION
# -------------------------
def calculate_total_gas_fee(amount_in_usd, congestion=False):
    base_fee = amount_in_usd * 0.009
    priority_fee_sol = 0.3 if congestion else 0.03
    sol_price = get_sol_price_usd()
    if sol_price == 0:
        print("[!] Unable to fetch SOL price for gas fee calculation.")
        return None
    return round(base_fee + (priority_fee_sol * sol_price), 4)

# -------------------------
# DEXSCREENER MARKET CAP FETCH with retries and Birdeye fallback
# -------------------------
def get_market_cap_from_birdeye(contract_address):
    try:
        url = f"https://public-api.birdeye.so/defi/price?address={contract_address}"
        headers = {"accept": "application/json"}  # No API key header
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        # Birdeye response structure may vary; try to extract market cap (fdv)
        if "fdv" in data:
            return float(data["fdv"])
        elif "price" in data and "marketCap" in data:
            # Sometimes it could be under marketCap or calculated
            return float(data.get("marketCap", 0))
        print("[!] Birdeye: FDV/MarketCap not found in response.")
        return None
    except Exception as e:
        print(f"[!] Birdeye fetch error: {e}")
        return None

def get_market_cap_from_dexscreener(contract_address, max_retries=3):
    dexscreener_api_key = get_env_variable("DEXSCREENER_API_KEY", required=False)
    retries = 0
    while retries < max_retries:
        try:
            url = f"https://api.dexscreener.io/latest/dex/pairs/solana/{contract_address}"
            headers = {}
            if dexscreener_api_key:
                headers["Authorization"] = f"Bearer {dexscreener_api_key}"
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if "pairs" in data and len(data["pairs"]) > 0 and "fdv" in data["pairs"][0]:
                return float(data["pairs"][0]["fdv"])
            elif "pair" in data and "fdv" in data["pair"]:
                return float(data["pair"]["fdv"])
            print("[!] Dexscreener: FDV not found in response.")
            return None
        except Exception as e:
            retries += 1
            print(f"[!] Dexscreener fetch error (attempt {retries}): {e}")

    # Fallback to Birdeye API if Dexscreener failed max_retries times
    print("[*] Dexscreener failed max retries, falling back to Birdeye API...")
    return get_market_cap_from_birdeye(contract_address)

# -------------------------
# TELEGRAM BOT MESSAGE SENDER
# -------------------------
def send_telegram_message(text):
    try:
        bot_token = get_env_variable("TELEGRAM_BOT_TOKEN")
        chat_id = int(get_env_variable("TELEGRAM_CHAT_ID"))
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        print(f"[!] Telegram send error: {e}")

# -------------------------
# TELEGRAM CHANNEL MESSAGE READER
# -------------------------
def read_from_target_channel(limit=10):
    try:
        api_id = int(get_env_variable("TELEGRAM_API_ID"))
        api_hash = get_env_variable("TELEGRAM_API_HASH")
        target_channel = int(get_env_variable("TARGET_CHANNEL_ID"))
        with TelegramClient('session_name', api_id, api_hash) as client:
            messages = client.get_messages(target_channel, limit=limit)
            return [msg.text for msg in messages if msg.text]
    except Exception as e:
        print(f"[!] Telegram read error: {e}")
        return []

# -------------------------
# USD TO SOL AMOUNT CALC
# -------------------------
def get_sol_amount_for_usd(usd_amount):
    sol_price = get_sol_price_usd()
    if sol_price == 0:
        print("[!] Cannot calculate SOL amount due to zero price.")
        return 0
    return round(usd_amount / sol_price, 5)

# -------------------------
# PROCESSED CONTRACTS TRACKER
# -------------------------
def save_processed_ca(ca):
    try:
        with open("processed_ca.txt", "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(f"{ca}\n")
            fcntl.flock(f, fcntl.LOCK_UN)
    except Exception as e:
        print(f"[!] Error saving processed CA: {e}")

def is_ca_processed(ca):
    try:
        if not os.path.exists("processed_ca.txt"):
            return False
        with open("processed_ca.txt", "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            processed = f.read()
            fcntl.flock(f, fcntl.LOCK_UN)
            return ca in processed
    except Exception as e:
        print(f"[!] Error checking processed CA: {e}")
        return False

def clear_processed_ca():
    try:
        with open("processed_ca.txt", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.truncate(0)
            fcntl.flock(f, fcntl.LOCK_UN)
    except Exception as e:
        print(f"[!] Error clearing processed CA: {e}")

# -------------------------
# DAILY BUY LIMIT CYCLER (cycles through DAILY_LIMIT comma list)
# -------------------------
def get_current_daily_limit():
    try:
        daily_limit_str = get_env_variable("DAILY_LIMITS", required=False, default="5,4")
        daily_limits = [int(x.strip()) for x in daily_limit_str.split(",") if x.strip()]
        if not daily_limits:
            daily_limits = [5, 4]
    except Exception as e:
        print(f"[!] Error parsing DAILY_LIMITS env var: {e}")
        daily_limits = [5, 4]

    day_number = datetime.utcnow().toordinal()
    index = day_number % len(daily_limits)
    return daily_limits[index]

# -------------------------
# JUPITER SWAP BUY
# -------------------------
def jupiter_buy(ca, amount_sol):
    try:
        if not is_valid_solana_address(ca):
            print(f"[!] Invalid Solana contract address: {ca}")
            return False
        print(f"[BUY] Swapping {amount_sol} SOL -> {ca}")
        swap_url = get_env_variable("JUPITER_SWAP_API")
        payload = {
            "inputMint": "So11111111111111111111111111111111111111112",
            "outputMint": ca,
            "amount": int(amount_sol * 1e9),
            "slippageBps": 50,
            "userPublicKey": get_env_variable("PUBLIC_KEY"),
        }
        resp = requests.post(swap_url, json=payload, timeout=15)
        resp.raise_for_status()
        print("[✓] Buy transaction sent.")
        return True
    except Exception as e:
        print(f"[!] Jupiter buy error: {e}")
        return False

# -------------------------
# JUPITER SWAP SELL
# -------------------------
def jupiter_sell(ca):
    try:
        if not is_valid_solana_address(ca):
            print(f"[!] Invalid Solana contract address: {ca}")
            return False
        print(f"[SELL] Swapping {ca} -> SOL")
        swap_url = get_env_variable("JUPITER_SWAP_API")
        amount_lamports = get_token_balance_lamports(ca)
        if amount_lamports == 0:
            print("[!] No token balance to sell.")
            return False
        payload = {
            "inputMint": ca,
            "outputMint": "So11111111111111111111111111111111111111112",
            "amount": amount_lamports,
            "slippageBps": 50,
            "userPublicKey": get_env_variable("PUBLIC_KEY"),
        }
        resp = requests.post(swap_url, json=payload, timeout=15)
        resp.raise_for_status()
        print("[✓] Sell transaction sent.")
        return True
    except Exception as e:
        print(f"[!] Jupiter sell error: {e}")
        return False

# -------------------------
# FETCH TOKEN BALANCE LAMPORTS (for selling)
# -------------------------
def get_token_balance_lamports(token_mint):
    rpc_url = get_env_variable("SOLANA_RPC")
    public_key = get_env_variable("PUBLIC_KEY")

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [
            public_key,
            {"mint": token_mint},
            {"encoding": "jsonParsed"}
        ]
    }
    try:
        resp = requests.post(rpc_url, json=payload, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        accounts = result.get("result", {}).get("value", [])
        total_balance = 0
        for acc in accounts:
            amount_str = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {}).get("tokenAmount", {}).get("amount", "0")
            total_balance += int(amount_str)
        return total_balance
    except Exception as e:
        print(f"[!] Token balance error: {e}")
        return 0
