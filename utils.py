import os
import json
import time
import base64
import base58
import fcntl
import logging
from typing import Optional
from datetime import datetime
import requests
from dotenv import load_dotenv

# Solana
from solana.rpc.api import Client
from solders.keypair import Keypair
from solana.transaction import Transaction
from solana.rpc.types import TxOpts

# -------------------------
# Load .env
# -------------------------
def load_env(dotenv_path: Optional[str] = None):
    if dotenv_path:
        load_dotenv(dotenv_path=dotenv_path)
    else:
        load_dotenv()

load_env(os.path.expanduser("~/t.env"))

# -------------------------
# Logging
# -------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logger = logging.getLogger("ux_solsniper")
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
if not logger.handlers:
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(sh)

# -------------------------
# DRY RUN & RPC
# -------------------------
DRY_RUN = int(os.getenv("DRY_RUN", "1"))
RPC_URL = os.getenv("SOLANA_RPC")
RPC = Client(RPC_URL)

# -------------------------
# Load Keypair
# -------------------------
def _load_keypair_from_env() -> Keypair:
    sk = os.getenv("PRIVATE_KEY")
    if not sk:
        raise EnvironmentError("PRIVATE_KEY missing in env")
    try:
        if sk.strip().startswith("["):
            arr = json.loads(sk)
            sk_bytes = bytes(arr)
        else:
            sk_bytes = base58.b58decode(sk)
        return Keypair.from_secret_key(sk_bytes)
    except Exception as e:
        logger.exception("Failed to load PRIVATE_KEY: %s", e)
        raise

KEYPAIR = _load_keypair_from_env()
PUBLIC_KEY = os.getenv("PUBLIC_KEY")

# -------------------------
# Jupiter endpoints
# -------------------------
JUPITER_QUOTE_API = os.getenv("JUPITER_QUOTE_API", "https://quote-api.jup.ag/v6/quote")
JUPITER_SWAP_API = os.getenv("JUPITER_SWAP_API", "https://quote-api.jup.ag/v6/swap")
JUPITER_PRICE_API = os.getenv("JUPITER_PRICE_API", "https://price.jup.ag/v4/price")

# -------------------------
# Telegram
# -------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram_message(bot_token: str, chat_id: str, text: str):
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {"chat_id": int(chat_id), "text": text, "parse_mode": "Markdown"}
        resp = requests.post(url, json=payload, timeout=10)
        if not resp.ok:
            logger.error("Telegram send failed: %s", resp.text)
    except Exception as e:
        logger.exception("Telegram send exception: %s", e)

# -------------------------
# Contract address helpers
# -------------------------
PROCESSED_FILE = "processed_ca.txt"

def save_processed_ca(ca: str):
    try:
        with open(PROCESSED_FILE, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(ca + "\n")
            fcntl.flock(f, fcntl.LOCK_UN)
    except Exception as e:
        logger.exception("save_processed_ca error: %s", e)

def is_ca_processed(ca: str) -> bool:
    try:
        if not os.path.exists(PROCESSED_FILE):
            return False
        with open(PROCESSED_FILE, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            processed = f.read().splitlines()
            fcntl.flock(f, fcntl.LOCK_UN)
            return ca in processed
    except Exception as e:
        logger.exception("is_ca_processed error: %s", e)
        return False

# -------------------------
# Extract contract address from message
# -------------------------
def extract_contract_address(message: str) -> Optional[str]:
    import re
    if not message:
        return None
    pattern = r"[A-Za-z0-9]{32,44}"  # Solana token mint pattern
    match = re.search(pattern, message)
    return match.group(0) if match else None

# -------------------------
# SOL/USD conversions
# -------------------------
def get_sol_price_usd() -> float:
    try:
        r = requests.get(f"{JUPITER_PRICE_API}?ids=SOL", timeout=8)
        r.raise_for_status()
        data = r.json()
        price = 0.0
        if "data" in data and "SOL" in data["data"]:
            price = float(data["data"]["SOL"]["price"])
        elif "SOL" in data:
            price = float(data["SOL"]["price"])
        return price
    except Exception as e:
        logger.warning("Failed SOL price fetch: %s", e)
        return 0.0

def usd_to_sol(usd: float, rpc_url: str) -> float:
    price = get_sol_price_usd()
    if price <= 0:
        logger.warning("Unknown SOL price")
        return 0.0
    return round(usd / price, 9)

# -------------------------
# Market cap
# -------------------------
def get_market_cap(ca: str) -> Optional[float]:
    try:
        url = f"https://api.dexscreener.io/latest/dex/pairs/solana/{ca}"
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        data = r.json()
        if "pairs" in data and data["pairs"]:
            return float(data["pairs"][0].get("fdv") or data["pairs"][0].get("marketCap") or 0)
        return None
    except Exception as e:
        logger.warning("Market cap fetch failed: %s", e)
        return None

# -------------------------
# Network congestion & priority fee
# -------------------------
MANUAL_CONGESTION = int(os.getenv("MANUAL_CONGESTION", "0"))  # 0 = normal, 1 = congested

def get_priority_fee() -> float:
    return 0.3 if MANUAL_CONGESTION else 0.03

# -------------------------
# Buy / Sell using Jupiter with Anti-MEV
# -------------------------
def jupiter_buy(ca_mint: str, sol_amount: float) -> Optional[str]:
    if DRY_RUN:
        logger.info(f"[DRY RUN] Buying {sol_amount} SOL for {ca_mint}")
        return f"SIMULATED_BUY_{ca_mint[:6]}"
    try:
        sol_input_mint = "So11111111111111111111111111111111111111112"
        amount_lamports = int(sol_amount * 1e9)
        quote = fetch_jupiter_quote(sol_input_mint, ca_mint, amount_lamports)
        if not quote:
            logger.error("No quote for buy")
            return None
        sig = execute_jupiter_swap_from_quote(quote)
        return sig
    except Exception as e:
        logger.exception("jupiter_buy error: %s", e)
        return None

def jupiter_sell(ca_mint: str) -> Optional[str]:
    if DRY_RUN:
        logger.info(f"[DRY RUN] Selling token {ca_mint}")
        return f"SIMULATED_SELL_{ca_mint[:6]}"
    try:
        sol_output_mint = "So11111111111111111111111111111111111111112"
        amount = get_token_balance_lamports(ca_mint)
        if amount == 0:
            logger.info(f"No balance to sell {ca_mint}")
            return None
        quote = fetch_jupiter_quote(ca_mint, sol_output_mint, amount)
        if not quote:
            logger.error("No quote for sell")
            return None
        sig = execute_jupiter_swap_from_quote(quote)
        return sig
    except Exception as e:
        logger.exception("jupiter_sell error: %s", e)
        return None

def fetch_jupiter_quote(input_mint: str, output_mint: str, amount: int, slippage_bps: int = 50) -> Optional[dict]:
    try:
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": slippage_bps,
            "onlyDirectRoutes": False,
            "asymmetricSlippage": True  # Anti-MEV
        }
        r = requests.get(JUPITER_QUOTE_API, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        if "data" in data and data["data"]:
            return data["data"][0]
        return None
    except Exception as e:
        logger.exception("fetch_jupiter_quote error: %s", e)
        return None

def execute_jupiter_swap_from_quote(quote: dict) -> Optional[str]:
    try:
        payload = {
            "quoteResponse": quote,
            "userPublicKey": PUBLIC_KEY,
            "wrapAndUnwrapSol": True,
            "asymmetricSlippage": True
        }
        priority_fee_sol = get_priority_fee()
        payload["prioritizationFeeLamports"] = int(priority_fee_sol * 1_000_000_000)

        r = requests.post(JUPITER_SWAP_API, json=payload, timeout=20)
        r.raise_for_status()
        swap_json = r.json()
        swap_tx_b64 = swap_json.get("swapTransaction") or swap_json.get("data", {}).get("swapTransaction")
        if not swap_tx_b64:
            logger.error("Swap API did not return transaction")
            return None
        raw = base64.b64decode(swap_tx_b64)
        tx = Transaction.deserialize(raw)
        tx.sign(KEYPAIR)
        serialized = bytes(tx.serialize())
        resp = RPC.send_raw_transaction(serialized, opts=TxOpts(skip_preflight=False, preflight_commitment="processed"))
        sig = resp.get("result") if isinstance(resp, dict) else resp
        logger.info(f"Broadcasted tx signature: {sig}")
        return sig
    except Exception as e:
        logger.exception("execute_jupiter_swap_from_quote error: %s", e)
        return None

# -------------------------
# Token balance
# -------------------------
def get_token_balance_lamports(token_mint: str) -> int:
    try:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [
                PUBLIC_KEY,
                {"mint": token_mint},
                {"encoding": "jsonParsed"}
            ]
        }
        resp = requests.post(RPC_URL, json=body, timeout=8)
        resp.raise_for_status()
        accounts = resp.json().get("result", {}).get("value", [])
        total = sum(
            int(
                acc.get("account", {})
                .get("data", {})
                .get("parsed", {})
                .get("info", {})
                .get("tokenAmount", {})
                .get("amount", "0")
            )
            for acc in accounts
        )
        return total
    except Exception as e:
        logger.exception("get_token_balance_lamports error: %s", e)
        return 0