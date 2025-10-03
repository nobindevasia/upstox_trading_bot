#!/usr/bin/env python3
"""
Upstox Algo Trading Bot
Main execution script
"""

from upstox_client import UpstoxClient
from strategy import TradingStrategy
import time
from datetime import datetime, time as dt_time
import sys


def is_market_open():
    """Check if market is open (9:15 AM to 3:30 PM IST on weekdays)"""
    now = datetime.now()

    # Check if it's a weekday (Monday=0, Sunday=6)
    if now.weekday() > 4:
        return False

    # Check if time is between 9:15 AM and 3:30 PM
    market_open = dt_time(9, 15)
    market_close = dt_time(15, 30)
    current_time = now.time()

    return market_open <= current_time <= market_close


def main():
    print("=" * 60)
    print("UPSTOX ALGO TRADING BOT")
    print("=" * 60)

    # Initialize client and strategy
    client = UpstoxClient()
    strategy = TradingStrategy()

    # Verify connection
    print("\n[INFO] Connecting to Upstox API...")
    profile = client.get_profile()

    if profile and 'data' in profile:
        print("[OK] Connected successfully!")
        print(f"User: {profile['data'].get('user_name', 'N/A')}")
        print(f"Email: {profile['data'].get('email', 'N/A')}")
    else:
        print("[FAIL] Failed to connect. Please check your credentials.")
        sys.exit(1)

    # Get fund information
    funds = client.get_funds()
    if funds and 'data' in funds:
        equity = funds['data'].get('equity', {})
        print(f"\n[INFO] Available Margin: INR {equity.get('available_margin', 0):,.2f}")

    print("\n" + "=" * 60)
    print("BOT STATUS: RUNNING")
    print("=" * 60)
    print("\nPress Ctrl+C to stop the bot\n")

    # Main trading loop
    try:
        iteration = 0
        while True:
            iteration += 1
            current_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # Check if market is open
            if not is_market_open():
                print(f"[{current_time_str}] Market is closed. Waiting...")
                time.sleep(60)  # Check every minute
                continue

            print(f"\n[Iteration {iteration}] {current_time_str}")

            # Get current price
            current_price = strategy.get_current_price()
            if current_price is not None:
                print(f"Current Price: INR {current_price:.2f}")

            # Check exit conditions first if in position
            if strategy.active_position:
                print(f"Position: {strategy.active_position} | Entry: INR {strategy.entry_price:.2f}")

                exit_reason = strategy.check_exit_conditions()
                if exit_reason:
                    strategy.exit_position(exit_reason)
            else:
                # Generate trading signal
                signal = strategy.generate_signal()
                print(f"Signal: {signal}")

                if signal == 'BUY':
                    strategy.enter_position('BUY')
                elif signal == 'SELL':
                    strategy.enter_position('SELL')

            # Wait before next iteration (e.g., 30 seconds)
            print("Next check in 30 seconds...")
            time.sleep(30)

    except KeyboardInterrupt:
        print("\n\n[INFO] Stopping bot...")

        # Close any open positions before exiting
        if strategy.active_position:
            print("Closing active position...")
            strategy.exit_position("Manual stop")

        print("Bot stopped successfully. Goodbye!")
        sys.exit(0)
    except Exception as e:
        print(f"\n[FAIL] Error occurred: {e}")
        import traceback
        traceback.print_exc()

        # Try to close positions on error
        if strategy.active_position:
            print("Attempting to close position due to error...")
            strategy.exit_position("Error")

        sys.exit(1)


if __name__ == "__main__":
    main()
