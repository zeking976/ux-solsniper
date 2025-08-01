--

📄 README.md

# 🪙 Solana Meme Coin Sniper Bot

This bot monitors a Telegram channel for new Solana contract addresses, buys the token with $10 USD worth of SOL using Jupiter aggregator, and auto-sells when the token hits **1.5× the market cap** it was bought at.

It reinvests the **entire balance** (original capital + profit) every day for 30 days.

---

## 🚀 Features

- ✅ Scrapes Solana contract addresses from a Telegram channel
- ✅ Buys token via Jupiter with $10 worth of SOL (reinvests daily)
- ✅ Sells automatically at **1.5x market cap**
- ✅ Tracks its own capital — won't touch any extra SOL in your wallet
- ✅ Sends Telegram alerts on every buy/sell with transaction links
- ✅ Uses **Dexscreener** for market cap data
- ✅ Runs 24/7 on VPS with auto-reconnect

---

## 🧪 Requirements

- Python 3.10+
- Solana wallet with at least 0.1 SOL
- Dedicated Vultr VPS or similar
- Telegram Bot Token and API credentials

---

## 🛠️ Setup

### 1. Clone the repo

```bash
git clone https://github.com/yourusername/solana-sniper-bot.git
cd solana-sniper-bot

2. Install dependencies

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

3. Create .env file

Copy from .env.example and set:

TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
PRIVATE_KEY=
TARGET_CHANNEL=

> ⚠️ Never upload your .env to GitHub. Keep it private and upload it to your VPS.




---

💡 How It Works

1. Monitors the Telegram channel for new token messages


2. Extracts the Solana contract address (44-character base58 string)


3. Fetches market cap from Dexscreener


4. Buys token with tracked balance using Jupiter


5. Monitors market cap until 1.5× target


6. Sells and reinvests the full amount the next day




---

🛡️ Security Tips

Use a new wallet just for the bot

Keep .env private (don’t commit it)

Never expose your private key

Set up a firewall on your VPS to limit access



---

👨‍💻 Run on Vultr 24/7

tmux new -s sniper
python bot.py
# press Ctrl+B then D to detach

Reattach anytime:

tmux attach -t sniper


---

📬 Telegram Message Format

Each trade sends:

📈 BUY
Coin: Fluffy Inu (FLUFF)
CA: So1abcXYZ123...
Buy Time: 2025-08-01 14:31 UTC
Market Cap: $120,000
Amount: $10.00
Tx: https://solscan.io/tx/BUYTXID

📉 SELL
Sell Time: 2025-08-01 16:48 UTC
Market Cap: $180,000
Amount: $15.00
Tx: https://solscan.io/tx/SELLTXID
🔁 Reinvesting tomorrow.


---

📎 License

MIT — free to use and modify.

---

Would you like me to zip up all the files (`bot.py`, `.env.example`, `requirements.txt`, `README.md`) for easy upload to your GitHub repo?
