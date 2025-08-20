import os
import json
import time
import base64
import base58
import logging
import fcntl
from typing import Optional
from datetime import datetime

import requests
from dotenv import load_dotenv

# Solana
from solana.rpc.api import Client
from solders.keypair import Keypair
from solana.transaction import Transaction
from solana.rpc.types import TxOpts

# Load env
load_dotenv(dotenv_path="t.env")

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logger = logging.getLogger("ux_solsniper")
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
if not logger.handlers:
    fh = logging.FileHandler("bot.log")
    fh.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(formatter)
    logger.addHandler(sh)

# -------------------------
# ENV VAR LOADER
# -------------------------
def get_env_variable(key, required=True, default=None):
    value = os.getenv(key, default)
    if required and (value is None or str(value).strip() == ""):
        raise EnvironmentError(f"[!] Missing required environment variable: {key}")
    return value

# Dry run switch
DRY_RUN = int(get_env_variable("DRY_RUN", required=False, default="0"))

# -------------------------
# Solana RPC client & keypair loader
# -------------------------
def _load_keypair_from_env() -> Keypair:
    """
    Accepts PRIVATE_KEY env in either:
    - base58 string of 64 bytes
    - JSON array of 64 ints (solana CLI style)
    """
    sk = get_env_variable("PRIVATE_KEY")
    # Try JSON array
    try:
        if sk.strip().startswith("["):
            arr = json.loads(sk)
            sk_bytes = bytes(arr)
            return Keypair.from_secret_key(sk_bytes)
    except Exception:
        pass
    # Try base58
    try:
        sk_bytes = base58.b58decode(sk)
        return Keypair.from_secret_key(sk_bytes)
    except Exception:
        # Try hex
        try:
            sk_bytes = bytes.fromhex(sk)
            return Keypair.from_secret_key(sk_bytes)
        except Exception as e:
            logger.error("Failed to parse PRIVATE_KEY: %s", e)
            raise

RPC_URL = get_env_variable("SOLANA_RPC")
RPC = Client(RPC_URL)
KEYPAIR = _load_keypair_from_env()
PUBLIC_KEY = get_env_variable("PUBLIC_KEY")

# Jupiter APIs
JUPITER_PRICE_API = get_env_variable("JUPITER_PRICE_API", required=False,
                                    default="https://price.jup.ag/v4/price")
JUPITER_QUOTE_API = get_env_variable("JUPITER_QUOTE_API", required=False,
                                    default="https://quote-api.jup.ag/v6/quote")
JUPITER_SWAP_API = get_env_variable("JUPITER_SWAP_API", required=False,
                                   default="https://quote-api.jup.ag/v6/swap")

DEXSCREENER_API_KEY = get_env_variable("DEXSCREENER_API_KEY", required=False, default="")

# Telegram
TELEGRAM_BOT_TOKEN = get_env_variable("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = get_env_variable("TELEGRAM_CHAT_ID")

# -------------------------
# Helpers
# -------------------------
def is_valid_solana_address(address: str) -> bool:
    try:
        decoded = base58.b58decode(address)
        return len(decoded) in (32, 34)  # token mints are 32 bytes
    except Exception:
        return False

# -------------------------
# SOL PRICE (USD)
# -------------------------
def get_sol_price_usd() -> float:
    try:
        resp = requests.get(f"{JUPITER_PRICE_API}?ids=SOL", timeout=8)
        resp.raise_for_status()
        data = resp.json()
        if "data" in data and "SOL" in data["data"]:
            return float(data["data"]["SOL"]["price"])
        if "SOL" in data:
            return float(data["SOL"]["price"])
    except Exception as e:
        logger.warning("Jupiter price API failed: %s", e)
    return 0.0

# -------------------------
# CONGESTION / TIP LOGIC
# -------------------------
def get_priority_fee(congestion: bool) -> float:
    """
    Return priority fee in SOL.
    Normal: 0.03 SOL, Congested: 0.3 SOL
    """
    return 0.3 if congestion else 0.03

def detect_network_congestion(threshold_ms: int = 500) -> bool:
    """
    Simple heuristic: measure a small RPC request latency
    If RPC latency > threshold_ms, mark as congested.
    """
    try:
        start = time.time()
        RPC.get_slot()
        elapsed = (time.time() - start) * 1000
        return elapsed > threshold_ms
    except Exception:
        return False

# -------------------------
# GAS FEE CALCULATION
# -------------------------
def calculate_total_gas_fee(amount_in_usd: float, congestion: Optional[bool] = None) -> Optional[float]:
    sol_price = get_sol_price_usd()
    if sol_price <= 0:
        logger.warning("SOL price unknown, cannot compute gas fees accurately.")
        return None
    base_fee_usd = amount_in_usd * 0.009   # 0.9% estimate
    if congestion is None:
        congestion = detect_network_congestion()
    priority_fee_sol = get_priority_fee(congestion)
    priority_fee_usd = priority_fee_sol * sol_price
    total = base_fee_usd + priority_fee_usd
    return round(total, 6)

# -------------------------
# SAVE GAS RESERVE FUNCTION
# -------------------------
def save_gas_reserve_after_trade(current_usd_balance: float, reserve_pct: float = 0.0009) -> float:
    reserve = current_usd_balance * reserve_pct
    new_balance = current_usd_balance - reserve
    logger.info("Saved gas reserve %.6f USD (%.4f%%). New balance: %.6f", reserve, reserve_pct*100, new_balance)
    return round(new_balance, 6)

# -------------------------
# TELEGRAM SENDER
# -------------------------
def send_telegram_message(text: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": int(TELEGRAM_CHAT_ID), "text": text, "parse_mode": "Markdown"}
        resp = requests.post(url, json=payload, timeout=10)
        if not resp.ok:
            logger.error("Telegram send failed: %s", resp.text)
    except Exception as e:
        logger.exception("Telegram send exception: %s", e)

# -------------------------
# PROCESSED CONTRACTS (file lock safe)
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
            data = f.read()
            fcntl.flock(f, fcntl.LOCK_UN)
            return ca in data.splitlines()
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

# -------------------------
# MARKETCAP FETCH
# -------------------------
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
            logger.warning("Dexscreener returned unexpected structure: %s", data)
            return None
        except Exception as e:
            logger.warning("Dexscreener attempt %d failed: %s", attempt, e)
            time.sleep(1 + attempt)
    return get_market_cap_from_birdeye(contract_address)

def get_market_cap_from_birdeye(contract_address: str) -> Optional[float]:
    try:
        url = f"https://public-api.birdeye.so/defi/price?address={contract_address}"
        resp = requests.get(url, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            if data.get("fdv"):
                return float(data.get("fdv"))
            if data.get("marketCap"):
                return float(data.get("marketCap"))
        return None
    except Exception as e:
        logger.warning("Birdeye fetch failed: %s", e)
        return None

# -------------------------
# RPC helpers (token balances)
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
        res = resp.json()
        accounts = res.get("result", {}).get("value", [])
        total = 0
        for acc in accounts:
            amount_str = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {}).get("tokenAmount", {}).get("amount", "0")
            total += int(amount_str)
        return total
    except Exception as e:
        logger.exception("get_token_balance_lamports error: %s", e)
        return 0

# -------------------------
# SOL USD conversion
# -------------------------
def get_sol_amount_for_usd(usd_amount: float) -> float:
    price = get_sol_price_usd()
    if price <= 0:
        logger.warning("SOL price unknown.")
        return 0.0
    return round(usd_amount / price, 9)

# -------------------------
# JUPITER QUOTE & SWAP (Anti-MEV)
# -------------------------
def fetch_jupiter_quote(input_mint: str, output_mint: str, amount: int, slippage_bps: int = 50) -> Optional[dict]:
    """
    Calls Jupiter quote endpoint with Anti-MEV flags.
    """
    try:
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": slippage_bps,
            "onlyDirectRoutes": False,
            "asymmetricSlippage": True  # MEV protection
        }
        r = requests.get(JUPITER_QUOTE_API, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        # prefer v6 structure: data -> array of routes
        if "data" in data and data["data"]:
            return data["data"][0]
        # fallback older structure
        if isinstance(data, dict) and data.get("route"):
            return data.get("route")
        logger.warning("No data in Jupiter quote response: %s", data)
        return None
    except Exception as e:
        logger.exception("fetch_jupiter_quote error: %s", e)
        return None

def execute_jupiter_swap_from_quote(quote: dict, priority_fee_sol: Optional[float] = None) -> Optional[str]:
    """
    Use Jupiter /swap endpoint to obtain a swapTransaction (base64), sign and broadcast it.
    Includes prioritizationFeeLamports if provided.
    """
    if DRY_RUN:
        logger.info("[DRY RUN] Would execute Jupiter swap: %s", quote)
        return "SIMULATED_TX_SIGNATURE"
    try:
        payload = {
            "quoteResponse": quote,
            "userPublicKey": PUBLIC_KEY,
            "wrapAndUnwrapSol": True,
            "asymmetricSlippage": True
        }
        # allow either priority_fee_sol param or compute via congestion detection
        if priority_fee_sol is None:
            priority_fee_sol = get_priority_fee(detect_network_congestion())
        try:
            payload["prioritizationFeeLamports"] = int(priority_fee_sol * 1_000_000_000)
        except Exception:
            pass

        r = requests.post(JUPITER_SWAP_API, json=payload, timeout=20)
        r.raise_for_status()
        swap_json = r.json()
        # Jupiter sometimes returns swapTransaction at top-level or inside 'swapTransaction'
        swap_tx_b64 = swap_json.get("swapTransaction") or swap_json.get("swap_tx") or swap_json.get("data", {}).get("swapTransaction")
        # another fallback: some endpoints return base64 under 'result'
        if not swap_tx_b64 and isinstance(swap_json, dict):
            # try nested search
            for v in swap_json.values():
                if isinstance(v, str) and len(v) > 100 and all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=" for c in v[:4]):
                    swap_tx_b64 = v
                    break

        if not swap_tx_b64:
            logger.error("Swap API did not return swapTransaction: %s", swap_json)
            return None

        raw = base64.b64decode(swap_tx_b64)
        # Deserialize unsigned transaction, sign it, and send raw
        tx = Transaction.deserialize(raw)
        # Sign using KEYPAIR (solders Keypair used to provide secret key)
        tx.sign(KEYPAIR)
        serialized = bytes(tx.serialize())
        resp = RPC.send_raw_transaction(serialized, opts=TxOpts(skip_preflight=False, preflight_commitment="processed"))
        sig = None
        if isinstance(resp, dict):
            sig = resp.get("result") or resp.get("signature")
        elif isinstance(resp, str):
            sig = resp
        # fallback field check
        if not sig:
            # some clients return {"jsonrpc":..., "result": "...."}
            try:
                sig = resp.get("result")
            except Exception:
                pass
        if sig:
            logger.info("Broadcasted tx signature: %s", sig)
            return sig
        logger.error("RPC response missing signature: %s", resp)
        return None
    except Exception as e:
        logger.exception("execute_jupiter_swap_from_quote error: %s", e)
        return None

def jupiter_buy(ca_mint: str, amount_sol: float, slippage_bps: int = 50) -> Optional[str]:
    if not is_valid_solana_address(ca_mint):
        logger.error("Invalid token mint: %s", ca_mint)
        return None
    congestion = detect_network_congestion()
    priority_fee_sol = get_priority_fee(congestion)
    logger.info("Preparing BUY %s for %.6f SOL (tip %.3f SOL, congestion=%s, dry_run=%s)", ca_mint, amount_sol, priority_fee_sol, congestion, DRY_RUN)
    if DRY_RUN:
        fake_sig = f"SIMULATED_BUY_{ca_mint[:6]}"
        logger.info("[DRY RUN] Simulated BUY tx signature: %s", fake_sig)
        return fake_sig
    amount_lamports = int(amount_sol * 1e9)
    sol_input_mint = "So11111111111111111111111111111111111111112"
    quote = fetch_jupiter_quote(sol_input_mint, ca_mint, amount_lamports, slippage_bps=slippage_bps)
    if not quote:
        logger.error("No quote for buy")
        return None
    sig = execute_jupiter_swap_from_quote(quote, priority_fee_sol=priority_fee_sol)
    if sig:
        logger.info("Buy tx sent: %s", sig)
    else:
        logger.error("Buy tx failed to broadcast")
    return sig

def jupiter_sell(ca_mint: str, slippage_bps: int = 50) -> Optional[str]:
    if not is_valid_solana_address(ca_mint):
        logger.error("Invalid token mint: %s", ca_mint)
        return None
    amount = get_token_balance_lamports(ca_mint)
    if amount == 0:
        logger.info("No token balance for %s", ca_mint)
        return None
    congestion = detect_network_congestion()
    priority_fee_sol = get_priority_fee(congestion)
    logger.info("Preparing SELL %s amount (lamports=%d) tip %.3f SOL, congestion=%s, dry_run=%s", ca_mint, amount, priority_fee_sol, congestion, DRY_RUN)
    if DRY_RUN:
        fake_sig = f"SIMULATED_SELL_{ca_mint[:6]}"
        logger.info("[DRY RUN] Simulated SELL tx signature: %s", fake_sig)
        return fake_sig
    sol_output_mint = "So11111111111111111111111111111111111111112"
    quote = fetch_jupiter_quote(ca_mint, sol_output_mint, amount, slippage_bps=slippage_bps)
    if not quote:
        logger.error("No quote for sell")
        return None
    sig = execute_jupiter_swap_from_quote(quote, priority_fee_sol=priority_fee_sol)
    if sig:
        logger.info("Sell tx sent: %s", sig)
    else:
        logger.error("Sell tx failed to broadcast")
    return sig