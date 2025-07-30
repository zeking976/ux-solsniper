# Solana Meme Coin Sniper Bot 🤖

This bot:

- Monitors a Telegram channel for new Solana meme coin contract addresses.
- Buys 7 times daily using randomized USD-based amounts (total $910/day).
- Converts USD to SOL in real-time via CoinGecko.
- Automatically simulates selling tokens at 10x price.
- Sends daily and monthly profit/loss reports via Telegram bot.

---

## 🧪 Setup

### 1. Install Requirements
```bash
pip install -r requirements.txt

2. Set Environment Variables

Create a .env file or export the following:

TELEGRAM_API_ID=your_telegram_api_id
TELEGRAM_API_HASH=your_telegram_api_hash
TELEGRAM_BOT_TOKEN=your_bot_token_for_reports
TELEGRAM_REPORT_CHAT_ID=your_telegram_user_id
PHANTOM_PRIVATE_KEY=base64_encoded_phantom_wallet_key

> Get your Telegram User ID by messaging @userinfobot




---

3. Run the Bot

python sniper.py


---

📊 Reports

Daily Report: Sent at 23:59 UTC summarizing buy/sell stats.

Monthly Report: Sent on 1st of each month at 00:05 UTC.



---

🔐 Security

Store your private keys safely.

Do not commit .env or private keys to GitHub.


---
