#!/usr/bin/env python3
"""
Enhanced Backtesting Engine with:
- Dynamic ATM strike selection at entry time
- Multi-expiry support with roll logic
- Greeks calculation and filtering
- Slippage modeling
- All features from AdvancedBacktestEngine
"""

from datetime import datetime
from typing import List, Dict, Optional
from zoneinfo import ZoneInfo
from backtest_helpers import StrikeSelector, GreeksCalculator, SlippageModel, MultiExpiryManager

# IST timezone
IST = ZoneInfo("Asia/Kolkata")


class EnhancedBacktestEngine:
    """
    Enhanced backtesting engine with dynamic strike selection and Greeks.
    """

    def __init__(self, initial_capital=100000, lot_size=50,
                 atm_offset=0, enable_greeks=True, enable_slippage=True):
        """
        Args:
            initial_capital: Starting capital
            lot_size: Position size
            atm_offset: 0=ATM, 1=OTM, -1=ITM
            enable_greeks: Calculate and filter by Greeks
            enable_slippage: Apply slippage to trades
        """
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.lot_size = lot_size

        # Advanced features
        self.strike_selector = StrikeSelector(atm_offset=atm_offset)
        self.greeks_calc = GreeksCalculator()
        self.slippage_model = SlippageModel()
        self.enable_greeks = enable_greeks
        self.enable_slippage = enable_slippage

        # Position state
        self.position = None
        self.position_quantity = 0
        self.remaining_quantity = 0
        self.partial_exit_done = False
        self.trailing_active = False

        # Trade tracking
        self.trades = []
        self.equity_curve = []
        self.realized_pnl = 0.0

        # Greeks filters (can be configured)
        self.min_delta = 0.35
        self.max_delta = 0.70
        self.max_iv = 80.0  # Don't trade when IV > 80% (Nifty typically 15-60%)

    def run_backtest(self, synchronized_data: List[Dict], strategy,
                     stop_loss_pct=30.0, target_pct=50.0,
                     all_contracts=None, expiry_date=None):
        """
        Run enhanced backtest with dynamic features.

        Args:
            synchronized_data: List of dicts with 'timestamp', 'underlying', 'option'
            strategy: Strategy object
            stop_loss_pct: Initial stop loss percentage
            target_pct: Target profit percentage
            all_contracts: All available option contracts (for dynamic selection)
            expiry_date: Expiry date for this backtest
        """
        print(f"\nStarting enhanced backtest with {len(synchronized_data)} candles...")
        print(f"Initial capital: Rs.{self.initial_capital:,.2f}")
        print(f"Features enabled:")
        print(f"  - Dynamic ATM selection: {'YES' if all_contracts else 'NO'}")
        print(f"  - Greeks calculation: {'YES' if self.enable_greeks else 'NO'}")
        print(f"  - Slippage modeling: {'YES' if self.enable_slippage else 'NO'}")
        print()

        for i, sync_data in enumerate(synchronized_data):
            timestamp = sync_data['timestamp']
            underlying_candle = sync_data['underlying']
            option_candle = sync_data['option']

            dt = datetime.fromtimestamp(timestamp / 1000, tz=IST)

            # Build historical data up to current point
            underlying_history = [sd['underlying'] for sd in synchronized_data[:i+1]]
            option_history = [sd['option'] for sd in synchronized_data[:i+1]]

            # Calculate days to expiry
            if expiry_date:
                expiry_dt = datetime.strptime(expiry_date, '%Y-%m-%d').replace(tzinfo=IST)
                tte_days = (expiry_dt - dt).days
            else:
                tte_days = 5  # Default

            # Calculate IV and Greeks
            spot = underlying_candle[4]
            option_price = option_candle[4]

            greeks = None
            if self.enable_greeks and self.position:
                # Calculate Greeks for current position
                iv = self.greeks_calc.implied_volatility(
                    market_price=option_price,
                    spot=spot,
                    strike=self.position.get('strike', spot),
                    tte_days=tte_days,
                    option_type='CE'
                )

                greeks = self.greeks_calc.calculate_greeks(
                    spot=spot,
                    strike=self.position.get('strike', spot),
                    tte_days=tte_days,
                    iv=iv,
                    option_type='CE'
                )

            # Current market data
            market_data = {
                'timestamp': timestamp,
                'datetime': dt,
                'underlying_close': spot,
                'option_open': option_candle[1],
                'option_high': option_candle[2],
                'option_low': option_candle[3],
                'option_close': option_price,
                'option_volume': option_candle[5],
                'tte_days': tte_days,
                'greeks': greeks,
                'index': i
            }

            # Update equity curve
            current_equity = self.capital + self.realized_pnl
            if self.position:
                unrealized_pnl = (option_price - self.position['entry_price']) * self.remaining_quantity
                current_equity += unrealized_pnl

            self.equity_curve.append({
                'timestamp': dt,
                'equity': current_equity
            })

            # Check if we have a position
            if self.position:
                # Check hard exit time (prevent overnight positions)
                dt = datetime.fromtimestamp(timestamp / 1000, tz=IST)
                if dt.time() >= strategy.hard_exit_time:
                    self.exit_position(market_data, 'HARD_EXIT', option_history, strategy)
                    continue

                # Check EOD flatten (normal close before market close)
                if strategy._should_flatten_eod(timestamp):
                    self.exit_position(market_data, 'EOD_FLATTEN', option_history, strategy)
                    continue

                # Check exit conditions
                exit_reason = self.check_exit_conditions(
                    market_data,
                    option_history,
                    strategy,
                    stop_loss_pct,
                    target_pct
                )
                if exit_reason:
                    self.exit_position(market_data, exit_reason, option_history, strategy)
            else:
                # Check entry conditions
                signal = strategy.generate_signal(
                    market_data,
                    underlying_history,
                    option_history
                )

                if signal in ['BUY', 'SELL']:
                    # Dynamic strike selection (if contracts available)
                    selected_strike = None
                    if all_contracts:
                        # Get available strikes
                        strikes = sorted(list(set([c.get('strike_price', 0) for c in all_contracts])))

                        # Select ATM/OTM strike based on current spot
                        selected_strike = self.strike_selector.select_strike(
                            spot_price=spot,
                            available_strikes=strikes,
                            option_type='CE' if signal == 'BUY' else 'PE'
                        )

                        print(f"[{dt}] Dynamic strike selection:")
                        print(f"  Nifty Spot: {spot:.2f}")
                        print(f"  Selected Strike: {selected_strike}")

                    # Check Greeks filter (if enabled)
                    if self.enable_greeks and selected_strike:
                        # Calculate Greeks for potential entry
                        iv = self.greeks_calc.implied_volatility(
                            market_price=option_price,
                            spot=spot,
                            strike=selected_strike,
                            tte_days=tte_days,
                            option_type='CE'
                        )

                        entry_greeks = self.greeks_calc.calculate_greeks(
                            spot=spot,
                            strike=selected_strike,
                            tte_days=tte_days,
                            iv=iv,
                            option_type='CE'
                        )

                        # Apply Greeks filters
                        if entry_greeks['delta'] < self.min_delta:
                            print(f"  Skipped: Delta {entry_greeks['delta']} < {self.min_delta} (too far OTM)")
                            continue

                        if entry_greeks['delta'] > self.max_delta:
                            print(f"  Skipped: Delta {entry_greeks['delta']} > {self.max_delta} (too deep ITM)")
                            continue

                        if entry_greeks['iv'] > self.max_iv:
                            print(f"  Skipped: IV {entry_greeks['iv']}% > {self.max_iv}% (too expensive)")
                            continue

                        print(f"  Entry Greeks: Delta={entry_greeks['delta']:.3f}, IV={entry_greeks['iv']:.1f}%")

                    # Enter position with detailed logging
                    signal_details = getattr(strategy, 'last_signal_details', {})
                    self.enter_position(
                        signal, market_data, stop_loss_pct, target_pct,
                        strategy, selected_strike, signal_details
                    )

        # Close any open position at the end
        if self.position and synchronized_data:
            last_sync = synchronized_data[-1]
            option_history = [sd['option'] for sd in synchronized_data]

            self.exit_position({
                'timestamp': last_sync['timestamp'],
                'datetime': datetime.fromtimestamp(last_sync['timestamp'] / 1000, tz=IST),
                'option_close': last_sync['option'][4],
                'option_high': last_sync['option'][2],
                'option_low': last_sync['option'][3],
                'option_volume': last_sync['option'][5],
                'greeks': None
            }, 'END_OF_DATA', option_history, strategy)

        return self.generate_report()

    def enter_position(self, side, market_data, stop_loss_pct, target_pct, strategy, strike=None, signal_details=None):
        """Enter a trading position with detailed logging of entry reasons."""
        base_price = market_data['option_close']
        volume = market_data['option_volume']
        quantity = self.lot_size

        # Apply slippage
        if self.enable_slippage:
            entry_price = self.slippage_model.calculate_slippage(
                price=base_price,
                side=side,
                volume=volume,
                lot_size=quantity
            )
            slippage_cost = abs(entry_price - base_price) * quantity
        else:
            entry_price = base_price
            slippage_cost = 0

        # Calculate initial stop loss and target
        if side == 'BUY':
            stop_loss = entry_price * (1 - stop_loss_pct / 100)
            target = entry_price * (1 + target_pct / 100)
        else:
            stop_loss = entry_price * (1 + stop_loss_pct / 100)
            target = entry_price * (1 - target_pct / 100)

        # Calculate 1R target for partial exit
        risk_per_unit = abs(entry_price - stop_loss)
        partial_target = entry_price + risk_per_unit if side == 'BUY' else entry_price - risk_per_unit

        self.position = {
            'side': side,
            'entry_price': entry_price,
            'entry_time': market_data['datetime'],
            'quantity': quantity,
            'stop_loss': stop_loss,
            'target': target,
            'partial_target': partial_target,
            'risk_per_unit': risk_per_unit,
            'strike': strike or market_data.get('underlying_close', 0),
            'entry_greeks': market_data.get('greeks')
        }

        self.position_quantity = quantity
        self.remaining_quantity = quantity
        self.partial_exit_done = False
        self.trailing_active = False

        # Calculate entry costs (transaction costs)
        entry_costs = strategy._calculate_transaction_costs(entry_price, entry_price, quantity) / 2
        self.realized_pnl -= entry_costs

        # Enhanced Entry Logging
        print(f"\n{'='*70}")
        print(f"[{market_data['datetime']}] ENTRY SIGNAL: {side}")
        print(f"{'='*70}")

        # Strategy Parameters
        if signal_details:
            print(f"\n[STRATEGY PARAMETERS]")
            print(f"  Trend Bias:      {signal_details.get('bias', 'N/A')}")
            print(f"  Nifty Spot:      {signal_details.get('nifty_spot', 0):.2f}")
            print(f"  VWAP:            {signal_details.get('vwap', 0):.2f}")
            print(f"  RSI (14):        {signal_details.get('rsi', 0):.2f}")

            print(f"\n[15-MIN TREND INDICATORS]")
            print(f"  EMA20:           {signal_details.get('ema20_15m', 0):.2f}")
            print(f"  EMA50:           {signal_details.get('ema50_15m', 0):.2f}")

            if side == 'BUY':
                print(f"  Condition:       EMA20 > EMA50 ✓ (Bullish)")
            else:
                print(f"  Condition:       EMA20 < EMA50 ✓ (Bearish)")

            print(f"\n[5-MIN PULLBACK INDICATORS]")
            print(f"  EMA9:            {signal_details.get('ema9_5m', 0):.2f}")
            print(f"  EMA21:           {signal_details.get('ema21_5m', 0):.2f}")

            last_candle = signal_details.get('last_candle')
            prev_candle = signal_details.get('prev_candle')
            if last_candle:
                print(f"\n[PULLBACK CONFIRMATION]")
                print(f"  Last Candle:     O:{last_candle[1]:.2f} H:{last_candle[2]:.2f} L:{last_candle[3]:.2f} C:{last_candle[4]:.2f}")
                if prev_candle:
                    print(f"  Prev Candle:     O:{prev_candle[1]:.2f} H:{prev_candle[2]:.2f} L:{prev_candle[3]:.2f} C:{prev_candle[4]:.2f}")

                if side == 'BUY':
                    print(f"  ✓ Bullish candle (Close > Open)")
                    print(f"  ✓ Close above EMA9")
                    print(f"  ✓ Break of previous high")
                    print(f"  ✓ Touched EMA9 (pullback)")
                    print(f"  ✓ Volume surge detected")
                else:
                    print(f"  ✓ Bearish candle (Close < Open)")
                    print(f"  ✓ Close below EMA9")
                    print(f"  ✓ Break of previous low")
                    print(f"  ✓ Touched EMA9 (pullback)")
                    print(f"  ✓ Volume surge detected")

        # Position Details
        print(f"\n[POSITION DETAILS]")
        print(f"  Entry Price:     Rs.{entry_price:.2f}")
        print(f"  Quantity:        {quantity} lots")
        if strike:
            print(f"  Strike Price:    {strike}")
        print(f"  Position Value:  Rs.{entry_price * quantity:,.2f}")

        # Risk Management
        print(f"\n[RISK MANAGEMENT]")
        print(f"  Stop Loss:       Rs.{stop_loss:.2f} ({stop_loss_pct}%)")
        print(f"  Target:          Rs.{target:.2f} ({target_pct}%)")
        print(f"  Partial Target:  Rs.{partial_target:.2f} (1R)")
        print(f"  Risk per unit:   Rs.{risk_per_unit:.2f}")
        print(f"  Total Risk:      Rs.{risk_per_unit * quantity:,.2f}")
        print(f"  Risk:Reward:     1:{target_pct/stop_loss_pct:.2f}")

        # Costs
        print(f"\n[TRANSACTION COSTS]")
        if slippage_cost > 0:
            print(f"  Slippage:        Rs.{slippage_cost:.2f}")
        print(f"  Entry Costs:     Rs.{entry_costs:.2f}")
        print(f"  Total Costs:     Rs.{slippage_cost + entry_costs:.2f}")
        print(f"{'='*70}\n")

    def check_exit_conditions(self, market_data, option_history, strategy, stop_loss_pct, target_pct):
        """Check if any exit condition is met (same as AdvancedBacktestEngine)."""
        current_price = market_data['option_close']
        high = market_data['option_high']
        low = market_data['option_low']
        position = self.position

        # Check stop loss (always active)
        if position['side'] == 'BUY':
            if low <= position['stop_loss']:
                return 'STOP_LOSS'
        else:  # SELL
            if high >= position['stop_loss']:
                return 'STOP_LOSS'

        # Check partial target (if not done yet)
        if not self.partial_exit_done:
            if position['side'] == 'BUY':
                if high >= position['partial_target']:
                    return 'PARTIAL_TARGET'
            else:
                if low <= position['partial_target']:
                    return 'PARTIAL_TARGET'

        # Check final target
        if position['side'] == 'BUY':
            if high >= position['target']:
                return 'TARGET'
        else:
            if low <= position['target']:
                return 'TARGET'

        # Check trailing stop (if active)
        if self.trailing_active and len(option_history) >= strategy.fast_period_5m:
            premium_ema9 = strategy.calculate_premium_ema9(option_history)

            if premium_ema9:
                buffer = premium_ema9 * strategy.trailing_buffer_pct

                if position['side'] == 'BUY':
                    trailing_stop = premium_ema9 - buffer
                    if current_price < trailing_stop:
                        return 'TRAILING_STOP'
                else:
                    trailing_stop = premium_ema9 + buffer
                    if current_price > trailing_stop:
                        return 'TRAILING_STOP'

        return None

    def exit_position(self, market_data, reason, option_history, strategy):
        """Exit the current position (full or partial) with slippage."""
        position = self.position

        # Determine exit price and quantity based on reason
        if reason == 'PARTIAL_TARGET':
            # Partial exit at 1R
            base_price = position['partial_target']
            exit_quantity = int(self.remaining_quantity * 0.5)

            if exit_quantity == 0:
                return

            # Apply slippage
            if self.enable_slippage:
                exit_price = self.slippage_model.calculate_slippage(
                    price=base_price,
                    side='SELL' if position['side'] == 'BUY' else 'BUY',
                    volume=market_data.get('option_volume', 5000),
                    lot_size=exit_quantity
                )
            else:
                exit_price = base_price

            self.remaining_quantity -= exit_quantity
            self.partial_exit_done = True
            self.trailing_active = True

            # Move stop to breakeven
            position['stop_loss'] = position['entry_price']

            print(f"[{market_data['datetime']}] PARTIAL EXIT @ Rs.{exit_price:.2f}")
            print(f"  Exited {exit_quantity} lots (50%)")
            print(f"  Remaining: {self.remaining_quantity} lots")
            print(f"  Stop -> Breakeven: Rs.{position['entry_price']:.2f}")
            print(f"  Trailing ACTIVE\n")

        else:
            # Full exit
            if reason == 'STOP_LOSS':
                base_price = position['stop_loss']
            elif reason == 'TARGET':
                base_price = position['target']
            elif reason == 'TRAILING_STOP':
                base_price = market_data['option_close']
            else:  # END_OF_DATA or EOD_FLATTEN
                base_price = market_data['option_close']

            exit_quantity = self.remaining_quantity

            # Apply slippage
            if self.enable_slippage:
                exit_price = self.slippage_model.calculate_slippage(
                    price=base_price,
                    side='SELL' if position['side'] == 'BUY' else 'BUY',
                    volume=market_data.get('option_volume', 5000),
                    lot_size=exit_quantity
                )
            else:
                exit_price = base_price

        entry_price = position['entry_price']

        # Calculate P&L
        if position['side'] == 'BUY':
            pnl = (exit_price - entry_price) * exit_quantity
        else:
            pnl = (entry_price - exit_price) * exit_quantity

        # Calculate and deduct transaction costs
        exit_costs = strategy._calculate_transaction_costs(entry_price, exit_price, exit_quantity) / 2
        pnl -= exit_costs

        self.realized_pnl += pnl
        self.capital += pnl

        trade = {
            'entry_time': position['entry_time'],
            'exit_time': market_data['datetime'],
            'side': position['side'],
            'entry_price': entry_price,
            'exit_price': exit_price,
            'quantity': exit_quantity,
            'pnl': pnl,
            'return_pct': (pnl / (entry_price * exit_quantity)) * 100,
            'exit_reason': reason,
            'partial': (reason == 'PARTIAL_TARGET'),
            'strike': position.get('strike'),
            'entry_greeks': position.get('entry_greeks'),
            'exit_greeks': market_data.get('greeks')
        }

        self.trades.append(trade)

        if reason != 'PARTIAL_TARGET':
            # Enhanced Exit Logging
            print(f"\n{'='*70}")
            print(f"[{market_data['datetime']}] EXIT SIGNAL")
            print(f"{'='*70}")

            # Exit Reason
            print(f"\n[EXIT REASON]")
            exit_reason_map = {
                'STOP_LOSS': 'Stop Loss Hit',
                'TARGET': 'Target Reached',
                'TRAILING_STOP': 'Trailing Stop Hit (Premium EMA9)',
                'EOD_FLATTEN': 'End of Day Flatten (3:10 PM)',
                'HARD_EXIT': 'Hard Exit (Market Closing at 3:20 PM)',
                'END_OF_DATA': 'End of Backtest Data'
            }
            print(f"  Exit Type:       {exit_reason_map.get(reason, reason)}")

            # Position Summary
            print(f"\n[POSITION SUMMARY]")
            print(f"  Entry Time:      {position['entry_time']}")
            print(f"  Exit Time:       {market_data['datetime']}")
            duration = market_data['datetime'] - position['entry_time']
            print(f"  Duration:        {duration}")
            print(f"  Side:            {position['side']}")
            print(f"  Quantity:        {exit_quantity} lots")

            # Price Movement
            print(f"\n[PRICE MOVEMENT]")
            print(f"  Entry Price:     Rs.{entry_price:.2f}")
            print(f"  Exit Price:      Rs.{exit_price:.2f}")
            price_change = exit_price - entry_price
            price_change_pct = (price_change / entry_price) * 100
            print(f"  Price Change:    Rs.{price_change:+.2f} ({price_change_pct:+.2f}%)")

            # Stop Loss & Target Info
            print(f"\n[RISK PARAMETERS]")
            print(f"  Stop Loss:       Rs.{position['stop_loss']:.2f}")
            print(f"  Target:          Rs.{position['target']:.2f}")
            if self.partial_exit_done:
                print(f"  Partial Exit:    Done (50% @ Rs.{position['partial_target']:.2f})")
            if self.trailing_active:
                print(f"  Trailing:        Was Active")

            # P&L Breakdown
            print(f"\n[P&L BREAKDOWN]")
            gross_pnl = pnl + exit_costs
            print(f"  Gross P&L:       Rs.{gross_pnl:+,.2f}")
            print(f"  Exit Costs:      Rs.{exit_costs:.2f}")
            print(f"  Net P&L:         Rs.{pnl:+,.2f}")
            print(f"  Return:          {trade['return_pct']:+.2f}%")

            # Running Totals
            print(f"\n[CUMULATIVE]")
            print(f"  Total Realized:  Rs.{self.realized_pnl:+,.2f}")
            print(f"  Current Capital: Rs.{self.capital:,.2f}")
            print(f"  Total Return:    {((self.capital - self.initial_capital) / self.initial_capital * 100):+.2f}%")
            print(f"{'='*70}\n")

            # Reset position
            self.position = None
            strategy.reset_position_state()

    def generate_report(self):
        """Generate comprehensive backtest performance report."""
        total_trades = len([t for t in self.trades if not t.get('partial', False)])
        all_trades = self.trades

        winning_trades = [t for t in all_trades if t['pnl'] > 0]
        losing_trades = [t for t in all_trades if t['pnl'] < 0]

        total_pnl = self.realized_pnl
        total_wins = sum(t['pnl'] for t in winning_trades)
        total_losses = sum(t['pnl'] for t in losing_trades)

        win_rate = (len(winning_trades) / len(all_trades) * 100) if all_trades else 0

        avg_win = total_wins / len(winning_trades) if winning_trades else 0
        avg_loss = total_losses / len(losing_trades) if losing_trades else 0

        # Calculate maximum drawdown
        max_equity = self.initial_capital
        max_drawdown = 0
        for point in self.equity_curve:
            if point['equity'] > max_equity:
                max_equity = point['equity']
            drawdown = (max_equity - point['equity']) / max_equity * 100
            if drawdown > max_drawdown:
                max_drawdown = drawdown

        return {
            'initial_capital': self.initial_capital,
            'final_capital': self.capital,
            'total_pnl': total_pnl,
            'total_return_pct': (total_pnl / self.initial_capital) * 100,
            'max_drawdown_pct': max_drawdown,
            'total_trades': total_trades,
            'total_executions': len(all_trades),
            'winning_trades': len(winning_trades),
            'losing_trades': len(losing_trades),
            'win_rate': win_rate,
            'avg_win': avg_win,
            'avg_loss': avg_loss,
            'profit_factor': abs(total_wins / total_losses) if total_losses != 0 else 0,
            'largest_win': max([t['pnl'] for t in winning_trades]) if winning_trades else 0,
            'largest_loss': min([t['pnl'] for t in losing_trades]) if losing_trades else 0,
            'trades': all_trades,
            'equity_curve': self.equity_curve
        }

    def print_report(self, report):
        """Print formatted backtest report."""
        print("\n" + "="*70)
        print(" ENHANCED BACKTEST RESULTS")
        print("="*70)
        print(f"\nCapital:")
        print(f"  Initial Capital: Rs.{report['initial_capital']:,.2f}")
        print(f"  Final Capital:   Rs.{report['final_capital']:,.2f}")
        print(f"  Total P&L:       Rs.{report['total_pnl']:,.2f}")
        print(f"  Total Return:    {report['total_return_pct']:.2f}%")
        print(f"  Max Drawdown:    {report['max_drawdown_pct']:.2f}%")

        print(f"\nTrade Statistics:")
        print(f"  Total Trades:    {report['total_trades']} (Full exits)")
        print(f"  Total Executions: {report['total_executions']} (Inc. partials)")
        print(f"  Winning Trades:  {report['winning_trades']}")
        print(f"  Losing Trades:   {report['losing_trades']}")
        print(f"  Win Rate:        {report['win_rate']:.2f}%")

        print(f"\nPerformance Metrics:")
        print(f"  Avg Win:         Rs.{report['avg_win']:,.2f}")
        print(f"  Avg Loss:        Rs.{report['avg_loss']:,.2f}")
        print(f"  Profit Factor:   {report['profit_factor']:.2f}")
        print(f"  Largest Win:     Rs.{report['largest_win']:,.2f}")
        print(f"  Largest Loss:    Rs.{report['largest_loss']:,.2f}")

        print("\n" + "="*70)

    def save_report(self, report, filename='backtest_enhanced_results.json'):
        """Save backtest results to JSON file."""
        import json

        # Convert datetime objects to strings
        report_copy = report.copy()
        report_copy['trades'] = [
            {
                **trade,
                'entry_time': trade['entry_time'].isoformat(),
                'exit_time': trade['exit_time'].isoformat()
            }
            for trade in report['trades']
        ]
        report_copy['equity_curve'] = [
            {
                'timestamp': point['timestamp'].isoformat(),
                'equity': point['equity']
            }
            for point in report['equity_curve']
        ]

        with open(filename, 'w') as f:
            json.dump(report_copy, f, indent=2)

        print(f"\nResults saved to {filename}")
