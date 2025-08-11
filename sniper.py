import time
import requests
import json
from datetime import datetime, timedelta
from utils import (
    get_env_variable,
    get_market_cap_from_dexscreener,
    calculate_total_gas_fee,
    get_sol_amount_for_usd,
    send_telegram_message,
    is_ca_processed,
    save_processed_ca,
    read_from_target_channel
)

# --- ENV Vars ---
PRIVATE_KEY = get_env_variable("PRIVATE_KEY")
SOLANA_RPC = get_env_variable("SOLANA_RPC")
JUPITER_API = get_env_variable("JUPITER_API")
investment_usd = float(get_env_variable("INVESTMENT_USD"))
required_multiplier = float(get_env_variable("REQUIRED_MULTIPLIER"))
daily_limit = int(get_env_variable("DAILY_LIMIT"))
cycle_limit = int(get_env_variable("CYCLE_LIMIT"))

# --- Jupiter Buy ---
def buy_token(ca, amount_sol):
    try:
        print(f"[BUY] Executing Jupiter swap for {ca} with {amount_sol} SOL...")
        swap_url = f"{JUPITER_API}/swap"
        payload = {
            "inputMint": "So11111111111111111111111111111111111111112",  # SOL
            "outputMint": ca,
            "amount": int(amount_sol * 1e9),  # lamports
            "slippageBps": 50,
            "userPublicKey": get_env_variable("PUBLIC_KEY"),
        }
        resp = requests.post(swap_url, json=payload)
        if resp.status_code == 200:
            print("[✓] Jupiter buy transaction sent.")
            return True
        else:
            print(f"[!] Jupiter buy failed: {resp.text}")
            return False
    except Exception as e:
        print(f"[!] Jupiter buy error: {e}")
        return False

# --- Jupiter Sell ---
def sell_token(ca):
    try:
        print(f"[SELL] Executing Jupiter swap from {ca} to SOL...")
        swap_url = f"{JUPITER_API}/swap"
        payload = {
            "inputMint": ca,
            "outputMint": "So11111111111111111111111111111111111111112",  # SOL
            "amount": get_token_balance_lamports(ca),
            "slippageBps": 50,
            "userPublicKey": get_env_variable("PUBLIC_KEY"),
        }
        resp = requests.post(swap_url, json=payload)
        if resp.status_code == 200:
            print("[✓] Jupiter sell transaction sent.")
            return True
        else:
            print(f"[!] Jupiter sell failed: {resp.text}")
            return False
    except Exception as e:
        print(f"[!] Jupiter sell error: {e}")
        return False

# Placeholder for balance fetch
def get_token_balance_lamports(ca):
    # Here you would query Solana RPC for token account balance
    return 1_000_000  # Replace with actual balance in lamports

# --- Main Sniping Logic ---
cycle_count = 0

while cycle_count < cycle_limit:
    current_day_investments = 0

    while current_day_investments < daily_limit:
        while True:
            messages = read_from_target_channel(limit=5)
            contract_address = None

            for msg in messages:
                if "CA:" in msg:
                    ca = msg.split("CA:")[1].strip().split()[0]
                    if not is_ca_processed(ca):
                        contract_address = ca
                        save_processed_ca(ca)
                        break

            if contract_address:
                break
            print("[!] No new contract address. Retrying in 2 min...")
            time.sleep(120)

        market_cap_at_buy = get_market_cap_from_dexscreener(contract_address)
        if not market_cap_at_buy:
            print("[!] Could not fetch market cap. Skipping token.")
            continue

        amount_sol = get_sol_amount_for_usd(investment_usd)
        if not amount_sol:
            print("[!] Could not convert USD to SOL. Skipping token.")
            continue

        if not buy_token(contract_address, amount_sol):
            print("[!] Buy failed. Skipping token.")
            continue

        print(f"[✓] Bought {contract_address} at {market_cap_at_buy} MC")
        send_telegram_message(
            f"[BUY] {contract_address}\nMarket Cap: ${market_cap_at_buy:,.0f}\nAmount: ${investment_usd:.2f}"
        )

        # Wait until price hits target multiplier
        while True:
            current_mc = get_market_cap_from_dexscreener(contract_address)
            if not current_mc:
                print("[!] Market cap unavailable. Retrying in 2 min...")
                time.sleep(120)
                continue

            print(f"[i] Waiting... Current MC: {current_mc:.0f} | Target: {market_cap_at_buy * required_multiplier:.0f}")
            if current_mc >= market_cap_at_buy * required_multiplier:
                if sell_token(contract_address):
                    print(f"[✓] Sold {contract_address} at {current_mc} MC")
                    send_telegram_message(
                        f"[SELL] {contract_address}\nMarket Cap: ${current_mc:,.0f}"
                    )

                    gross_return = investment_usd * required_multiplier
                    gas_fee = calculate_total_gas_fee(gross_return)
                    investment_usd = round(gross_return - gas_fee, 2)

                    print(f"[✓] New Investment Capital: ${investment_usd:.2f} (after gas fee: ${gas_fee:.2f})")
                    break
                else:
                    print("[!] Sell failed. Retrying in 5 min...")
                    time.sleep(300)

        current_day_investments += 1

    cycle_count += 1
    print(f"[*] Finished day {cycle_count} of cycle. Sleeping until next UTC midnight.")
    now = datetime.utcnow()
    next_midnight = datetime(now.year, now.month, now.day) + timedelta(days=1)
    time.sleep((next_midnight - now).total_seconds())
