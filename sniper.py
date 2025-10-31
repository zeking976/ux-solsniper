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

from solders.message import to_bytes_versioned
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

# ============================================================
# REFERRAL SETTINGS (optional)
# ============================================================
REFERRAL_BPS = int(os.getenv("REFERRAL_FEE_BPS", "0"))
REFERRAL_ACCOUNT = os.getenv("REFERRAL_ACCOUNT", "").strip()

if REFERRAL_ACCOUNT:
    logger.info(f"📎 Using referral account {REFERRAL_ACCOUNT} ({REFERRAL_BPS} bps)")
else:
    logger.info("📎 No referral account configured.")
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

# ---------- Telegram client ----------
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
    Background worker: processes CAs from _pending_cas queue → buy → monitor → sell.
    - Applies filters (price, liquidity, mcap/liq ratio, sell tax)
    - Executes BUY via Jupiter Ultra
    - Sends Telegram BUY message
    - Spawns monitor_position()
    - On sell: sends matching SELL Telegram message
    """
    global daily_trades
    try:
        privkey = os.getenv("PRIVATE_KEY")
        if not privkey:
            logger.error("No PRIVATE_KEY in environment — cannot sign transactions")
            return

        # === Derive keypair & pubkey ===
        try:
            from solders.keypair import Keypair
            import base58
            try:
                kp = Keypair.from_bytes(base58.b58decode(privkey))
            except Exception:
                arr = json.loads(privkey)
                kp = Keypair.from_bytes(bytes(arr))
            pubkey = str(kp.pubkey())
            wallet = kp  # for signing
        except Exception as e:
            logger.exception("Failed to load keypair from PRIVATE_KEY: %s", e)
            return

        async with aiohttp.ClientSession() as session:
            position_active = False

            while True:
                ca = await dequeue_ca(_pending_cas)
                if not ca:
                    await sleep_with_logging(1.0, "No CA in queue, waiting...")
                    continue

                if position_active:
                    logger.info(f"Active trade in progress — skipping CA {ca}")
                    continue

                position_active = True
                logger.info(f"Processing CA {ca}...")

                try:
                    # === Fetch token info ===
                    token_info = await get_market_cap_or_priceinfo(ca)
                    mcap_val, price_usd, supply, price_source = parse_token_info(token_info)
                    coin_name = resolve_token_name(ca)

                    # === Dexscreener data ===
                    liquidity_usd, volume_usd, sell_tax = await get_dexscreener_data(ca)

                    # === Log summary ===
                    logger.info(
                        "CA %s | %s | Price=%.8f | MCAP=$%.2f | Liq=$%.2f | Vol=$%.2f | src=%s",
                        ca[:8], coin_name, price_usd or 0, mcap_val, liquidity_usd, volume_usd, price_source
                    )

                    # === Filters ===
                    if not await passes_filters(ca, price_usd, mcap_val, liquidity_usd, sell_tax):
                        save_processed_ca(ca)
                        continue

                    # === Trade amount ===
                    usd_net = DAILY_CAPITAL_USD * (1.0 - BUY_FEE_PERCENT / 100)
                    sol_lamports = int(usd_to_sol(usd_net) * 1e9)
                    if sol_lamports <= 0:
                        logger.warning("Zero lamports for buy — skipping %s", ca)
                        save_processed_ca(ca)
                        continue

                    # === Execute BUY ===
                    quote = {
                        "inputMint": SOL_MINT,
                        "outputMint": ca,
                        "inAmount": sol_lamports,
                    }

                    tx_sig = await execute_jupiter_swap_from_quote(
                        session=session,
                        quote=quote,
                        privkey=wallet,
                        pubkey=pubkey,
                        fee_percent=BUY_FEE_PERCENT,
                        coin_name=coin_name,
                        market_cap=mcap_val,
                    )

                    if not tx_sig or tx_sig.startswith("DRY_RUN"):
                        logger.warning("BUY failed or dry-run — skipping monitor for %s", ca)
                        save_processed_ca(ca)
                        continue

                    # === Record & Notify ===
                    fee_usd = usd_net * BUY_FEE_PERCENT / 100
                    usd_gross = usd_net + fee_usd

                    record_buy(
                        ca=ca,
                        coin_name=coin_name,
                        market_cap=mcap_val,
                        usd_amount_gross=usd_gross,
                        usd_amount_net=usd_net,
                        fee_usd=fee_usd,
                        priority_fee_sol=0,
                    )

                    buy_msg = (
                        f"✅ BUY executed\n"
                        f"Coin: {coin_name}\n"
                        f"CA: `{ca}`\n"
                        f"Price: ${price_usd:.8f}\n"
                        f"MCAP: ${mcap_val:,.2f}\n"
                        f"Amount (net): ${usd_net:.2f}\n"
                        f"Fee: ${fee_usd:.2f}\n"
                        f"Amount (gross): ${usd_gross:.2f}\n"
                        f"TX: [View](https://solscan.io/tx/{tx_sig})"
                    )
                    send_telegram_message(buy_msg)
                    logger.info("Buy executed: CA=%s, tx=%s", ca, tx_sig)

                    # === Spawn monitor (with sell callback) ===
                    sell_tx = await monitor_position(
                        session=session,
                        ca=ca,
                        entry_price=price_usd,
                        price_source=price_source,
                        coin_name=coin_name,
                        position_balance_lamports=sol_lamports,
                        privkey=wallet,
                        pubkey=pubkey,
                        usd_amount_net=usd_net,
                    )

                    # === On sell: send mirrored message ===
                    if sell_tx and not sell_tx.startswith("DRY_RUN"):
                        sell_out_usd = await estimate_sell_value(ca, sol_lamports, session)
                        sell_fee_usd = sell_out_usd * SELL_FEE_PERCENT / 100
                        profit_usd = sell_out_usd - usd_net

                        sell_msg = (
                            f"🟥 SELL executed\n"
                            f"Coin: {coin_name}\n"
                            f"CA: `{ca}`\n"
                            f"Price: ${price_usd:.8f}\n"
                            f"MCAP: ${mcap_val:,.2f}\n"
                            f"Amount (out): ${sell_out_usd:.2f}\n"
                            f"Fee: ${sell_fee_usd:.2f}\n"
                            f"Profit: ${profit_usd:+.2f}\n"
                            f"TX: [View](https://solscan.io/tx/{sell_tx})"
                        )
                        send_telegram_message(sell_msg)
                        logger.info("Sell executed: CA=%s, tx=%s, profit=$%.2f", ca, sell_tx, profit_usd)

                    daily_trades += 1
                    save_processed_ca(ca)

                except Exception as e:
                    logger.exception("Error processing CA %s: %s", ca, e)
                finally:
                    position_active = False
                    await sleep_with_logging(TRADE_SLEEP_SEC, f"Post-trade cooldown {TRADE_SLEEP_SEC}s")

    except Exception as e:
        logger.exception("process_pending_cas crashed: %s", e)
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
                logger.info("⛔ Stop-Loss triggered for %s (%.2f%%). Selling...", ca, pct_from_entry)
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
                logger.info("🎯 Take-Profit triggered for %s (%.2f%%). Selling...", ca, pct_from_entry)
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
    - Sets referralFeeBps=0 to disable referral fees.
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
                    logger.error(f"❌  Invalid SOL price response: {data}")
                    return 150.0  # Fallback price
        except Exception as e:
            logger.error(f"❌  Failed to fetch SOL price: {e}")
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
            logger.error(f"❌  Failed to load payer wallet keypair: {e}")
            return None
    ORDER_URL = "https://lite-api.jup.ag/ultra/v1/order"
    EXEC_URL = "https://lite-api.jup.ag/ultra/v1/execute"
    wallet = privkey if isinstance(privkey, Keypair) else Keypair.from_base58_string(privkey.strip())

    # ✅ Fetch the actual token balance before creating the order
    try:
        ui_amount = await get_token_balance(pubkey, token_mint)
        if not ui_amount or ui_amount <= 0:
            logger.warning(f"⚠️ No balance found for {token_mint}. Skipping SELL.")
            return None
        decimals = await get_token_decimals(token_mint)
        position_balance_lamports = int(float(ui_amount) * (10 ** decimals))
        logger.debug(f"📊 Updated sell amount to actual wallet balance: {position_balance_lamports} lamports")
    except Exception as e:
        logger.error(f"❌ Failed to fetch token balance for {token_mint}: {e}")
        return None

    params = {
        "inputMint": token_mint,
        "outputMint": "So11111111111111111111111111111111111111112",
        "amount": str(position_balance_lamports),
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
    # === Fetch order ===
    async with session.get(ORDER_URL, params=params, timeout=15) as r:
        if r.headers.get("Content-Type", "").startswith("text/plain"):
            text = await r.text()
            logger.error(f"❌  Non-JSON response from /order: {text} (Status: {r.status})")
            return None
        order = await r.json()
    if not order.get("transaction"):
        logger.error(f"❌  Invalid order response for {token_mint}: {order}")
        return None
    est_out_sol = int(order["outAmount"]) / 1e9
    out_usd = est_out_sol * SOL_USD_PRICE
    fee_usd = out_usd * (total_fee_pct / 100.0)
    # === Validate minimum amount for gasless transactions ===
    if out_usd < 15.0 and not payer_privkey:
        logger.error(f"❌  Swap output ${out_usd:.2f} is below $15 minimum for gasless transactions. Provide payer_privkey or set PRIVATE_KEY in env.")
        return None
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
            logger.info(f"✅  DRY_RUN SELL recorded for {token_mint}")
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
                    logger.error(f"❌  Non-JSON response from /execute: {text} (Status: {resp.status})")
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
                logger.info(f"🚀 SELL success | {coin_name or token_mint}")
                logger.info(f"🔗 Solscan: https://solscan.io/tx/{sig}")
                return sig
            else:
                logger.warning(f"Attempt {attempt}/3 failed: {result}")
        except Exception as e:
            logger.warning(f"Attempt {attempt}/3 error: {e}")
            await asyncio.sleep(2 * attempt)
    logger.error(f"❌  SELL failed after 3 retries for {token_mint}")
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

# ============================================================
# MAIN LOOP
# ============================================================
async def main():
    logger.info("Starting Solana Meme Coin Sniper Bot 🟣🌱🧨")
    # Load real balance (or simulated one if DRY_RUN)
    load_balance()
    reset_daily_cycle()
    # Start Telegram client
    await client.start(bot_token=TELEGRAM_BOT_TOKEN if TELEGRAM_BOT_TOKEN else None)
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
