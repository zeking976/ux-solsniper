# utils.py - full toolbox aligned with t.env and sniper.py
import os
import json
import time
import base64
import base58
import fcntl
import logging
from typing import Optional, Dict, Any
from datetime import datetime
import requests
from dotenv import load_dotenv

# Solana / signing
from solana.rpc.api import Client
from solders.keypair import Keypair
from solana.transaction import Transaction
from solana.rpc.types import TxOpts

# ----------------------------------------
# Load .env (Termux / VULTR friendly)
# ----------------------------------------
def load_env(dotenv_path: Optional[str] = None):
    """
    Load environment variables from a dotenv path (default: none -> system env).
    Call this early from your main modules (sniper.py) to ensure envs are loaded.
    """
    if dotenv_path:
        load_dotenv(dotenv_path=dotenv_path)
    else:
        load_dotenv()

# Allow callers to call load_env(...) early
load_env(os.path.expanduser("~/t.env"))

# ----------------------------------------
# Logging
# ----------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logger = logging.getLogger("ux_solsniper")
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
if not logger.handlers:
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(sh)

# ----------------------------------------
# Simple env helper
# ----------------------------------------
def get_env_variable(key: str, required: bool = True, default: Optional[str] = None) -> str:
    v = os.getenv(key, default)
    if required and (v is None or str(v).strip() == ""):
        raise EnvironmentError(f"[!] Missing required environment variable: {key}")
    return v

# ----------------------------------------
# Runtime / RPC / Keys
# ----------------------------------------
DRY_RUN = int(os.getenv("DRY_RUN", "1"))

# RPC URL variable name in your t.env is RPC_URL (or SOLANA_RPC in some variants). Try both.
RPC_URL = os.getenv("RPC_URL") or os.getenv("SOLANA_RPC")
if not RPC_URL:
    raise EnvironmentError("RPC_URL / SOLANA_RPC must be set in t.env")

RPC = Client(RPC_URL)

# Keypair loader: supports JSON array or base58 string
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
PUBLIC_KEY = get_env_variable("PUBLIC_KEY")

# ----------------------------------------
# Jupiter & APIs
# ----------------------------------------
JUPITER_QUOTE_API = os.getenv("JUPITER_QUOTE_API", "https://quote-api.jup.ag/v6/quote")
JUPITER_SWAP_API = os.getenv("JUPITER_SWAP_API", "https://quote-api.jup.ag/v6/swap")
JUPITER_PRICE_API = os.getenv("JUPITER_PRICE_API", "https://price.jup.ag/v4/price")

DEXSCREENER_API_KEY = os.getenv("DEXSCREENER_API_KEY", "")

# ----------------------------------------
# Bot trading config (aligns with t.env)
# ----------------------------------------
# These names follow the t.env convention you gave me earlier.
DAILY_CAPITAL_USD = float(os.getenv("DAILY_CAPITAL_USD", os.getenv("INVESTMENT_USD", "25")))
MAX_BUYS_PER_DAY = int(os.getenv("MAX_BUYS_PER_DAY", "5"))
GAS_BUFFER = float(os.getenv("GAS_BUFFER", "0.009"))

# tipping / priority fee values (manual in t.env)
NORMAL_TIP_SOL = float(os.getenv("NORMAL_TIP_SOL", os.getenv("NORMAL_PRIORITY_FEE", "0.015")))
CONGESTION_TIP_SOL = float(os.getenv("CONGESTION_TIP_SOL", os.getenv("HIGH_PRIORITY_FEE", "0.1")))

# priority fees used for estimating gas; can be manual or computed via MANUAL_CONGESTION
NORMAL_PRIORITY_FEE = float(os.getenv("NORMAL_PRIORITY_FEE", "0.009"))
HIGH_PRIORITY_FEE = float(os.getenv("HIGH_PRIORITY_FEE", "0.02"))
MANUAL_CONGESTION = int(os.getenv("MANUAL_CONGESTION", "0"))  # 0 normal, 1 congested

# Anti-MEV toggle
MEV_PROTECTION = int(os.getenv("MEV_PROTECTION", "1"))

# Stop loss / take profit related defaults (sniper.py may re-read its own envs)
STOP_LOSS_PERCENT = float(os.getenv("STOP_LOSS", os.getenv("STOP_LOSS_PERCENT", "-20")))
TAKE_PROFIT = float(os.getenv("TAKE_PROFIT", os.getenv("TAKE_PROFIT_MULTIPLIER", "100")))

# ----------------------------------------
# Telegram
# ----------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram_message(bot_token: Optional[str], chat_id: Optional[str], text: str):
    """
    Sends a markdown Telegram message. If bot_token or chat_id missing, logs and returns.
    sniper.py also uses a send_telegram_message wrapper.
    """
    if not bot_token:
        bot_token = TELEGRAM_BOT_TOKEN
    if not chat_id:
        chat_id = TELEGRAM_CHAT_ID
    if not bot_token or not chat_id:
        logger.warning("[!] Telegram token/chat missing, cannot send message.")
        return
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {"chat_id": int(chat_id), "text": text, "parse_mode": "Markdown"}
        resp = requests.post(url, json=payload, timeout=12)
        if not resp.ok:
            logger.error("Telegram send failed: %s", resp.text)
    except Exception as e:
        logger.exception("Telegram send exception: %s", e)

# small helper to wrap code in markdown
def md_code(s: Optional[str]) -> str:
    if s is None:
        s = ""
    return f"`{str(s).replace('`', '\\`')}`"

# ----------------------------------------
# Processed contract address helpers (file lock safe)
# ----------------------------------------
PROCESSED_FILE = os.getenv("PROCESSED_FILE", "processed_ca.txt")

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

def clear_processed_ca():
    try:
        with open(PROCESSED_FILE, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.truncate(0)
            fcntl.flock(f, fcntl.LOCK_UN)
    except Exception as e:
        logger.exception("clear_processed_ca error: %s", e)

# ----------------------------------------
# Token / message helpers
# ----------------------------------------
def extract_contract_address(message: str) -> Optional[str]:
    """Extract a 32-44 char base58-like token mint from message text."""
    import re
    if not message:
        return None
    pattern = r"[A-Za-z0-9]{32,44}"
    m = re.search(pattern, message)
    return m.group(0) if m else None

# ----------------------------------------
# SOL price / conversions
# ----------------------------------------
def get_sol_price_usd() -> float:
    try:
        r = requests.get(f"{JUPITER_PRICE_API}?ids=SOL", timeout=8)
        r.raise_for_status()
        data = r.json()
        if "data" in data and "SOL" in data["data"]:
            return float(data["data"]["SOL"]["price"])
        if "SOL" in data:
            return float(data["SOL"]["price"])
    except Exception as e:
        logger.warning("Failed to fetch SOL price: %s", e)
    return 0.0

def usd_to_sol(usd: float) -> float:
    price = get_sol_price_usd()
    if price <= 0:
        logger.warning("SOL price unknown, usd_to_sol -> 0.0")
        return 0.0
    return round(usd / price, 9)

# ----------------------------------------
# Market cap (dexscreener primary, birdeye fallback)
# ----------------------------------------
def get_market_cap_from_dexscreener(contract_address: str, max_retries: int = 3) -> Optional[float]:
    base = f"https://api.dexscreener.io/latest/dex/pairs/solana/{contract_address}"
    headers = {}
    if DEXSCREENER_API_KEY:
        headers["Authorization"] = f"Bearer {DEXSCREENER_API_KEY}"
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(base, headers=headers, timeout=8)
            resp.raise_for_status()
            data = resp.json()
            if "pairs" in data and data["pairs"]:
                fdv = data["pairs"][0].get("fdv") or data["pairs"][0].get("marketCap") or 0
                if fdv:
                    return float(fdv)
            if "pair" in data:
                fdv = data["pair"].get("fdv") or data["pair"].get("marketCap") or 0
                if fdv:
                    return float(fdv)
            return None
        except Exception as e:
            logger.warning("Dexscreener attempt %d failed: %s", attempt, e)
            time.sleep(1 + attempt)
    return get_market_cap_from_birdeye(contract_address)

def get_market_cap_from_birdeye(contract_address: str) -> Optional[float]:
    try:
        url = f"https://public-api.birdeye.so/defi/price?address={contract_address}"
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):
            if data.get("fdv"):
                return float(data.get("fdv"))
            if data.get("marketCap"):
                return float(data.get("marketCap"))
        return None
    except Exception as e:
        logger.warning("Birdeye fetch failed: %s", e)
        return None

# Backwards-compatible name used in sniper.py
def get_market_cap(contract_address: str) -> Optional[float]:
    return get_market_cap_from_dexscreener(contract_address)

# ----------------------------------------
# Congestion detection / priority tip selection
# ----------------------------------------
def detect_network_congestion(threshold_ms: int = 500) -> bool:
    """
    Lightweight RPC latency heuristic. Returns True if slow.
    Note: this pings the RPC node and can be tuned or overridden by MANUAL_CONGESTION.
    """
    if MANUAL_CONGESTION:
        return True
    try:
        start = time.time()
        RPC.get_slot()  # small call
        elapsed = (time.time() - start) * 1000.0
        return elapsed > threshold_ms
    except Exception:
        # If RPC is failing, treat as congested (conservative)
        return True

def get_priority_fee() -> float:
    """
    Return priority fee in SOL. Uses manual HIGH/NORMAL settings from env.
    """
    if MANUAL_CONGESTION:
        return HIGH_PRIORITY_FEE
    congested = detect_network_congestion()
    return HIGH_PRIORITY_FEE if congested else NORMAL_PRIORITY_FEE

# ----------------------------------------
# Gas fee estimate & reserve saver
# ----------------------------------------
def calculate_total_gas_fee(amount_in_usd: float, congestion: Optional[bool] = None) -> Optional[float]:
    """
    Rough fee estimator (USD). Combines a percentage-based trading fee + priority tip (SOL -> USD).
    amount_in_usd: gross trade amount in USD (used to estimate percent fees)
    """
    sol_price = get_sol_price_usd()
    if sol_price <= 0:
        logger.warning("SOL price unknown, cannot compute gas fees accurately.")
        return None
    base_fee_usd = amount_in_usd * 0.009  # 0.9% default estimate
    if congestion is None:
        congestion = detect_network_congestion()
    priority_fee_sol = HIGH_PRIORITY_FEE if congestion else NORMAL_PRIORITY_FEE
    priority_fee_usd = priority_fee_sol * sol_price
    total = base_fee_usd + priority_fee_usd
    return round(total, 6)

def save_gas_reserve_after_trade(current_usd_balance: float, reserve_pct: float = 0.0009) -> float:
    """
    Save a very small reserve from USD balance for gas, returns new balance.
    """
    reserve = current_usd_balance * reserve_pct
    new_balance = current_usd_balance - reserve
    logger.info("Saved gas reserve %.6f USD (%.4f%%). New balance: %.6f", reserve, reserve_pct*100, new_balance)
    return round(new_balance, 6)

# ----------------------------------------
# Token balances (RPC)
# ----------------------------------------
def get_token_balance_lamports(token_mint: str) -> int:
    """
    Returns total token amount (in raw token smallest units) for PUBLIC_KEY owner across accounts.
    """
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
        total = 0
        for acc in accounts:
            amount_str = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {}).get("tokenAmount", {}).get("amount", "0")
            try:
                total += int(amount_str)
            except Exception:
                pass
        return total
    except Exception as e:
        logger.exception("get_token_balance_lamports error: %s", e)
        return 0

# ----------------------------------------
# Jupiter Quote & Swap (Anti-MEV support)
# ----------------------------------------
def fetch_jupiter_quote(input_mint: str, output_mint: str, amount: int, slippage_bps: int = 50) -> Optional[Dict[str, Any]]:
    """
    Calls Jupiter quote endpoint with anti-MEV flags (asymmetricSlippage).
    Returns the first route/quote dict or None.
    """
    try:
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": slippage_bps,
            "onlyDirectRoutes": False,
            "asymmetricSlippage": bool(MEV_PROTECTION)
        }
        r = requests.get(JUPITER_QUOTE_API, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "data" in data and data["data"]:
            return data["data"][0]
        # fallback structures
        if isinstance(data, dict) and data.get("route"):
            return data.get("route")
    except Exception as e:
        logger.exception("fetch_jupiter_quote error: %s", e)
    return None

def execute_jupiter_swap_from_quote(quote: Dict[str, Any], priority_fee_sol: Optional[float] = None) -> Optional[str]:
    """
    Calls Jupiter /swap to obtain a base64 tx, decodes, signs with KEYPAIR, and broadcasts.
    Returns signature string or None.
    """
    if DRY_RUN:
        logger.info("[DRY RUN] execute_jupiter_swap_from_quote would broadcast a tx")
        return "SIMULATED_SWAP_SIGNATURE"

    try:
        payload = {
            "quoteResponse": quote,
            "userPublicKey": PUBLIC_KEY,
            "wrapAndUnwrapSol": True,
            "asymmetricSlippage": bool(MEV_PROTECTION)
        }
        if priority_fee_sol is None:
            priority_fee_sol = get_priority_fee()
        try:
            payload["prioritizationFeeLamports"] = int(priority_fee_sol * 1_000_000_000)
        except Exception:
            pass

        r = requests.post(JUPITER_SWAP_API, json=payload, timeout=20)
        r.raise_for_status()
        swap_json = r.json()
        # Jupiter returns swapTransaction base64 in a few possible fields
        swap_tx_b64 = swap_json.get("swapTransaction") or swap_json.get("swap_tx") or swap_json.get("data", {}).get("swapTransaction")
        if not swap_tx_b64:
            # attempt to find a long base64-looking string in response values
            for v in swap_json.values() if isinstance(swap_json, dict) else []:
                if isinstance(v, str) and len(v) > 100:
                    swap_tx_b64 = v
                    break
        if not swap_tx_b64:
            logger.error("Swap API did not return swapTransaction: %s", swap_json)
            return None

        raw = base64.b64decode(swap_tx_b64)
        tx = Transaction.deserialize(raw)
        tx.sign(KEYPAIR)
        serialized = bytes(tx.serialize())
        resp = RPC.send_raw_transaction(serialized, opts=TxOpts(skip_preflight=False, preflight_commitment="processed"))
        # RPC client may return a dict or string depending on library
        sig = None
        if isinstance(resp, dict):
            sig = resp.get("result") or resp.get("signature")
        elif isinstance(resp, str):
            sig = resp
        if not sig:
            try:
                sig = resp.get("result")
            except Exception:
                pass
        if sig:
            logger.info("Broadcasted tx signature: %s", sig)
            return sig
        logger.error("RPC response missing signature: %s", resp)
    except Exception as e:
        logger.exception("execute_jupiter_swap_from_quote error: %s", e)
    return None

# ----------------------------------------
# High-level Jupiter buy/sell wrappers
# ----------------------------------------
def jupiter_buy(ca_mint: str, sol_amount: float, slippage_bps: int = 50, tip: Optional[float] = None) -> Optional[str]:
    """
    Buy CA using SOL amount. Returns signature or None.
    tip: explicit prioritization tip in SOL (overrides priority fee logic)
    """
    if DRY_RUN:
        logger.info("[DRY RUN] jupiter_buy simulated for %s amount %.9f", ca_mint, sol_amount)
        return f"SIMULATED_BUY_{ca_mint[:6]}"

    if not ca_mint or sol_amount <= 0:
        logger.error("Invalid buy params")
        return None
    try:
        sol_input_mint = "So11111111111111111111111111111111111111112"
        amount_lamports = int(sol_amount * 1e9)
        quote = fetch_jupiter_quote(sol_input_mint, ca_mint, amount_lamports, slippage_bps=slippage_bps)
        if not quote:
            logger.error("No quote for buy")
            return None
        priority = tip if tip is not None else get_priority_fee()
        sig = execute_jupiter_swap_from_quote(quote, priority_fee_sol=priority)
        return sig
    except Exception as e:
        logger.exception("jupiter_buy error: %s", e)
        return None

def jupiter_sell(ca_mint: str, slippage_bps: int = 50, tip: Optional[float] = None) -> Optional[str]:
    """
    Sell entire token balance of CA -> SOL. Returns signature or None.
    """
    if DRY_RUN:
        logger.info("[DRY RUN] jupiter_sell simulated for %s", ca_mint)
        return f"SIMULATED_SELL_{ca_mint[:6]}"

    try:
        sol_output_mint = "So11111111111111111111111111111111111111112"
        amount = get_token_balance_lamports(ca_mint)
        if amount == 0:
            logger.info("No token balance to sell for %s", ca_mint)
            return None
        quote = fetch_jupiter_quote(ca_mint, sol_output_mint, amount, slippage_bps=slippage_bps)
        if not quote:
            logger.error("No quote for sell")
            return None
        priority = tip if tip is not None else get_priority_fee()
        sig = execute_jupiter_swap_from_quote(quote, priority_fee_sol=priority)
        return sig
    except Exception as e:
        logger.exception("jupiter_sell error: %s", e)
        return None

# ----------------------------------------
# Small reporting helpers
# ----------------------------------------
def resolve_token_name(contract_address: str) -> Optional[str]:
    """
    Attempt to resolve token name from Dexscreener for nicer reports.
    """
    try:
        base = f"https://api.dexscreener.io/latest/dex/pairs/solana/{contract_address}"
        headers = {}
        if DEXSCREENER_API_KEY:
    