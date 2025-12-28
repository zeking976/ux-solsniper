# 🚀 UX-SolSniper

A Solana meme coin sniper bot that **automatically buys tokens from Telegram channels** and **sells based on market cap targets**. Supports **DRY_RUN simulations**, **daily/monthly reports**, and **TARGET_MULTIPLIER stop logic**.  

---

## ⚡ Features

- Monitor Telegram channels for new token contract addresses 📨  
- Auto-buy tokens on Solana using **JupiterSwap** 💸  
- Auto-sell at **take profit / stop loss** levels based on market cap 📊  
- Track profits/losses in **JSON** (`position_state.json`) 🗂️  
- Daily and monthly profit reports via Telegram 📑  
- DRY_RUN mode for testing without spending SOL 🛡️  
- Auto-stop when `TARGET_MULTIPLIER` is reached ⛔  
- Handles **priority fees** and **MEV protection** 🚦  

---

## 🛠️ Installation

1. **Clone the repo**

```bash
git clone https://github.com/zeking976/ux-solsniper.git
cd ux-solsniper

2. Create a Python virtual environment



python3 -m venv venv
source venv/bin/activate   # Linux / macOS
venv\Scripts\activate      # Windows

3. Install dependencies



pip install -r requirements.txt

4. Set environment variables in t.env:



BOT_TOKEN=your_telegram_bot_token
CHAT_ID=your_telegram_chat_id
DAILY_CAPITAL_USD=100


---

▶️ Running the Bot

python3 main.py

Use DRY_RUN=1 in t.env to simulate trades without spending SOL 🧪

Use DRY_RUN=0 for live trading 💰



---

📂 Files

main.py — Orchestrates the bot

sniper.py — Core sniper logic

buy.py / sell.py — Execute JupiterSwap buy/sell

utils.py — Helper functions

reports.py — Daily/monthly stats & reporting

t.env — Environment variables

position_state.json — Current balance & active trades



---

⚠️ Notes

Start balance is automatically tracked to calculate TARGET_MULTIPLIER

Make sure your wallet has enough SOL for transactions

Telegram bot is only used for reporting, not for trading



---

📬 Support

Report issues or contribute via GitHub Issues / Pull Requests 💡