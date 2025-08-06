import os import time import random import math from datetime import datetime, timedelta from dotenv import load_dotenv from utils import get_market_cap_from_dexscreener, get_sol_price_usd, calculate_total_gas_fee, send_telegram_message

--- Load Environment Variables ---

load_dotenv()

--- Constants ---

INITIAL_INVESTMENT_USD = 55 REQUIRED_MULTIPLIER = 2.0 TOTAL_DAYS = 30 GAS_FEE_PERCENTAGE = 0.009

--- Globals ---

investment_usd = INITIAL_INVESTMENT_USD start_day = datetime.utcnow().date()

--- Placeholder Buy Function (Implement Real Logic) ---

def buy_token(contract_address, amount_sol): print(f"[+] Buying token {contract_address} with {amount_sol:.4f} SOL") return True

--- Placeholder Sell Function (Implement Real Logic) ---

def sell_token(contract_address): print(f"[+] Selling token {contract_address}") return True

--- Convert USD to SOL ---

def convert_usd_to_sol(usd): sol_price = get_sol_price_usd() if sol_price: return usd / sol_price else: print("[!] Cannot convert USD to SOL: No SOL price") return None

--- Main Trading Cycle ---

def run_trading_cycle(): global investment_usd

for day in range(1, TOTAL_DAYS + 1):
    print(f"\n=== Day {day} | UTC: {datetime.utcnow()} ===")

    contract_address = get_contract_address_from_telegram()
    if not contract_address:
        print("[!] No contract address found. Retrying in 10 min...")
        time.sleep(600)
        continue

    # Get market cap at buy time
    market_cap_at_buy = get_market_cap_from_dexscreener(contract_address)
    if not market_cap_at_buy:
        print("[!] Could not fetch market cap. Skipping token.")
        continue

    amount_sol = convert_usd_to_sol(investment_usd)
    if not amount_sol:
        print("[!] Could not convert USD to SOL. Skipping token.")
        continue

    buy_success = buy_token(contract_address, amount_sol)
    if not buy_success:
        print("[!] Buy failed. Skipping token.")
        continue

    print(f"[✓] Bought {contract_address} at {market_cap_at_buy} MC")
    send_telegram_message(f"[BUY] {contract_address}\nMarket Cap: ${market_cap_at_buy:,.0f}\nAmount: {investment_usd:.2f} USD")

    # Wait for target market cap to hit
    while True:
        current_mc = get_market_cap_from_dexscreener(contract_address)
        if not current_mc:
            print("[!] Market cap unavailable. Retrying in 2 min...")
            time.sleep(120)
            continue

        print(f"[i] Waiting... Current MC: {current_mc:.0f} | Target: {market_cap_at_buy * REQUIRED_MULTIPLIER:.0f}")
        if current_mc >= market_cap_at_buy * REQUIRED_MULTIPLIER:
            sell_success = sell_token(contract_address)
            if sell_success:
                print(f"[✓] Sold {contract_address} at {current_mc} MC")
                send_telegram_message(f"[SELL] {contract_address}\nMarket Cap: ${current_mc:,.0f}")

                # Calculate new investment after subtracting gas
                gross_return = investment_usd * REQUIRED_MULTIPLIER
                gas_fee = calculate_total_gas_fee(gross_return)
                investment_usd = round(gross_return - gas_fee, 2)
                print(f"[✓] New Investment Capital: ${investment_usd:.2f} after gas ${gas_fee:.2f}")
            else:
                print("[!] Sell failed. Retrying in 5 min...")
                time.sleep(300)
                continue
            break
        time.sleep(120)

    # Sleep until 00:00 UTC the next day
    now = datetime.utcnow()
    tomorrow = now + timedelta(days=1)
    midnight = datetime.combine(tomorrow.date(), datetime.min.time())
    sleep_duration = (midnight - now).total_seconds()
    print(f"[*] Sleeping {sleep_duration / 3600:.2f} hours until next cycle...\n")
    time.sleep(sleep_duration)

--- Simulate Getting Contract Address ---

def get_contract_address_from_telegram(): try: # Replace this logic with actual Telegram scraping dummy_addresses = [ "6TgL7cywVZP1zFjkpHMGgf6kYE3tAEBjVJVu6QQAGvWb", "9skSh2vG9ZaFVZT38aThAYGVx4GZtYZ3rEZ2ULXYtK9T", "J7kLGdXh8LmD4PvTfDSaAqQxsvbULv59Bzv4obBFQz9P" ] return random.choice(dummy_addresses) except Exception as e: print(f"[!] Error fetching contract address: {e}") return None

--- Run Bot ---

if name == "main": try: run_trading_cycle() except Exception as e: print(f"[FATAL] Uncaught error in sniper bot: {e}") send_telegram_message(f"[ERROR] Sniper crashed: {e}")


