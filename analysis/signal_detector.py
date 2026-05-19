"""
Stateful intraday signal detector.

Trigger logic (per S/R level, per 1-minute bar)
------------------------------------------------
Counter increments when ALL of the following hold:
  1. Spot is within LEVEL_PROXIMITY_PCT (0.5%) of the level.
  2. At least one volume condition is met:
       - equity spike     : bar volume >= VOLUME_SPIKE_MULTIPLIER (2.0×) × 20-bar avg
       - primary cluster  : option delta at THIS strike >= OPT_VOL_MIN_CLUSTER contracts/min
       - adjacent cluster : any OTHER S/R level for the same symbol also shows
                            option delta >= OPT_VOL_MIN_CLUSTER (correlated institutional flow)

When the counter reaches CONSECUTIVE_SPIKES_REQUIRED the signal fires,
subject to SIGNAL_COOLDOWN_MINUTES cooldown on the same level.

Signal semantics
-----------------
  SUPPORT  level → BULLISH  / "Call-side bias"
  RESISTANCE     → BEARISH  / "Put-side bias"
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

        # ── Pre-compute opt_vol_delta for ALL levels in one pass ──────────────
        # This lets the adjacent-cluster check see deltas for every level
        # without ordering issues from the per-level loop below.
        vol_deltas: dict[tuple[float, str], int] = {}   # (strike, opt_type) -> delta
        opt_data_map: dict[tuple[float, str], dict] = {}
        if option_quotes:
            for lv in levels:
                s  = float(lv['strike'])
                ot = str(lv.get('option_type', ''))
                data    = option_quotes.get((s, ot), {})
                cur_vol = int(data.get('volume', 0) or 0)
                opt_key: _OptKey = (symbol, s, ot)
                prev_vol = self._prev_opt_vol.get(opt_key, cur_vol)
                delta    = max(0, cur_vol - prev_vol)
                self._prev_opt_vol[opt_key] = cur_vol
                vol_deltas[(s, ot)]   = delta
                opt_data_map[(s, ot)] = data

        new_signals: list[dict] = []

        for level in levels:
            strike      = float(level['strike'])
            level_type  = level['level_type']
            option_type = str(level.get('option_type', ''))
            key: _LevelKey = (symbol, level_type, strike)

            near = self._near_level(close_price, strike)

            opt_vol_delta = vol_deltas.get((strike, option_type), 0)
            opt_data      = opt_data_map.get((strike, option_type), {})

            # Primary cluster: unusual volume on this specific strike
            opt_cluster = opt_vol_delta >= config.OPT_VOL_MIN_CLUSTER

            # Adjacent cluster: any OTHER level for this symbol also surging
            # Signals correlated institutional flow across multiple strikes
            adj_cluster = any(
                delta >= config.OPT_VOL_MIN_CLUSTER
                for (s, ot), delta in vol_deltas.items()
                if not (s == strike and ot == option_type)
            )

            # ── Counter update ────────────────────────────────────────────────
            if near and (eq_spike or opt_cluster or adj_cluster):
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
                    adj_cluster=adj_cluster,
                    expiry=expiry,
                )
                if signal:
                    new_signals.append(signal)
                    self._counters[key] = 0

        return new_signals

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _near_level(price: float, level_price: float) -> bool:
        """Return True when price is within LEVEL_PROXIMITY_PCT of the strike."""
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
        adj_cluster: bool = False,
        expiry: Optional[date] = None,
    ) -> Optional[dict]:
        """
        Build and return a signal dict if the cooldown has expired; None otherwise.

        Also updates _last_signal so subsequent calls in the same cooldown window
        are suppressed without re-reading the database.
        """
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
            'adj_cluster':        adj_cluster,
            'price_to_enter':     price_to_enter,
            'price_to_exit':      price_to_exit,
        }

        adj_tag = "  adj_cluster=YES" if adj_cluster else ""
        logger.info(
            "SIGNAL >> %s %s (%s)  %s@%.4f  "
            "eq_vol=%s vs avg=%.0f  opt_%s delta=%d  mark=%s  streak=%d%s",
            symbol, signal_type, bias, level_type, strike,
            f"{current_bar['volume']:,}", avg_vol,
            option_type, opt_vol_delta,
            f"${opt_mark:.2f}" if opt_mark else "n/a",
            consecutive, adj_tag,
        )
        return signal
