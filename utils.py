# utils.py
import os
import json
import time
import base64
import base58
import fcntl
import logging
from typing import Optional, Dict, Any, Tuple, Sequence, List
from datetime import datetime, timedelta
import requests
from dotenv import load_dotenv

# ============================================================
# Solana libs for signing and broadcast (solders-compatible)
# ============================================================
from solana.rpc.api import Client
from solders.keypair import Keypair
from solders.transaction import Transaction  # NOTE: some builds prefer Transaction.from_bytes
from solana.rpc.types import TxOpts

# ============================================================
# Environment loader (Termux / VULTR friendly)
# ============================================================
def load_env(dotenv_path: Optional[str] = None) -> None:
    """
    Load environment vars from a .env file (or system env if not provided).
    Call this in your main file as: utils.load_env(dotenv_path="~/t.env")
    """
    if dotenv_path:
        load_dotenv(dotenv_path=dotenv_path)
    else:
        load_dotenv()

# Explicitly load "t.env" in home directory on import to make CLI runs easy.
# If your main file calls load_env again, it's harmless (dotenv merges).
load_env(os.path.expanduser("~/t.env"))

# ============================================================
# Logging
# ============================================================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logger = logging.getLogger("ux_solsniper")
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
logger.propagate = False
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_h)

# ============================================================
# Runtime / RPC / Keypair / Basic config from env (manual via t.env)
# ============================================================
DRY_RUN = int(os.getenv("DRY_RUN", "1"))

RPC_URL = os.getenv("RPC_URL") or os.getenv("SOLANA_RPC") or "https://api.mainnet-beta.solana.com"
RPC = Client(RPC_URL)

def _load_keypair_from_env() -> Keypair:
    """
    PRIVATE_KEY can be:
      - JSON array of 64 bytes:   "[12,34,...]"
      - base58 string (phantom):  "5y7...."
      - hex string:               "ab12cd34..."
    """
    sk = os.getenv("PRIVATE_KEY")
    if not sk:
        raise EnvironmentError("PRIVATE_KEY missing in env")
    # JSON array (bytes)
    try:
        if sk.strip().startswith("["):
            arr = json.loads(sk)
            if isinstance(arr, list):
                sk_bytes = bytes(arr)
                return Keypair.from_secret_key(sk_bytes)
    except Exception:
        # fallthrough to other decodings
        pass
    # base58
    try:
        sk_bytes = base58.b58decode(sk)
        return Keypair.from_secret_key(sk_bytes)
    except Exception:
        # hex
        try:
            sk_bytes = bytes.fromhex(sk)
            return Keypair.from_secret_key(sk_bytes)
        except Exception as e:
            logger.exception("Failed to parse PRIVATE_KEY (base58/json/hex): %s", e)
            raise

# Load keypair on import; if PRIVATE_KEY missing this will raise immediately.
KEYPAIR = _load_keypair_from_env()
PUBLIC_KEY = os.getenv("PUBLIC_KEY") or str(KEYPAIR.public_key)

# ============================================================
# Endpoints (Jupiter / Dex)
# ============================================================
JUPITER_QUOTE_API = os.getenv("JUPITER_QUOTE_API", "https://quote-api.jup.ag/v6/quote")
JUPITER_SWAP_API  = os.getenv("JUPITER_SWAP_API",  "https://quote-api.jup.ag/v6/swap")
JUPITER_PRICE_API = os.getenv("JUPITER_PRICE_API", "https://price.jup.ag/v4/price")

DEXSCREENER_API_KEY = os.getenv("DEXSCREENER_API_KEY", "")

# ============================================================
# Bot trading config (ALL configurable via t.env)
# ============================================================
# Source of truth for the *starting* bankroll each cycle/day. Compounding happens in sniper.py.
DAILY_CAPITAL_USD = float(os.getenv("DAILY_CAPITAL_USD", "25"))

# Daily max buys (or tuple "5,4" to rotate across days). Used by sniper.py via CYCLE_LIMIT.
# We keep DAILY_LIMITS only for backward compatibility with older configs that import it.
DAILY_LIMITS = int(os.getenv("DAILY_LIMITS", os.getenv("MAX_BUYS_PER_DAY", "5")))

# Parse CYCLE_LIMIT from env; support single int or tuple like "5,4,6"
_raw_cycle = os.getenv("CYCLE_LIMIT", str(DAILY_LIMITS))
if "," in _raw_cycle:
    try:
        CYCLE_LIMIT: Tuple[int, ...] = tuple(int(x.strip()) for x in _raw_cycle.split(",") if x.strip())
    except Exception:
        logger.warning("Invalid CYCLE_LIMIT format '%s'. Falling back to single value.", _raw_cycle)
        CYCLE_LIMIT = (int(DAILY_LIMITS),)
else:
    # allow int or str that represents int
    try:
        CYCLE_LIMIT = int(_raw_cycle)
    except Exception:
        CYCLE_LIMIT = int(DAILY_LIMITS)

# Risk params
TAKE_PROFIT = float(os.getenv("TAKE_PROFIT", os.getenv("TAKE_PROFIT_MULTIPLIER", "100")))   # %
STOP_LOSS   = float(os.getenv("STOP_LOSS",   os.getenv("STOP_LOSS_PERCENT",     "-20")))    # %

# Priority fees in SOL (names align with your t.env)
NORMAL_PRIORITY_FEE = float(os.getenv("NORMAL_PRIORITY_FEE", os.getenv("NORMAL_TIP_SOL",     "0.015")))
HIGH_PRIORITY_FEE   = float(os.getenv("HIGH_PRIORITY_FEE",   os.getenv("CONGESTION_TIP_SOL", "0.1")))

# MEV toggles
MEV_PROTECTION = int(os.getenv("MEV_PROTECTION", "1"))

# Manual congestion override (1 = force HIGH_PRIORITY_FEE)
MANUAL_CONGESTION = int(os.getenv("MANUAL_CONGESTION", "0"))

# Processed CA store (used to avoid duplicates per day)
PROCESSED_FILE = os.getenv("PROCESSED_FILE", "processed_ca.txt")

# Gas buffer % of USD balance to reserve after each trade (e.g. 0.009 = 0.9%)
GAS_BUFFER = float(os.getenv("GAS_BUFFER", "0.009"))

# ============================================================
# Telegram helper
# ============================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram_message(text: str, bot_token: Optional[str] = None, chat_id: Optional[str] = None) -> None:
    token = bot_token or TELEGRAM_BOT_TOKEN
    cid   = chat_id or TELEGRAM_CHAT_ID
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

# ============================================================
# Processed CA helpers
# ============================================================
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

# ============================================================
# Contract extraction
# ============================================================
import re

def extract_contract_address(message_text: str = None, message_obj=None) -> Optional[str]:
    """
    Try to extract a Solana mint/CA from the text, or from inline button URLs.
    """
    if message_text:
        m = re.search(r"[A-Za-z0-9]{32,44}", message_text)
        if m:
            return m.group(0)
    if message_obj and hasattr(message_obj, "reply_markup") and message_obj.reply_markup:
        try:
            for row in message_obj.reply_markup.rows:
                for button in row.buttons:
                    if hasattr(button, "url") and button.url:
                        # Exclude O/0/I/l ambiguous chars (base58-ish)
                        matches = re.findall(r"[1-9A-HJ-NP-Za-km-z]{32,44}", button.url)
                        if matches:
                            return matches[-1]
        except Exception as e:
            logger.error(f"extract_contract_address button parse error: {e}")
    return None

# ============================================================
# Price / conversions
# ============================================================
def get_sol_price_usd() -> float:
    """
    Get SOL price in USD from Jupiter. Returns 0.0 on failure.
    """
    try:
        r = requests.get(f"{JUPITER_PRICE_API}?ids=SOL", timeout=8)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "data" in data and "SOL" in data["data"]:
            return float(data["data"]["SOL"]["price"])
        if isinstance(data, dict) and "SOL" in data:
            return float(data["SOL"]["price"])
    except Exception as e:
        logger.warning("get_sol_price_usd failed: %s", e)
    return 0.0

def usd_to_sol(usd: float) -> float:
    """
    Convert USD to SOL using the current spot price.
    Returns 0.0 if price unavailable.
    """
    price = get_sol_price_usd()
    if price <= 0:
        logger.warning("SOL price unknown, usd_to_sol returning 0.0")
        return 0.0
    return round(usd / price, 9)

def sol_to_usd(sol: float) -> float:
    """
    Convert SOL to USD using the current spot price.
    Returns 0.0 if price unavailable.
    """
    price = get_sol_price_usd()
    if price <= 0:
        logger.warning("SOL price unknown, sol_to_usd returning 0.0")
        return 0.0
    return round(sol * price, 6)

# ============================================================
# Market cap helpers
# ============================================================
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
    Try dexscreener first, fallback to birdeye.
    """
    mcap = get_market_cap_from_dexscreener(contract_address)
    if mcap:
        return mcap
    return get_market_cap_from_birdeye(contract_address)

# ============================================================
# Network congestion / priority fee
# ============================================================
def detect_network_congestion(timeout_ms: int = 600) -> bool:
    """
    Naive congestion detector based on RPC latency for get_slot().
    If MANUAL_CONGESTION is set, always returns True.
    """
    try:
        if MANUAL_CONGESTION:
            return True
        start = time.time()
        RPC.get_slot()
        elapsed_ms = (time.time() - start) * 1000
        return elapsed_ms > timeout_ms
    except Exception:
        return True  # assume congested on error

def get_priority_fee_manual() -> float:
    """
    Decide priority fee in SOL based on congestion or manual override.
    """
    if MANUAL_CONGESTION:
        return HIGH_PRIORITY_FEE
    try:
        congested = detect_network_congestion()
        return HIGH_PRIORITY_FEE if congested else NORMAL_PRIORITY_FEE
    except Exception:
        return HIGH_PRIORITY_FEE

def get_priority_fee(congestion: Optional[bool] = None) -> float:
    """
    Priority fee in SOL. If congestion is provided, uses that flag.
    """
    if congestion is not None:
        return HIGH_PRIORITY_FEE if congestion else NORMAL_PRIORITY_FEE
    return get_priority_fee_manual()

# ============================================================
# Gas fee calculation & reserve
# ============================================================
def calculate_total_gas_fee(
    amount_in_usd: float,
    congestion: Optional[bool] = None,
    *,
    base_reserve_pct: Optional[float] = None,
    num_priority_txs: int = 1
) -> Optional[float]:
    """
    Estimated *USD* gas/priority cost for a trade.

    - amount_in_usd: your intended USD size for this leg.
    - congestion: override auto-detection (True=high fee, False=normal).
    - base_reserve_pct: if provided, add a base reserve (defaults to GAS_BUFFER env).
    - num_priority_txs: number of priority-fee-bearing txs to account for (1 for buy, 2 for round-trip).

    Returns: total USD fee estimate (float), or None if SOL price unknown.
    """
    sol_price = get_sol_price_usd()
    if sol_price <= 0:
        logger.warning("SOL price unknown; cannot compute gas fee accurately.")
        return None

    # Reserve buffer in USD (user-tunable % of *amount*)
    reserve_pct = GAS_BUFFER if base_reserve_pct is None else base_reserve_pct
    base_fee_usd = amount_in_usd * float(reserve_pct)

    # Priority fee component in USD
    if congestion is None:
        congestion = detect_network_congestion()
    priority_fee_sol = HIGH_PRIORITY_FEE if congestion else NORMAL_PRIORITY_FEE
    priority_fee_usd_per_tx = priority_fee_sol * sol_price

    # Multiply by number of prioritized txs (e.g., 1 buy or 2 buy+sell)
    total_priority_usd = priority_fee_usd_per_tx * max(int(num_priority_txs), 1)

    total_usd = base_fee_usd + total_priority_usd
    return round(total_usd, 6)

def save_gas_reserve_after_trade(current_usd_balance: float, reserve_pct: float = GAS_BUFFER) -> float:
    """
    After each trade completes, skim off a % of the *current* balance to keep as a small gas runway.
    This is intentionally tiny (default 0.9%), and *compounding code in sniper.py* handles reinvest sizing.
    """
    reserve = current_usd_balance * float(reserve_pct)
    new_balance = current_usd_balance - reserve
    logger.info(
        "Saved gas reserve %.6f USD (%.4f%%). New available balance: %.6f",
        reserve, float(reserve_pct) * 100, new_balance
    )
    return round(new_balance, 6)

# ============================================================
# Token balance (RPC)
# ============================================================
def get_token_balance_lamports(token_mint: str) -> int:
    """
    Sum lamports across all token accounts for PUBLIC_KEY / token_mint.
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
            amount_str = (
                acc.get("account", {})
                  .get("data", {})
                  .get("parsed", {})
                  .get("info", {})
                  .get("tokenAmount", {})
                  .get("amount", "0")
            )
            try:
                total += int(amount_str)
            except Exception:
                continue
        return total
    except Exception as e:
        logger.exception("get_token_balance_lamports error: %s", e)
        return 0

# ============================================================
# Jupiter quote & swap (Anti-MEV, solders-compatible)
# ============================================================
def fetch_jupiter_quote(
    input_mint: str,
    output_mint: str,
    amount: int,
    slippage_bps: int = 50,
    only_direct: bool = False
) -> Optional[Dict[str, Any]]:
    """
    Fetch a quote route from Jupiter API for a token swap.
    Returns the *first* route dict (quote) or None.
    """
    try:
        params = {
            "inputMint":          input_mint,
            "outputMint":         output_mint,
            "amount":             str(amount),
            "slippageBps":        slippage_bps,
            "onlyDirectRoutes":   only_direct,
            "asymmetricSlippage": bool(MEV_PROTECTION)
        }
        r = requests.get(JUPITER_QUOTE_API, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()

        # v6 often returns {"data":[{...route...}, ...]}
        if isinstance(data, dict) and "data" in data and data["data"]:
            return data["data"][0]

        # Some servers respond with {"route": {...}}
        if isinstance(data, dict) and data.get("route"):
            return data.get("route")

        logger.warning("No usable route in Jupiter quote: %s", data)
        return None
    except Exception as e:
        logger.exception("fetch_jupiter_quote error: %s", e)
        return None

# ---------- solders compatibility helpers ----------
def _safe_deserialize_transaction(raw: bytes) -> Transaction:
    """
    Handle solders builds that prefer Transaction.deserialize(raw) vs Transaction.from_bytes(raw).
    """
    # Try deserialize first (most common)
    try:
        return Transaction.deserialize(raw)
    except Exception as e1:
        logger.debug("Transaction.deserialize failed; trying Transaction.from_bytes. err=%s", e1)
        try:
            # Some wheels require from_bytes
            return Transaction.from_bytes(raw)  # type: ignore[attr-defined]
        except Exception as e2:
            logger.error("Both Transaction.deserialize and from_bytes failed. e1=%s e2=%s", e1, e2)
            raise

def _safe_sign_transaction(tx: Transaction, keypair: Keypair) -> None:
    """
    Some solders builds want tx.sign([kp]); others want tx.sign(kp).
    Try list first, then single. If both fail, raise the final exception.
    """
    try:
        tx.sign([keypair])  # works on many recent solders versions
        return
    except Exception as e1:
        logger.debug(".sign([KEYPAIR]) failed; trying .sign(KEYPAIR). err=%s", e1)
        try:
            tx.sign(keypair)  # type: ignore[arg-type]
            return
        except Exception as e2:
            logger.error("Failed to sign transaction via both styles. e1=%s e2=%s", e1, e2)
            # raise the most informative exception
            raise

def execute_jupiter_swap_from_quote(quote: dict, congestion: bool = False) -> Optional[str]:
    """
    Execute a Jupiter swap using solders.Transaction.
    Handles DRY_RUN, priority fee, and MEV protection.
    Returns the transaction signature (str) or None on failure.
    """
    if DRY_RUN:
        logger.info("[DRY RUN] execute_jupiter_swap_from_quote - simulated")
        return "SIMULATED_TX_SIGNATURE"

    try:
        payload = {
            "quoteResponse": quote,
            "userPublicKey": PUBLIC_KEY,
            "wrapAndUnwrapSol": True,
            "asymmetricSlippage": bool(MEV_PROTECTION),
        }

        priority_fee_sol = get_priority_fee(congestion)
        payload["prioritizationFeeLamports"] = int(priority_fee_sol * 1_000_000_000)

        r = requests.post(JUPITER_SWAP_API, json=payload, timeout=20)
        r.raise_for_status()
        swap_json = r.json()

        # Extract base64 transaction
        swap_tx_b64 = (
            swap_json.get("swapTransaction")
            or swap_json.get("data", {}).get("swapTransaction")
        )
        if not swap_tx_b64:
            # Fallback: try to detect a long base64 string in the response
            for v in swap_json.values():
                if isinstance(v, str) and len(v) > 100:
                    # additional heuristic: basic base64 charset check
                    if set(v[:4]).issubset(set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")):
                        swap_tx_b64 = v
                        break

        if not swap_tx_b64:
            logger.error("Swap API did not return a swapTransaction: %s", swap_json)
            return None

        raw = base64.b64decode(swap_tx_b64)
        tx = _safe_deserialize_transaction(raw)

        _safe_sign_transaction(tx, KEYPAIR)
        serialized = bytes(tx)

        resp = RPC.send_raw_transaction(
            serialized,
            opts=TxOpts(skip_preflight=False, preflight_commitment="processed"),
        )

        sig = None
        if isinstance(resp, dict):
            sig = resp.get("result") or resp.get("signature")
        elif isinstance(resp, str):
            sig = resp

        if not sig:
            logger.error("No signature returned from RPC: %s", resp)
            return None

        logger.info("Broadcasted tx signature: %s", sig)
        return sig

    except Exception as e:
        logger.exception("execute_jupiter_swap_from_quote error: %s", e)
        return None

def sign_transaction(tx, keypair):
    """
    Backwards-compatible wrapper to sign a solders transaction object.
    """
    try:
        tx.sign([keypair])
    except TypeError:
        # some builds expect the keypair directly
        tx.sign(keypair)
    return tx

# ============================================================
# Small utilities used by reporting / other modules
# ============================================================
def md_code(s: str) -> str:
    """
    Wrap code/addresses in monospace Markdown to send to Telegram.
    """
    if not s:
        return "`N/A`"
    # escape backticks if any
    safe = s.replace("`", "'")
    return f"`{safe}`"

def resolve_token_name(contract_address: str) -> Optional[str]:
    """
    Best-effort token name resolver. For now this is a light wrapper that tries:
      - Dexscreener -> pair name
      - Simple heuristics
    Returns token name (string) or None if unknown.
    """
    if not contract_address:
        return None
    try:
        base = f"https://api.dexscreener.io/latest/dex/pairs/solana/{contract_address}"
        r = requests.get(base, timeout=6)
        if r.ok:
            data = r.json()
            if isinstance(data, dict):
                # "pair" or "pairs" may contain names
                if "pair" in data and data["pair"].get("baseToken", {}).get("name"):
                    return data["pair"]["baseToken"]["name"]
                if "pairs" in data and data["pairs"]:
                    p0 = data["pairs"][0]
                    name = p0.get("pairName") or p0.get("label") or p0.get("baseToken", {}).get("name")
                    if name:
                        return name
    except Exception:
        pass
    # fallback: return shortened address
    return contract_address[:6] + "..." + contract_address[-4:]

# ============================================================
# Long checklist and examples (keeps file explicit & long)
# ============================================================
#
# Checklist / notes:
#
# - Ensure PRIVATE_KEY is set correctly in t.env (base58/json/hex). If not present, utils import will raise.
# - If solders version complains about Transaction.deserialize/from_bytes, the safe helpers above attempt both.
# - To change gas / priority fee behavior: edit GAS_BUFFER / NORMAL_PRIORITY_FEE / HIGH_PRIORITY_FEE in t.env.
# - CYCLE_LIMIT may be a single integer (e.g. 5) or a comma-separated list (e.g. "5,4") to rotate limits across days.
# - Use calculate_total_gas_fee(...) to estimate full USD costs (priority fee + reserve) if you want to pre-size buys conservatively.
# - This utils module intentionally centralizes env reading to avoid duplicated reads in multiple files.
#
# Example usage highlights (for your reference):
#
#   from utils import calculate_total_gas_fee, usd_to_sol, get_priority_fee
#
#   # minimal approach (what the bot already does in many places):
#   priority_fee_sol = get_priority_fee()
#   sol_amount = usd_to_sol(25.0)
#   sol_after_tip = max(sol_amount - priority_fee_sol, 0.0)
#
#   # recommended approach (use USD estimation first to avoid double-counting):
#   est_gas_usd = calculate_total_gas_fee(25.0, congestion=None, num_priority_txs=2) or 0.0
#   usd_for_swap = max(25.0 - est_gas_usd, 0.01)
#   sol_for_swap = usd_to_sol(usd_for_swap)
#
# Troubleshooting:
#
# - If you see RPC latency errors during congestion detection, consider increasing the timeout_ms parameter passed to detect_network_congestion.
# - If your RPC rejects send_raw_transaction, ensure the RPC_URL you provided supports send_raw_transaction and that your keypair is valid.
# - If you see "No signature returned from RPC" check response payload shape. Different RPC providers return different shapes.
# - If you want cycle rotation to survive restarts persist current_cycle_idx somewhere (file, redis, etc.).
#
# Notes on MEV protection:
#
# - Some Jupiter endpoints support "asymmetricSlippage" and other flags; we pass "asymmetricSlippage" param in fetch_jupiter_quote and in the swap payload.
# - MEV_PROTECTION toggle in env (0/1) will be cast to bool and included in requests.
# - We also include prioritizationFeeLamports in the swap payload so Jupiter can attach tip lamports to the transaction.
#
# Security recommendations:
#
# - Keep the PRIVATE_KEY out of version control and limit file access to the service user only.
# - Add the Telethon session file to your .gitignore (.session or custom name).
# - Monitor logs for repeated failed RPC calls — that often indicates either an invalid RPC or a rate-limited endpoint.
#
# Larger design notes:
#
# - The utils module is intended to be the ground-truth for gas & fee calculations. Call calculate_total_gas_fee(...) in sniper.py to ensure consistent accounting.
# - If you prefer the simpler flow: subtract the SOL tip from the SOL amount being swapped and then subtract the USD equivalent after trade completion. That also works but is a bit less tidy in accounting.
# - The safer, recommended approach is to estimate USD gas for round-trip and subtract it before converting USD -> SOL.
#
# End of file remarks:
#
# This file intentionally contains extra documentation and examples because it is a central piece
# of the bot and we want the important usage notes and gotchas captured at runtime.
#
# If you want me to also produce a short unit-test harness (requests mocked) to validate:
# - calculate_total_gas_fee behavior
# - get_priority_fee congestion detection behavior (with a mocked RPC)
# - fetch_jupiter_quote parsing when Jupiter returns different shapes
#
# ...tell me and I will prepare a small test file you can run locally under pytest or plain python.
#
# ============================================================
# EOF - utils.py
# ============================================================