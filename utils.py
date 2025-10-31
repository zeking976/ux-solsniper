"""
utils.py
Comprehensive utilities for Solana Meme Coin Sniper Bot.

Features:
- DRY_RUN simulation mode
- Price & marketcap fetching with fallbacks (Dexscreener, PumpFun, Birdeye, Jupiter)
- Jupiter quote fetching and attempt to build/swap transactions
- Signing & submitting transactions via solders/solana Client
- Telegram helper, JSON record-keeping (buy/sell/processed CA/position state)
- Private key parsing that supports Phantom JSON-array, base58, hex
- Lamports / SOL / USD conversion helpers
- CA extraction helpers with detailed diagnostics
- Compounding balance / DAILY_CAPITAL_USD handling
"""
import asyncio
import base64
import base58
import json
import logging
from loguru import logger
import os
import time
import re
import sys
from datetime import datetime
from typing import Dict, Optional, Tuple, Union
import aiohttp
import requests
import base58
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from ux-solsniper/t.env
env_path = Path(__file__).resolve().parent / "ux-solsniper" / "t.env"
load_dotenv(dotenv_path=env_path)

# ============================================================
# REFERRAL SETTINGS (optional)
# ============================================================
REFERRAL_BPS = int(os.getenv("REFERRAL_FEE_BPS", "0"))
REFERRAL_ACCOUNT = os.getenv("REFERRAL_ACCOUNT", "").strip()

if REFERRAL_ACCOUNT:
    logger.info(f"📎 Using referral account {REFERRAL_ACCOUNT} ({REFERRAL_BPS} bps)")
else:
    logger.info("📎 No referral account configured.")

# --- Solana / Solders Imports ---
from solders.message import to_bytes_versioned
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction as SoldersVersionedTransaction
from solders.transaction import VersionedTransaction

from solana.rpc.api import Client as SolanaClient
from solana.rpc.async_api import AsyncClient
from solana.rpc.types import TxOpts

# ---------- Logging ----------
logger = logging.getLogger("ux-solsniper-utils")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.DEBUG if os.getenv("LOG_LEVEL", "INFO").upper() == "DEBUG" else logging.INFO)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DEXSCREENER_API = os.environ.get("DEXSCREENER_API", "https://api.dexscreener.com/latest/dex/tokens")
# default Jupiter endpoints (v6 for quote, v1 for swap as fallback)
JUPITER_QUOTE_API = os.environ.get("JUPITER_QUOTE_API", "https://api.jup.ag/quote")
JUPITER_SWAP_API = os.environ.get("JUPITER_SWAP_API", "https://api.jup.ag/v6/swap")
RPC_URL = os.environ.get("RPC_URL", "https://api.mainnet-beta.solana.com")
PRIVATE_KEY_RAW = os.environ.get("PRIVATE_KEY", "")
DRY_RUN = int(os.environ.get("DRY_RUN", "1"))  # 1 = simulate, 0 = live
BUY_FEE_PERCENT = float(os.environ.get("BUY_FEE_PERCENT", "1.0"))
SELL_FEE_PERCENT = float(os.environ.get("SELL_FEE_PERCENT", "1.0"))
TRADE_RECORD_FILE = os.environ.get("TRADE_RECORD_FILE", "trade_records.json")
PROCESSED_CA_FILE = os.environ.get("PROCESSED_CA_FILE", "processed_cas.json")
POSITION_STATE_FILE = os.environ.get("POSITION_STATE_FILE", "position_state.json")
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY", "")

# Useful constants
LAMPORTS_PER_SOL = 1_000_000_000
WSOL_MINT = os.environ.get("WSOL_MINT", "So11111111111111111111111111111111111111112")


def _escape_markdown(text: str) -> str:
    return re.sub(r'([_*[\]()~`>#+\-=|{}.!])', r'\\\1', text)

def send_telegram_message(text: str) -> bool:
    """Send a MarkdownV2 message to configured Telegram chat (escaped)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.debug("Telegram token or chat ID not set; skipping message.")
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        safe_text = _escape_markdown(text)
        resp = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": safe_text,
                "parse_mode": "MarkdownV2"
            },
        )
        if resp.status_code == 200:
            return True
        else:
            logger.warning("Failed to send Telegram message: %s", resp.text)
            return False
    except Exception as e:
        logger.exception("Telegram send failed: %s", e)
        return False

# ---------- JSON helpers ----------
def _load_json(file_path: str):
    try:
        with open(file_path, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_json(file_path: str, data):
    with open(file_path, "w") as f:
        json.dump(data, f, indent=2)

# ---------- Processed CA ----------
def is_ca_processed(ca: str) -> bool:
    processed = _load_json(PROCESSED_CA_FILE)
    return ca in processed

def save_processed_ca(ca: str):
    processed = _load_json(PROCESSED_CA_FILE)
    processed[ca] = datetime.utcnow().isoformat()
    _save_json(PROCESSED_CA_FILE, processed)

# ---------- Position / compounding state ----------
def load_position_state() -> dict:
    state = _load_json(POSITION_STATE_FILE)
    if not state:
        # default structure
        state = {
            "current_balance_usd": 0.0,
            "cycle": 0,
            "last_daily_capital": None
        }
    return state

def save_position_state(state: dict):
    _save_json(POSITION_STATE_FILE, state)

def update_compound_balance(
    after_profit_usd: float | None = None,
    usd_in: float | None = None,
    usd_out: float | None = None,
) -> dict:
    """
    Update the compounding balance after *each* completed trade.

    Usage:
      - update_compound_balance(usd_in=29.70, usd_out=35.28)
      - update_compound_balance(after_profit_usd=+5.58)   # backward-compatible

    Behavior:
      * Computes profit = usd_out - usd_in (or uses after_profit_usd if given)
      * Resets internal state if DAILY_CAPITAL_USD changed
      * Tracks reserved_fees for info (buy+sell), not deducted here
      * Persists extra diagnostics for visibility
    """
    # --- compute profit robustly ---
    profit: float | None = None
    try:
        if usd_in is not None and usd_out is not None:
            profit = float(usd_out) - float(usd_in)
        elif after_profit_usd is not None:
            profit = float(after_profit_usd)
        else:
            profit = 0.0
    except Exception:
        profit = 0.0

    # --- load current persisted state ---
    state = load_position_state()  # expected to return dict or {}
    if not isinstance(state, dict):
        state = {}

    # --- read env/config ---
    try:
        daily_cap = float(os.environ.get("DAILY_CAPITAL_USD", "0") or 0)
    except Exception:
        daily_cap = 0.0

    try:
        buy_fee = float(BUY_FEE_PERCENT)
    except Exception:
        buy_fee = 0.0
    try:
        sell_fee = float(SELL_FEE_PERCENT)
    except Exception:
        sell_fee = 0.0

    total_fee_pct = (buy_fee + sell_fee) / 100.0

    # --- reset tracking if daily cap changed ---
    if state.get("last_daily_capital") != daily_cap:
        logger.info(
            "🔄 DAILY_CAPITAL_USD changed (was=%s now=%s). Resetting compound state.",
            state.get("last_daily_capital"), daily_cap
        )
        state = {
            "current_balance_usd": 0.0,
            "cycle": 0,
            "last_daily_capital": daily_cap,
            # diagnostics
            "cumulative_profit_usd": 0.0,
            "total_invested_usd": 0.0,
            "total_proceeds_usd": 0.0,
            "last_update_ts": None,
            "last_trade_result": None,
            "last_profit_usd": 0.0,
        }

    # --- initialize missing fields safely ---
    state.setdefault("current_balance_usd", 0.0)
    state.setdefault("cycle", 0)
    state.setdefault("cumulative_profit_usd", 0.0)
    state.setdefault("total_invested_usd", 0.0)
    state.setdefault("total_proceeds_usd", 0.0)

    # --- update aggregates ---
    if usd_in is not None:
        try:
            state["total_invested_usd"] += float(usd_in)
        except Exception:
            pass
    if usd_out is not None:
        try:
            state["total_proceeds_usd"] += float(usd_out)
        except Exception:
            pass

    # add profit into the compounding balance
    try:
        state["current_balance_usd"] = float(state["current_balance_usd"]) + float(profit or 0.0)
    except Exception:
        # last resort: don’t crash the bot if file is malformed
        state["current_balance_usd"] = float(profit or 0.0)

    # informational: how much fees will be reserved for a new cycle
    state["reserved_fees_usd"] = float(daily_cap) * float(total_fee_pct)

    # meta fields
    state["cycle"] = int(state.get("cycle", 0)) + 1
    state["last_daily_capital"] = daily_cap
    state["last_update_ts"] = datetime.utcnow().isoformat() + "Z"
    state["last_trade_result"] = "WIN" if (profit or 0.0) > 0 else ("LOSS" if (profit or 0.0) < 0 else "BREAKEVEN")
    state["last_profit_usd"] = float(profit or 0.0)

    # --- persist atomically ---
    try:
        save_position_state(state)
    except Exception as e:
        logger.exception("Failed to persist compound state: %s", e)

    logger.info(
        "💰 Compound updated: Δprofit=%+.2f | balance=%.2f | reserved_fees=%.2f | cycle=%d",
        float(profit or 0.0),
        float(state.get("current_balance_usd", 0.0)),
        float(state.get("reserved_fees_usd", 0.0)),
        int(state.get("cycle", 0)),
    )
    return state
# ---------- Trade records ----------
def record_buy(
    ca: str,
    coin_name: str,
    market_cap: float,
    usd_amount_gross: float,
    usd_amount_net: float,
    priority_fee_sol: float,
    fee_usd: float | None = None,
    price_usd: float | None = None,
):
    """
    Save buy info to TRADE_RECORD_FILE.
    Stores both gross (before fees) and net (after fees) USD spent, plus
    market cap, coin name, fee, entry price, and timestamp.
    Keeps priority fee value intact for later analytics.
    """
    records = _load_json(TRADE_RECORD_FILE)
    timestamp = datetime.utcnow().isoformat()

    if ca not in records:
        records[ca] = {}

    records[ca]["buy"] = {
        "coin_name": coin_name,
        "market_cap": float(market_cap) if market_cap else None,
        "price_usd": float(price_usd) if price_usd else None,   # 🔹 Added entry price
        "usd_amount_gross": float(usd_amount_gross),
        "usd_amount_net": float(usd_amount_net),
        "fee_usd": float(fee_usd) if fee_usd else None,
        "priority_fee_sol": float(priority_fee_sol),
        "timestamp": timestamp,
    }

    _save_json(TRADE_RECORD_FILE, records)
    logger.info(
        "💾 Recorded BUY | %s | coin=%s | mcap=%.2f | price_usd=%s | gross=$%.2f | net=$%.2f | fee=$%.4f | priority_fee=%.3f SOL",
        ca,
        coin_name,
        market_cap or 0,
        f'{price_usd:.10f}' if price_usd else "n/a",
        usd_amount_gross,
        usd_amount_net,
        fee_usd or 0.0,
        priority_fee_sol,
    )

def record_sell(
    ca: str,
    coin_name: str,
    market_cap: float,
    usd_amount_gross: float,
    usd_amount_net: float,
    priority_fee_sol: float,
    fee_usd: float | None = None,
    price_usd: float | None = None,
):
    """
    Save sell info to TRADE_RECORD_FILE.
    Stores both gross (before fees) and net (after fees) USD received,
    plus automatic profit/loss computation if matching buy info exists.
    Keeps priority fee for reporting and analytics.
    """
    records = _load_json(TRADE_RECORD_FILE)
    timestamp = datetime.utcnow().isoformat()

    if ca not in records:
        records[ca] = {}

    buy_info = records[ca].get("buy", {})
    profit = None
    entry_net = None
    entry_mcap = None
    entry_price = None

    # Compute net PnL using stored buy info
    if buy_info:
        try:
            entry_net = float(buy_info.get("usd_amount_net", 0))
            profit = float(usd_amount_net) - entry_net
            entry_mcap = float(buy_info.get("market_cap", 0))
            entry_price = float(buy_info.get("price_usd", 0))
        except Exception:
            profit = None

    records[ca]["sell"] = {
        "coin_name": coin_name,
        "market_cap": float(market_cap) if market_cap else None,
        "price_usd": float(price_usd) if price_usd else None,   # 🔹 Added exit price
        "usd_amount_gross": float(usd_amount_gross),
        "usd_amount_net": float(usd_amount_net),
        "fee_usd": float(fee_usd) if fee_usd else None,
        "priority_fee_sol": float(priority_fee_sol),
        "profit": float(profit) if profit is not None else None,
        "timestamp": timestamp,
    }

    _save_json(TRADE_RECORD_FILE, records)

    # Log detailed outcome
    logger.info(
        "💾 Recorded SELL | %s | coin=%s | mcap=%.2f | exit_price=%s | gross=$%.2f | net=$%.2f | fee=$%.4f | priority_fee=%.3f SOL | profit=%s",
        ca,
        coin_name,
        market_cap or 0,
        f'{price_usd:.10f}' if price_usd else "n/a",
        usd_amount_gross,
        usd_amount_net,
        fee_usd or 0.0,
        priority_fee_sol,
        (f"${profit:+.2f}" if profit is not None else "n/a"),
    )

    # Update compound balance after a real or simulated sell
    try:
        if profit is not None:
            new_state = update_compound_balance(float(profit))
            logger.info(
                "🔁 Compound balance updated after SELL | profit=%+.2f | new_balance=%.6f | cycle=%d",
                profit,
                new_state.get("current_balance_usd", 0.0),
                new_state.get("cycle", 0),
            )
    except Exception as e:
        logger.warning("⚠️ Failed to update compound balance after SELL %s: %s", ca, e)
# ---------- Utility helpers ----------
def md_code(text: str) -> str:
    return f"`{text}`"

def resolve_token_name(contract_address: str) -> str:
    return f"TKN_{contract_address[-6:]}" if contract_address else "N/A"

def format_coin_name(ca: str) -> str:
    if not ca:
        return "UNKNOWN"
    return f"`{ca}`"

def safe_div(a, b):
    try:
        return a / b
    except Exception:
        return 0

async def sleep_with_logging(seconds: float, reason: str = ""):
    if reason:
        logger.info("Sleeping %.2fs: %s", seconds, reason)
    await asyncio.sleep(seconds)

# ---------- HTTP helpers ----------
async def _async_json_get(session: aiohttp.ClientSession, url: str, timeout: int = 10) -> Optional[dict]:
    try:
        async with session.get(url, timeout=timeout) as resp:
            text = await resp.text()
            try:
                return json.loads(text)
            except Exception:
                try:
                    return await resp.json()
                except Exception as e:
                    logger.debug("Failed to parse JSON from %s: %s", url, e)
                    return None
    except Exception as e:
        logger.debug("HTTP GET failed for %s: %s", url, e)
        return None

# compatibility wrapper expected by sniper.py
async def fetch_json(url: str, timeout: int = 10) -> Optional[dict]:
    async with aiohttp.ClientSession() as session:
        return await _async_json_get(session, url, timeout=timeout)

# ---------- Price & MCAP fetching with fallbacks ----------
async def fetch_token_price_and_mcap(ca: str) -> Dict[str, Optional[float]]:
    """
    Priority:
      1) Dexscreener JSON (priceUsd, circulatingSupply, marketCap)
      2) Jupiter Lite API (/tokens/v2/search) - priceUsd, mcap, liquidity (retries up to 2x)
    Returns dict:
        {
            "priceUsd": float|None,
            "circulatingSupply": float|None,
            "marketCap": float|None,
            "liquidity": float|None,
            "source": str
        }
    """
    result = {
        "priceUsd": None,
        "circulatingSupply": None,
        "marketCap": None,
        "liquidity": None,
        "source": None
    }

    ca_param = ca
    async with aiohttp.ClientSession() as session:
        # 1️⃣ Dexscreener
        ds_url = f"{DEXSCREENER_API}/{ca_param}"
        logger.debug("Attempting Dexscreener for %s -> %s", ca, ds_url)
        ds_data = await _async_json_get(session, ds_url)
        if ds_data:
            pairs = ds_data.get("pairs") or []
            token_info = ds_data.get("tokenInfo") or {}
            ds_price = ds_supply = ds_mcap = None

            if pairs and isinstance(pairs, list) and len(pairs) > 0:
                first = pairs[0] or {}
                ds_price = first.get("priceUsd") or first.get("price")
                ds_mcap = first.get("marketCap")
                ds_supply = first.get("circulatingSupply") or token_info.get("circulatingSupply")

            if not ds_price:
                ds_price = token_info.get("priceUsd") or token_info.get("price")
            if not ds_supply:
                ds_supply = token_info.get("circulatingSupply")
            if not ds_mcap:
                ds_mcap = token_info.get("marketCap")

            try:
                price = float(ds_price) if ds_price is not None else None
                supply = float(ds_supply) if ds_supply is not None else None
                mcap = float(ds_mcap) if ds_mcap is not None else None
            except Exception:
                price = supply = mcap = None

            if mcap:
                result.update({
                    "priceUsd": price,
                    "circulatingSupply": supply,
                    "marketCap": mcap,
                    "source": "dexscreener:mcap"
                })
                logger.info("✅ Dexscreener MCAP for %s: %.2f (price=%s, supply=%s)", ca, mcap, price, supply)
                return result

            if price is not None and supply is not None:
                computed = price * supply
                result.update({
                    "priceUsd": price,
                    "circulatingSupply": supply,
                    "marketCap": computed,
                    "source": "dexscreener:calc"
                })
                logger.info("ℹ️ Dexscreener computed MCAP for %s: %.2f (price=%.8f × supply=%.2f)", ca, computed, price, supply)
                return result

            if price is not None:
                result.update({
                    "priceUsd": price,
                    "circulatingSupply": None,
                    "marketCap": None,
                    "source": "dexscreener:price"
                })
                logger.info("ℹ️ Dexscreener price only for %s: %.8f", ca, price)
                return result

        # 2️⃣ Jupiter Lite API fallback (with retries)
        j_url = f"https://lite-api.jup.ag/tokens/v2/search?query={ca_param}"
        for attempt in range(1, 3):
            try:
                j_data = await _async_json_get(session, j_url)
                if j_data and isinstance(j_data, list) and len(j_data) > 0:
                    token = j_data[0]
                    j_price = token.get("usdPrice")
                    j_mcap = token.get("mcap")
                    j_liquidity = token.get("liquidity")

                    price = float(j_price) if j_price is not None else None
                    mcap = float(j_mcap) if j_mcap is not None else None
                    liquidity = float(j_liquidity) if j_liquidity is not None else None

                    if mcap:
                        result.update({
                            "priceUsd": price,
                            "circulatingSupply": None,
                            "marketCap": mcap,
                            "liquidity": liquidity,
                            "source": "jupiter:mcap"
                        })
                        logger.info("🪙 Jupiter MCAP for %s: %.2f | price=%.8f | liquidity=%.2f",
                                    ca, mcap, price or 0, liquidity or 0)
                        return result

                    elif price:
                        result.update({
                            "priceUsd": price,
                            "circulatingSupply": None,
                            "marketCap": None,
                            "liquidity": liquidity,
                            "source": "jupiter:price"
                        })
                        logger.info("🪙 Jupiter price only for %s: %.8f | liquidity=%.2f",
                                    ca, price or 0, liquidity or 0)
                        return result

                if attempt < 2:
                    await asyncio.sleep(0.6)
            except Exception:
                if attempt < 2:
                    await asyncio.sleep(0.6)
                    continue

        logger.warning("⚠️ Jupiter Lite API failed for %s after 2 retries — skipping.", ca)

    logger.debug("All price/mcap/liquidity fallbacks failed for %s", ca)
    return result
#------------Mcap--------------
async def get_market_cap_or_priceinfo(
    ca: str,
) -> Tuple[float, Optional[float], Optional[float], str, Optional[float], Optional[bool], Optional[float]]:
    """
    Fetch token price and market cap info for a given contract address.
    Returns tuple: (mcap, price, supply, source, sell_tax, liq_locked, liquidity)
    """
    info = await fetch_token_price_and_mcap(ca)
    if not info:
        logger.warning("No token info returned for %s", ca)
        return 0.0, None, None, "none", None, None, None

    price = info.get("priceUsd")
    supply = info.get("circulatingSupply")
    mcap = info.get("marketCap")
    source = info.get("source") or "none"

    # Optional fields (if provided by API)
    sell_tax = info.get("sellTax")  # e.g., 0.05 means 5%
    liq_locked = info.get("liquidityLocked")  # True/False/None

    # Liquidity can appear as float or nested dict (e.g. {"usd": 1234})
    liq_field = info.get("liquidity") or info.get("liquidityUsd")
    liquidity = None
    try:
        if isinstance(liq_field, dict):
            liquidity = float(liq_field.get("usd") or 0)
        elif liq_field is not None:
            liquidity = float(liq_field)
    except Exception as e:
        logger.debug("Failed to parse liquidity for %s: %s", ca, e)
        liquidity = None

    # Prefer provided market cap
    if mcap:
        try:
            return float(mcap), float(price) if price else None, supply, source, sell_tax, liq_locked, liquidity
        except Exception as e:
            logger.debug("Invalid MCAP type for %s: %s", ca, e)

    # Compute fallback MCAP if missing
    if price is not None and supply is not None:
        try:
            computed = float(price) * float(supply)
            logger.debug("Computed MCAP from price*supply for %s: %.2f", ca, computed)
            return computed, float(price), supply, f"{source}:computed", sell_tax, liq_locked, liquidity
        except Exception as e:
            logger.debug("Failed to compute fallback MCAP for %s: %s", ca, e)

    # Default fallback
    return 0.0, float(price) if price else None, supply, source, sell_tax, liq_locked, liquidity
# ---------- SOL price helpers ----------

def get_sol_price_usd() -> float:
    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",
            timeout=10
        )
        return float(resp.json()["solana"]["usd"])
    except Exception as e:
        logger.debug("Coingecko SOL price fetch failed: %s — defaulting to 20.0", e)
        return 20.0
def usd_to_sol(usd_amount: float, apply_buy_fee: bool = False) -> float:
    """Convert USD → SOL, optionally applying BUY_FEE_PERCENT deduction."""
    sol_price = get_sol_price_usd()
    if apply_buy_fee:
        usd_amount = usd_amount * (1.0 - BUY_FEE_PERCENT / 100.0)
    return usd_amount / sol_price


def sol_to_usd(sol_amount: float) -> float:
    """Convert SOL → USD."""
    return sol_amount * get_sol_price_usd()


def usd_to_lamports(usd_amount: float, apply_buy_fee: bool = False) -> int:
    """Convert USD → lamports, optionally applying BUY_FEE_PERCENT deduction."""
    sol = usd_to_sol(usd_amount, apply_buy_fee=apply_buy_fee)
    return int(round(sol * LAMPORTS_PER_SOL))


def lamports_to_usd(lamports: int) -> float:
    """Convert lamports → USD."""
    return sol_to_usd(lamports / LAMPORTS_PER_SOL)
# ---------- Private key parsing helpers ----------
def _parse_private_key_bytes(pk_raw: str) -> Optional[bytes]:
    """
    Attempts to parse a PRIVATE_KEY string into raw bytes.
    Accepts:
      - JSON array (Phantom export) e.g. "[1,2,3,...]"
      - base58 encoded
      - hex string
    Returns raw bytes (32 or 64) or None.
    """
    if not pk_raw:
        return None
    s = pk_raw.strip()
    # JSON array
    if s.startswith("[") and s.endswith("]"):
        try:
            arr = json.loads(s)
            if isinstance(arr, list) and all(isinstance(x, int) for x in arr):
                b = bytes(arr)
                logger.debug("Parsed PRIVATE_KEY as JSON-array (len=%d)", len(b))
                return b
        except Exception:
            pass
    # base58
    try:
        b = base58.b58decode(s)
        if len(b) in (32, 64):
            logger.debug("Parsed PRIVATE_KEY as base58 (len=%d)", len(b))
            return b
    except Exception:
        pass
    # hex
    try:
        b = bytes.fromhex(s)
        if len(b) in (32, 64):
            logger.debug("Parsed PRIVATE_KEY as hex (len=%d)", len(b))
            return b
    except Exception:
        pass
    logger.warning("Unable to parse PRIVATE_KEY; expected JSON-array, base58 or hex.")
    return None

# ---------- Jupiter swap / signing / submission ----------

def _create_keypair_from_secret(secret_bytes: bytes) -> Keypair:
    """
    Try multiple Keypair constructors for solders Keypair compatibility.
    Returns a Keypair object or raises.
    """
    if not secret_bytes:
        raise ValueError("secret_bytes is empty")

    # Try direct from_bytes (32 or 64)
    try:
        return Keypair.from_bytes(secret_bytes)
    except Exception:
        pass

    # Try from_seed (first 32 bytes)
    try:
        if len(secret_bytes) >= 32:
            return Keypair.from_seed(secret_bytes[:32])
    except Exception:
        pass

    # Fallback
    raise RuntimeError(
        "Unable to construct Keypair from provided secret bytes. "
        "Ensure you supplied a Phantom-compatible key or base58/hex secret."
    )


def _ensure_keypair(raw) -> Keypair:
    """
    Normalize raw input (Keypair, bytes, str) into a solders.Keypair object.
    Supports:
      - Existing Keypair
      - Bytes/bytearray (32 or 64)
      - JSON array of ints (Solana CLI format)
      - Base58 string (Phantom export / solana-keygen)
      - Hex string
    """
    # Already a Keypair
    if isinstance(raw, Keypair):
        return raw

    # Bytes-like
    if isinstance(raw, (bytes, bytearray)):
        return _create_keypair_from_secret(bytes(raw))

    # String inputs
    if isinstance(raw, str):
        s = raw.strip()

        # Case: JSON array of ints
        if s.startswith("[") and s.endswith("]"):
            try:
                arr = json.loads(s)
                if isinstance(arr, list):
                    return _create_keypair_from_secret(bytes(arr))
            except Exception:
                pass

        # Case: Base58 string
        try:
            import base58 as _b58
            decoded = _b58.b58decode(s)
            return _create_keypair_from_secret(decoded)
        except Exception:
            pass

        # Case: Hex string
        try:
            decoded = bytes.fromhex(s)
            return _create_keypair_from_secret(decoded)
        except Exception:
            pass

    # If nothing worked
    raise RuntimeError("Unable to turn provided privkey into a Keypair for signing")
# === Jupiter Swap Execution ===
# ----Jupiter_Swap----
async def execute_jupiter_swap_from_quote(
    session: aiohttp.ClientSession,
    quote: dict,
    privkey: str | Keypair,
    pubkey: str,
    fee_percent: float | None = None,
    coin_name: str | None = None,
    market_cap: float | None = None,
    priority_fee_sol: float | None = None,
    payer_privkey: str | Keypair = None,
) -> str | None:
    """
    Execute a BUY swap using Jupiter Ultra API.
    Flow: /ultra/v1/order -> sign -> /ultra/v1/execute
    - DRY_RUN=1 simulates without sending TX.
    - Applies BUY_FEE_PERCENT exactly once.
    - Use payer_privkey for small swaps (<$15) to bypass gasless minimum.
    - Sets referralFeeBps=0 to disable referral fees.
    """

    DRY_RUN = bool(int(os.getenv("DRY_RUN", "0")))
    BUY_FEE_PERCENT = float(os.getenv("BUY_FEE_PERCENT", "0"))
    RPC_URL = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
    # Load payer_privkey from env if not provided
    payer_privkey = payer_privkey or os.getenv("PRIVATE_KEY")

    fee_percent = fee_percent if fee_percent is not None else BUY_FEE_PERCENT

    # === Fetch live SOL price using Jupiter Ultra API ===
    async def fetch_sol_usd_price() -> float:
        PRICE_URL = "https://lite-api.jup.ag/price/v3?ids=So11111111111111111111111111111111111111112"
        try:
            async with session.get(PRICE_URL, timeout=10) as r:
                data = await r.json()
                sol_key = "So11111111111111111111111111111111111111112"
                if sol_key in data and "usdPrice" in data[sol_key]:
                    price = float(data[sol_key]["usdPrice"])
                    logger.info(f"💵 Live SOL price: ${price:.2f}")
                    return price
                else:
                    logger.error(f"❌ Invalid SOL price response: {data}")
                    return 150.0  # Fallback price
        except Exception as e:
            logger.error(f"❌ Failed to fetch SOL price: {e}")
            return 150.0  # Fallback price

    SOL_PRICE_USD = await fetch_sol_usd_price()
    if SOL_PRICE_USD == 150.0:
        logger.warning(f"⚠️ Using fallback SOL price: ${SOL_PRICE_USD:.2f}")

    input_mint = quote.get("inputMint")
    output_mint = quote.get("outputMint")
    in_amount = int(quote["inAmount"])

    # === Apply total buy fee ===
    if fee_percent > 0:
        adjusted = int(in_amount * (1 - fee_percent / 100))
        logger.info(f"💰 Applying BUY fee {fee_percent:.2f}%: {in_amount} → {adjusted}")
        in_amount = adjusted

    # === Validate minimum amount for gasless transactions ===
    usd_value = (in_amount / 1e9) * SOL_PRICE_USD
    if usd_value < 15.0 and not payer_privkey:
        logger.error(f"❌ Swap amount ${usd_value:.2f} is below $15 minimum for gasless transactions. Provide payer_privkey or set PRIVATE_KEY in env.")
        return None

    # === Load wallet ===
    try:
        wallet = privkey if isinstance(privkey, Keypair) else Keypair.from_base58_string(privkey.strip())
        pubkey_str = str(wallet.pubkey())
    except Exception as e:
        logger.error(f"❌ Failed to load wallet keypair: {e}")
        return None

    payer_wallet = None
    if payer_privkey:
        try:
            payer_wallet = payer_privkey if isinstance(payer_privkey, Keypair) else Keypair.from_base58_string(payer_privkey.strip())
        except Exception as e:
            logger.error(f"❌ Failed to load payer wallet keypair: {e}")
            return None

    # === DRY RUN ===
    if DRY_RUN:
        fake_tx = f"DRY_RUN_BUY_{int(time.time())}"
        fee_usd = usd_value * (fee_percent / 100.0)
        try:
            update_compound_balance(after_profit_usd=-usd_value)
            record_buy(
                ca=output_mint,
                coin_name=coin_name or "Unknown",
                market_cap=market_cap or 0,
                usd_amount_gross=usd_value,
                usd_amount_net=usd_value - fee_usd,
                fee_usd=fee_usd,
                priority_fee_sol=priority_fee_sol or 0,
            )
            logger.info(f"✅ DRY_RUN BUY recorded for {coin_name or output_mint}")
        except Exception as e:
            logger.warning(f"⚠️ DRY_RUN record failed: {e}")
        return fake_tx

    # === Check SOL balance ===
    try:
        async with AsyncClient(RPC_URL) as rpc:
            bal_resp = await rpc.get_balance(wallet.pubkey())
            lamports = bal_resp.value
        if lamports < (in_amount + 200_000):
            logger.error(f"❌ Insufficient SOL balance ({lamports/1e9:.6f} SOL).")
            return None
    except Exception as e:
        logger.warning(f"⚠️ Balance check failed: {e}")

    ORDER_URL = "https://lite-api.jup.ag/ultra/v1/order"
    EXEC_URL = "https://lite-api.jup.ag/ultra/v1/execute"
    wallet = privkey if isinstance(privkey, Keypair) else Keypair.from_base58_string(privkey.strip())
    params = {
    "inputMint": input_mint,
    "outputMint": output_mint,
    "amount": str(in_amount),
    "taker": pubkey,
    }
    # Optional Referral Params
    if REFERRAL_BPS > 0 and REFERRAL_ACCOUNT:
        params["referralFee"] = REFERRAL_BPS
        params["referralAccount"] = REFERRAL_ACCOUNT
        logger.debug(f"🪙 Added referral params → {REFERRAL_ACCOUNT} ({REFERRAL_BPS} bps)")

    # Set payer and closeAuthority safely
    payer_key = None
    if payer_wallet:
        payer_key = str(payer_wallet.pubkey())
    else:
        payer_key = str(wallet.pubkey())  # fallback

    params["payer"] = payer_key
    params["closeAuthority"] = payer_key  # always match payer

    logger.debug(f"👛 Using payer={payer_key} | closeAuthority={payer_key}")

    # === Execute BUY ===
    for attempt in range(1, 4):
        try:
            async with session.get(ORDER_URL, params=params, timeout=15) as r:
                if r.headers.get("Content-Type", "").startswith("text/plain"):
                    text = await r.text()
                    logger.error(f"❌ Non-JSON response from /order: {text} (Status: {r.status})")
                    continue
                order = await r.json()

            if not order.get("transaction"):
                logger.error(f"❌ Invalid order: {order}")
                continue
            # === OFFICIAL JUPITER PYTHON SIGNING (FIXED) ===
            tx_bytes = base64.b64decode(order["transaction"])
            tx = VersionedTransaction.from_bytes(tx_bytes)

            # Create signed transaction using populate
            signed_tx_obj = VersionedTransaction.populate(
                tx.message,
                [wallet.sign_message(to_bytes_versioned(tx.message))]
            )
            # Serialize to bytes → then base64 encode
            signed_tx = base64.b64encode(bytes(signed_tx_obj)).decode("utf-8")
            # =================================================
            payload = {"signedTransaction": signed_tx, "requestId": order["requestId"]}
            async with session.post(EXEC_URL, json=payload, timeout=20) as resp:
                if resp.headers.get("Content-Type", "").startswith("text/plain"):
                    text = await resp.text()
                    logger.error(f"❌ Non-JSON response from /execute: {text} (Status: {resp.status})")
                    continue
                res = await resp.json()

            if res.get("status", "").lower() == "success":
                sig = res.get("signature") or res.get("txid")
                usd_value = (in_amount / 1e9) * SOL_PRICE_USD
                fee_usd = usd_value * (fee_percent / 100.0)
                update_compound_balance(after_profit_usd=-usd_value)
                record_buy(
                    ca=output_mint,
                    coin_name=coin_name or "Unknown",
                    market_cap=market_cap or 0,
                    usd_amount_gross=usd_value,
                    usd_amount_net=usd_value - fee_usd,
                    fee_usd=fee_usd,
                    priority_fee_sol=priority_fee_sol or 0,
                )
                logger.info(f"🚀 BUY success | {coin_name or output_mint}")
                logger.info(f"🔗 Solscan: https://solscan.io/tx/{sig}")
                return sig
            else:
                logger.warning(f"Attempt {attempt}/3 failed: {res}")
        except Exception as e:
            logger.warning(f"⚠️ Attempt {attempt}/3 error: {e}")
            await asyncio.sleep(attempt * 2)

    logger.error(f"❌ BUY failed after 3 retries for {coin_name or output_mint}")
    return None
# ---------Jupiter_Swap------------
def sanitize_mint(mint: str) -> Optional[str]:
    """Ensure mint is a valid base58 Solana address (basic check)."""
    if not mint:
        return None
    if re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", mint):
        return mint
    return None

# ---------- Network & fee helpers ----------
def detect_network_congestion():
    return False
def get_dynamic_fee(base_percent=1.0, congestion=False):
    return base_percent + 0.5 if congestion else base_percent

# ----Detect_Cas-----------
def extract_contract_address(msg) -> Optional[dict]:
    """
    Extract CA from Telegram message:
      1) Try buttons URL first
      2) Try t.me start param
      3) Fallback: raw text pattern
    Returns {"ca": "<...>"} or None
    """
    ca = None
    diagnostics = {"buttons_present": False, "buttons_have_urls": False, "buttons_url_matched": False, "text_has_ca_pattern": False}
    text = getattr(msg, "text", "") or getattr(msg, "message", "") or ""
    # 1) Buttons
    try:
        buttons = getattr(msg, "buttons", None) or []
        if buttons:
            diagnostics["buttons_present"] = True
        for row in buttons:
            for btn in row:
                url = getattr(btn, "url", "") or ""
                if url:
                    diagnostics["buttons_have_urls"] = True
                    m = re.search(r"([1-9A-HJ-NP-Za-km-z]{32,44}(?:pump|bonk)?)", url)
                    if m:
                        ca = m.group(1)
                        diagnostics["buttons_url_matched"] = True
                        break
            if ca:
                break
    except Exception:
        logger.debug("Error while scanning buttons for CA", exc_info=True)

    # 2) t.me start=CA link
    if not ca and text:
        m = re.search(r"https?://t\.me/[^\s?]+\?start=([1-9A-HJ-NP-Za-km-z]{32,44}(?:pump|bonk)?)", text)
        if m:
            ca = m.group(1)

    # 3) fallback text search
    if not ca and text:
        m = re.search(r"([1-9A-HJ-NP-Za-km-z]{32,44}(?:pump|bonk)?)", text)
        if m:
            ca = m.group(1)
            diagnostics["text_has_ca_pattern"] = True

    if not ca:
        logger.debug("extract_contract_address diagnostics: %s, text_sample=%s", diagnostics, (text[:400] if text else ""))
        return None
    return {"ca": ca}

# ---------- DRY_RUN support ----------
def is_buy_allowed() -> bool:
    # placeholder, integrate cycle/daily limit logic as needed
    return True

def on_successful_buy():
    if DRY_RUN:
        logger.info("DRY_RUN: Buy simulated successfully")

# ---------- Async queue helpers ----------
async def enqueue_ca(queue, ca: str):
    if not is_ca_processed(ca):
        await queue.put(ca)
        logger.debug("CA queued: %s", ca)
    else:
        logger.debug("CA skipped, already processed: %s", ca)
async def dequeue_ca(queue):
    if queue.empty():
        return None
    return await queue.get()

# End of utils.py
