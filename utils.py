# utils.py
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

# Solana libs for signing and broadcast
from solana.rpc.api import Client
from solders.keypair import Keypair
from solana.transaction import Transaction
from solana.rpc.types import TxOpts

# -------------------------
# Environment loader (Termux / VULTR friendly)
# -------------------------
def load_env(dotenv_path: Optional[str] = None) -> None:
    """
    Load environment from a .env file (default uses system env if not provided).
    Call this in your main file as utils.load_env(dotenv_path=...)
    """
    if dotenv_path:
        load_dotenv(dotenv_path=dotenv_path)
    else:
        load_dotenv()

# Immediately (useful if modules import utils directly)
# NOTE: the main scripts also call load_env explicitly; calling it here is harmless.
load_env(os.path.expanduser("~/t.env"))

# -------------------------
# Logging
# -------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logger = logging.getLogger("ux_solsniper")
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
if not logger.handlers:
    fh = logging.StreamHandler()
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(fh)

# -------------------------
# Runtime / RPC / Keypair / Basic config from env (manual via t.env)
# -------------------------
# DRY_RUN: 1 = simulate / don't broadcast, 0 = real
DRY_RUN = int(os.getenv("DRY_RUN", "1"))

# RPC URL: t.env may call it RPC_URL or SOLANA_RPC; support both
RPC_URL = os.getenv("RPC_URL") or os.getenv("SOLANA_RPC") or "https://api.mainnet-beta.solana.com"
RPC = Client(RPC_URL)

# Keypair loader - supports base58 secretkey OR JSON array string
def _load_keypair_from_env() -> Keypair:
    sk = os.getenv("PRIVATE_KEY")
    if not sk:
        raise EnvironmentError("PRIVATE_KEY missing in env")
    # try JSON array
    try:
        if sk.strip().startswith("["):
            arr = json.loads(sk)
            sk_bytes = bytes(arr)
            return Keypair.from_secret_key(sk_bytes)
    except Exception:
        # fall through to base58/hex attempt
        pass
    # try base58
    try:
        sk_bytes = base58.b58decode(sk)
        return Keypair.from_secret_key(sk_bytes)
    except Exception:
        # try hex
        try:
            sk_bytes = bytes.fromhex(sk)
            return Keypair.from_secret_key(sk_bytes)
        except Exception as e:
            logger.exception("Failed to parse PRIVATE_KEY (base58/json/hex): %s", e)
            raise

KEYPAIR = _load_keypair_from_env()
PUBLIC_KEY = os.getenv("PUBLIC_KEY")  # public key string for RPC payloads

# -------------------------
# Jupiter / Dex endpoints (configurable)
# -------------------------
JUPITER_QUOTE_API = os.getenv("JUPITER_QUOTE_API", "https://quote-api.jup.ag/v6/quote")
JUPITER_SWAP_API = os.getenv("JUPITER_SWAP_API", "https://quote-api.jup.ag/v6/swap")
JUPITER_PRICE_API = os.getenv("JUPITER_PRICE_API", "https://price.jup.ag/v4/price")

# Dexscreener / Birdeye keys (optional)
DEXSCREENER_API_KEY = os.getenv("DEXSCREENER_API_KEY", "")
# -------------------------
# Bot trading config (read-only here; change in t.env)
# -------------------------
# Investment & daily limits might be primarily managed by sniper.py but expose defaults
INVESTMENT_USD = float(os.getenv("INVESTMENT_USD", os.getenv("DAILY_CAPITAL_USD", "25")))  # backward compat
DAILY_LIMITS = os.getenv("DAILY_LIMITS", os.getenv("MAX_BUYS_PER_DAY", "5"))  # string or csv
CYCLE_LIMIT = int(os.getenv("CYCLE_LIMIT", "1"))

# Auto TP & SL config (make them configurable via t.env)
# These values are used by sniper.py for monitoring
TAKE_PROFIT = float(os.getenv("TAKE_PROFIT", os.getenv("TAKE_PROFIT_MULTIPLIER", "100")))  # percent (e.g. 100 for +100%)
STOP_LOSS = float(os.getenv("STOP_LOSS", os.getenv("STOP_LOSS_PERCENT", "-20")))           # percent negative (e.g. -20)

# Transaction priority fees - manual config in t.env
NORMAL_PRIORITY_FEE = float(os.getenv("NORMAL_PRIORITY_FEE", os.getenv("NORMAL_TIP_SOL", "0.015")))
HIGH_PRIORITY_FEE = float(os.getenv("HIGH_PRIORITY_FEE", os.getenv("CONGESTION_TIP_SOL", "0.1")))
MEV_PROTECTION = int(os.getenv("MEV_PROTECTION", os.getenv("MEV_PROTECTION", "1")))  # 1=enable asymmetricSlippage

# Manual congestion toggle (0 or 1). When 1, we use HIGH_PRIORITY_FEE
MANUAL_CONGESTION = int(os.getenv("MANUAL_CONGESTION", "0"))

# Processed CA file (keeps track of already-processed contract addresses)
PROCESSED_FILE = os.getenv("PROCESSED_FILE", "processed_ca.txt")

# -------------------------
# Telegram helper
# -------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram_message(text: str, bot_token: Optional[str] = None, chat_id: Optional[str] = None) -> None:
    """
    Send a Telegram message using bot token & chat id (from env by default).
    keep parse_mode = Markdown to support formatted messages.
    Accepts optional bot_token/chat_id override.
    """
    token = bot_token or TELEGRAM_BOT_TOKEN
    cid = chat_id or TELEGRAM_CHAT_ID
    if not token or not cid:
        logger.warning("[!] Telegram token or chat id missing; message not sent.")
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": int(cid), "text": text, "parse_mode": "Markdown"}
        r = requests.post(url, json=payload, timeout=10)
        if not r.ok:
            logger.warning("Telegram send failed: %s", r.text)
    except Exception as e:
        logger.exception("send_telegram_message error: %s", e)

# -------------------------
# Processed CA helpers (file lock safe)
# -------------------------
def save_processed_ca(ca: str) -> None:
    try:
        with open(PROCESSED_FILE, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(ca + "\n")
            f.flush()
            os.fsync(f.fileno())
            fcntl.flock(f, fcntl.LOCK_UN)
    except Exception as e:
        logger.exception("save_processed_ca error: %s", e)

def is_ca_processed(ca: str) -> bool:
    try:
        if not os.path.exists(PROCESSED_FILE):
            return False
        with open(PROCESSED_FILE, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            data = f.read().splitlines()
            fcntl.flock(f, fcntl.LOCK_UN)
            return ca in data
    except Exception as e:
        logger.exception("is_ca_processed error: %s", e)
        return False

def clear_processed_ca() -> None:
    try:
        with open(PROCESSED_FILE, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.truncate(0)
            fcntl.flock(f, fcntl.LOCK_UN)
    except Exception as e:
        logger.exception("clear_processed_ca error: %s", e)

# -------------------------
# Contract extraction
# -------------------------
def extract_contract_address(text: str) -> Optional[str]:
    """
    Extract a 32-44 char base58-like token mint from a message.
    Returns first match or None.
    """
    if not text:
        return None
    import re
    # Basic base58-ish pattern (letters+digits) length 32-44
    m = re.search(r"[A-Za-z0-9]{32,44}", text)
    return m.group(0) if m else None

# -------------------------
# Price / conversions
# -------------------------
def get_sol_price_usd() -> float:
    """
    Use Jupiter price endpoint to fetch SOL price (fallbacks to 0.0 on error).
    """
    try:
        r = requests.get(f"{JUPITER_PRICE_API}?ids=SOL", timeout=8)
        r.raise_for_status()
        data = r.json()
        # v4 returns data -> SOL -> price
        if isinstance(data, dict) and "data" in data and "SOL" in data["data"]:
            return float(data["data"]["SOL"]["price"])
        # fallback structure
        if isinstance(data, dict) and "SOL" in data:
            return float(data["SOL"]["price"])
    except Exception as e:
        logger.warning("get_sol_price_usd failed: %s", e)
    return 0.0

def usd_to_sol(usd: float) -> float:
    """
    Convert USD to SOL using current price; returns 0.0 if price unknown.
    """
    price = get_sol_price_usd()
    if price <= 0:
        logger.warning("SOL price unknown, usd_to_sol returning 0.0")
        return 0.0
    return round(usd / price, 9)

# -------------------------
# Market cap helpers
# -------------------------
def get_market_cap_from_dexscreener(contract_address: str, max_retries: int = 3) -> Optional[float]:
    base = f"https://api.dexscreener.io/latest/dex/pairs/solana/{contract_address}"
    headers = {}
    if DEXSCREENER_API_KEY:
        headers["Authorization"] = f"Bearer {DEXSCREENER_API_KEY}"
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(base, headers=headers, timeout=8)
            r.raise_for_status()
            data = r.json()
            # structure: { "pairs": [ { "fdv": .., ... } ] }
            if isinstance(data, dict):
                if "pairs" in data and data["pairs"]:
                    fdv = data["pairs"][0].get("fdv") or data["pairs"][0].get("marketCap")
                    if fdv:
                        return float(fdv)
                if "pair" in data:
                    fdv = data["pair"].get("fdv") or data["pair"].get("marketCap")
                    if fdv:
                        return float(fdv)
            return None
        except Exception as e:
            logger.warning("dexscreener attempt %d failed: %s", attempt, e)
            time.sleep(1 + attempt)
    return None

def get_market_cap_from_birdeye(contract_address: str) -> Optional[float]:
    # Public birdeye fallback
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
    except Exception as e:
        logger.warning("birdeye fetch failed: %s", e)
    return None

def get_market_cap(contract_address: str) -> Optional[float]:
    """
    Try dexscreener first then birdeye fallback.
    """
    mcap = get_market_cap_from_dexscreener(contract_address)
    if mcap:
        return mcap
    return get_market_cap_from_birdeye(contract_address)

# -------------------------
# Network congestion / priority fee
# -------------------------
def detect_network_congestion(timeout_ms: int = 600) -> bool:
    """
    Heuristic: measure a small RPC call latency (get_slot).
    If latency > timeout_ms (ms), consider congested.
    Returns boolean. Use MANUAL_CONGESTION env to override.
    """
    try:
        if MANUAL_CONGESTION:
            return True
        start = time.time()
        RPC.get_slot()
        elapsed_ms = (time.time() - start) * 1000
        return elapsed_ms > timeout_ms
    except Exception:
        # If RPC fails, be conservative and say congested
        return True

def get_priority_fee_manual() -> float:
    """
    Choose priority fee based on manual toggle CONGESTION or measured.
    Uses env NORMAL_PRIORITY_FEE and HIGH_PRIORITY_FEE.
    """
    if MANUAL_CONGESTION:
        return HIGH_PRIORITY_FEE
    try:
        congested = detect_network_congestion()
        return HIGH_PRIORITY_FEE if congested else NORMAL_PRIORITY_FEE
    except Exception:
        return HIGH_PRIORITY_FEE

# -------------------------
# Gas fee calculation & reserve
# -------------------------
def calculate_total_gas_fee(amount_in_usd: float, congestion: Optional[bool] = None) -> Optional[float]:
    """
    Rough estimate of total gas fees in USD:
    - base: percentage of amount_in_usd (use 0.9% default for buys/sells combined)
    - priority fee in SOL converted to USD
    """
    sol_price = get_sol_price_usd()
    if sol_price <= 0:
        logger.warning("SOL price unknown; cannot compute gas fee accurately.")
        return None
    base_fee_usd = amount_in_usd * 0.009  # default 0.9% (configurable in code if desired)
    if congestion is None:
        congestion = detect_network_congestion()
    priority_fee_sol = HIGH_PRIORITY_FEE if congestion else NORMAL_PRIORITY_FEE
    priority_fee_usd = priority_fee_sol * sol_price * 2.0  # assume tip for buy+sell combined (approx)
    total = base_fee_usd + priority_fee_usd
    return round(total, 6)

def save_gas_reserve_after_trade(current_usd_balance: float, reserve_pct: float = 0.0009) -> float:
    """
    Reserve a small percentage for gas; return new usable balance.
    """
    reserve = current_usd_balance * reserve_pct
    new_balance = current_usd_balance - reserve
    logger.info("Saved gas reserve %.6f USD (%.4f%%). New available balance: %.6f", reserve, reserve_pct*100, new_balance)
    return round(new_balance, 6)

# -------------------------
# Token balance (RPC)
# -------------------------
def get_token_balance_lamports(token_mint: str) -> int:
    """
    Returns total lamports (token smallest units) of token_mint owned by PUBLIC_KEY.
    Uses RPC getTokenAccountsByOwner to aggregate.
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
        r = requests.post(RPC_URL, json=body, timeout=8)
        r.raise_for_status()
        data = r.json()
        accounts = data.get("result", {}).get("value", [])
        total = 0
        for acc in accounts:
            amount_str = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {}).get("tokenAmount", {}).get("amount", "0")
            try:
                total += int(amount_str)
            except Exception:
                continue
        return total
    except Exception as e:
        logger.exception("get_token_balance_lamports error: %s", e)
        return 0

# -------------------------
# Jupiter quote & swap (Anti-MEV)
# -------------------------
def fetch_jupiter_quote(input_mint: str, output_mint: str, amount: int, slippage_bps: int = 50, only_direct: bool = False) -> Optional[Dict[str, Any]]:
    """
    Call Jupiter quote endpoint with Anti-MEV flags (asymmetricSlippage True).
    Returns the first route object or None.
    amount is integer lamports for SOL or token smallest units for tokens.
    """
    try:
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": slippage_bps,
            "onlyDirectRoutes": only_direct,
            "asymmetricSlippage": bool(MEV_PROTECTION)
        }
        r = requests.get(JUPITER_QUOTE_API, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        # v6 structure: data -> list of routes
        if isinstance(data, dict) and "data" in data and data["data"]:
            return data["data"][0]
        # older fallback
        if isinstance(data, dict) and data.get("route"):
            return data.get("route")
        logger.warning("No usable route in Jupiter quote: %s", data)
        return None
    except Exception as e:
        logger.exception("fetch_jupiter_quote error: %s", e)
        return None

def execute_jupiter_swap_from_quote(quote: Dict[str, Any], priority_fee_sol: Optional[float] = None) -> Optional[str]:
    """
    POST to Jupiter /swap to get a base64 unsigned transaction, sign with KEYPAIR and broadcast.
    Returns signature string on success or None.
    """
    if DRY_RUN:
        logger.info("[DRY RUN] execute_jupiter_swap_from_quote - simulated")
        return "SIMULATED_TX_SIGNATURE"

    try:
        payload = {
            "quoteResponse": quote,
            "userPublicKey": PUBLIC_KEY,
            "wrapAndUnwrapSol": True,
            "asymmetricSlippage": bool(MEV_PROTECTION)
        }
        if priority_fee_sol is None:
            priority_fee_sol = get_priority_fee_manual()
        try:
            payload["prioritizationFeeLamports"] = int(priority_fee_sol * 1_000_000_000)
        except Exception:
            # ignore if invalid
            pass

        r = requests.post(JUPITER_SWAP_API, json=payload, timeout=20)
        r.raise_for_status()
        swap_json = r.json()

        # find base64 swap transaction
        swap_tx_b64 = swap_json.get("swapTransaction") or swap_json.get("data", {}).get("swapTransaction") or None
        # fallback search for likely base64 string
        if not swap_tx_b64:
            for v in swap_json.values():
                if isinstance(v, str) and len(v) > 100 and set(v[:4]).issubset(set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")):
                    swap_tx_b64 = v
                    break

        if not swap_tx_b64:
            logger.error("Swap API did not return a swapTransaction: %s", swap_json)
            return None

        raw = base64.b64decode(swap_tx_b64)
        tx = Transaction.deserialize(raw)
        tx.sign(KEYPAIR)
        serialized = bytes(tx.serialize())

        # send raw
        resp = RPC.send_raw_transaction(serialized, opts=TxOpts(skip_preflight=False, preflight_commitment="processed"))
        sig = None
        if isinstance(resp, dict):
            sig = resp.get("result") or resp.get("signature")
        elif isinstance(resp, str):
            sig = resp
        logger.info("Broadcasted tx signature: %s", sig)
        return sig
    except Exception as e:
        logger.exception("execute_jupiter_swap_from_quote error: %s", e)
        return None

# -------------------------
# High-level buy / sell wrappers
# -------------------------
def jupiter_buy(ca_mint: str, sol_amount: float, slippage_bps: int = 50, tip: Optional[float] = None) -> Optional[str]:
    """
    Buy `ca_mint` using SOL input amount (in SOL units).
    tip: optional manual priority fee in SOL (overrides manual detection).
    """
    if DRY_RUN:
        fake = f"SIMULATED_BUY_{ca_mint[:8]}_{int(time.time())}"
        logger.info("[DRY RUN] jupiter_buy simulated: %s", fake)
        return fake

    if not ca_mint or sol_amount <= 0:
        logger.error("Invalid buy parameters")
        return None

    try:
        sol_input_mint = "So11111111111111111111111111111111111111112"
        amount_lamports = int(sol_amount * 1e9)
        quote = fetch_jupiter_quote(sol_input_mint, ca_mint, amount_lamports, slippage_bps=slippage_bps)
        if not quote:
            logger.error("No quote received for buy")
            return None
        priority = tip if tip is not None else get_priority_fee_manual()
        sig = execute_jupiter_swap_from_quote(quote, priority_fee_sol=priority)
        return sig
    except Exception as e:
        logger.exception("jupiter_buy error: %s", e)
        return None

def jupiter_sell(ca_mint: str, slippage_bps: int = 50, tip: Optional[float] = None) -> Optional[str]:
    """
    Sell entire balance of ca_mint to SOL.
    """
    if DRY_RUN:
        fake = f"SIMULATED_SELL_{ca_mint[:8]}_{int(time.time())}"
        logger.info("[DRY RUN] jupiter_sell simulated: %s", fake)
        return fake

    try:
        amount = get_token_balance_lamports(ca_mint)
        if amount <= 0:
            logger.info("No token balance for %s", ca_mint)
            return None
        sol_output_mint = "So11111111111111111111111111111111111111112"
        quote = fetch_jupiter_quote(ca_mint, sol_output_mint, amount, slippage_bps=slippage_bps)
        if not quote:
            logger.error("No quote for sell")
            return None
        priority = tip if tip is not None else get_priority_fee_manual()
        sig = execute_jupiter_swap_from_quote(quote, priority_fee_sol=priority)