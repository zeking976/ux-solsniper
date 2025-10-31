🚀 UX SolSniper — Solana Meme Coin Sniper Bot

UX SolSniper is a high-speed automated Solana meme-coin sniper that watches Telegram for new contract addresses, executes fast Jupiter Ultra swaps on Mainnet, and sends detailed Telegram buy/sell alerts and reports. The README below reflects the recent updates we've applied to the codebase (Jupiter Ultra API, live SOL price fetch, referral/payer handling, improved signing with solders, DRY_RUN bookkeeping, guardrails and monitoring logic).


---

✅ Highlights (what changed / what's important)

Uses Jupiter Ultra API (/ultra/v1/order → sign → /ultra/v1/execute) — no legacy quote flows.

Live SOL price fetched from Jupiter Lite price endpoint (fallback to $150).

Buy / Sell fees are applied exactly once (BUY_FEE_PERCENT, SELL_FEE_PERCENT) and recorded as USD.

Handles gasless minimum ($15): automatically retries order with gasless=false or uses a payer_privkey.

Uses solders VersionedTransaction.from_bytes(...) then populates a signed tx compatible with Jupiter Ultra.

Referral support: REFERRAL_FEE_BPS and REFERRAL_ACCOUNT (strings) added to order params when set.

payer and closeAuthority are always set to a valid key (payer wallet or taker fallback).

Filter: skip tokens with price_usd < 0.0000008 (defensive).

process_pending_cas() prevents concurrent buys (position_active) and spawns monitor_position() for each opened position.

execute_sell() now fetches real token decimals and token balance before creating the order and sells full balance (SL/TP both use same sell flow).

Buy and Sell Telegram messages mirror each other (sell message mirrors buy message).

DRY_RUN support remains; simulated buys/sells update simulated balance and history.



---

Table of Contents

1. Requirements


2. Install


3. Files & Structure


4. Environment / t.env (example)


5. Create Telethon session (quick)


6. Run the bot


7. How it works — high-level flow


8. Important behaviors & guards


9. Troubleshooting / Common logs & fixes


10. Contributing / Notes




---

Requirements

Python 3.11+

aiohttp, telethon, solders, solana-py (async RPC), loguru, etc. — see requirements.txt

A VPS or machine with stable network (Frankfurt/Europe nodes recommended for lower latency if your target Telegram community is European)



---

Install

git clone <your-repo>
cd ux-solsniper
python3 -m venv ~/venv
source ~/venv/bin/activate
pip install -r requirements.txt
cp t.env.example t.env
# edit t.env with your keys


---

Files & Structure (key files)

sniper.py — main orchestrator (process_pending_cas, main loop, monitoring, etc.)

utils.py — helper functions (execute_jupiter_swap_from_quote, execute_sell, record_buy, record_sell, fetch helpers)

create_session.py — helper to create Telethon session file (if included)

trade_logs.json / trade_record.json — persistent trade records

t.env — environment variables

requirements.txt — dependencies

reports.py — daily/monthly report generator



---

Environment / t.env (example keys)

Below are the important environment variables. Keep them in t.env and never commit secrets.

# Core keys
PRIVATE_KEY="<base58 or JSON array of key bytes>"    # used for signing; required
TELEGRAM_BOT_TOKEN="<bot-token or empty if using user session>"
TELEGRAM_CHAT_ID="<chat id for reports/alerts>"

# Operation
DRY_RUN="1"                          # 1 to simulate (no broadcast)
DAILY_CAPITAL_USD="15.0"            # daily USD capital per trade (adjust)
BUY_FEE_PERCENT="1.0"               # percent applied to buys (USD)
SELL_FEE_PERCENT="1.0"              # percent applied to sells (USD)
MAX_MCAP_LIQ_RATIO="10"             # guard: MCAP / LIQ ratio

# Jupiter & RPC
RPC_URL="https://api.mainnet-beta.solana.com"
SOL_MINT="So11111111111111111111111111111111111111112"

# Referral/payer
REFERRAL_FEE_BPS="0"                # integer, e.g. 50 (0.5%)
REFERRAL_ACCOUNT=""                 # string pubkey if you have a referral account
PRIVATE_PAYER_KEY=""                # optional: use to bypass $15 gasless minimum

# Monitoring & Limits
TRADE_SLEEP_SEC="4.0"               # cooldown after a trade
LAMPORTS_PER_SOL="1000000000"

# Logging / Reporting
LOG_LEVEL="INFO"

Notes:

REFERRAL_ACCOUNT must be a string (already fixed).

PRIVATE_KEY can be base58 or JSON-encoded list of bytes; process_pending_cas() handles both.



---

Create Telethon session (quick)

If you want the bot to send messages via your personal Telegram account (instead of a bot) or monitor private channels with a user session:

1. Ensure create_session.py exists (or use the snippet below).


2. Run:

source ~/venv/bin/activate
python3 create_session.py

That script should prompt you to login and will save a session file (e.g. session.session or whatever name configured). Put the session file path in t.env as TELETHON_SESSION.



If you use a bot token, set TELEGRAM_BOT_TOKEN in t.env and the bot will be used for sending messages.


---

Run the bot

Example run (background):

source ~/venv/bin/activate
set -a
source /root/ux-solsniper/t.env
set +a
cd /root/ux-solsniper
nohup python3 /root/ux-solsniper/sniper.py > ~/ux-solsniper/sniper_dryrun.log 2>&1 &
tail -f ~/ux-solsniper/sniper_dryrun.log

Notes:

nohup + tail -f is common. If you see intermittent Telegram message delivery — that may be due to Telethon reconnects or being rate-limited by Telegram. See Troubleshooting.



---

How it works — high-level flow

1. process_pending_cas() runs in background: it dequeues CAs from _pending_cas.


2. It enforces position_active to avoid concurrent buy/sell logic (prevents double-opening positions).


3. For each CA:

Fetch price & mcap via get_market_cap_or_priceinfo() (Dexscreener first, Jupiter fallback).

Fetch Dexscreener liquidity & volume for additional filters.

Apply defensive filters:

price_usd must exist and be >= 0.0000008.

non-zero liquidity.

MCAP / LIQ ratio guard.

Reject tokens with sell tax or unlocked liquidity where configured.


Decide USD amount to invest and compute SOL amount (auto subtracts buy fee).

Build Jupiter Ultra order params (includes referral + payer + closeAuthority when set).

Call execute_jupiter_swap_from_quote() (Ultra flow).

On success, record_buy() persists buy record and sends Telegram buy message.

Spawns monitor_position() that polls prices and triggers SL/TP; execute_sell() handles selling (full balance, with decimal-awareness).



4. Sells (SL/TP/manual) use the same Ultra flow and record P&L via record_sell() and update compounding balance.




---

Important behaviors & guards (details)

Jupiter Ultra Errors: Some /order responses may not contain a transaction (e.g., gasless minimum errors or other errors). The code:

Detects errorMessage like Minimum $15 for gasless and retries with gasless=false or uses payer_privkey.

Logs and skips if still invalid.


Signature / Signing:

The code decodes order["transaction"] (base64) and uses VersionedTransaction.from_bytes(...) then populates a signed tx compatible with solders. This avoids the older .sign() misuse.


Fees:

Buy fee (BUY_FEE_PERCENT) and sell fee (SELL_FEE_PERCENT) are applied as USD percentages on gross output and recorded. Priority fee (tip) in SOL is handled separately (priority fee estimate).

The code applies only the configured buy/sell percentage as total fees (no double-charging).


Referral params:

You can set REFERRAL_FEE_BPS and REFERRAL_ACCOUNT in t.env. When set, the params includes them as strings and they are logged.


Payer & closeAuthority:

payer is set to payer_wallet.pubkey() if available; otherwise falls back to wallet.pubkey(). closeAuthority is set the same — this avoids the closeAuthority must be provided with payer error from Jupiter.


Minimum gasless output:

If estimated output USD < $15 and no payer privkey provided, the swap is blocked and a helpful error logs. Provide PRIVATE_PAYER_KEY to bypass.


Token decimals & actual token balance:

execute_sell() now fetches token decimals and token balance (via get_token_decimals & get_token_balance) and reconstructs position_balance_lamports = int(ui_amount * (10 ** decimals)) — ensures the sell uses the full amount you actually hold.


SL / TP sells:

Both Stop-Loss and Take-Profit trigger execute_sell() and will sell the full token balance (same path). Sell messages mirror buy messages.




---

Telegram messages

Buy message contains: coin name, CA, price USD, MCAP, USD amounts (net/gross), fee USD, priority fee est (SOL), TX id.

Sell message mirrors the buy message (coin name, CA, price USD, MCAP, amounts, fees, TX). Both are sent after record_{buy|sell} succeeds.



---

Troubleshooting / Common logs & fixes

Missing 'transaction' field in Jupiter order: usually Jupiter returned an order with an error (e.g., Minimum $15 for gasless) or gasless route requires a payer. The code now:

retries with gasless=false or uses payer_privkey.

logs full order for debugging.


Non-JSON response from /order: {"error":"closeAuthority must be provided with payer"}:

Ensure params["payer"] and params["closeAuthority"] set — code now sets both to payer_key.


io error: unexpected end of file or similar RPC jitters:

Intermittent network failures — the code retries the Ultra execute call up to 3 times with backoff.


solders.transaction.VersionedTransaction has no attribute 'sign':

Old signing API was used. We now populate a signed transaction using VersionedTransaction.populate() or the correct pattern for solders.


Telegram messages intermittent:

Telethon can disconnect/reconnect. The main loop should guard client.run_until_disconnected() in a reconnect loop. Use the main() pattern provided in sniper.py (it starts Telethon client and restarts background tasks).

If messages are intermittent, check: .session file, network stability, rate limits, and whether the bot account has permission to post in the target channel.


Insufficient SOL balance:

The code checks wallet SOL balance and skips if insufficient. Ensure SOL is available for swaps + priority fee.


Referral param NameError:

Ensure REFERRAL_ACCOUNT is read from env as a string. Do NOT put an unquoted bare identifier in the code — the code uses the env value.




---

Quick FAQ

Q: Does the bot sell the entire position on SL/TP?
A: Yes — execute_sell() now fetches actual token balance/decimals and sells the full amount (both SL and TP use same flow).

Q: Does the buy/sell fee get double-applied?
A: No — the bot applies BUY_FEE_PERCENT on the buy path and SELL_FEE_PERCENT on the sell path exactly once and records them in USD.

Q: Will a large Telegram channel (100k members) cause message lag?
A: Possibly — high volume channels generate many messages. The bot uses a queue and position_active guard. Still, huge bursts can cause delays. Consider rate-limiting incoming CA processing or using a message filter.



---

Contributing / Notes

Keep t.env secrets safe.

When updating signing logic, make sure you test in DRY_RUN=1 first.

If you want to change the low-price filter, edit the threshold in process_pending_cas() (0.0000008 currently).

For advanced MEV protection or blocklist avoidance, modify utils hooks (this repo currently includes basic MEV protection hooks).