import time
from datetime import datetime, timedelta
from utils import (
    get_env_variable,
    get_market_cap_from_dexscreener,
    calculate_total_gas_fee,
    get_sol_amount_for_usd,
    send_telegram_message,
    is_ca_processed,
    save_processed_ca,
    read_from_target_channel,
    jupiter_buy,
    jupiter_sell
)

# --- ENV Vars ---
PRIVATE_KEY = get_env_variable("PRIVATE_KEY")
SOLANA_RPC = get_env_variable("SOLANA_RPC")
INVESTMENT_USD = float(get_env_variable("INVESTMENT_USD"))
REQUIRED_MULTIPLIER = float(get_env_variable("REQUIRED_MULTIPLIER"))

# Parse daily limits as list of ints from env var "DAILY_LIMITS"
daily_limits_str = get_env_variable("DAILY_LIMITS")  # e.g. "5,4"
DAILY_LIMITS = [int(x.strip()) for x in daily_limits_str.split(",")]

CYCLE_LIMIT = int(get_env_variable("CYCLE_LIMIT"))

# --- Main Sniping Logic ---
cycle_count = 0
capital_usd = INVESTMENT_USD

while cycle_count < CYCLE_LIMIT:
    # Get the daily limit for this cycle day, fallback to last value if out of range
    daily_limit = DAILY_LIMITS[cycle_count] if cycle_count < len(DAILY_LIMITS) else DAILY_LIMITS[-1]

    current_day_investments = 0

    while current_day_investments < daily_limit:
        # Get a fresh contract address from Telegram target channel
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
            print("[!] No new CA. Retrying in 2 min...")
            time.sleep(120)

        # Fetch MC at buy
        market_cap_at_buy = get_market_cap_from_dexscreener(contract_address)
        if not market_cap_at_buy:
            print("[!] Could not fetch market cap. Skipping.")
            continue

        # Calculate buy amount in SOL
        amount_sol = get_sol_amount_for_usd(capital_usd)
        if not amount_sol:
            print("[!] Could not convert USD to SOL. Skipping.")
            continue

        # Execute Buy
        if not jupiter_buy(contract_address, amount_sol):
            print("[!] Buy failed. Skipping.")
            continue

        print(f"[✓] Bought {contract_address} at MC ${market_cap_at_buy:,.0f}")
        send_telegram_message(
            f"[BUY] {contract_address}\nMC: ${market_cap_at_buy:,.0f}\nAmt: ${capital_usd:.2f}"
        )

        # Wait for multiplier hit
        while True:
            current_mc = get_market_cap_from_dexscreener(contract_address)
            if not current_mc:
                print("[!] MC unavailable. Retrying in 2 min...")
                time.sleep(120)
                continue

            target_mc = market_cap_at_buy * REQUIRED_MULTIPLIER
            print(f"[i] Waiting... MC: {current_mc:.0f} | Target: {target_mc:.0f}")

            if current_mc >= target_mc:
                if jupiter_sell(contract_address):
                    print(f"[✓] Sold {contract_address} at MC ${current_mc:,.0f}")
                    send_telegram_message(
                        f"[SELL] {contract_address}\nMC: ${current_mc:,.0f}"
                    )

                    gross_return = capital_usd * REQUIRED_MULTIPLIER
                    gas_fee = calculate_total_gas_fee(gross_return)
                    capital_usd = round(gross_return - gas_fee, 2)

                    print(f"[✓] New capital: ${capital_usd:.2f} (after ${gas_fee:.2f} fee)")
                    break
                else:
                    print("[!] Sell failed. Retrying in 5 min...")
                    time.sleep(300)

        current_day_investments += 1

    cycle_count += 1
    print(f"[*] Finished day {cycle_count} of cycle. Sleeping until UTC midnight.")
    now = datetime.utcnow()
    next_midnight = datetime(now.year, now.month, now.day) + timedelta(days=1)
    time.sleep((next_midnight - now).total_seconds())
