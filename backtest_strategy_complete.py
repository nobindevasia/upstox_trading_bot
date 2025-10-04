#!/usr/bin/env python3
"""
Complete Backtesting Strategy matching strategy.py
Implements full professional Nifty options strategy with:
- Underlying-based signals (Nifty 50)
- Multi-strike ATM selection
- Session window filtering
- Partial exits and trailing stops
- Transaction costs
"""

from datetime import datetime, time as dt_time
from typing import List, Dict, Optional
from zoneinfo import ZoneInfo

# IST timezone
IST = ZoneInfo("Asia/Kolkata")


class CompleteBacktestStrategy:
    """
    Professional Nifty options backtesting strategy.

    Strategy components:
    - Uses underlying (Nifty 50) for all signals
    - 15m EMA20/50 for trend bias
    - 5m EMA9/21 for pullback entries
    - VWAP for session bias
    - RSI for momentum
    - ATM option selection at entry
    - Session windows (9:25-11:30, 13:45-15:15)
    - Partial exits (50% at 1R)
    - Trailing stops (EMA9 of premium)
    - Transaction costs
    """

    def __init__(self):
        # Strategy parameters
        self.fast_period_15m = 20
        self.slow_period_15m = 50
        self.fast_period_5m = 9
        self.slow_period_5m = 21

        self.rsi_period = 14
        self.rsi_bull_threshold = 55
        self.rsi_bear_threshold = 45

        self.pullback_tolerance_points = 5.0
        self.volume_surge_multiplier = 1.2

        # Session windows (IST hours)
        # Market hours: 9:15 AM - 3:30 PM
        # Avoid first 20 min (9:15-9:35) and last 20 min (3:10-3:30)
        # Safe trading windows: 9:35-11:30, 1:45-3:10 PM
        self.market_open = dt_time(9, 15)
        self.market_close = dt_time(15, 30)
        self.buffer_minutes_start = 20  # Wait 20 min after market open
        self.buffer_minutes_end = 20    # Stop 20 min before market close

        self.session_windows = [
            (dt_time(9, 35), dt_time(11, 30)),   # Morning session (after 20 min buffer)
            (dt_time(13, 45), dt_time(15, 10)),  # Afternoon session (20 min before close)
        ]
        self.flatten_time = dt_time(15, 10)  # Flatten 20 min before market close
        self.hard_exit_time = dt_time(15, 20)  # Force exit if still holding

        # Exit parameters
        self.partial_exit_pct = 0.5  # Exit 50% at 1R
        self.trailing_buffer_pct = 0.04  # 4% buffer for trailing

        # Transaction costs (per trade)
        self.brokerage_per_order = 20.0  # Rs. 20 per order
        self.stt_pct = 0.0005  # 0.05% on sell side
        self.exchange_charges_pct = 0.00035  # 0.035%
        self.gst_pct = 0.18  # 18% on brokerage

        # State
        self.last_signal = None
        self.position_quantity = 0
        self.partial_done = False
        self.trailing_active = False

    # ========== Utility Functions ==========

    def _calculate_ema(self, values, period):
        """Calculate EMA series."""
        if len(values) < period:
            return []

        multiplier = 2 / (period + 1)
        ema_values = [None] * (period - 1)

        ema = sum(values[:period]) / period
        ema_values.append(ema)

        for value in values[period:]:
            ema = (value - ema) * multiplier + ema
            ema_values.append(ema)

        return ema_values

    def _calculate_rsi(self, closes, period=14):
        """Calculate RSI."""
        if len(closes) <= period:
            return None

        gains = []
        losses = []

        for i in range(1, period + 1):
            change = closes[i] - closes[i - 1]
            gains.append(max(change, 0))
            losses.append(abs(min(change, 0)))

        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period

        if avg_loss == 0:
            return 100.0

        for i in range(period + 1, len(closes)):
            change = closes[i] - closes[i - 1]
            gain = max(change, 0)
            loss = abs(min(change, 0))
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

        return rsi

    def _calculate_vwap(self, candles):
        """Calculate VWAP from candles."""
        total_pv = 0
        total_volume = 0

        for candle in candles:
            typical_price = (candle[2] + candle[3] + candle[4]) / 3
            volume = candle[5]
            total_pv += typical_price * volume
            total_volume += volume

        if total_volume == 0:
            closes = [c[4] for c in candles]
            return sum(closes) / len(closes)

        return total_pv / total_volume

    def _aggregate_to_15min(self, candles_5m):
        """Aggregate 5-min candles to 15-min."""
        aggregated = []

        for i in range(0, len(candles_5m), 3):
            chunk = candles_5m[i:i+3]
            if len(chunk) < 3:
                continue

            agg_candle = [
                chunk[0][0],  # timestamp
                chunk[0][1],  # open
                max(c[2] for c in chunk),  # high
                min(c[3] for c in chunk),  # low
                chunk[-1][4],  # close
                sum(c[5] for c in chunk),  # volume
                chunk[-1][6] if len(chunk[0]) > 6 else 0  # oi
            ]
            aggregated.append(agg_candle)

        return aggregated

    def _check_volume_surge(self, candles, lookback=10):
        """Check if recent volume is surging."""
        if len(candles) < lookback + 1:
            return True

        recent_vol = candles[-1][5]
        avg_vol = sum(c[5] for c in candles[-lookback-1:-1]) / lookback

        return recent_vol >= avg_vol * self.volume_surge_multiplier

    def _is_within_trading_window(self, timestamp):
        """Check if timestamp is within trading windows."""
        dt = datetime.fromtimestamp(timestamp / 1000, tz=IST)
        current_time = dt.time()

        for start, end in self.session_windows:
            if start <= current_time <= end:
                return True

        return False

    def _should_flatten_eod(self, timestamp):
        """Check if we should flatten position at EOD."""
        dt = datetime.fromtimestamp(timestamp / 1000, tz=IST)
        return dt.time() >= self.flatten_time

    def _calculate_transaction_costs(self, entry_price, exit_price, quantity):
        """Calculate total transaction costs."""
        entry_value = entry_price * quantity
        exit_value = exit_price * quantity

        # Brokerage (both sides)
        total_brokerage = self.brokerage_per_order * 2

        # STT (only on sell side for options)
        stt = exit_value * self.stt_pct

        # Exchange charges (both sides)
        exchange_charges = (entry_value + exit_value) * self.exchange_charges_pct

        # GST on brokerage
        gst = total_brokerage * self.gst_pct

        total_costs = total_brokerage + stt + exchange_charges + gst

        return total_costs

    # ========== Strategy Logic ==========

    def _determine_trend_bias(self, candles_15m_underlying, vwap):
        """Determine market bias from 15m EMAs on underlying."""
        if len(candles_15m_underlying) < self.slow_period_15m:
            return 'NEUTRAL'

        closes = [c[4] for c in candles_15m_underlying]

        ema20 = self._calculate_ema(closes, self.fast_period_15m)
        ema50 = self._calculate_ema(closes, self.slow_period_15m)

        if not ema20 or not ema50:
            return 'NEUTRAL'

        current_ema20 = ema20[-1]
        current_ema50 = ema50[-1]
        prev_ema20 = ema20[-2] if len(ema20) > 1 else current_ema20
        prev_ema50 = ema50[-2] if len(ema50) > 1 else current_ema50
        last_close = closes[-1]

        # Bullish: EMA20 > EMA50, EMA20 rising, price > VWAP
        # Match live strategy: ema20 > ema50 and ema20 >= ema20_prev and last_close >= session_vwap
        if (current_ema20 > current_ema50 and
            current_ema20 >= prev_ema20 and
            last_close >= vwap):
            return 'BULLISH'

        # Bearish: EMA20 < EMA50, EMA20 falling, price < VWAP
        # Match live strategy: ema20 < ema50 and ema20 <= ema50_prev and last_close <= session_vwap
        # CORRECTED: Was checking prev_ema20, should check prev_ema50
        if (current_ema20 < current_ema50 and
            current_ema20 <= prev_ema50 and
            last_close <= vwap):
            return 'BEARISH'

        return 'NEUTRAL'

    def _check_pullback_setup(self, candles_5m_underlying, side):
        """Check for pullback entry setup on 5m underlying."""
        if len(candles_5m_underlying) < self.slow_period_5m:
            return False

        closes = [c[4] for c in candles_5m_underlying]

        ema9 = self._calculate_ema(closes, self.fast_period_5m)
        ema21 = self._calculate_ema(closes, self.slow_period_5m)

        if not ema9 or not ema21:
            return False

        current_ema9 = ema9[-1]
        current_ema21 = ema21[-1]

        last_candle = candles_5m_underlying[-1]
        prev_candle = candles_5m_underlying[-2] if len(candles_5m_underlying) > 1 else last_candle

        last_open = last_candle[1]
        last_high = last_candle[2]
        last_low = last_candle[3]
        last_close = last_candle[4]

        prev_high = prev_candle[2]
        prev_low = prev_candle[3]

        if side == 'BUY':
            # Bullish pullback conditions
            if current_ema9 < current_ema21:
                return False

            if last_close <= last_open:
                return False

            if last_close <= current_ema9:
                return False

            if last_close <= prev_high:
                return False

            # Check pullback touch
            tolerance = self.pullback_tolerance_points
            touched = (last_low <= current_ema9 + tolerance or
                      prev_low <= current_ema9 + tolerance)

            if not touched:
                return False

            # Volume check
            if not self._check_volume_surge(candles_5m_underlying):
                return False

            return True

        else:  # SELL
            # Bearish pullback conditions
            if current_ema9 > current_ema21:
                return False

            if last_close >= last_open:
                return False

            if last_close >= current_ema9:
                return False

            if last_close >= prev_low:
                return False

            # Check pullback touch
            tolerance = self.pullback_tolerance_points
            touched = (last_high >= current_ema9 - tolerance or
                      prev_high >= current_ema9 - tolerance)

            if not touched:
                return False

            # Volume check
            if not self._check_volume_surge(candles_5m_underlying):
                return False

            return True

    def generate_signal(self, current_data, underlying_candles_5m, option_candles_5m):
        """
        Generate trading signal using underlying data.

        Args:
            current_data: Current market data
            underlying_candles_5m: Historical underlying (Nifty 50) 5-min candles
            option_candles_5m: Historical option 5-min candles (for premium EMA)

        Returns:
            tuple: (signal, signal_details) where signal is 'BUY'/'SELL'/'HOLD'
                   and signal_details is dict with strategy parameters
        """
        # Initialize signal details
        self.last_signal_details = {}
        # Check session window
        if not self._is_within_trading_window(current_data['timestamp']):
            return 'HOLD'

        # Need sufficient data
        if len(underlying_candles_5m) < 100:
            return 'HOLD'

        # Create 15m candles from underlying 5m data
        underlying_candles_15m = self._aggregate_to_15min(underlying_candles_5m)

        if len(underlying_candles_15m) < self.slow_period_15m:
            return 'HOLD'

        # Calculate VWAP on underlying
        vwap = self._calculate_vwap(underlying_candles_5m)

        # Determine trend bias from underlying
        bias = self._determine_trend_bias(underlying_candles_15m, vwap)

        if bias == 'NEUTRAL':
            return 'HOLD'

        # Calculate RSI on underlying 5m
        closes_5m = [c[4] for c in underlying_candles_5m]
        rsi = self._calculate_rsi(closes_5m, self.rsi_period)

        if rsi is None:
            return 'HOLD'

        last_underlying_close = underlying_candles_5m[-1][4]

        # Calculate EMAs for logging
        closes_15m = [c[4] for c in underlying_candles_15m]
        ema20 = self._calculate_ema(closes_15m, self.fast_period_15m)
        ema50 = self._calculate_ema(closes_15m, self.slow_period_15m)

        ema9 = self._calculate_ema(closes_5m, self.fast_period_5m)
        ema21 = self._calculate_ema(closes_5m, self.slow_period_5m)

        # Check for BUY signal
        if bias == 'BULLISH':
            if last_underlying_close < vwap:
                return 'HOLD'

            if rsi < self.rsi_bull_threshold:
                return 'HOLD'

            # Check pullback setup on underlying
            if not self._check_pullback_setup(underlying_candles_5m, 'BUY'):
                return 'HOLD'

            # Avoid duplicate signals
            if self.last_signal == 'BUY':
                return 'HOLD'

            # Capture signal details
            self.last_signal_details = {
                'bias': bias,
                'nifty_spot': last_underlying_close,
                'vwap': vwap,
                'rsi': rsi,
                'ema20_15m': ema20[-1] if ema20 else None,
                'ema50_15m': ema50[-1] if ema50 else None,
                'ema9_5m': ema9[-1] if ema9 else None,
                'ema21_5m': ema21[-1] if ema21 else None,
                'last_candle': underlying_candles_5m[-1],
                'prev_candle': underlying_candles_5m[-2] if len(underlying_candles_5m) > 1 else None
            }

            self.last_signal = 'BUY'
            return 'BUY'

        # Check for SELL signal
        if bias == 'BEARISH':
            if last_underlying_close > vwap:
                return 'HOLD'

            if rsi > self.rsi_bear_threshold:
                return 'HOLD'

            # Check pullback setup on underlying
            if not self._check_pullback_setup(underlying_candles_5m, 'SELL'):
                return 'HOLD'

            # Avoid duplicate signals
            if self.last_signal == 'SELL':
                return 'HOLD'

            # Capture signal details
            self.last_signal_details = {
                'bias': bias,
                'nifty_spot': last_underlying_close,
                'vwap': vwap,
                'rsi': rsi,
                'ema20_15m': ema20[-1] if ema20 else None,
                'ema50_15m': ema50[-1] if ema50 else None,
                'ema9_5m': ema9[-1] if ema9 else None,
                'ema21_5m': ema21[-1] if ema21 else None,
                'last_candle': underlying_candles_5m[-1],
                'prev_candle': underlying_candles_5m[-2] if len(underlying_candles_5m) > 1 else None
            }

            self.last_signal = 'SELL'
            return 'SELL'

        return 'HOLD'

    def calculate_premium_ema9(self, option_candles):
        """Calculate EMA9 of option premium for trailing stop."""
        if len(option_candles) < self.fast_period_5m:
            return None

        closes = [c[4] for c in option_candles]
        ema9 = self._calculate_ema(closes, self.fast_period_5m)

        return ema9[-1] if ema9 else None

    def reset_position_state(self):
        """Reset position state after exit."""
        self.position_quantity = 0
        self.partial_done = False
        self.trailing_active = False
