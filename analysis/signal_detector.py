"""
Stateful intraday signal detector.

Trigger logic (per S/R level, per 1-minute bar)
------------------------------------------------
A counter increments when the underlying's close is within LEVEL_PROXIMITY_PCT
of the level AND at least one of:
  - equity spike  : bar volume >= VOLUME_SPIKE_MULTIPLIER × 20-bar avg
  - option cluster: option contracts traded at that strike in the last
                    minute >= OPT_VOL_MIN_CLUSTER

When the counter reaches CONSECUTIVE_SPIKES_REQUIRED the signal fires,
subject to SIGNAL_COOLDOWN_MINUTES cooldown on the same level.

Signal semantics
-----------------
  SUPPORT  level → BULLISH  / "Call-side bias"
  RESISTANCE     → BEARISH  / "Put-side bias"

The fired signal includes the option's live bid/ask/mark so an actionable
price alert can be issued immediately.
"""
import logging
from collections import defaultdict
from datetime import date, datetime
from typing import Optional

import config
from data.market_utils import CST

logger = logging.getLogger(__name__)

_LevelKey = tuple[str, str, float]   # (symbol, level_type, strike)
_OptKey   = tuple[str, float, str]   # (symbol, strike, option_type)


class SignalDetector:

    def __init__(self) -> None:
        self._counters:     dict[_LevelKey, int]      = defaultdict(int)
        self._last_signal:  dict[_LevelKey, datetime] = {}
        self._prev_opt_vol: dict[_OptKey, int]        = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def check(
        self,
        symbol: str,
        bars: list[dict],
        levels: list,
        option_quotes: dict | None = None,
        expiry: Optional[date] = None,
    ) -> list[dict]:
        """
        Evaluate the latest bar and option quotes against all S/R levels.

        Parameters
        ----------
        bars          : 1-min bars sorted oldest-first (from WebullClient.get_bars)
        levels        : rows from db.get_today_levels()
        option_quotes : {(strike, option_type): normalised_contract}
                        from WebullClient.get_option_quotes_for_levels()
        """
        need = config.VOLUME_LOOKBACK_BARS + 1
        if len(bars) < need:
            return []

        current     = bars[-1]
        close_price = current['close']

        lookback = bars[-(config.VOLUME_LOOKBACK_BARS + 1):-1]
        avg_vol  = sum(b['volume'] for b in lookback) / len(lookback)
        eq_spike = avg_vol > 0 and current['volume'] >= config.VOLUME_SPIKE_MULTIPLIER * avg_vol

        new_signals: list[dict] = []

        for level in levels:
            strike      = float(level['strike'])
            level_type  = level['level_type']
            option_type = str(level.get('option_type', ''))
            key: _LevelKey = (symbol, level_type, strike)

            near = self._near_level(close_price, strike)

            # ── Option volume cluster ─────────────────────────────────────────
            opt_data: dict = {}
            opt_vol_delta  = 0

            if near and option_quotes:
                opt_data = option_quotes.get((strike, option_type), {})
                opt_vol  = int(opt_data.get('volume', 0) or 0)
                opt_key: _OptKey = (symbol, strike, option_type)
                # First tick: seed previous volume without triggering a cluster
                prev_vol = self._prev_opt_vol.get(opt_key, opt_vol)
                opt_vol_delta = max(0, opt_vol - prev_vol)
                self._prev_opt_vol[opt_key] = opt_vol

            opt_cluster = opt_vol_delta >= config.OPT_VOL_MIN_CLUSTER

            # ── Counter update ────────────────────────────────────────────────
            if near and (eq_spike or opt_cluster):
                self._counters[key] += 1
            else:
                self._counters[key] = 0

            consecutive = self._counters[key]

            if consecutive >= config.CONSECUTIVE_SPIKES_REQUIRED:
                signal = self._maybe_fire(
                    symbol=symbol,
                    level=level,
                    current_bar=current,
                    avg_vol=avg_vol,
                    consecutive=consecutive,
                    key=key,
                    opt_data=opt_data,
                    opt_vol_delta=opt_vol_delta,
                    expiry=expiry,
                )
                if signal:
                    new_signals.append(signal)
                    self._counters[key] = 0

        return new_signals

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _near_level(price: float, level_price: float) -> bool:
        if level_price == 0:
            return False
        return abs(price - level_price) / level_price <= config.LEVEL_PROXIMITY_PCT

    def _maybe_fire(
        self,
        symbol: str,
        level,
        current_bar: dict,
        avg_vol: float,
        consecutive: int,
        key: _LevelKey,
        opt_data: dict,
        opt_vol_delta: int,
        expiry: Optional[date] = None,
    ) -> Optional[dict]:
        now          = datetime.now(CST)
        level_type   = level['level_type']
        strike       = float(level['strike'])
        option_type  = str(level.get('option_type', ''))
        cooldown_sec = config.SIGNAL_COOLDOWN_MINUTES * 60

        last = self._last_signal.get(key)
        if last and (now - last).total_seconds() < cooldown_sec:
            logger.debug(
                "%s %s@%.4f: cooldown active (%.0fs remaining)",
                symbol, level_type, strike,
                cooldown_sec - (now - last).total_seconds(),
            )
            return None

        self._last_signal[key] = now

        signal_type = 'BULLISH' if level_type == 'SUPPORT' else 'BEARISH'
        bias        = 'Call-side bias' if level_type == 'SUPPORT' else 'Put-side bias'

        opt_mark = opt_data.get('mark')
        opt_bid  = opt_data.get('bid')
        opt_ask  = opt_data.get('ask')

        # Actionable option prices: enter at ask, target 2× for exit
        price_to_enter = round(opt_ask, 2)  if opt_ask else None
        price_to_exit  = round(opt_ask * 2, 2) if opt_ask else None

        signal = {
            'symbol':             symbol,
            'signal_time':        now,
            'signal_type':        signal_type,
            'bias':               bias,
            'level_type':         level_type,
            'level_price':        strike,
            'expiry':             expiry,
            'trigger_price':      current_bar['close'],
            'avg_volume_20':      round(avg_vol, 2),
            'spike_volume':       current_bar['volume'],
            'consecutive_spikes': consecutive,
            'option_type':        option_type,
            'opt_mark':           opt_mark,
            'opt_bid':            opt_bid,
            'opt_ask':            opt_ask,
            'opt_vol_delta':      opt_vol_delta,
            'price_to_enter':     price_to_enter,
            'price_to_exit':      price_to_exit,
        }

        logger.info(
            "SIGNAL >> %s %s (%s)  %s@%.4f  "
            "eq_vol=%s vs avg=%.0f  opt_%s delta=%d  mark=%s  streak=%d",
            symbol, signal_type, bias, level_type, strike,
            f"{current_bar['volume']:,}", avg_vol,
            option_type, opt_vol_delta,
            f"${opt_mark:.2f}" if opt_mark else "n/a",
            consecutive,
        )
        return signal
