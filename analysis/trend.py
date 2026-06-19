"""
Lightweight per-symbol intraday trend tracker (§8-12) — leadership + move%.

An "active trend" requires BOTH meaningful underlying movement AND supporting option
leadership, so isolated option volume never falsely creates a trend. Used by the
countertrend reversal-conviction gate to decide whether a candidate opposes a strong,
still-working move. All thresholds are config-driven; VWAP is NOT a gate.

  established move  : |spot − session_open| / session_open ≥ ESTABLISHED_MOVE_PCT
  direction         : sign of the move, confirmed by the dominant call/put leadership
  still working     : new session high (bull) / low (bear) within TREND_PROGRESS_LOOKBACK_BARS
  same-side fading  : same-side leadership ≤ LEADERSHIP_FADE_RATIO × its session peak
                      AND no fresh same-side conviction in FRESH_CONVICTION_LOOKBACK_MIN
"""
import config


class IntradayTrend:
    """Per-symbol session trend state, updated once per poll."""

    def __init__(self):
        self._state: dict = {}

    def _s(self, symbol):
        return self._state.setdefault(symbol, dict(
            session_open=None, session_high=None, session_low=None,
            high_bar=0, low_bar=0, bar_count=0, spot=None,
            call_ld=0.0, put_ld=0.0, call_ld_peak=0.0, put_ld_peak=0.0,
            last_call_conv=None, last_put_conv=None))

    def reset(self, symbol=None):
        if symbol is None:
            self._state = {}
        else:
            self._state.pop(symbol, None)

    def update(self, symbol, spot, bar_time, leadership):
        """Fold one poll's spot + leadership scores into the session state."""
        if spot is None:
            return
        s = self._s(symbol)
        if s['session_open'] is None:
            s['session_open'] = s['session_high'] = s['session_low'] = spot
        s['bar_count'] += 1
        if spot > s['session_high']:
            s['session_high'], s['high_bar'] = spot, s['bar_count']
        if spot < s['session_low']:
            s['session_low'], s['low_bar'] = spot, s['bar_count']
        s['spot'] = spot
        call_ld = (leadership or {}).get('call_leadership', 0.0) or 0.0
        put_ld  = (leadership or {}).get('put_leadership', 0.0) or 0.0
        s['call_ld'], s['put_ld'] = call_ld, put_ld
        s['call_ld_peak'] = max(s['call_ld_peak'], call_ld)
        s['put_ld_peak']  = max(s['put_ld_peak'], put_ld)
        if call_ld >= config.CHAIN_LEADERSHIP_MIN:
            s['last_call_conv'] = bar_time
        if put_ld >= config.CHAIN_LEADERSHIP_MIN:
            s['last_put_conv'] = bar_time

    def active_direction(self, symbol):
        """
        'BULLISH'/'BEARISH' when an established move is genuinely leadership-confirmed,
        else None. Requires BOTH meaningful underlying movement (≥ ESTABLISHED_MOVE_PCT)
        AND that the move-side led at some point this session (session leadership peak ≥
        CHAIN_LEADERSHIP_MIN) — so isolated option volume can't fabricate a trend, while
        the trend stays 'active' through a later leadership fade (which `same_side_fading`
        detects separately).
        """
        s = self._state.get(symbol)
        if not s or s['session_open'] is None or s['spot'] is None:
            return None
        move = (s['spot'] - s['session_open']) / s['session_open']
        if abs(move) < config.ESTABLISHED_MOVE_PCT:
            return None
        if move > 0 and s['call_ld_peak'] >= config.CHAIN_LEADERSHIP_MIN:
            return 'BULLISH'
        if move < 0 and s['put_ld_peak'] >= config.CHAIN_LEADERSHIP_MIN:
            return 'BEARISH'
        return None

    def still_working(self, symbol):
        """True if the active trend made a new directional session high/low recently."""
        s = self._state.get(symbol)
        d = self.active_direction(symbol)
        if not s or d is None:
            return False
        n = config.TREND_PROGRESS_LOOKBACK_BARS
        if d == 'BULLISH':
            return (s['bar_count'] - s['high_bar']) <= n
        return (s['bar_count'] - s['low_bar']) <= n

    def same_side_fading(self, symbol, trend_dir, bar_time):
        """True if the trend's own side has faded (leadership down + no fresh conviction)."""
        s = self._state.get(symbol)
        if not s:
            return False
        if trend_dir == 'BULLISH':
            ld, peak, last = s['call_ld'], s['call_ld_peak'], s['last_call_conv']
        else:
            ld, peak, last = s['put_ld'], s['put_ld_peak'], s['last_put_conv']
        faded = peak > 0 and ld <= config.LEADERSHIP_FADE_RATIO * peak
        no_fresh = (last is None
                    or (bar_time - last).total_seconds() / 60.0 >= config.FRESH_CONVICTION_LOOKBACK_MIN)
        return bool(faded and no_fresh)

    def move_pct(self, symbol):
        s = self._state.get(symbol)
        if not s or not s['session_open'] or s['spot'] is None:
            return 0.0
        return round((s['spot'] - s['session_open']) / s['session_open'] * 100, 3)
