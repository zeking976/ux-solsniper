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

try:
    PRIVATE_KEY = get_env_variable("PRIVATE_KEY")
    SOLANA_RPC = get_env_variable("SOLANA_RPC")
    INVESTMENT_USD = float(get_env_variable("INVESTMENT_USD"))
    REQUIRED_MULTIPLIER = float(get_env_variable("REQUIRED_MULTIPLIER"))

    daily_limit_str = get_env_variable("DAILY_LIMIT")
    DAILY_LIMITS = [int(x.strip()) for x in daily_limit_str.split(",")]

    CYCLE_LIMIT = int(get_env_variable("CYCLE_LIMIT"))
except Exception as e:
    print(f"[!] Environment variable error: {e}")
    exit(1)

cycle_count = 0
capital_usd = INVESTMENT_USD

while cycle_count < CYCLE_LIMIT:
    daily_limit = DAILY_LIMITS[cycle_count] if cycle_count < len(DAILY_LIMITS) else DAILY_LIMITS[-1]

    current_day_investments = 0

    while current_day_investments < daily_limit:
        contract_address = None

        while not contract_address:
            messages = read_from_target_channel(limit=5)

            for msg in messages:
                if msg and "CA:" in msg:
                    ca = msg.split("CA:")[1].strip().split()[0]
                    if not is_ca_processed(ca):
                        contract_address = ca
                        save_processed_ca(ca)
                        break

            if not contract_address:
                print("[!] No new contract address found. Retrying in 2 minutes...")
                time.sleep(120)

        market_cap_at_buy = get_market_cap_from_dexscreener(contract_address)
        if not market_cap_at_buy:
            print("[!] Could not fetch market cap. Skipping this CA.")
            continue

        amount_sol = get_sol_amount_for_usd(capital_usd)
        if amount_sol == 0:
            print("[!] Could not convert USD to SOL. Skipping this CA.")
            continue

        if not jupiter_buy(contract_address, amount_sol):
            print("[!] Buy transaction failed. Skipping this CA.")
            continue

        print(f"[✓] Bought {contract_address} at MC ${market_cap_at_buy:,.0f}")
        send_telegram_message(
            f"[BUY] {contract_address}\nMC: ${market_cap_at_buy:,.0f}\nAmount invested: ${capital_usd:.2f}"
        )

        while True:
            current_mc = get_market_cap_from_dexscreener(contract_address)
            if not current_mc:
                print("[!] Market cap unavailable. Retrying in 2 minutes...")
                time.sleep(120)
                continue

            target_mc = market_cap_at_buy * REQUIRED_MULTIPLIER
            print(f"[i] Waiting... Current MC: ${current_mc:,.0f} | Target MC: ${target_mc:,.0f}")

            if current_mc >= target_mc:
                if jupiter_sell(contract_address):
                    print(f"[✓] Sold {contract_address} at MC ${current_mc:,.0f}")
                    send_telegram_message(
                        f"[SELL] {contract_address}\nMC: ${current_mc:,.0f}"
                    )

                    gross_return = capital_usd * REQUIRED_MULTIPLIER
                    gas_fee = calculate_total_gas_fee(gross_return)
                    if gas_fee is None:
                        gas_fee = 0
                    capital_usd = round(gross_return - gas_fee, 2)

                    print(f"[✓] Updated capital after fees: ${capital_usd:.2f} (fees: ${gas_fee:.2f})")
                    break
                else:
                    print("[!] Sell transaction failed. Retrying in 5 minutes...")
                    time.sleep(300)
            else:
                time.sleep(60)

        current_day_investments += 1

    cycle_count += 1
    print(f"[*] Finished day {cycle_count} of cycle. Sleeping until next UTC midnight.")
    now = datetime.utcnow()
    next_midnight = datetime(now.year, now.month, now.day) + timedelta(days=1)
    sleep_seconds = (next_midnight - now).total_seconds()
    time.sleep(sleep_seconds)
