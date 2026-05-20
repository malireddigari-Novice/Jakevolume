"""
Volume cluster positioning monitor.

Watches option volume near ATM and classifies accumulation into two patterns:

  SAME_DAY_MOVER        — 0DTE option: single 1-min bar >= CLUSTER_VOL_0DTE
                           contracts triggers FORMING.
  NEXT_EXPIRY_POSITIONING — next expiry: rolling CLUSTER_WINDOW-bar total >=
                            CLUSTER_VOL_NEXT triggers FORMING.

State machine per (symbol, strike, option_type, expiry):
  FORMING  -> CONFIRMED after CLUSTER_CONFIRM consecutive above-threshold bars.
  CONFIRMED -> FADED after CLUSTER_FADE consecutive below-threshold bars.

All state transitions are persisted to the volume_clusters Postgres table.
No trading signals are emitted; this is a monitoring/positioning layer only.
"""
import logging
from collections import defaultdict, deque
from datetime import date, datetime
from typing import Optional

import config
import db.ops as db
from data.market_utils import CST

logger = logging.getLogger(__name__)

_ClusterKey = tuple  # (symbol, strike, option_type, expiry)


def _nearest_sr(
    strike: float,
    underlying_price: float,
    levels: list,
) -> tuple[Optional[str], Optional[float], Optional[float]]:
    """Return (level_type, sr_strike, distance_pct) for the closest S/R level."""
    best_level_type: Optional[str]  = None
    best_sr_strike:  Optional[float] = None
    best_dist:       float           = float('inf')

    for lv in levels:
        sr_strike = float(lv['strike'])
        dist = abs(underlying_price - sr_strike) / underlying_price if underlying_price else float('inf')
        if dist < best_dist:
            best_dist        = dist
            best_sr_strike   = sr_strike
            best_level_type  = lv['level_type']

    dist_pct = round(best_dist * 100, 4) if best_dist < float('inf') else None
    return (best_level_type, best_sr_strike, dist_pct)


class PositioningMonitor:

    def __init__(self) -> None:
        # Rolling volume windows for NEXT_EXPIRY_POSITIONING
        self._windows: dict[_ClusterKey, deque] = defaultdict(
            lambda: deque(maxlen=config.CLUSTER_WINDOW)
        )
        # DB row ids for active clusters
        self._cluster_ids: dict[_ClusterKey, int] = {}
        # Current status string per key
        self._cluster_status: dict[_ClusterKey, str] = {}
        # Count of consecutive bars *above* threshold (drives FORMING->CONFIRMED)
        self._above_count: dict[_ClusterKey, int] = defaultdict(int)
        # Count of consecutive bars *below* threshold (drives CONFIRMED->FADED)
        self._below_count: dict[_ClusterKey, int] = defaultdict(int)

    # ── Public API ────────────────────────────────────────────────────────────

    def update(
        self,
        symbol: str,
        opt_quotes: dict,
        expiry_pair: tuple,
        levels: list,
        underlying_price: float,
    ) -> None:
        """
        Process one polling cycle.

        Parameters
        ----------
        opt_quotes      : {(strike, opt_type, expiry): bar_data}  from
                          DatabentoClient.get_atm_option_quotes_all_expiries()
        expiry_pair     : (today_exp_or_None, next_exp_or_None) from
                          DatabentoClient.get_expiry_pair()
        levels          : today's OI levels from db.get_today_levels()
        underlying_price: current equity close price
        """
        today_exp, next_exp = expiry_pair
        now = datetime.now(CST)

        # ── Process each tracked contract ─────────────────────────────────────
        for (strike, opt_type, expiry), bar in opt_quotes.items():
            vol = int(bar.get('volume', 0) or 0)
            key: _ClusterKey = (symbol, strike, opt_type, expiry)

            if expiry == today_exp:
                self._handle_0dte(key, vol, underlying_price, levels, now)
            elif expiry == next_exp:
                self._handle_next(key, vol, underlying_price, levels, now)

        # ── Fade any active cluster whose key is no longer reporting volume ──
        active_keys = set(opt_quotes.keys())
        active_keys_full = {(symbol, s, o, e) for (s, o, e) in active_keys}
        for key in list(self._cluster_ids):
            if key[0] == symbol and key not in active_keys_full:
                self._tick_below(key, now)

    # ── Pattern handlers ──────────────────────────────────────────────────────

    def _handle_0dte(
        self,
        key: _ClusterKey,
        vol: int,
        underlying_price: float,
        levels: list,
        now: datetime,
    ) -> None:
        above = vol >= config.CLUSTER_VOL_0DTE
        self._tick(key, vol, above, 'SAME_DAY_MOVER', underlying_price, levels, now)

    def _handle_next(
        self,
        key: _ClusterKey,
        vol: int,
        underlying_price: float,
        levels: list,
        now: datetime,
    ) -> None:
        win = self._windows[key]
        win.append(vol)
        rolling_total = sum(win)
        above = len(win) == config.CLUSTER_WINDOW and rolling_total >= config.CLUSTER_VOL_NEXT
        self._tick(key, rolling_total, above, 'NEXT_EXPIRY_POSITIONING',
                   underlying_price, levels, now)

    # ── State machine ─────────────────────────────────────────────────────────

    def _tick(
        self,
        key: _ClusterKey,
        cluster_volume: int,
        above: bool,
        pattern_type: str,
        underlying_price: float,
        levels: list,
        now: datetime,
    ) -> None:
        if above:
            self._above_count[key] += 1
            self._below_count[key] = 0
        else:
            self._below_count[key] += 1
            self._above_count[key] = 0

        status = self._cluster_status.get(key)

        if above and status is None:
            # Transition: None -> FORMING
            self._open_cluster(key, cluster_volume, pattern_type, underlying_price, levels, now)

        elif above and status == 'FORMING' and self._above_count[key] >= config.CLUSTER_CONFIRM:
            # Transition: FORMING -> CONFIRMED
            self._update_status(key, 'CONFIRMED', cluster_volume, now)

        elif above and status in ('FORMING', 'CONFIRMED'):
            # Stay in current status; update volume
            db.update_cluster(self._cluster_ids[key], {
                'cluster_volume': cluster_volume,
                'bar_count': self._above_count[key],
                'avg_vol_per_bar': round(cluster_volume / max(1, self._above_count[key]), 2),
                'underlying_price': underlying_price,
                'updated_at': now,
            })

        elif not above and status in ('FORMING', 'CONFIRMED'):
            if self._below_count[key] >= config.CLUSTER_FADE:
                # Transition: active -> FADED
                self._fade_cluster(key, now)

    def _tick_below(self, key: _ClusterKey, now: datetime) -> None:
        """Increment below-count for keys no longer present in the quote feed."""
        self._below_count[key] += 1
        self._above_count[key] = 0
        status = self._cluster_status.get(key)
        if status in ('FORMING', 'CONFIRMED') and self._below_count[key] >= config.CLUSTER_FADE:
            self._fade_cluster(key, now)

    # ── DB helpers ────────────────────────────────────────────────────────────

    def _open_cluster(
        self,
        key: _ClusterKey,
        cluster_volume: int,
        pattern_type: str,
        underlying_price: float,
        levels: list,
        now: datetime,
    ) -> None:
        symbol, strike, opt_type, expiry = key
        sr_level, sr_strike, dist_pct = _nearest_sr(strike, underlying_price, levels)

        cluster = {
            'symbol':                  symbol,
            'detected_at':             now,
            'updated_at':              now,
            'pattern_type':            pattern_type,
            'option_type':             opt_type,
            'strike':                  strike,
            'expiry':                  expiry,
            'underlying_price':        underlying_price,
            'cluster_volume':          cluster_volume,
            'bar_count':               1,
            'avg_vol_per_bar':         float(cluster_volume),
            'status':                  'FORMING',
            'nearest_sr_level':        sr_level,
            'nearest_sr_strike':       sr_strike,
            'distance_from_price_pct': dist_pct,
        }
        row_id = db.insert_cluster(cluster)
        self._cluster_ids[key]    = row_id
        self._cluster_status[key] = 'FORMING'

    def _update_status(
        self, key: _ClusterKey, new_status: str, cluster_volume: int, now: datetime
    ) -> None:
        symbol, strike, opt_type, expiry = key
        db.update_cluster(self._cluster_ids[key], {
            'status':          new_status,
            'cluster_volume':  cluster_volume,
            'bar_count':       self._above_count[key],
            'avg_vol_per_bar': round(cluster_volume / max(1, self._above_count[key]), 2),
            'updated_at':      now,
        })
        self._cluster_status[key] = new_status
        logger.info(
            "Cluster %d -> %s  %s %s %s@%.2f exp=%s",
            self._cluster_ids[key], new_status,
            symbol, opt_type, '', strike, expiry,
        )

    def _fade_cluster(self, key: _ClusterKey, now: datetime) -> None:
        db.fade_cluster(self._cluster_ids[key], now)
        self._cluster_status[key] = 'FADED'
        # Clean up in-memory state for this key
        del self._cluster_ids[key]
        del self._cluster_status[key]
        self._above_count.pop(key, None)
        self._below_count.pop(key, None)
        self._windows.pop(key, None)
