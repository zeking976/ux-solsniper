# 🚀 UX SolSniper - Solana Meme Coin Sniper Bot

UX SolSniper is a high-speed **automated Solana meme coin sniper bot** with **real-time Telegram alerts**, **dynamic capital management**, and **profit tracking**. 💎✨  

---

## 🔥 Key Features  

### 📲 Telegram Integration  
- Monitors Telegram channels for new contract addresses.  
- Sends **buy/sell alerts** with market cap, profit, and fees.  
- Generates **daily & monthly reports** with trade summaries.  

### ⚡ Automated Trading  
- Executes **real buys & sells** using **JupiterSwap**.  
- Ultra-fast **RPC calls** with anti-MEV protection.  
- Dynamic capital allocation across multiple trades.  
- Configurable **Stop-Loss (SL)** and **Take-Profit (TP)** via `t.env`.  

### 📊 Capital & Cycle Management  
- **Daily capital reset at UTC 00:00**.  
- Supports **multi-cycle trading** (configurable in `t.env`).  
- Auto converts USD → SOL using live price feeds.  
- Tracks **priority fees** (normal vs congestion).  

### ⛽ Priority Fees & Network Safety  
- Normal fee: `0.03 SOL`.  
- Congestion fee: `0.2–0.3 SOL`.  
- Protects against **front-running & MEV bots**.  

### 🗂 Logging & Reporting  
- Logs all trades in `trade_logs.json`.  
- Tracks **market cap, profits, and tips** per trade.  
- Telegram reports include:  
  - ✅ Profits per token  
  - ⛽ Normal vs congestion tips  
  - 📅 Daily & monthly summaries  

### 🔒 Safe & Configurable  
- Uses **t.env** for sensitive keys & parameters.  
- All settings (capital, SL/TP, cycle limits, fees) are configurable.  
- Supports **DRY_RUN** mode for safe testing.  

### 🖥 Compatibility  
- Works on **VULTR VPS**, **Termux**, or local machine.  
- Requires **Python 3.11+**.  
- Lightweight: runs on **cloud or shared CPUs**.  

---

💡 **UX SolSniper = speed + automation + transparency.**  
Built for traders who want **fast, safe, and profitable Solana meme coin sniping.** 🚀