import os
import time
import random
import re
from datetime import datetime, timedelta

from dotenv import load_dotenv
load_dotenv(dotenv_path="t.env")

from utils import (
    get_market_cap_from_dexscreener,
    get_sol_price_usd,
    calculate_total_gas_fee,
    send_telegram_message,
    fetch_latest_messages_from_channel
)

from reports import (
    record_buy,
    record_sell,
    send_daily_report
)

# Config
INITIAL_INVESTMENT_USD = 11
REQUIRED_MULTIPLIER = 2.0
GAS_FEE_PERCENTAGE = 0.009
CYCLE_INVESTMENTS = [5, 4]  # 5 reinvestments on Day 1, 4 on Day 2

# State
investment_usd = INITIAL_INVESTMENT_USD
seen_contracts = set()
last_sell_time = None
current_cycle_day = 0
cycle_count = 0
cycle_investments_done = 0

def buy_token(contract_address, amount_sol):
    print(f"[+] Buying token {contract_address} with {amount_sol:.4f} SOL")
    return True

def sell_token(contract_address):
    print(f"[+] Selling token {contract_address}")
    return True

def convert_usd_to_sol(usd):
    sol_price = get_sol_price_usd()
    if sol_price:
        return usd / sol_price
    else:
        print("[!] Cannot convert USD to SOL: No SOL price")
        return None

def extract_contract_address_from_text(text):
    pattern = r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b"
    matches = re.findall(pattern, text)
    return matches[0] if matches else None

def get_new_contract_address(after_timestamp):
    messages = fetch_latest_messages_from_channel()
    for msg in messages:
        address = extract_contract_address_from_text(msg)
        if address and address not in seen_contracts:
            msg_time = datetime.utcnow()  # Assuming all fetched messages are recent
            if msg_time > after_timestamp:
                seen_contracts.add(address)
                return address
    return None

def sleep_until_midnight_utc():
    now = datetime.utcnow()
    midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time())
    seconds = (midnight - now).total_seconds()
    print(f"[*] Sleeping until UTC midnight: {int(seconds)} seconds")
    time.sleep(seconds)

def run_forever():
    global investment_usd, last_sell_time, current_cycle_day, cycle_count, cycle_investments_done

    while True:
        if cycle_count >= len(CYCLE_INVESTMENTS):
            cycle_count = 0
            investment_usd = INITIAL_INVESTMENT_USD
            print("[*] Resetting investment to $11 and starting new 2-day cycle.")

        daily_limit = CYCLE_INVESTMENTS[cycle_count]
        current_day_investments = 0

        while current_day_investments < daily_limit:
            print(f"\n=== Cycle Day {cycle_count + 1} | Investment #{current_day_investments + 1} ===")
            while True:
                contract_address = get_new_contract_address(last_sell_time or datetime.utcnow() - timedelta(minutes=1))
                if contract_address:
                    break
                print("[!] No new contract address. Retrying in 2 min...")
                time.sleep(120)

            market_cap_at_buy = get_market_cap_from_dexscreener(contract_address)
            if not market_cap_at_buy:
                print("[!] Could not fetch market cap. Skipping token.")
                continue

            amount_sol = convert_usd_to_sol(investment_usd)
            if not amount_sol:
                print("[!] Could not convert USD to SOL. Skipping token.")
                continue

            if not buy_token(contract_address, amount_sol):
                print("[!] Buy failed. Skipping token.")
                continue

            print(f"[✓] Bought {contract_address} at {market_cap_at_buy} MC")
            send_telegram_message(f"[BUY] {contract_address}\nMarket Cap: ${market_cap_at_buy:,.0f}\nAmount: ${investment_usd:.2f}")
            record_buy(contract_address, investment_usd, market_cap_at_buy)

            # Wait until price hits 2x MC
            while True:
                current_mc = get_market_cap_from_dexscreener(contract_address)
                if not current_mc:
                    print("[!] Market cap unavailable. Retrying in 2 min...")
                    time.sleep(120)
                    continue

                print(f"[i] Waiting... Current MC: {current_mc:.0f} | Target: {market_cap_at_buy * REQUIRED_MULTIPLIER:.0f}")
                if current_mc >= market_cap_at_buy * REQUIRED_MULTIPLIER:
                    if sell_token(contract_address):
                        last_sell_time = datetime.utcnow()
                        print(f"[✓] Sold {contract_address} at {current_mc} MC")
                        send_telegram_message(f"[SELL] {contract_address}\nMarket Cap: ${current_mc:,.0f}")
                        record_sell(contract_address, current_mc)

                        gross_return = investment_usd * REQUIRED_MULTIPLIER
                        gas_fee = calculate_total_gas_fee(gross_return)
                        investment_usd = round(gross_return - gas_fee, 2)

                        print(f"[✓] New Investment Capital: ${investment_usd:.2f} (after gas fee: ${gas_fee:.2f})")
                        break
                    else:
                        print("[!] Sell failed. Retrying in 5 min...")
                        time.sleep(300)

            current_day_investments += 1
            cycle_investments_done += 1

        # Send daily summary report
        send_daily_report()

        cycle_count += 1
        print(f"[*] Finished day {cycle_count} of cycle. Sleeping until next UTC midnight.")
        sleep_until_midnight_utc()

if __name__ == "__main__":
    run_forever()
