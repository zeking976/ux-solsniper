import os import time import random import traceback from datetime import datetime, timedelta from dotenv import load_dotenv from utils import ( get_realtime_sol_price, get_priority_fee, get_contract_from_telegram, jupiter_buy, jupiter_sell, get_token_marketcap, get_token_name, is_congested ) from report import send_telegram_message

load_dotenv()

STARTING_USD = 55 GAS_FEE_PERCENT = 0.009 UTC_NOW = lambda: datetime.utcnow()

TOTAL_DAYS = 7 GOAL_AMOUNT = 10000

def calculate_daily_reinvestments(starting_amount, goal, days, gas_fee_percent): reinvestments = [] current = starting_amount for _ in range(days): gas_fee = current * gas_fee_percent reinvest = (current * 2) - gas_fee  # 2x returns reinvestments.append(current) current = reinvest return reinvestments

def wait_until_midnight(): now = UTC_NOW() tomorrow = datetime.combine(now.date() + timedelta(days=1), datetime.min.time()) delta = (tomorrow - now).total_seconds() print(f"[SLEEP] Sleeping for {delta / 3600:.2f} hours until next 00:00 UTC") time.sleep(delta)

def main(): reinvest_plan = calculate_daily_reinvestments(STARTING_USD, GOAL_AMOUNT, TOTAL_DAYS, GAS_FEE_PERCENT)

for day, invest_usd in enumerate(reinvest_plan, 1):
    try:
        print(f"\n[DAY {day}] Starting new cycle at {UTC_NOW()} with ${invest_usd:.2f}")

        # Convert USD to SOL
        sol_price = get_realtime_sol_price()
        invest_sol = invest_usd / sol_price

        # Priority fee
        congested = is_congested()
        priority_fee = get_priority_fee(congested)

        # Get contract from Telegram
        contract_address = get_contract_from_telegram()
        token_name = get_token_name(contract_address)
        buy_marketcap = get_token_marketcap(contract_address)

        # Buy token
        print(f"[BUY] Attempting to buy {token_name} at market cap ${buy_marketcap:.2f}")
        buy_time = UTC_NOW()
        buy_success = jupiter_buy(contract_address, invest_sol, priority_fee)
        while not buy_success:
            time.sleep(10)
            buy_success = jupiter_buy(contract_address, invest_sol, priority_fee)

        # Wait for target market cap
        target_cap = buy_marketcap * 2
        print(f"[WAIT] Waiting for {token_name} to reach ${target_cap:.2f} market cap")

        sell_success = False
        interval = 30
        while not sell_success:
            current_cap = get_token_marketcap(contract_address)
            if current_cap >= target_cap:
                print(f"[SELL] Selling at market cap ${current_cap:.2f}")
                sell_success = jupiter_sell(contract_address, priority_fee)
                while not sell_success:
                    time.sleep(10)
                    sell_success = jupiter_sell(contract_address, priority_fee)
                break
            time.sleep(interval)

        sell_time = UTC_NOW()
        duration = (sell_time - buy_time).total_seconds()
        seconds_until_midnight = max(0, 86400 - duration)
        print(f"[SLEEP] Sleeping for {seconds_until_midnight / 3600:.2f} hours until next cycle")
        send_telegram_message(
            f"\ud83d\udcc8 *Cycle Report Day {day}*\n"
            f"Token: `{token_name}`\n"
            f"Buy Cap: ${buy_marketcap:.2f}\n"
            f"Target Sell Cap: ${target_cap:.2f}\n"
            f"Buy Time: {buy_time.strftime('%H:%M:%S')} UTC\n"
            f"Sell Time: {sell_time.strftime('%H:%M:%S')} UTC\n"
            f"Duration: {duration / 60:.1f} minutes\n"
            f"Used Priority Fee: {priority_fee} SOL\n"
        )

        time.sleep(seconds_until_midnight)

    except Exception as e:
        error = traceback.format_exc()
        print(f"[ERROR] {error}")
        send_telegram_message(f"❌ Bot crashed on day {day}:\n```{error}```")
        wait_until_midnight()

if name == "main": main()

