# 🚀 UX SolSniper - Solana Meme Coin Sniper Bot

Welcome to **UX SolSniper**, the ultimate Solana meme coin sniper bot designed for automated trading, profit tracking, and real-time notifications. 💎✨

---

## 🔥 Key Features

### 1️⃣ Telegram Integration
- 📝 **Monitors Telegram channels** for new token contract addresses.  
- 📲 Sends **real-time buy/sell notifications** to your Telegram account.  
- 🗂 Keeps a **record of all trades** in JSON logs for daily/monthly reports.

### 2️⃣ Automated Trading
- 💰 Executes **real buys and sells** using **JupiterSwap**.  
- ⚡ Uses **fast Solana RPC calls** for speed and efficiency.  
- 🛡 Includes **Anti-MEV protection** to prevent front-running.  
- 💵 **Dynamic investment**: invests a portion of daily capital across multiple buy cycles.  
- 🧮 Calculates **target sell price** using configurable **investment multipliers** (e.g., 2× by default).  

### 3️⃣ Capital Management
- 📊 Configurable **daily capital** and **max buys per day**.  
- 🔄 Supports **compounded investment strategy** across multiple buys.  
- 🪙 Converts **USD to SOL** in real-time with **market price detection**.  
- ⛽ Automatically accounts for **Solana network fees** and **priority fees** (normal & congestion).  

### 4️⃣ Priority Fees & MEV Protection
- ⏱ Detects **network congestion** and dynamically adjusts **priority fee**:  
  - 0.03 SOL normal  
  - 0.2–0.3 SOL during congestion  
- 🛡 Minimizes risk from **front-running bots** and MEV exploits.

### 5️⃣ Logging & Reports
- 🗂 Tracks **processed contract addresses** to avoid double buys.  
- 📈 Maintains **JSON logs** for all buys, sells, profits, and fees.  
- 🟢 Sends **daily & monthly Telegram reports** with:  
  - Total profits  
  - Tips paid (normal vs congestion)  
  - Buy/sell timestamps  
  - Market caps at buy and sell  

### 6️⃣ Safe & Configurable
- 🔒 Uses **.env (t.env)** for sensitive data like keys and tokens.  
- ⚙️ All key parameters (daily capital, multiplier, max buys, fees) are **editable at any time**.  
- 🧪 Supports **DRY_RUN mode** for testing without spending real SOL.  

### 7️⃣ Compatibility
- 🖥 Works on VPS, Termux, or local machine.  
- 🤖 Compatible with **Python 3.11+** and popular packages: `telethon`, `solana`, `solders`, `python-dotenv`.  
- 📦 Includes **all necessary tools** for signing transactions and communicating with JupiterSwap.  

---

## 💡 Extra Highlights
- 🎯 **Solana-only focus** for ultra-fast meme coin sniping.  
- 🔁 Automatic **retry on failed transactions** for both buy and sell.  
- 🕒 **UTC 00:00 cycle scheduling** ensures daily capital reset.  
- 📊 Detailed **profit & fee tracking** for better performance insights.  

---

**💎 UX SolSniper** is built for serious Solana meme coin traders who want speed, automation, and transparency.  

---

### ⚡ Emojis Key
- ✅ = Successful trade  
- 🟣 = Sell executed  
- 🔹 = Trade entry  
- 📅 = Daily/monthly reports  
- ⛽ = Gas / priority fees