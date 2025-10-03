from datetime import datetime, time as dt_time
import math
from zoneinfo import ZoneInfo

from upstox_client import UpstoxClient
from config import (
    TRADING_SYMBOL,
    QUANTITY,
    STOP_LOSS_PERCENTAGE,
    TARGET_PERCENTAGE,
    ACCOUNT_RISK_PER_TRADE,
    MAX_POSITION_SIZE,
    INSTRUMENT_TYPE,
    OPTION_STOP_LOSS_PERCENTAGE,
    OPTION_TARGET_PERCENTAGE,
    # NEW (required): underlying for indicators
    UNDERLYING_SYMBOL,
)


class TradingStrategy:
    """
    NIFTY options intraday strategy:
      - Signals on UNDERLYING (15m EMA20/50 bias + 5m pullback EMA9/21 + VWAP + RSI)
      - Orders on TRADING_SYMBOL (option)
      - Premium-percent SL/TP (toggleable), partial at 1R, BE, trail by premium EMA9
      - Liquidity/spread guard, lot-size enforcement, IST session windows
    """

    # ----- Tunables -----
    use_premium_percent_targets = True  # if True: use OPTION_* % for SL/TP; else 1R structure on premium
    min_premium = 5.0                   # skip trades if option premium < min
    spread_limit = 0.010                # 1% max relative spread to take trade
    trailing_buffer_pct = 0.04          # 4% trailing stop buffer from premium EMA9
    sl_buffer_points = 5.0              # buffer for structure SL (when not using %)
    pullback_tolerance_points = 5.0     # touch tolerance around EMA9 (underlying)
    minimum_candles_required = 25
    partial_scale_fraction = 0.5        # scale-out size at 1R

    def __init__(self):
        # --- API & symbols ---
        self.client = UpstoxClient()
        self.symbol = TRADING_SYMBOL
        self.underlying_symbol = UNDERLYING_SYMBOL
        self.exchange_tz = ZoneInfo("Asia/Kolkata")

        # --- Config ---
        self.default_quantity = QUANTITY
        self.stop_loss_pct = STOP_LOSS_PERCENTAGE
        self.target_pct = TARGET_PERCENTAGE
        self.account_risk_per_trade = max(ACCOUNT_RISK_PER_TRADE, 0.0)
        self.max_position_size = MAX_POSITION_SIZE if MAX_POSITION_SIZE and MAX_POSITION_SIZE > 0 else None

        instr = (INSTRUMENT_TYPE or "").upper()
        # robust option detection
        self.is_option = ("OPT" in instr) or ("OPT" in (self.symbol or "").upper())

        # option-specific
        self.option_stop_loss_pct = OPTION_STOP_LOSS_PERCENTAGE or 30.0
        self.option_target_pct = OPTION_TARGET_PERCENTAGE or 60.0

        # session windows in IST
        self.session_windows = [
            (dt_time(9, 25), dt_time(11, 30)),
            (dt_time(13, 45), dt_time(15, 15)),
        ]
        self.flatten_time = dt_time(15, 20)  # EOD flatten

        # --- Dynamic state ---
        self.active_position = None
        self.entry_price = None
        self.stop_loss_price = None
        self.target_price = None
        self.last_signal_time = None
        self.last_signal_side = None
        self.latest_snapshot = None

        self.position_quantity = 0
        self.remaining_quantity = 0
        self.partial_exit_done = False
        self.partial_target_price = None
        self.initial_risk_per_unit = None
        self.trailing_active = False
        self.realized_pnl = 0.0

    # -------------------- Utilities --------------------

    def _now_ist(self):
        return datetime.now(tz=self.exchange_tz)

    def _parse_timestamp(self, value):
        """
        Parse API timestamp, make it tz-aware IST.
        Accepts ISO8601 (with/without Z) or 'YYYY-mm-dd HH:MM:SS'.
        """
        if not value:
            return None
        if isinstance(value, datetime):
            dt_obj = value
        else:
            cleaned = value.replace('Z', '+00:00') if (isinstance(value, str) and value.endswith('Z')) else value
            dt_obj = None
            # Try ISO
            try:
                dt_obj = datetime.fromisoformat(cleaned)
            except Exception:
                # Try fallbacks
                for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
                    try:
                        dt_obj = datetime.strptime(str(cleaned), fmt)
                        break
                    except Exception:
                        continue
        if not dt_obj:
            return None
        # normalize to IST
        if dt_obj.tzinfo is None:
            # Assume UTC if naive and endswith Z originally, else assume IST
            if isinstance(value, str) and value.endswith('Z'):
                dt_obj = dt_obj.replace(tzinfo=ZoneInfo("UTC")).astimezone(self.exchange_tz)
            else:
                dt_obj = dt_obj.replace(tzinfo=self.exchange_tz)
        else:
            dt_obj = dt_obj.astimezone(self.exchange_tz)
        return dt_obj

    def _fetch_raw_candles_symbol(self, symbol, start_time, end_time, interval='1minute'):
        resp = self.client.get_intraday_candles(
            symbol,
            interval=interval,
            start_time=start_time,
            end_time=end_time,
        )
        data = resp.get('data') if resp else None
        candles = []
        if isinstance(data, dict):
            for entry in data.get('candles', []):
                try:
                    timestamp, o, h, l, c, v = entry[:6]
                except (TypeError, ValueError):
                    continue
                bar_time = self._parse_timestamp(timestamp)
                if not bar_time:
                    continue
                candles.append({
                    'time': bar_time,
                    'open': float(o),
                    'high': float(h),
                    'low': float(l),
                    'close': float(c),
                    'volume': float(v) if v is not None else 0.0,
                })
        candles.sort(key=lambda x: x['time'])
        return candles

    def _get_session_start(self, now_ist):
        return now_ist.replace(hour=9, minute=15, second=0, microsecond=0)

    def _fetch_session_candles_underlying(self):
        now_ist = self._now_ist()
        session_start = self._get_session_start(now_ist)
        if now_ist <= session_start or not self.underlying_symbol:
            return []
        return self._fetch_raw_candles_symbol(self.underlying_symbol, session_start, now_ist, interval='1minute')

    def _fetch_session_candles_premium(self):
        # premium candles for trailing & (optional) structure SL
        now_ist = self._now_ist()
        session_start = self._get_session_start(now_ist)
        if now_ist <= session_start:
            return []
        return self._fetch_raw_candles_symbol(self.symbol, session_start, now_ist, interval='1minute')

    def _aggregate_candles(self, candles, bucket_minutes):
        if bucket_minutes <= 1:
            return list(candles)
        aggregated, bucket = [], None
        for candle in candles:
            t = candle['time']
            bucket_start = t.replace(minute=(t.minute // bucket_minutes) * bucket_minutes, second=0, microsecond=0)
            if bucket is None or bucket_start != bucket['start']:
                if bucket:
                    aggregated.append({
                        'time': bucket['end'],
                        'open': bucket['open'],
                        'high': bucket['high'],
                        'low': bucket['low'],
                        'close': bucket['close'],
                        'volume': bucket['volume'],
                    })
                bucket = {
                    'start': bucket_start,
                    'end': t,
                    'open': candle['open'],
                    'high': candle['high'],
                    'low': candle['low'],
                    'close': candle['close'],
                    'volume': candle['volume'],
                }
            else:
                bucket['end'] = t
                bucket['high'] = max(bucket['high'], candle['high'])
                bucket['low'] = min(bucket['low'], candle['low'])
                bucket['close'] = candle['close']
                bucket['volume'] += candle['volume']
        if bucket:
            aggregated.append({
                'time': bucket['end'],
                'open': bucket['open'],
                'high': bucket['high'],
                'low': bucket['low'],
                'close': bucket['close'],
                'volume': bucket['volume'],
            })
        return aggregated

    def _trim_incomplete_candle(self, candles, bucket_minutes):
        """Remove last candle if it hasn't completed its time bucket yet."""
        if not candles:
            return candles

        from datetime import timedelta

        last_bar = candles[-1]
        now_ist = self._now_ist()

        # Calculate the bucket start time
        bar_time = last_bar['time']
        bucket_start_minute = (bar_time.minute // bucket_minutes) * bucket_minutes
        bucket_start = bar_time.replace(minute=bucket_start_minute, second=0, microsecond=0)

        # Calculate bucket end by adding bucket_minutes
        bucket_end = bucket_start + timedelta(minutes=bucket_minutes)

        # Keep candle only if we're past the bucket completion time
        if now_ist >= bucket_end:
            return candles
        return candles[:-1]

    def _ema_series(self, values, period):
        if len(values) < period:
            return []
        m = 2 / (period + 1)
        ema_vals = [None] * (period - 1)
        ema = sum(values[:period]) / period
        ema_vals.append(ema)
        for x in values[period:]:
            ema = (x - ema) * m + ema
            ema_vals.append(ema)
        return ema_vals

    def _prev_valid(self, series):
        for v in reversed(series[:-1]):
            if v is not None:
                return v
        return None

    def _rsi(self, closes, period=14):
        if len(closes) <= period:
            return None
        gains, losses = [], []
        for i in range(1, period + 1):
            ch = closes[i] - closes[i - 1]
            gains.append(max(ch, 0))
            losses.append(abs(min(ch, 0)))
        avg_g = sum(gains) / period
        avg_l = sum(losses) / period
        if avg_l == 0:
            return 100.0
        for i in range(period + 1, len(closes)):
            ch = closes[i] - closes[i - 1]
            g = max(ch, 0)
            l = abs(min(ch, 0))
            avg_g = (avg_g * (period - 1) + g) / period
            avg_l = (avg_l * (period - 1) + l) / period
        if avg_l == 0:
            return 100.0
        rs = avg_g / avg_l
        return 100 - (100 / (1 + rs))

    def _vwap(self, candles, lookback_bars=None):
        """
        Calculate VWAP. If lookback_bars is specified, use rolling VWAP from last N bars.
        For intraday options, consider using lookback for relevance (e.g., last 60 bars = 1 hour on 1m).
        """
        if lookback_bars and len(candles) > lookback_bars:
            candles = candles[-lookback_bars:]

        tpv = 0.0
        vol = 0.0
        for c in candles:
            v = c.get('volume', 0.0) or 0.0
            tp = (c['high'] + c['low'] + c['close']) / 3.0
            tpv += tp * v
            vol += v
        if vol == 0:
            closes = [c['close'] for c in candles if c.get('close') is not None]
            return sum(closes) / len(closes) if closes else None
        return tpv / vol

    def _atr(self, candles, period=14):
        if len(candles) < period:
            return None
        trs, prev_close = [], candles[0]['close']
        for c in candles[1:]:
            h, l = c['high'], c['low']
            tr = max(h - l, abs(h - prev_close), abs(prev_close - l))
            trs.append(tr)
            prev_close = c['close']
        if len(trs) < period:
            return None
        atr = sum(trs[:period]) / period
        for tr in trs[period:]:
            atr = ((period - 1) * atr + tr) / period
        return atr

    def _is_within_trading_window(self, time_ist):
        return any(start <= time_ist <= end for start, end in self.session_windows)

    def _pullback_tolerance(self, atr_under):
        if atr_under:
            return max(self.pullback_tolerance_points, atr_under * 0.25)
        return self.pullback_tolerance_points

    def _check_volume_surge(self, candles, lookback=10):
        """Check if recent volume is surging (20% above average)."""
        if len(candles) < lookback + 1:
            return True  # Assume OK if insufficient data
        recent_vol = candles[-1]['volume']
        avg_vol = sum(c['volume'] for c in candles[-lookback-1:-1]) / lookback
        return recent_vol >= avg_vol * 1.2  # 20% above average

    def _get_lot_size(self):
        # Try to fetch from API; fall back to 25 for NIFTY (updated lot size)
        try:
            meta = self.client.get_instrument_meta(self.symbol)
            lot = int(meta.get('lot_size') or meta.get('lotSize') or 0)
            return lot if lot > 0 else 25
        except Exception:
            return 25

    def _determine_position_size(self, risk_per_unit):
        lot = self._get_lot_size()
        if risk_per_unit <= 0:
            return 0
        if self.account_risk_per_trade > 0:
            # For options: risk_per_unit is premium difference per contract
            # Total risk per lot = risk_per_unit * lot_size
            risk_per_lot = risk_per_unit * lot
            contracts = int(self.account_risk_per_trade // risk_per_lot)
            contracts = max(contracts, 1)
            if self.max_position_size:
                max_contracts = max(self.max_position_size // lot, 1)
                contracts = min(contracts, max_contracts)
            return contracts * lot
        # default quantity rounded to lot
        return max((self.default_quantity // lot) * lot, lot)

    # -------------------- Context & Signals --------------------

    def _determine_trend_bias(self, ema20_series, ema50_series, last_close, session_vwap):
        ema20 = ema20_series[-1] if ema20_series else None
        ema20_prev = self._prev_valid(ema20_series) if ema20_series else None
        ema50 = ema50_series[-1] if ema50_series else None
        ema50_prev = self._prev_valid(ema50_series) if ema50_series else None
        bias = 'NEUTRAL'
        if None not in (ema20, ema50, ema20_prev, ema50_prev, last_close, session_vwap):
            if ema20 > ema50 and ema20 >= ema20_prev and last_close >= session_vwap:
                bias = 'BULLISH'
            elif ema20 < ema50 and ema20 <= ema50_prev and last_close <= session_vwap:
                bias = 'BEARISH'
        session_side = None
        if last_close is not None and session_vwap is not None:
            session_side = 'ABOVE' if last_close >= session_vwap else 'BELOW'
        return {
            'ema20': ema20, 'ema20_prev': ema20_prev,
            'ema50': ema50, 'ema50_prev': ema50_prev,
            'bias': bias, 'session_side': session_side
        }

    def _check_pullback_setup(self, candles_5m_under, ema9_under, ema21_under, side, atr_under):
        if not candles_5m_under or len(candles_5m_under) < 3:
            return False
        if ema9_under is None or ema21_under is None:
            return False
        tol = self._pullback_tolerance(atr_under)
        last_bar = candles_5m_under[-1]
        prev_bar = candles_5m_under[-2]

        if side == 'BUY':
            if ema9_under < ema21_under:
                return False
            if last_bar['close'] <= last_bar['open']:
                return False
            if last_bar['close'] <= ema9_under:
                return False
            if last_bar['close'] <= prev_bar['high']:
                return False
            touched = (last_bar['low'] <= ema9_under + tol) or (prev_bar['low'] <= ema9_under + tol)
            if not touched:
                return False
            # Volume surge check
            if not self._check_volume_surge(candles_5m_under):
                return False
            return True

        # SELL
        if ema9_under > ema21_under:
            return False
        if last_bar['close'] >= last_bar['open']:
            return False
        if last_bar['close'] >= ema9_under:
            return False
        if last_bar['close'] >= prev_bar['low']:
            return False
        touched = (last_bar['high'] >= ema9_under - tol) or (prev_bar['high'] >= ema9_under - tol)
        if not touched:
            return False
        # Volume surge check
        if not self._check_volume_surge(candles_5m_under):
            return False
        return True

    def _get_market_context(self):
        # Underlying candles for all indicators & entries
        u_1m = self._fetch_session_candles_underlying()
        if len(u_1m) < self.minimum_candles_required:
            return None
        u_vwap = self._vwap(u_1m)

        u_5m = self._trim_incomplete_candle(self._aggregate_candles(u_1m, 5), 5)
        if len(u_5m) < self.minimum_candles_required:
            return None
        u_5m = u_5m[-150:]
        u5_closes = [c['close'] for c in u_5m]
        ema9_u = self._ema_series(u5_closes, 9)
        ema21_u = self._ema_series(u5_closes, 21)
        if not ema9_u or not ema21_u:
            return None
        ema9_under = ema9_u[-1]
        ema21_under = ema21_u[-1]
        rsi_under = self._rsi(u5_closes, 14)
        atr_under = self._atr(u_5m, 14)
        u_last = u_5m[-1]

        u_15m = self._trim_incomplete_candle(self._aggregate_candles(u_1m, 15), 15)
        u15_closes = [c['close'] for c in u_15m] if u_15m else []
        ema20_u = self._ema_series(u15_closes, 20) if u15_closes else []
        ema50_u = self._ema_series(u15_closes, 50) if u15_closes else []
        last_15_close = u15_closes[-1] if u15_closes else None
        trend = self._determine_trend_bias(ema20_u, ema50_u, last_15_close, u_vwap)

        # Premium candles only for trailing / (optional) structure SL
        p_1m = self._fetch_session_candles_premium()
        p_5m = self._trim_incomplete_candle(self._aggregate_candles(p_1m, 5), 5) if p_1m else []
        p5_closes = [c['close'] for c in p_5m] if p_5m else []
        ema9_prem_series = self._ema_series(p5_closes, 9) if p5_closes else []
        ema9_prem = ema9_prem_series[-1] if ema9_prem_series else None
        p_last = p_5m[-1] if p_5m else None

        return {
            'now': self._now_ist(),
            'u_vwap': u_vwap,
            'u_5m': u_5m,
            'u_15m': u_15m,
            'ema9_under': ema9_under,
            'ema21_under': ema21_under,
            'rsi_under': rsi_under,
            'atr_under': atr_under,
            'u_last_close': u_last['close'],
            'u_last_time': u_last['time'],
            'trend': trend,
            'p_5m': p_5m,
            'ema9_prem': ema9_prem,
            'p_last_time': p_last['time'] if p_last else None,
        }

    def _liquidity_guard(self, side):
        try:
            q = self.client.get_quote(self.symbol)
            if not q or 'data' not in q:
                print("Quote missing or malformed — skip trade.")
                return None, None, False, None

            quote_key = self.symbol.replace('|', ':')
            quote_data = q['data'].get(quote_key, {})

            # Try to get bid/ask from depth or ohlc
            depth = quote_data.get('depth', {})
            buy_depth = depth.get('buy', [{}])
            sell_depth = depth.get('sell', [{}])

            bid = float(buy_depth[0].get('price', 0)) if buy_depth else 0
            ask = float(sell_depth[0].get('price', 0)) if sell_depth else 0

            # Fallback to LTP if bid/ask not available
            if bid <= 0 or ask <= 0:
                ltp = float(quote_data.get('last_price', 0))
                if ltp <= 0:
                    print("Cannot get valid price — skip trade.")
                    return None, None, False, None
                bid = ask = ltp
        except Exception as e:
            print(f"Quote error: {e} — skip trade.")
            return None, None, False, None

        if bid <= 0 or ask <= 0 or ask < bid:
            print("Invalid market (bid/ask) — skip trade.")
            return None, None, False, None

        mid = (bid + ask) / 2.0
        spread = (ask - bid) / mid if mid > 0 else 999.0
        if spread > self.spread_limit:
            print(f"Spread too wide ({spread:.2%}) — skip trade.")
            return bid, ask, False, None

        # price we submit: limit-at-market
        px = ask if side == 'BUY' else bid
        return bid, ask, True, px

    def generate_signal(self):
        ctx = self._get_market_context()
        self.latest_snapshot = ctx
        if ctx is None:
            print("Insufficient data for indicators")
            return 'HOLD'

        t_ist = ctx['now'].time()
        if not self._is_within_trading_window(t_ist):
            print("Outside trading window")
            return 'HOLD'

        u_vwap = ctx.get('u_vwap')
        rsi = ctx.get('rsi_under')
        if u_vwap is None or rsi is None:
            print("Missing context data")
            return 'HOLD'

        trend = ctx['trend']
        print(
            "Context | 5m (under) EMA9: {:.2f} EMA21: {:.2f} | "
            "15m EMA20: {} EMA50: {} | VWAP: {:.2f} | RSI: {:.1f}".format(
                ctx['ema9_under'] if ctx['ema9_under'] is not None else float('nan'),
                ctx['ema21_under'] if ctx['ema21_under'] is not None else float('nan'),
                f"{trend.get('ema20'):.2f}" if trend.get('ema20') is not None else "N/A",
                f"{trend.get('ema50'):.2f}" if trend.get('ema50') is not None else "N/A",
                u_vwap if u_vwap is not None else float('nan'),
                rsi if rsi is not None else float('nan')
            )
        )

        if self.active_position:
            return 'HOLD'

        bias = trend.get('bias')
        last_close = ctx['u_last_close']
        bar_time = ctx['u_last_time']

        if bias == 'BULLISH':
            if last_close < u_vwap or rsi < 55:
                return 'HOLD'
            if not self._check_pullback_setup(ctx['u_5m'], ctx['ema9_under'], ctx['ema21_under'], 'BUY', ctx['atr_under']):
                return 'HOLD'
            if self.last_signal_time == bar_time and self.last_signal_side == 'BUY':
                print("Signal already acted on for current bar")
                return 'HOLD'
            self.last_signal_time = bar_time
            self.last_signal_side = 'BUY'
            print("Long pullback continuation setup confirmed")
            return 'BUY'

        if bias == 'BEARISH':
            if last_close > u_vwap or rsi > 45:
                return 'HOLD'
            if not self._check_pullback_setup(ctx['u_5m'], ctx['ema9_under'], ctx['ema21_under'], 'SELL', ctx['atr_under']):
                return 'HOLD'
            if self.last_signal_time == bar_time and self.last_signal_side == 'SELL':
                print("Signal already acted on for current bar")
                return 'HOLD'
            self.last_signal_time = bar_time
            self.last_signal_side = 'SELL'
            print("Short pullback continuation setup confirmed")
            return 'SELL'

        print("Higher timeframe bias neutral")
        return 'HOLD'

    # -------------------- Orders & Risk --------------------

    def _get_current_price(self, symbol=None):
        sym = symbol or self.symbol
        ltp_data = self.client.get_ltp(sym)
        if not ltp_data or 'data' not in ltp_data:
            return None
        response_key = sym.replace('|', ':')
        price_info = ltp_data['data'].get(response_key)
        if not price_info:
            return None
        last_price = price_info.get('last_price')
        return float(last_price) if last_price is not None else None

    def _reset_position_state(self):
        self.active_position = None
        self.entry_price = None
        self.stop_loss_price = None
        self.target_price = None
        self.position_quantity = 0
        self.remaining_quantity = 0
        self.partial_exit_done = False
        self.partial_target_price = None
        self.initial_risk_per_unit = None
        self.trailing_active = False
        self.realized_pnl = 0.0
        self.last_signal_time = None
        self.last_signal_side = None
        self.latest_snapshot = None

    def enter_position(self, side='BUY'):
        ctx = self._get_market_context()
        if ctx is None:
            print("Cannot get indicators for validation. Order not placed.")
            return False
        self.latest_snapshot = ctx

        t_ist = ctx['now'].time()
        if not self._is_within_trading_window(t_ist):
            print("Outside trading window. Order not placed.")
            return False

        u_vwap = ctx.get('u_vwap')
        rsi = ctx.get('rsi_under')
        if u_vwap is None or rsi is None:
            print("Missing context values. Order not placed.")
            return False

        trend = ctx['trend']
        if side == 'BUY':
            if trend.get('bias') != 'BULLISH' or ctx['u_last_close'] < u_vwap or rsi < 55:
                print("BUY conditions no longer valid. Order not placed.")
                return False
            if not self._check_pullback_setup(ctx['u_5m'], ctx['ema9_under'], ctx['ema21_under'], 'BUY', ctx['atr_under']):
                print("BUY pullback setup invalidated. Order not placed.")
                return False
        else:
            if trend.get('bias') != 'BEARISH' or ctx['u_last_close'] > u_vwap or rsi > 45:
                print("SELL conditions no longer valid. Order not placed.")
                return False
            if not self._check_pullback_setup(ctx['u_5m'], ctx['ema9_under'], ctx['ema21_under'], 'SELL', ctx['atr_under']):
                print("SELL pullback setup invalidated. Order not placed.")
                return False

        # Liquidity & spread guard
        liq = self._liquidity_guard(side)
        if not liq or len(liq) < 4:
            return False
        bid, ask, ok, px = liq
        if not ok:
            return False

        current_price = self._get_current_price(self.symbol)
        if current_price is None:
            print("Cannot get current premium. Order not placed.")
            return False
        if current_price < self.min_premium:
            print(f"Premium too low ({current_price:.2f} < {self.min_premium}). Skip.")
            return False

        # Compute SL & TP (premium space)
        if self.is_option and self.use_premium_percent_targets:
            if side == 'BUY':
                stop_loss = current_price * (1 - self.option_stop_loss_pct / 100.0)
                target_price = current_price * (1 + self.option_target_pct / 100.0)
            else:
                stop_loss = current_price * (1 + self.option_stop_loss_pct / 100.0)
                target_price = current_price * (1 - self.option_target_pct / 100.0)
            risk_per_unit = abs(current_price - stop_loss)
        else:
            # Non-option or toggle disabled: use 1R based on structure (premium)
            # Use simple swing-based SL on premium as a fallback
            p_5m = ctx.get('p_5m') or []
            lookback = min(len(p_5m), 5)
            stop_loss = None
            if lookback >= 2:
                if side == 'BUY':
                    swing_low = min(c['low'] for c in p_5m[-lookback:])
                    stop_loss = swing_low - self.sl_buffer_points
                else:
                    swing_high = max(c['high'] for c in p_5m[-lookback:])
                    stop_loss = swing_high + self.sl_buffer_points
            if stop_loss is None:
                # percent fallback
                pct = self.option_stop_loss_pct if self.is_option else self.stop_loss_pct
                stop_loss = current_price * (1 - pct/100.0) if side == 'BUY' else current_price * (1 + pct/100.0)

            risk_per_unit = abs(current_price - stop_loss)
            if side == 'BUY':
                target_price = current_price + risk_per_unit
            else:
                target_price = current_price - risk_per_unit
        if risk_per_unit <= 0:
            print("Invalid stop calculation. Order not placed.")
            return False

        quantity = self._determine_position_size(risk_per_unit)
        if quantity <= 0:
            print("Position size resolved to zero. Order not placed.")
            return False

        print(f"\n[{self._now_ist()}] Entering {side} position")
        print(f"Symbol: {self.symbol}")
        print(f"Qty (rounded to lot): {quantity}")
        print(f"Premium LTP: ₹{current_price:.2f} | Under VWAP: ₹{u_vwap:.2f} | Bias: {trend.get('bias')} | RSI: {rsi:.1f}")
        print(f"Stop: ₹{stop_loss:.2f} | Target: ₹{target_price:.2f}")
        print(f"Order type: LIMIT-at-market @ ₹{px:.2f} | Spread guard OK")

        result = self.client.place_order(
            symbol=self.symbol,
            quantity=quantity,
            side=side,
            order_type='LIMIT',
            price=px,
            product='I',
        )

        if result and 'data' in result:
            self.active_position = side
            self.entry_price = current_price
            self.stop_loss_price = stop_loss
            self.target_price = target_price
            self.position_quantity = quantity
            self.remaining_quantity = quantity
            self.partial_exit_done = False
            self.partial_target_price = target_price
            self.initial_risk_per_unit = abs(current_price - stop_loss)
            self.trailing_active = False
            self.realized_pnl = 0.0

            print("[OK] Order placed successfully!")
            print(f"Entry: ₹{current_price:.2f} | Initial SL: ₹{stop_loss:.2f} | 1R/TP: ₹{target_price:.2f}")
            return True

        print("[FAIL] Failed to place order")
        return False

    def _update_trailing_stop(self, ctx):
        if not self.trailing_active:
            return
        ema9_prem = ctx.get('ema9_prem')

        # Fallback: if premium EMA9 not available, use simple price-based trail
        if ema9_prem is None:
            current_price = self._get_current_price(self.symbol)
            if current_price is None:
                return
            # Use percentage-based trailing as fallback
            if self.active_position == 'BUY':
                proposal = current_price * (1 - self.trailing_buffer_pct)
                if self.stop_loss_price is None or proposal > self.stop_loss_price:
                    self.stop_loss_price = proposal
            elif self.active_position == 'SELL':
                proposal = current_price * (1 + self.trailing_buffer_pct)
                if self.stop_loss_price is None or proposal < self.stop_loss_price:
                    self.stop_loss_price = proposal
            return

        if self.active_position == 'BUY':
            proposal = ema9_prem * (1 - self.trailing_buffer_pct)
            if self.stop_loss_price is None or proposal > self.stop_loss_price:
                self.stop_loss_price = proposal
        elif self.active_position == 'SELL':
            proposal = ema9_prem * (1 + self.trailing_buffer_pct)
            if self.stop_loss_price is None or proposal < self.stop_loss_price:
                self.stop_loss_price = proposal

    def _execute_partial_exit(self, current_price):
        if self.partial_exit_done or self.position_quantity < 2:
            return False

        lot = self._get_lot_size()
        partial_qty = int(math.floor(self.position_quantity * self.partial_scale_fraction))

        # Round to nearest lot size
        partial_qty = (partial_qty // lot) * lot

        if partial_qty <= 0:
            partial_qty = lot  # At least one lot
        if partial_qty >= self.remaining_quantity:
            return False

        exit_side = 'SELL' if self.active_position == 'BUY' else 'BUY'
        result = self.client.place_order(
            symbol=self.symbol,
            quantity=partial_qty,
            side=exit_side,
            order_type='MARKET',
            product='I',
        )
        if result and 'data' in result:
            realized = (current_price - self.entry_price) * partial_qty if self.active_position == 'BUY' else (self.entry_price - current_price) * partial_qty
            self.realized_pnl += realized
            self.remaining_quantity -= partial_qty
            self.partial_exit_done = True
            self.partial_target_price = current_price
            self.stop_loss_price = self.entry_price  # move to BE
            self.trailing_active = True
            print(f"Scaled out {partial_qty} at 1R/TP. Remaining qty: {self.remaining_quantity}")
            return True
        print("Failed to scale out at 1R/TP.")
        return False

    def exit_position(self, reason=None):
        if not self.active_position:
            print("No active position to exit")
            return False
        if self.remaining_quantity <= 0:
            print("Position already flat")
            self._reset_position_state()
            return True

        exit_side = 'SELL' if self.active_position == 'BUY' else 'BUY'
        current_price = self._get_current_price(self.symbol)
        if current_price is None:
            print("Cannot get current price to exit")
            return False

        reason_label = reason or 'manual'
        print(f"\n[{self._now_ist()}] Exiting {self.active_position} position ({reason_label})")

        result = self.client.place_order(
            symbol=self.symbol,
            quantity=self.remaining_quantity,
            side=exit_side,
            order_type='MARKET',
            product='I',
        )

        if result and 'data' in result:
            pnl_remaining = (current_price - self.entry_price) * self.remaining_quantity if self.active_position == 'BUY' else (self.entry_price - current_price) * self.remaining_quantity
            total_pnl = self.realized_pnl + pnl_remaining
            print("[OK] Position closed successfully!")
            print(f"Entry: ₹{self.entry_price:.2f} | Exit: ₹{current_price:.2f}")
            print(f"Total Qty: {self.position_quantity} | Final Qty Closed: {self.remaining_quantity}")
            print(f"P&L: ₹{total_pnl:.2f}")
            self._reset_position_state()
            return True

        print("[FAIL] Failed to exit position")
        return False

    def check_exit_conditions(self):
        if not self.active_position or not self.entry_price or self.remaining_quantity <= 0:
            return None

        ctx = self._get_market_context()
        if ctx is None:
            return None
        self.latest_snapshot = ctx

        t_ist = ctx['now'].time()
        if t_ist >= self.flatten_time:
            print("\nEnd-of-day flatten time reached.")
            return "End of day"

        u_vwap = ctx.get('u_vwap')
        current_price = self._get_current_price(self.symbol)
        if current_price is None or u_vwap is None:
            return None

        # Trailing update (premium EMA9 based)
        self._update_trailing_stop(ctx)

        # Hard SL
        if self.stop_loss_price is not None:
            if self.active_position == 'BUY' and current_price <= self.stop_loss_price:
                print("\nStop loss hit on long position.")
                return "Stop loss"
            if self.active_position == 'SELL' and current_price >= self.stop_loss_price:
                print("\nStop loss hit on short position.")
                return "Stop loss"

        # VWAP recross (underlying) - Only exit before profit target hit
        u_last = ctx['u_last_close']
        if not self.partial_exit_done:  # Only before 1R target
            if self.active_position == 'BUY' and u_last < u_vwap:
                print("\nUnderlying recrossed VWAP before target.")
                return "VWAP recross"
            if self.active_position == 'SELL' and u_last > u_vwap:
                print("\nUnderlying recrossed VWAP before target.")
                return "VWAP recross"

        # Partial at 1R (or percent TP)
        if not self.partial_exit_done and self.initial_risk_per_unit:
            if self.use_premium_percent_targets and self.target_price is not None:
                # percent target mode
                hit = current_price >= self.target_price if self.active_position == 'BUY' else current_price <= self.target_price
                if hit:
                    if self.position_quantity < 2:
                        print("TP reached (single lot). Move SL to BE; enable trailing.")
                        self.stop_loss_price = self.entry_price
                        self.partial_exit_done = True
                        self.partial_target_price = self.target_price
                        self.trailing_active = True
                    elif self._execute_partial_exit(current_price):
                        return None
            else:
                # 1R mode
                if self.active_position == 'BUY':
                    target = self.entry_price + self.initial_risk_per_unit
                    if current_price >= target:
                        if self.position_quantity < 2:
                            print("1R reached (single lot). Move SL to BE; enable trailing.")
                            self.stop_loss_price = self.entry_price
                            self.partial_exit_done = True
                            self.partial_target_price = target
                            self.trailing_active = True
                        elif self._execute_partial_exit(current_price):
                            return None
                else:
                    target = self.entry_price - self.initial_risk_per_unit
                    if current_price <= target:
                        if self.position_quantity < 2:
                            print("1R reached (single lot). Move SL to BE; enable trailing.")
                            self.stop_loss_price = self.entry_price
                            self.partial_exit_done = True
                            self.partial_target_price = target
                            self.trailing_active = True
                        elif self._execute_partial_exit(current_price):
                            return None

        # Final target exit (when using percent targets and partial already done)
        if self.use_premium_percent_targets and self.partial_exit_done and self.target_price is not None:
            # For final exit, use a more aggressive target (e.g., 1.5x or 2x the initial target)
            # Or exit at final target only after partial is done
            if self.active_position == 'BUY' and current_price >= self.target_price * 1.5:
                print(f"\nFinal target reached at {self.target_price * 1.5:.2f}.")
                return "Final target"
            if self.active_position == 'SELL' and current_price <= self.target_price * 1.5:
                print(f"\nFinal target reached at {self.target_price * 1.5:.2f}.")
                return "Final target"

        # 2R profit target (for 1R mode, after partial exit)
        if not self.use_premium_percent_targets and self.partial_exit_done and self.initial_risk_per_unit:
            # Check 2R target on remaining position
            if self.active_position == 'BUY':
                target_2r = self.entry_price + (2 * self.initial_risk_per_unit)
                if current_price >= target_2r:
                    print("\n2R profit target hit.")
                    return "2R target"
            else:
                target_2r = self.entry_price - (2 * self.initial_risk_per_unit)
                if current_price <= target_2r:
                    print("\n2R profit target hit.")
                    return "2R target"

        # Trailing trigger (premium)
        if self.trailing_active and self.stop_loss_price is not None:
            if self.active_position == 'BUY' and current_price <= self.stop_loss_price:
                print("\nTrailing stop triggered (long).")
                return "Trailing stop"
            if self.active_position == 'SELL' and current_price >= self.stop_loss_price:
                print("\nTrailing stop triggered (short).")
                return "Trailing stop"

        return None
