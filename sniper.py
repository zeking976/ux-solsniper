#!/usr/bin/env python3
"""
sniper.py
- Main runner for Solana Meme Coin Sniper Bot🌱🟣
- Reads config from t.env (env vars)
- Listens to Telegram target channel for new contract addresses (🔥 messages)
- Enqueues CA, fetches price/marketcap (via utils), executes buys via Jupiter (utils),
  records trades, starts monitors for TP/SL and posts Telegram notifications.
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import aiohttp
import statistics
import re
import base64
import time
from datetime import date, datetime
from typing import Optional
from loguru import logger
from telethon import TelegramClient, events
import utils

from solders.pubkey import Pubkey
from solana.rpc.api import Client as SolanaClient
from solana.rpc.async_api import AsyncClient
from solana.rpc.types import TxOpts
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from utils import record_sell, update_compound_balance, fetch_token_price_and_mcap

# ---------- Logging ----------
logger = logging.getLogger("ux-solsniper")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.DEBUG if os.getenv("LOG_LEVEL", "INFO").upper() == "DEBUG" else logging.INFO)

# ---------- Config (from t.env) ----------
DRY_RUN = int(os.environ.get("DRY_RUN", "1"))
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
API_ID = int(os.environ.get("TELEGRAM_API_ID", "0"))
API_HASH = os.environ.get("TELEGRAM_API_HASH", "")
TARGET_CHANNEL_ID = int(os.environ.get("TARGET_CHANNEL_ID", "0"))
SESSION_NAME = os.environ.get("SESSION_NAME", "sniper_session")
# ----------Defined---------
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex/tokens"
# Trading config
DAILY_CAPITAL_USD = float(os.environ.get("DAILY_CAPITAL_USD", "10"))
MAX_BUYS_PER_DAY = int(os.environ.get("MAX_BUYS_PER_DAY", "10"))
BUY_FEE_PERCENT = float(os.environ.get("BUY_FEE_PERCENT", "1.0"))
SELL_FEE_PERCENT = float(os.environ.get("SELL_FEE_PERCENT", "1.0"))
STOP_LOSS = float(os.environ.get("STOP_LOSS", "-20"))  # percent, negative number
TAKE_PROFIT = float(os.environ.get("TAKE_PROFIT", "40"))  # percent
TRADE_SLEEP_SEC = float(os.environ.get("TRADE_SLEEP_SEC", "5.0"))
TARGET_MULTIPLIER = float(os.environ.get("TARGET_MULTIPLIER", "1.4"))
CYCLE_LIMIT_RAW = os.environ.get("CYCLE_LIMIT", "")
CYCLE_LIMIT = ([int(x.strip()) for x in CYCLE_LIMIT_RAW.split(",") if x.strip()] if CYCLE_LIMIT_RAW else [])

# ---------- Helpers from utils ----------
usd_to_sol = utils.usd_to_sol
sol_to_usd = utils.sol_to_usd
get_sol_price_usd = utils.get_sol_price_usd
execute_jupiter_swap_from_quote = utils.execute_jupiter_swap_from_quote
is_ca_processed = utils.is_ca_processed
save_processed_ca = utils.save_processed_ca
record_buy = utils.record_buy
record_sell = utils.record_sell
is_buy_allowed = utils.is_buy_allowed
on_successful_buy = utils.on_successful_buy
get_dynamic_fee = utils.get_dynamic_fee
detect_network_congestion = utils.detect_network_congestion
send_telegram_message = utils.send_telegram_message
extract_contract_address = utils.extract_contract_address
enqueue_ca = utils.enqueue_ca
dequeue_ca = utils.dequeue_ca
sleep_with_logging = utils.sleep_with_logging
format_coin_name = utils.format_coin_name
get_market_cap_or_priceinfo = utils.get_market_cap_or_priceinfo
fetch_token_price_and_mcap = utils.fetch_token_price_and_mcap
fetch_json = getattr(utils, "fetch_json", None)  # compatibility if available

# ---------- State ----------
SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000
_pending_cas: asyncio.Queue = asyncio.Queue()
daily_trades = 0
_last_cycle_date = date.today()
BALANCE_FILE = "balance.json"
current_usd_balance = None

# # ---------- Telegram client ----------
client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
@client.on(events.NewMessage(chats=TARGET_CHANNEL_ID))
async def _on_new_message(event):
    """
    Robust Telegram handler:
      - logs raw message
      - cleans leading invisible chars
      - ensures the message is a '🔥' pick (either first char of first line or present in first line)
      - tries to extract CA using utils.extract_contract_address (which checks buttons and text)
      - enqueues CA for processing
      - logs detailed diagnostics on failure to extract CA
    """
    try:
        msg = event.message
        # best raw text extraction Telethon provides
        raw_text = getattr(event, "raw_text", None) or getattr(msg, "text", "") or getattr(msg, "message", "") or ""
        logger.info("📩 Raw incoming Telegram message (repr): %r", raw_text)
        # Clean leading zero-width/invisible whitespace then strip
        cleaned = re.sub(r'^[\s\u200B\u200C\u200D\uFEFF]+', '', raw_text).strip() if raw_text else ""
        logger.debug("Cleaned text (first 200 chars): %s", cleaned[:200])
        if not cleaned:
            logger.debug("Message contained no text after cleaning, skipping")
            return
        first_line = cleaned.splitlines()[0] if cleaned.splitlines() else cleaned
        first_char = first_line[0] if first_line else ""
        try:
            logger.debug("First char: %r (U+%04X)", first_char, ord(first_char) if first_char else 0)
        except Exception:
            logger.debug("First char: %r", first_char)
        # Skip messages starting with 📈 ,💰 or 🏆 (these are not new CA posts)
        if first_char in ("📈", "💰", "🏆"):
            logger.info("Skipping message due to first character: %s", first_char)
            return
        # Accept messages that either start with 🔥 or contain 🔥 on the first line
        if not (first_line.startswith("🔥") or "🔥" in first_line):
            logger.debug("Message does not start with or contain a leading 🔥, skipping")
            return
        # Use the extractor (which prefers buttons then text)
        res = extract_contract_address(msg)
        logger.debug("extract_contract_address -> %s", res)
        if not res or not res.get("ca"):
            # Try to give more diagnostics if possible
            diagnostics = {
                "buttons_present": False,
                "buttons_have_urls": False,
                "buttons_url_matched": False,
                "text_has_ca_pattern": False,
            }
            try:
                buttons = getattr(msg, "buttons", None) or []
                if buttons:
                    diagnostics["buttons_present"] = True
                    for row in buttons:
                        for btn in row:
                            url = getattr(btn, "url", "") or ""
                            if url:
                                diagnostics["buttons_have_urls"] = True
                                if re.search(r"([1-9A-HJ-NP-Za-km-z]{32,44})(?:pump|bonk)?", url):
                                    diagnostics["buttons_url_matched"] = True
                                    break
                        if diagnostics["buttons_url_matched"]:
                            break
                text = getattr(msg, "text", "") or getattr(msg, "message", "") or ""
                if text and re.search(r"([1-9A-HJ-NP-Za-km-z]{32,44})(?:pump|bonk)?", text):
                    diagnostics["text_has_ca_pattern"] = True
            except Exception:
                pass
            logger.warning("Could not extract contract address from message; diagnostics=%s", diagnostics)
            return
        ca = res["ca"]
        if is_ca_processed(ca):
            logger.info("CA already processed, skipping: %s", ca)
            return
        allowed = True
        try:
            allowed = is_buy_allowed()
        except Exception:
            allowed = True
        if not allowed:
            logger.info("Buys not allowed currently by cycle settings - saving CA and skipping: %s", ca)
            try:
                save_processed_ca(ca)
            except Exception:
                logger.warning("Failed to save processed CA %s", ca)
            return
        await enqueue_ca(_pending_cas, ca)
        logger.info("✅     Enqueued CA for processing: %s", ca)
    except Exception as e:
        logger.exception("Error in _on_new_message: %s", e)

# ---------Token_name----------
def resolve_token_name(ca: str) -> str:
    """Simple fallback resolver for token name."""
    try:
        import requests
        url = f"https://api.dexscreener.com/latest/dex/tokens/{ca}"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        pairs = data.get("pairs")
        if pairs and isinstance(pairs, list):
            first = pairs[0]
            return first.get("baseToken", {}).get("symbol") or first.get("baseToken", {}).get("name") or ca[:8]
    except Exception:
        pass
    return ca[:8]
# ---------- Trade execution & processing ----------
async def process_pending_cas():
    """
    Background worker that consumes _pending_cas queue and:
      - fetches price/mcap via get_market_cap_or_priceinfo
      - applies liquidity / sell-tax / liq-locked filters (defensive parsing)
      - decides trade amounts (auto-subtracts buy fee)
      - fetches Jupiter quote
      - executes swap via execute_jupiter_swap_from_quote()
      - records buy & spawns monitor_position for that CA
    """
    global daily_trades
    try:
        privkey = os.getenv("PRIVATE_KEY")
        if not privkey:
            logger.error("No PRIVATE_KEY in environment — cannot sign transactions")
            return

        # Derive pubkey
        try:
            from solders.keypair import Keypair
            import base58
            try:
                kp = Keypair.from_bytes(base58.b58decode(privkey))
            except Exception:
                arr = json.loads(privkey)
                if isinstance(arr, list):
                    kp = Keypair.from_bytes(bytes(arr))
                else:
                    raise
            pubkey = str(kp.pubkey())
        except Exception as e:
            logger.exception("Failed to prepare keypair from PRIVATE_KEY: %s", e)
            return

        async with aiohttp.ClientSession() as session:
            while True:
                ca = await dequeue_ca(_pending_cas)
                if not ca:
                    await sleep_with_logging(1.0, "No CA in queue, waiting...")
                    continue

                try:
                    # --- Fetch price + mcap ---
                    try:
                        token_info = await get_market_cap_or_priceinfo(ca)
                    except Exception as e:
                        logger.debug("get_market_cap_or_priceinfo raised: %s", e)
                        token_info = None

                    mcap_val = 0.0
                    price_usd = None
                    supply = None
                    price_source = "none"

                    if isinstance(token_info, (list, tuple)):
                        if len(token_info) >= 4:
                            mcap_val = float(token_info[0] or 0.0)
                            price_usd = token_info[1]
                            supply = token_info[2]
                            price_source = token_info[3] or "none"
                        else:
                            try:
                                mcap_val = float(token_info[0] or 0.0)
                            except Exception:
                                mcap_val = 0.0
                    elif isinstance(token_info, dict):
                        mcap_val = float(token_info.get("marketCap") or token_info.get("mcap") or 0.0)
                        price_usd = token_info.get("priceUsd") or token_info.get("price")
                        supply = token_info.get("circulatingSupply") or token_info.get("supply")
                        price_source = token_info.get("source") or "none"
                    else:
                        try:
                            mcap_val, price_usd, supply, price_source = await get_market_cap_or_priceinfo(ca)
                            mcap_val = float(mcap_val or 0.0)
                        except Exception:
                            mcap_val = 0.0
                            price_usd = None
                            price_source = "none"

                    coin_name = resolve_token_name(ca)

                    # --- Dexscreener liquidity fetch ---
                    liquidity_usd = 0.0
                    volume_usd = 0.0
                    try:
                        ds_url = f"{DEXSCREENER_API}/{ca}"
                        ds_data = await fetch_json(ds_url, timeout=6)
                        if ds_data and isinstance(ds_data, dict):
                            pairs = ds_data.get("pairs") or []
                            if isinstance(pairs, list) and pairs:
                                first = pairs[0] or {}
                                liq = first.get("liquidity") or {}
                                if isinstance(liq, dict):
                                    liquidity_usd = float(
                                        liq.get("usd") or liq.get("USD")
                                        or liq.get("quote", {}).get("USD", {}).get("value", 0) or 0
                                    )
                                else:
                                    try:
                                        liquidity_usd = float(liq)
                                    except Exception:
                                        liquidity_usd = 0.0

                                vol = first.get("volume") or {}
                                if isinstance(vol, dict):
                                    volume_usd = float(
                                        vol.get("h24") or vol.get("h1")
                                        or vol.get("m5") or vol.get("usd") or 0
                                    )
                                else:
                                    try:
                                        volume_usd = float(vol)
                                    except Exception:
                                        volume_usd = 0.0
                    except Exception as e:
                        logger.debug("Dexscreener liquidity fetch failed for %s: %s", ca, e)

                    sell_tax = None
                    liq_locked = None
                    try:
                        if 'ds_data' in locals() and isinstance(ds_data, dict):
                            token_info_block = ds_data.get("tokenInfo") or {}
                            sell_tax = token_info_block.get("sellTax") or token_info_block.get("tax", {}).get("sell")
                    except Exception:
                        sell_tax = None

                    logger.info(
                        "Processing CA %s (%s) PriceUsd=%s, MCAP=%.2f, Liq=%.2f, Vol=%.2f (src=%s)",
                        ca, coin_name, (price_usd if price_usd is not None else "N/A"),
                        float(mcap_val or 0.0), float(liquidity_usd or 0.0),
                        float(volume_usd or 0.0), price_source
                    )

                    # --- Filters ---
                    if price_usd is None:
                        logger.warning("No priceUsd found for %s (src=%s). Skipping.", ca, price_source)
                        save_processed_ca(ca)
                        continue

                    if liquidity_usd <= 0:
                        logger.info("❌ Skipping %s — no liquidity found (%.2f)", coin_name, liquidity_usd)
                        save_processed_ca(ca)
                        continue

                    try:
                        ratio = (mcap_val / liquidity_usd) if liquidity_usd else float("inf")
                    except Exception:
                        ratio = float("inf")

                    MAX_RATIO = float(os.getenv("MAX_MCAP_LIQ_RATIO", "10"))
                    if ratio > MAX_RATIO:
                        logger.info("❌ Skipping %s — MCAP/LIQ ratio too high (%.2f > %.2f)", coin_name, ratio, MAX_RATIO)
                        save_processed_ca(ca)
                        continue

                    try:
                        if sell_tax is not None and float(sell_tax) > 0:
                            logger.info("❌ Skipping %s — has sell tax (%.2f%%)", coin_name, float(sell_tax))
                            save_processed_ca(ca)
                            continue
                    except Exception:
                        pass

                    if liq_locked is False:
                        logger.info("❌ Skipping %s — liquidity not locked", coin_name)
                        save_processed_ca(ca)
                        continue

                    # --- Trade execution ---
                    usd_amount = DAILY_CAPITAL_USD * (1.0 - BUY_FEE_PERCENT / 100)
                    sol_amount = usd_to_sol(usd_amount)
                    if usd_amount <= 0 or sol_amount <= 0:
                        logger.warning("Non-positive buy amount USD=%.8f SOL=%.12f — skipping %s", usd_amount, sol_amount, ca)
                        save_processed_ca(ca)
                        continue

                    congestion = detect_network_congestion()
                    priority_fee_sol = get_dynamic_fee(BUY_FEE_PERCENT, congestion)
                    lamports = int(round(sol_amount * LAMPORTS_PER_SOL))
                    if lamports <= 0:
                        logger.warning("Computed lamports 0 for %.2f USD; skipping %s", usd_amount, ca)
                        save_processed_ca(ca)
                        continue
                    try:
                        quote = {
                            "inputMint": SOL_MINT,
                            "outputMint": ca,
                            "inAmount": int(sol_amount * 1e9),
                        }

                        tx_sig = await execute_jupiter_swap_from_quote(
                            session=session,
                            quote=quote,
                            privkey=privkey,
                            pubkey=pubkey,
                            fee_percent=BUY_FEE_PERCENT,
                            coin_name=coin_name,
                        )

                    except Exception as e:
                        logger.exception("Unexpected error during swap for %s: %s", ca, e)
                        save_processed_ca(ca)
                        continue

                    fee_usd = usd_amount * BUY_FEE_PERCENT / 100
                    usd_amount_gross = usd_amount + fee_usd
                    usd_amount_net = usd_amount

                    try:
                        record_buy(ca, coin_name, mcap_val or 0.0, usd_amount_gross, usd_amount_net, priority_fee_sol, fee_usd)
                        on_successful_buy()
                        save_processed_ca(ca)
                        daily_trades += 1
                    except Exception as e:
                        logger.exception("Error during buy record or post-processing: %s", e)

                    buy_msg = (
                        f"✅ BUY executed\n"
                        f"Coin: {coin_name}\n"
                        f"CA: {format_coin_name(ca)}\n"
                        f"Price (USD): {price_usd:.8f}\n"
                        f"MCAP: ${float(mcap_val or 0.0):.2f}\n"
                        f"Amount (USD, net): ${usd_amount_net:.2f}\n"
                        f"Fee (USD): ${fee_usd:.2f}\n"
                        f"Amount (USD, gross): ${usd_amount_gross:.2f}\n"
                        f"Priority Fee (SOL est): {priority_fee_sol}\n"
                        f"TX: {tx_sig}"
                    )
                    logger.info("Buy executed: CA=%s, tx=%s", ca, tx_sig)

                    try:
                        send_telegram_message(buy_msg)
                    except Exception as e:
                        logger.warning("Failed to send buy telegram for %s: %s", ca, e)

                    # --- DRY_RUN bookkeeping ---
                    if DRY_RUN:
                        try:
                            entry_price_val = float(price_usd) if price_usd is not None else None
                            entry_mcap_val = float(mcap_val) if mcap_val is not None else None

                            async def _record_buy_demo(amount_usd, ca_val, coin_name_val, txid, entry_price_val, entry_mcap_val):
                                async with SIM_LOCK:
                                    if SIM_STATE["balance"] < float(amount_usd):
                                        logger.warning("DRY_RUN: insufficient simulated balance for buy of $%s", amount_usd)
                                        return False
                                    SIM_STATE["balance"] -= float(amount_usd)
                                    SIM_STATE["buys_today"] = SIM_STATE.get("buys_today", 0) + 1
                                    SIM_STATE["history"].append({
                                        "ca": ca_val,
                                        "coin_name": coin_name_val,
                                        "buy_usd": float(amount_usd),
                                        "usd_in": float(amount_usd),
                                        "usd_out": None,
                                        "entry_price": entry_price_val,
                                        "entry_mcap": entry_mcap_val,
                                        "tx": txid,
                                        "timestamp": datetime.utcnow().isoformat() + "Z"
                                    })
                                await save_sim_state()
                                logger.info("DRY_RUN: simulated BUY recorded: $%.2f -> balance $%.2f", amount_usd, SIM_STATE["balance"])
                                return True

                            ok = await _record_buy_demo(usd_amount_net, ca, coin_name, tx_sig, entry_price_val, entry_mcap_val)
                            if not ok:
                                logger.warning("DRY_RUN: did not start monitor for %s due to insufficient demo balance", ca)
                        except Exception as e:
                            logger.exception("DRY_RUN: error recording simulated buy: %s", e)

                    # --- Spawn monitor ---
                    try:
                        asyncio.create_task(
                            monitor_position(
                                session=session,
                                ca=ca,
                                entry_price=float(price_usd),
                                price_source=price_source,
                                coin_name=coin_name,
                                position_balance_lamports=lamports,
                                privkey=privkey,
                                pubkey=pubkey,
                                usd_amount_net=float(usd_amount_net),
                            )
                        )
                        logger.info("Spawned monitor_position for %s", ca)
                    except Exception as e:
                        logger.exception("Failed to spawn monitor_position for %s: %s", ca, e)

                    try:
                        await sleep_with_logging(TRADE_SLEEP_SEC, f"Post-trade cooldown for {TRADE_SLEEP_SEC}s")
                        continue
                    except Exception as e:
                        logger.exception("Cooldown sleep failed for %s: %s", ca, e)
                        await asyncio.sleep(1.0)
                        continue

                except Exception as e:
                    logger.exception("process_pending_cas inner loop failed for %s: %s", ca, e)
                    continue

    except Exception as e:
        logger.exception("process_pending_cas failed: %s", e)

# ---------- Part 2 will continue with monitor_position, daily cycle, balance, main loop ----------
# ============================================================
# PRICE MONITOR FUNCTION (fixed & improved)
# ============================================================
async def monitor_position(
    session: aiohttp.ClientSession,
    ca: str,
    entry_price: float,
    price_source: str,
    coin_name: str,
    position_balance_lamports: int,
    privkey=None,
    pubkey=None,
    usd_amount_net: float | None = None,  # optional: USD spent at buy
):
    """
    Monitor token price after entry and trigger sell or simulate sell when DRY_RUN.
    Improvements:
     - normalize SL/TP to positive values
     - robust numeric parsing for price/mcap
     - only decide TP/SL when thresholds actually crossed
     - persist entry_mcap if missing (best-effort)
     - clearer logging
    """
    def _safe_float_env(name: str, default: float) -> float:
        try:
            return float(os.getenv(name, str(default)))
        except (TypeError, ValueError):
            return default

    def _fmt_amt(x: float) -> str:
        try:
            return f"{float(x):.8f}"
        except Exception:
            return str(x)

    def _parse_number(val):
        """Try to coerce a value (string or number) into float, stripping $ and commas."""
        if val is None:
            return None
        if isinstance(val, (int, float)):
            return float(val)
        s = str(val).strip()
        s = re.sub(r"[^0-9eE\.\-]", "", s)
        if s == "":
            return None
        try:
            return float(s)
        except Exception:
            return None

    # normalize stop-loss and take-profit to positive percentages
    stop_loss_pct = abs(_safe_float_env("STOP_LOSS", 20.0))
    take_profit_pct = abs(_safe_float_env("TAKE_PROFIT", 40.0))
    poll_interval = _safe_float_env("MONITOR_POLL_SEC", 2.4)
    sell_fee_pct = _safe_float_env("SELL_FEE_PERCENT", 1.0)

    logger.info(
        "📈 Started monitor for %s | entry=%s | src=%s | SL=%.2f%% | TP=%.2f%% | SELL_FEE=%.2f%%",
        ca, _fmt_amt(entry_price), price_source, stop_loss_pct, take_profit_pct, sell_fee_pct,
    )

    # helper to locate the most recent history record for this CA with usd_in and no usd_out yet
    async def _find_history_record_for_ca(ca_val):
        async with SIM_LOCK:
            for rec in reversed(SIM_STATE.get("history", [])):
                if rec.get("ca") == ca_val and rec.get("usd_in") is not None and rec.get("usd_out") is None:
                    return rec
        return None

    try:
        while True:
            info = await fetch_token_price_and_mcap(ca)

            # normalize/resilient reads
            raw_price = None
            raw_mcap = None
            if isinstance(info, dict):
                raw_price = info.get("priceUsd") or info.get("price") or info.get("priceUsd")
                raw_mcap = info.get("marketCap") or info.get("market_cap") or info.get("mcap") or info.get("marketCapUsd")
            current_price = _parse_number(raw_price)
            current_mcap = _parse_number(raw_mcap)
            current_source = (info.get("source") if isinstance(info, dict) else getattr(info, "source", None)) or "unknown"

            # Log numeric values (debug)
            logger.debug(
                "Monitor poll for %s | price=%s | mcap=%s | src=%s",
                ca,
                (f"{current_price:.8f}" if current_price is not None else "N/A"),
                (f"{current_mcap:.2f}" if current_mcap is not None else "N/A"),
                current_source,
            )

            # no useful data yet
            if (current_price is None or current_price <= 0.0) and current_mcap is None:
                await asyncio.sleep(poll_interval)
                continue

            # compute price change from entry (guard against zero entry_price)
            pct_from_entry = 0.0
            try:
                if entry_price and float(entry_price) > 0:
                    pct_from_entry = (( (current_price or 0.0) - float(entry_price)) / float(entry_price)) * 100.0
            except Exception:
                pct_from_entry = 0.0

            logger.debug(
                "Monitor %s: price=%s entry=%s Δ(entry)=%+.2f%% (src=%s) mcap=%s",
                ca,
                (f"{current_price:.8f}" if current_price is not None else "N/A"),
                _fmt_amt(entry_price),
                pct_from_entry,
                current_source,
                (f"{current_mcap:.2f}" if current_mcap is not None else "N/A"),
            )

            # DRY_RUN simulation path
            if DRY_RUN:
                rec = await _find_history_record_for_ca(ca)
                usd_in = None
                entry_mcap = None
                if rec:
                    usd_in = _parse_number(rec.get("usd_in"))
                    entry_mcap = _parse_number(rec.get("entry_mcap"))
                else:
                    # fallback to passed usd_amount_net
                    try:
                        usd_in = float(usd_amount_net) if usd_amount_net is not None else None
                    except Exception:
                        usd_in = None
                    # compute profit and update compounding
                    try:
                        profit_usd = float(net_return) - float(usd_in or 0.0)
                        update_compound_balance(usd_in=usd_in, usd_out=net_return)  # also computes profit internally
                    except Exception:
                        logger.exception("Failed to update compound balance for %s", ca)
                # If we have a history record but no entry_mcap, persist current_mcap as entry_mcap (best-effort)
                if rec and entry_mcap is None and current_mcap is not None:
                    try:
                        async with SIM_LOCK:
                            rec["entry_mcap"] = float(current_mcap)
                        await save_sim_state()
                        entry_mcap = float(current_mcap)
                        logger.debug("DRY_RUN: set missing entry_mcap for %s -> %s", ca, entry_mcap)
                    except Exception:
                        pass

                # Decide outcome: prefer mcap-based decision if both known AND thresholds crossed
                outcome = None
                if (entry_mcap is not None) and (current_mcap is not None):
                    tp_mcap = entry_mcap * (1.0 + (take_profit_pct / 100.0))
                    sl_mcap = entry_mcap * (1.0 - (stop_loss_pct / 100.0))
                    if current_mcap >= tp_mcap:
                        outcome = "TP"
                    elif current_mcap <= sl_mcap:
                        outcome = "SL"
                    else:
                        outcome = None  # not yet crossed

                # Only consider price thresholds when mcap decision unavailable
                if outcome is None:
                    if pct_from_entry >= abs(take_profit_pct):
                        outcome = "TP"
                    elif pct_from_entry <= -abs(stop_loss_pct):
                        outcome = "SL"

                # If outcome decided, compute returns & update SIM_STATE
                if outcome:
                    # ensure usd_in available
                    if usd_in is None:
                        usd_in = float(entry_price) if entry_price else 0.0

                    if outcome == "TP":
                        gross_return = usd_in * (1.0 + (take_profit_pct / 100.0))
                    else:  # SL
                        gross_return = usd_in * (1.0 - (stop_loss_pct / 100.0))

                    net_return = gross_return * (1.0 - (sell_fee_pct / 100.0))

                    # update SIM_STATE (history + counters + balance)
                    async with SIM_LOCK:
                        if rec:
                            rec["usd_out"] = float(net_return)
                            rec["exit_price"] = float(current_price) if current_price is not None else None
                            rec["result"] = "WIN" if outcome == "TP" else "LOSS"
                            rec["timestamp_exit"] = datetime.utcnow().isoformat() + "Z"
                        SIM_STATE["completed_trades"] = SIM_STATE.get("completed_trades", 0) + 1
                        if outcome == "TP":
                            SIM_STATE["wins"] = SIM_STATE.get("wins", 0) + 1
                        else:
                            SIM_STATE["losses"] = SIM_STATE.get("losses", 0) + 1
                        # add the returned USD back to balance (buy already deducted usd_in at buy-time)
                        SIM_STATE["balance"] = SIM_STATE.get("balance", 0.0) + float(net_return)

                    await save_sim_state()

                    # log clearly — TP vs SL and numeric values
                    logger.info(
                        "DRY_RUN: %s simulated for %s | usd_in=%.2f usd_out=%.2f -> balance=%.2f (entry_mcap=%s current_mcap=%s)",
                        ("TP" if outcome == "TP" else "SL"),
                        ca,
                        usd_in,
                        net_return,
                        SIM_STATE["balance"],
                        (f"{entry_mcap:.2f}" if entry_mcap is not None else "N/A"),
                        (f"{current_mcap:.2f}" if current_mcap is not None else "N/A"),
                    )

                    # send summary if we've reached daily cap
                    async with SIM_LOCK:
                        buys = SIM_STATE.get("buys_today", 0)
                    if buys >= MAX_BUYS_PER_DAY:
                        try:
                            await send_simulation_summary()
                        except Exception:
                            logger.exception("Failed to send simulation summary")

                    break  # stop monitoring after simulated sell

                # continue polling if no outcome yet
                await asyncio.sleep(poll_interval)
                continue

            # REAL execution path
            if pct_from_entry <= -abs(stop_loss_pct):
                logger.warning("⛔ Stop-Loss triggered for %s (%.2f%%). Selling...", ca, pct_from_entry)
                await execute_sell(
                    session=session,
                    token_mint=ca,
                    privkey=privkey,
                    pubkey=pubkey,
                    position_balance_lamports=position_balance_lamports,
                    total_fee_pct=sell_fee_pct,
                )
                break

            if pct_from_entry >= abs(take_profit_pct):
                logger.success("🎯 Take-Profit triggered for %s (%.2f%%). Selling...", ca, pct_from_entry)
                await execute_sell(
                    session=session,
                    token_mint=ca,
                    privkey=privkey,
                    pubkey=pubkey,
                    position_balance_lamports=position_balance_lamports,
                    total_fee_pct=sell_fee_pct,
                )
                break

            await asyncio.sleep(poll_interval)

    except asyncio.CancelledError:
        logger.info("Monitor cancelled for %s", ca)
    except Exception as e:
        logger.exception("Error in monitor for %s: %s", ca, e)
    finally:
        logger.info("Monitor finished for %s", ca)
# ---------- Daily cycle & balance helpers ----------
def reset_daily_cycle():
    global daily_trades, _last_cycle_date
    today = date.today()
    if today != _last_cycle_date:
        logger.info("Resetting daily cycle counters for new day")
        daily_trades = 0
        _last_cycle_date = today


def load_balance():
    global current_usd_balance
    try:
        with open(BALANCE_FILE, "r") as f:
            data = json.load(f)
            current_usd_balance = data.get("usd_balance", DAILY_CAPITAL_USD)
    except Exception:
        current_usd_balance = DAILY_CAPITAL_USD


def save_balance():
    global current_usd_balance
    try:
        with open(BALANCE_FILE, "w") as f:
            json.dump({"usd_balance": current_usd_balance}, f, indent=2)
    except Exception as e:
        logger.warning("Failed to save balance: %s", e)

# --- Simulation / DRY_RUN state (add near top, after imports) ---
DRY_RUN = os.getenv("DRY_RUN", "0").lower() in ("1", "true", "yes")

# path to persist sim state
SIM_STATE_PATH = os.path.join(os.path.dirname(__file__), "sim_state.json")

# initial state (will be overwritten by load if file exists)
_initial_balance = float(os.getenv("DEMO_BALANCE", "20.0"))
SIM_STATE = {
    "starting_balance": _initial_balance,
    "balance": _initial_balance,
    "wins": 0,
    "losses": 0,
    "completed_trades": 0,
    "buys_today": 0,
    "history": [],  # each entry: dict with keys below
}
SIM_LOCK = asyncio.Lock()
MAX_BUYS_PER_DAY = int(os.getenv("MAX_BUYS_PER_DAY", "50"))


async def load_sim_state():
    """Load SIM_STATE from disk if present (async-friendly)."""
    global SIM_STATE
    try:
        if os.path.exists(SIM_STATE_PATH):
            with open(SIM_STATE_PATH, "r") as f:
                data = json.load(f)
            async with SIM_LOCK:
                # merge but preserve keys
                SIM_STATE.update(data)
            logger.info("Loaded simulation state from %s", SIM_STATE_PATH)
    except Exception as e:
        logger.warning("Failed to load SIM_STATE: %s", e)


async def save_sim_state():
    """Persist SIM_STATE to disk (atomic write)."""
    try:
        async with SIM_LOCK:
            tmp = SIM_STATE.copy()
        tmp_path = SIM_STATE_PATH + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(tmp, f, indent=2)
        os.replace(tmp_path, SIM_STATE_PATH)
    except Exception as e:
        logger.warning("Failed to save SIM_STATE: %s", e)


async def send_simulation_summary():
    """
    Compose and send a DRY_RUN daily summary via your send_telegram_message helper.
    Includes median return (percent) computed across completed trades.
    """
    try:
        async with SIM_LOCK:
            start = SIM_STATE.get("starting_balance", 0.0)
            end = SIM_STATE.get("balance", 0.0)
            wins = SIM_STATE.get("wins", 0)
            losses = SIM_STATE.get("losses", 0)
            completed = SIM_STATE.get("completed_trades", 0)
            buys = SIM_STATE.get("buys_today", 0)
            history = list(SIM_STATE.get("history", []))

        total = wins + losses
        win_rate = (wins / total * 100.0) if total > 0 else 0.0

        # Build list of percent returns for median calculation.
        # Each history entry should include 'usd_in' and 'usd_out' (net after fees)
        pct_returns = []
        for rec in history:
            usd_in = rec.get("usd_in")
            usd_out = rec.get("usd_out")
            if usd_in is None or usd_out is None or usd_in == 0:
                continue
            pct = ((usd_out - usd_in) / usd_in) * 100.0
            pct_returns.append(pct)

        median_pct = float(statistics.median(pct_returns)) if pct_returns else 0.0

        msg = (
            f"📊 *DRY_RUN Daily Simulation Summary*\n"
            f"🧪 Starting Balance: ${start:.2f}\n"
            f"🏁 Ending Balance: ${end:.2f}\n"
            f"🛒 Buys simulated: {buys}\n"
            f"✅ Wins: {wins} | ❌ Losses: {losses}\n"
            f"📈 Win rate: {win_rate:.2f}%\n"
            f"📉 Median return per trade: {median_pct:.2f}%\n"
            f"🔁 Completed simulated trades: {completed}"
        )
        # send_telegram_message is assumed to be synchronous in your codebase;
        # if it's async, replace call with await send_telegram_message(msg)
        try:
            send_telegram_message(msg)
        except Exception:
            logger.exception("Failed to send DRY_RUN summary via send_telegram_message")
    except Exception as e:
        logger.exception("Failed to prepare DRY_RUN summary: %s", e)

#---- Dry Run Reset
async def daily_reset_loop():
    while True:
        # compute seconds until next 00:00 UTC
        now = datetime.utcnow()
        tomorrow = (now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1))
        seconds = (tomorrow - now).total_seconds()
        await asyncio.sleep(seconds)
        async with SIM_LOCK:
            SIM_STATE["buys_today"] = 0
            # optionally keep history or move to archived file
        await save_sim_state()

# ============================================================
# SELL EXECUTION FUNCTION
# ============================================================
async def execute_sell(
    session: aiohttp.ClientSession,
    token_mint: str,
    privkey: str | Keypair,
    pubkey: str,
    position_balance_lamports: int,
    total_fee_pct: float | None = None,
    coin_name: str | None = None,
    market_cap: float | None = None,
    priority_fee_sol: float | None = None,
    payer_privkey: str | Keypair = None,
) -> str | None:
    """
    Executes a SELL (token -> SOL) using Jupiter Ultra.
    DRY_RUN=1 simulates TX.
    Applies SELL_FEE_PERCENT exactly once as total fee.
    - Use payer_privkey for small swaps (<$15) to bypass gasless minimum.
    """

    DRY_RUN = bool(int(os.getenv("DRY_RUN", "1")))
    total_fee_pct = total_fee_pct or float(os.getenv("SELL_FEE_PERCENT", "0"))
    # Load payer_privkey from env if not provided
    payer_privkey = payer_privkey or os.getenv("PRIVATE_KEY")

    # Fetch SOL/USD price from Jupiter Ultra API
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

    SOL_USD_PRICE = await fetch_sol_usd_price()
    if SOL_USD_PRICE == 150.0:
        logger.warning(f"⚠️ Using fallback SOL price: ${SOL_USD_PRICE:.2f}")

    wallet = privkey if isinstance(privkey, Keypair) else Keypair.from_base58_string(privkey.strip())
    logger.info(f"🟡 Preparing SELL for {token_mint} | Fee={total_fee_pct:.2f}% | DRY_RUN={DRY_RUN}")

    payer_wallet = None
    if payer_privkey:
        try:
            payer_wallet = payer_privkey if isinstance(payer_privkey, Keypair) else Keypair.from_base58_string(payer_privkey.strip())
        except Exception as e:
            logger.error(f"❌ Failed to load payer wallet keypair: {e}")
            return None

    ORDER_URL = "https://lite-api.jup.ag/ultra/v1/order"
    EXEC_URL = "https://lite-api.jup.ag/ultra/v1/execute"
    params = {
        "inputMint": token_mint,
        "outputMint": "So11111111111111111111111111111111111111112",
        "amount": str(position_balance_lamports),
        "taker": pubkey,
    }
    if payer_wallet:
        params["payer"] = str(payer_wallet.pubkey())

    # === Fetch order ===
    async with session.get(ORDER_URL, params=params, timeout=15) as r:
        if r.headers.get("Content-Type", "").startswith("text/plain"):
            text = await r.text()
            logger.error(f"❌ Non-JSON response from /order: {text} (Status: {r.status})")
            return None
        order = await r.json()

    if not order.get("transaction"):
        logger.error(f"❌ Invalid order response for {token_mint}: {order}")
        return None

    est_out_sol = int(order["outAmount"]) / 1e9
    out_usd = est_out_sol * SOL_USD_PRICE
    fee_usd = out_usd * (total_fee_pct / 100.0)

    # === Validate minimum amount for gasless transactions ===
    if out_usd < 15.0 and not payer_privkey:
        logger.error(f"❌ Swap output ${out_usd:.2f} is below $15 minimum for gasless transactions. Provide payer_privkey or set PRIVATE_KEY in env.")
        return None

    tx_bytes = base64.b64decode(order["transaction"])
    tx = VersionedTransaction.from_bytes(tx_bytes)
    message_bytes = tx.message.serialize()

    signatures = []
    signatures.append(wallet.sign_message(message_bytes))
    if payer_wallet:
        signatures.append(payer_wallet.sign_message(message_bytes))

    tx = VersionedTransaction(tx.message, signatures)
    signed_tx = base64.b64encode(tx.serialize()).decode("utf-8")

    # === DRY RUN ===
    if DRY_RUN:
        fake_tx = f"DRY_RUN_SELL_{int(time.time())}"
        try:
            update_compound_balance(after_profit_usd=out_usd)
            record_sell(
                ca=token_mint,
                coin_name=coin_name or "Unknown",
                market_cap=market_cap or 0,
                usd_amount_gross=out_usd,
                usd_amount_net=out_usd - fee_usd,
                fee_usd=fee_usd,
                priority_fee_sol=priority_fee_sol or 0,
            )
            logger.success(f"✅ DRY_RUN SELL recorded for {token_mint}")
        except Exception as e:
            logger.warning(f"⚠️ DRY_RUN record failed: {e}")
        return fake_tx

    # === Real transaction ===
    payload = {"signedTransaction": signed_tx, "requestId": order["requestId"]}
    for attempt in range(1, 4):
        try:
            async with session.post(EXEC_URL, json=payload, timeout=20) as resp:
                if resp.headers.get("Content-Type", "").startswith("text/plain"):
                    text = await resp.text()
                    logger.error(f"❌ Non-JSON response from /execute: {text} (Status: {resp.status})")
                    continue
                result = await resp.json()

            if result.get("status", "").lower() == "success":
                sig = result.get("signature") or result.get("txid")
                update_compound_balance(after_profit_usd=out_usd)
                record_sell(
                    ca=token_mint,
                    coin_name=coin_name or "Unknown",
                    market_cap=market_cap or 0,
                    usd_amount_gross=out_usd,
                    usd_amount_net=out_usd - fee_usd,
                    fee_usd=fee_usd,
                    priority_fee_sol=priority_fee_sol or 0,
                )
                logger.success(f"🚀 SELL success | {coin_name or token_mint}")
                logger.info(f"🔗 Solscan: https://solscan.io/tx/{sig}")
                return sig
            else:
                logger.warning(f"Attempt {attempt}/3 failed: {result}")
        except Exception as e:
            logger.warning(f"Attempt {attempt}/3 error: {e}")
            await asyncio.sleep(2 * attempt)

    logger.error(f"❌ SELL failed after 3 retries for {token_mint}")
    return None

# ============================================================
# MAIN LOOP
# ============================================================
import psutil
import sqlite3

# Single-instance check to prevent multiple bot instances
def check_single_instance():
    pid_file = "/tmp/sniper_bot.pid"
    if os.path.exists(pid_file):
        with open(pid_file, "r") as f:
            pid = int(f.read().strip())
        if psutil.pid_exists(pid):
            logger.error(f"Bot already running with PID {pid}. Exiting.")
            exit(1)
    with open(pid_file, "w") as f:
        f.write(str(os.getpid()))

# Retry on database lock
async def start_client_with_retry(session_name, api_id, api_hash, bot_token=None, retries=5, delay=1):
    for attempt in range(retries):
        try:
            client = TelegramClient(session_name, api_id, api_hash, timeout=30)
            await client.connect()
            await client.start(bot_token=bot_token if bot_token else None)
            return client
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e):
                logger.warning(f"Database locked, retrying {attempt+1}/{retries}...")
                await asyncio.sleep(delay)
            else:
                raise
    logger.error("Failed to start client after retries due to database lock")
    return None

async def main():
    logger.info("Starting Solana Meme Coin Sniper Bot 🟣🌱🧨")
    check_single_instance()  # Prevent multiple instances
    # Load real balance (or simulated one if DRY_RUN)
    load_balance()
    reset_daily_cycle()
    # Start Telegram client with retry
    global client
    client = await start_client_with_retry(
        session_name=SESSION_NAME,
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=TELEGRAM_BOT_TOKEN
    )
    if not client:
        logger.error("Failed to initialize Telegram client")
        return
    logger.info("Telegram client started, listening for new messages...")
    # Background worker to process pending contract addresses
    asyncio.create_task(process_pending_cas())
    # Continuous watchdog for Telethon connection + main cycle
    while True:
        try:
            # Run Telegram connection loop (keeps listening for CA messages)
            await client.run_until_disconnected()
        except Exception as e:
            logger.warning(f"⚠️ Telethon disconnected: {e}, retrying in 10s...")
            await asyncio.sleep(10)
        finally:
            # Maintain daily reset heartbeat even if Telethon reconnects
            reset_daily_cycle()
            await sleep_with_logging(60.0, "Main loop heartbeat, checking daily cycle")

# ============================================================
# ENTRY POINT
# ============================================================
if __name__ == "__main__":
    try:
        if DRY_RUN:
            # Load simulation state before running main
            asyncio.run(load_sim_state())
            logger.info(
                "DRY_RUN active → starting balance = %.2f, MAX_BUYS_PER_DAY = %d",
                SIM_STATE.get("balance", 0.0),
                MAX_BUYS_PER_DAY,
            )
        # Start main async loop with resilience and non-blocking tasks
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down sniper gracefully...")
