"""
Stateful intraday signal detector — full 0DTE pipeline.

Steps 2-10 (per S/R level, per 1-minute bar)
----------------------------------------------
  Step 2   Tiered proximity score: ≤0.25% → 1.00 | ≤0.35% → 0.70 | ≤0.50% → 0.50 | beyond → skip
  Step 3   1-min option volume spike: ratio ≥ 3× N-bar baseline AND ≥ MinSpikeVol
  Step 4   3-min cluster window: rolling 3-bar sum ≥ MinClusterVol
  Step 5   Timing alignment score (ATM+ITM peak same bar → 1.00 | both active → 0.70)
  Step 6   ClusterStrength = 0.45×ATM + 0.35×ITM + 0.20×Timing  [informational only]
  Step 7   Extreme single-strike override: ratio ≥ 6× AND vol ≥ 2×MinCluster → CONCENTRATED
  Step 8   Contract low filter: mark/intraday_low > 2.50 blocks (unless extreme)
  Step 9   Spread filter: (ask-bid)/mid > 0.50 blocks signal
  Step 10  Target room filter: nearest opposing level must be ≥ 0.25% away

Volume logic
------------
  Spike path    — ATM OR ITM 1-min spike alone fires the signal
  Cluster path  — ATM AND ITM both show abnormal 3-min volume together
  No ATM/ITM validation → no alert (hard gate)

Signal semantics
-----------------
  SUPPORT    level → BULLISH  / "Call-side bias"  — confirmed by CALL volume
  RESISTANCE level → BEARISH  / "Put-side bias"   — confirmed by PUT  volume
"""
import logging
from collections import defaultdict, deque
from datetime import date, datetime
from typing import Optional

import config
from data.market_utils import CST

logger = logging.getLogger(__name__)

_FiredKey = tuple[str, str]           # (symbol, signal_type) e.g. ('AAPL', 'BULLISH')
_OptKey   = tuple[str, float, str]   # (symbol, strike, opt_type)


# ── Pure helper functions ─────────────────────────────────────────────────────

def _proximity_score(price: float, level_price: float) -> float:
    if level_price == 0:
        return 0.0
    pct = abs(price - level_price) / level_price
    if pct <= config.PROX_BAND_TIGHT:
        return 1.00
    if pct <= config.PROX_BAND_MID:
        return 0.70
    if pct <= config.PROX_BAND_WIDE:
        return 0.50
    return 0.0


def _timing_score(atm_hist: list[int], itm_hist: list[int]) -> float:
    n = min(3, len(atm_hist), len(itm_hist))
    if n == 0:
        return 0.0
    atm_3 = atm_hist[-n:]
    itm_3 = itm_hist[-n:]
    atm_max = max(atm_3)
    itm_max = max(itm_3)
    if atm_max == 0 or itm_max == 0:
        return 0.0
    return 1.00 if atm_3.index(atm_max) == itm_3.index(itm_max) else 0.70


def _spike_stats(
    history: list[int],
    delta: int,
    min_spike_vol: int,
) -> tuple[float, bool]:
    """Baseline = avg of history[:-1] (prior N bars). history[-1] == delta (current)."""
    prior = history[:-1] if len(history) > 1 else []
    if not prior:
        return 0.0, False
    baseline = sum(prior) / len(prior)
    if baseline < config.OPT_MIN_BASELINE_VOL:
        return 0.0, False
    ratio    = delta / baseline
    is_spike = ratio >= config.OPT_MIN_SPIKE_RATIO and delta >= min_spike_vol
    return round(ratio, 2), is_spike


def _pc_conviction(signal_type: str, pc_ratio: Optional[float]) -> str:
    """
    Compare signal direction against the morning P/C ratio.

    BULLISH signal: WITH_BIAS when call-heavy (P/C < PC_BULL_CUTOFF),
                    AGAINST_BIAS when put-heavy (P/C > PC_BEAR_CUTOFF).
    BEARISH signal: the reverse.
    NEUTRAL when P/C is in the balanced zone or not yet available.
    """
    if pc_ratio is None:
        return 'NEUTRAL'
    if signal_type == 'BULLISH':
        if pc_ratio < config.PC_BULL_CUTOFF:
            return 'WITH_BIAS'
        if pc_ratio > config.PC_BEAR_CUTOFF:
            return 'AGAINST_BIAS'
    else:  # BEARISH
        if pc_ratio > config.PC_BEAR_CUTOFF:
            return 'WITH_BIAS'
        if pc_ratio < config.PC_BULL_CUTOFF:
            return 'AGAINST_BIAS'
    return 'NEUTRAL'


def _target_room(
    signal_type: str,
    spot: float,
    levels: list,
) -> tuple[float, float]:
    """
    Step 10: compute target room score and room % (Step 10 spec).

    BULLISH: TargetRoom = nearest RESISTANCE above spot - spot
    BEARISH: TargetRoom = spot - nearest SUPPORT below spot

    Returns (score, room_pct).
    If no opposing level exists in the direction, returns (1.00, inf) — unlimited room.
    """
    if signal_type == 'BULLISH':
        targets = [
            float(l['strike']) for l in levels
            if l['level_type'] == 'RESISTANCE' and float(l['strike']) > spot
        ]
        if not targets:
            return 1.00, float('inf')
        room = min(targets) - spot
    else:
        targets = [
            float(l['strike']) for l in levels
            if l['level_type'] == 'SUPPORT' and float(l['strike']) < spot
        ]
        if not targets:
            return 1.00, float('inf')
        room = spot - max(targets)

    room_pct = room / spot if spot > 0 else 0.0

    if room_pct >= config.TARGET_ROOM_HIGH:
        score = 1.00
    elif room_pct >= config.TARGET_ROOM_MID:
        score = 0.70
    elif room_pct >= config.TARGET_ROOM_LOW:
        score = 0.40
    else:
        score = 0.00

    return score, round(room_pct, 6)


# ── Detector ──────────────────────────────────────────────────────────────────

class SignalDetector:

    def __init__(self) -> None:
        self._fired_today:  set[_FiredKey]       = set()   # (symbol, signal_type) once per day
        self._prev_opt_vol: dict[_OptKey, int]   = {}
        self._opt_vol_hist: dict[_OptKey, deque] = defaultdict(
            lambda: deque(maxlen=config.OPT_SPIKE_LOOKBACK)
        )
        self._opt_mark_low: dict[_OptKey, float] = {}
        self._history_date: Optional[date]       = None

    # ── Public API ────────────────────────────────────────────────────────────

    def check(
        self,
        symbol: str,
        bars: list[dict],
        levels: list,
        option_quotes: dict | None = None,
        expiry: Optional[date] = None,
        pc_ratio: Optional[float] = None,
    ) -> list[dict]:
        """
        Run the full Steps 2-10 pipeline for the latest 1-min bar.

        Parameters
        ----------
        bars          : 1-min bars sorted oldest-first (from SchwabClient.get_bars)
        levels        : rows from db.get_today_levels()  — all 6 S/R levels
        option_quotes : {(strike, opt_type): contract_dict}
                        from SchwabClient.get_watched_contracts() — 2 nearest strikes
                        per side.  If empty/None no alert fires (hard gate).
        pc_ratio      : morning P/C OI ratio — used for pc_conviction label on signal.
                        None means not yet available (no conviction label applied).
        """
        if not bars:
            return []

        current     = bars[-1]
        close_price = current['close']
        today       = current['bar_time'].date()

        # ── Reset intraday state on a new trading day ─────────────────────────
        if self._history_date != today:
            self._history_date  = today
            self._fired_today   = set()
            self._prev_opt_vol  = {}
            self._opt_vol_hist  = defaultdict(lambda: deque(maxlen=config.OPT_SPIKE_LOOKBACK))
            self._opt_mark_low  = {}

        # ── Hard gate: no option quotes → no alert ────────────────────────────
        if not option_quotes:
            return []

        # ── Step 1: Compute deltas and update rolling histories ───────────────
        opt_data_map: dict[tuple[float, str], dict] = {}
        vol_deltas:   dict[tuple[float, str], int]  = {}

        for (s, ot), data in option_quotes.items():
            opt_key: _OptKey = (symbol, s, ot)
            cur_vol  = int(data.get('volume', 0) or 0)
            prev_vol = self._prev_opt_vol.get(opt_key, cur_vol)
            delta    = max(0, cur_vol - prev_vol)
            self._prev_opt_vol[opt_key] = cur_vol

            self._opt_vol_hist[opt_key].append(delta)

            mark = data.get('mark')
            if mark is not None and mark > 0:
                prev_low = self._opt_mark_low.get(opt_key)
                self._opt_mark_low[opt_key] = mark if prev_low is None else min(prev_low, mark)

            opt_data_map[(s, ot)] = data
            vol_deltas[(s, ot)]   = delta

        new_signals: list[dict] = []

        for level in levels:
            strike     = float(level['strike'])
            level_type = level['level_type']
            confirm_type = 'PUT' if level_type == 'RESISTANCE' else 'CALL'
            signal_type  = 'BEARISH' if level_type == 'RESISTANCE' else 'BULLISH'
            key: _LevelKey = (symbol, level_type, strike)

            # ── Step 2: Proximity score ───────────────────────────────────────
            prox_score = _proximity_score(close_price, strike)
            if prox_score == 0.0:
                continue

            # ── Identify ATM / ITM watched contracts ──────────────────────────
            # ATM = nearest strike to spot; ITM = second nearest (same confirm_type)
            ct_keys = sorted(
                [(s, ot) for (s, ot) in opt_data_map if ot == confirm_type],
                key=lambda k: abs(k[0] - close_price),
            )
            if not ct_keys:
                logger.debug("%s %s@%.4f: no %s quotes", symbol, level_type, strike, confirm_type)
                continue

            atm_key  = ct_keys[0]
            itm_key  = ct_keys[1] if len(ct_keys) > 1 else None

            atm_data  = opt_data_map[atm_key]
            atm_delta = vol_deltas.get(atm_key, 0)
            atm_okey: _OptKey = (symbol, atm_key[0], confirm_type)
            atm_hist  = list(self._opt_vol_hist[atm_okey])

            itm_delta = vol_deltas.get(itm_key, 0) if itm_key else 0
            itm_data  = opt_data_map[itm_key]      if itm_key else {}
            itm_okey: _OptKey = (symbol, itm_key[0], confirm_type) if itm_key else ('', 0.0, '')
            itm_hist  = list(self._opt_vol_hist[itm_okey]) if itm_key else []

            min_spike_vol   = config.OPT_MIN_SPIKE_VOL.get(symbol,   config.OPT_MIN_SPIKE_VOL_DEFAULT)
            min_cluster_vol = config.OPT_MIN_CLUSTER_VOL.get(symbol, config.OPT_MIN_CLUSTER_VOL_DEFAULT)

            # ── Step 3: 1-min spike ───────────────────────────────────────────
            atm_spike_ratio, atm_is_spike = _spike_stats(atm_hist, atm_delta, min_spike_vol)
            itm_spike_ratio, itm_is_spike = _spike_stats(itm_hist, itm_delta, min_spike_vol)

            # ── Step 4: 3-min cluster volumes ─────────────────────────────────
            atm_vol_3m = sum(atm_hist[-3:]) if atm_hist else atm_delta
            itm_vol_3m = sum(itm_hist[-3:]) if itm_hist else itm_delta

            # ── Step 5: Timing ────────────────────────────────────────────────
            timing = _timing_score(atm_hist, itm_hist)

            # ── Step 6: ClusterStrength (informational) ───────────────────────
            atm_component    = min(1.0, atm_vol_3m / max(min_cluster_vol, 1))
            itm_component    = min(1.0, itm_vol_3m / max(min_cluster_vol, 1))
            cluster_strength = (
                0.45 * atm_component
                + 0.35 * itm_component
                + 0.20 * timing
            )

            # ── Step 7: Extreme single-strike override ─────────────────────────
            atm_extreme = (
                atm_spike_ratio >= config.OPT_EXTREME_SPIKE_RATIO
                and atm_delta >= 2 * min_cluster_vol
            )

            # ── Volume gate: spike OR cluster (hard gate) ─────────────────────
            # Spike path  — ATM OR ITM alone shows abnormal 1-min volume
            spike_valid = atm_is_spike or itm_is_spike
            # Cluster path — ATM AND ITM both show abnormal 3-min volume together
            cluster_3m_valid = (
                atm_vol_3m >= min_cluster_vol and itm_vol_3m >= min_cluster_vol
            )
            volume_valid = spike_valid or cluster_3m_valid or atm_extreme

            if not volume_valid:
                logger.debug(
                    "%s %s@%.4f: no volume signal "
                    "(atm_spike=%s itm_spike=%s atm_3m=%d itm_3m=%d min=%d)",
                    symbol, level_type, strike,
                    atm_is_spike, itm_is_spike,
                    atm_vol_3m, itm_vol_3m, min_cluster_vol,
                )
                continue

            # ── Flow shape ────────────────────────────────────────────────────
            if atm_extreme:
                flow_shape = 'CONCENTRATED'
            elif atm_is_spike and itm_is_spike:
                flow_shape = 'SPIKE_BOTH'
            elif cluster_3m_valid:
                flow_shape = 'CLUSTER'
            elif atm_is_spike:
                flow_shape = 'SPIKE_ATM'
            else:
                flow_shape = 'SPIKE_ITM'

            strong_cluster = atm_is_spike and itm_is_spike  # both 1-min spikes

            # ── Step 8: Contract low filter ────────────────────────────────────
            atm_mark     = atm_data.get('mark')
            atm_mark_low = self._opt_mark_low.get(atm_okey)
            low_dist: Optional[float] = None
            if atm_mark and atm_mark_low and atm_mark_low > 0:
                low_dist = atm_mark / atm_mark_low
                if low_dist > config.CONTRACT_LOW_MAX_DIST:
                    if not atm_extreme:
                        logger.debug(
                            "%s %s@%.4f: contract low blocked — mark=%.2f low=%.2f ratio=%.2f",
                            symbol, level_type, strike, atm_mark, atm_mark_low, low_dist,
                        )
                        continue

            # ── Step 9: Spread filter ──────────────────────────────────────────
            atm_bid = atm_data.get('bid')
            atm_ask = atm_data.get('ask')
            spread_pct: Optional[float] = None
            if atm_bid is not None and atm_ask is not None:
                mid = (atm_bid + atm_ask) / 2
                if mid > 0:
                    spread_pct = (atm_ask - atm_bid) / mid
                    if spread_pct > config.MAX_SPREAD_PCT:
                        logger.debug(
                            "%s %s@%.4f: spread blocked — %.0f%%",
                            symbol, level_type, strike, spread_pct * 100,
                        )
                        continue

            # ── Step 10: Target room filter ────────────────────────────────────
            # BULLISH: nearest resistance above spot must be ≥ 0.25% away
            # BEARISH: nearest support below spot must be ≥ 0.25% away
            room_score, room_pct = _target_room(signal_type, close_price, levels)
            if room_score == 0.00:
                logger.debug(
                    "%s %s@%.4f: target room blocked — room=%.3f%% (need ≥%.2f%%)",
                    symbol, level_type, strike,
                    room_pct * 100, config.TARGET_ROOM_LOW * 100,
                )
                continue

            # ── P/C conviction multiplier ─────────────────────────────────────
            # WITH_BIAS   — signal direction agrees with morning P/C reading
            # AGAINST_BIAS — signal direction contradicts morning P/C reading
            # NEUTRAL     — P/C is in the balanced zone (or not yet computed)
            pc_conviction = _pc_conviction(signal_type, pc_ratio)

            # ── All filters passed — attempt to fire ──────────────────────────
            signal = self._maybe_fire(
                symbol=symbol,
                level=level,
                confirm_type=confirm_type,
                signal_type=signal_type,
                current_bar=current,
                expiry=expiry,
                prox_score=prox_score,
                atm_key=atm_key,
                atm_data=atm_data,
                atm_delta=atm_delta,
                atm_spike_ratio=atm_spike_ratio,
                atm_vol_3m=atm_vol_3m,
                itm_delta=itm_delta,
                itm_spike_ratio=itm_spike_ratio,
                itm_vol_3m=itm_vol_3m,
                cluster_strength=cluster_strength,
                strong_cluster=strong_cluster,
                flow_shape=flow_shape,
                spread_pct=spread_pct,
                low_dist=low_dist,
                room_score=room_score,
                room_pct=room_pct,
                pc_ratio=pc_ratio,
                pc_conviction=pc_conviction,
                key=key,
            )
            if signal:
                new_signals.append(signal)

        # If multiple levels fired keep the one with the most target room
        if len(new_signals) > 1:
            new_signals = [max(new_signals, key=lambda s: s['room_score'])]

        return new_signals

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _maybe_fire(
        self,
        symbol: str,
        level,
        confirm_type: str,
        signal_type: str,
        current_bar: dict,
        expiry: Optional[date],
        prox_score: float,
        atm_key: tuple,
        atm_data: dict,
        atm_delta: int,
        atm_spike_ratio: float,
        atm_vol_3m: int,
        itm_delta: int,
        itm_spike_ratio: float,
        itm_vol_3m: int,
        cluster_strength: float,
        strong_cluster: bool,
        flow_shape: str,
        spread_pct: Optional[float],
        low_dist: Optional[float],
        room_score: float,
        room_pct: float,
        pc_ratio: Optional[float],
        pc_conviction: str,
        key: tuple,
    ) -> Optional[dict]:
        now        = datetime.now(CST)
        level_type = level['level_type']
        strike     = float(level['strike'])

        # One alert per direction per symbol per day.
        fired_key: _FiredKey = (symbol, signal_type)
        if fired_key in self._fired_today:
            logger.debug(
                "%s %s@%.4f: already fired %s today — skipping",
                symbol, level_type, strike, signal_type,
            )
            return None

        self._fired_today.add(fired_key)

        bias = 'Call-side bias' if level_type == 'SUPPORT' else 'Put-side bias'

        opt_mark = atm_data.get('mark')
        opt_bid  = atm_data.get('bid')
        opt_ask  = atm_data.get('ask')
        price_to_enter = round(opt_ask, 2) if opt_ask else None
        price_to_exit  = round(opt_ask * 2, 2) if opt_ask else None

        signal = {
            'symbol':           symbol,
            'signal_time':      now,
            'signal_type':      signal_type,
            'bias':             bias,
            'level_type':       level_type,
            'level_price':      strike,
            'expiry':           expiry,
            'trigger_price':    current_bar['close'],
            'option_type':      confirm_type,
            'opt_mark':         opt_mark,
            'opt_bid':          opt_bid,
            'opt_ask':          opt_ask,
            'price_to_enter':   price_to_enter,
            'price_to_exit':    price_to_exit,
            # Cluster analytics
            'prox_score':       prox_score,
            'cluster_strength': round(cluster_strength, 4),
            'strong_cluster':   strong_cluster,
            'flow_shape':       flow_shape,
            'atm_vol_1m':       atm_delta,
            'atm_spike_ratio':  atm_spike_ratio,
            'atm_vol_3m':       atm_vol_3m,
            'itm_vol_1m':       itm_delta,
            'itm_spike_ratio':  itm_spike_ratio,
            'itm_vol_3m':       itm_vol_3m,
            'spread_pct':       round(spread_pct, 4) if spread_pct is not None else None,
            'low_dist':         round(low_dist, 4)   if low_dist  is not None else None,
            'room_score':       room_score,
            'room_pct':         round(room_pct, 6)   if room_pct != float('inf') else None,
            'pc_ratio':         pc_ratio,
            'pc_conviction':    pc_conviction,
            # Legacy columns kept nullable
            'opt_vol_delta':      atm_delta,
            'avg_volume_20':      None,
            'spike_volume':       None,
            'consecutive_spikes': None,
        }

        logger.info(
            "SIGNAL >> %s %s (%s)  %s@%.4f  prox=%.2f  "
            "atm=%s 1m=%d(x%.1f) 3m=%d  itm=1m=%d(x%.1f) 3m=%d  "
            "shape=%s  room=%.3f%%[%.2f]  pc=%.2f[%s]  mark=%s  enter=%s",
            symbol, signal_type, bias, level_type, strike, prox_score,
            confirm_type, atm_delta, atm_spike_ratio, atm_vol_3m,
            itm_delta, itm_spike_ratio, itm_vol_3m,
            flow_shape,
            (room_pct * 100) if room_pct != float('inf') else 999,
            room_score,
            pc_ratio if pc_ratio is not None else 0.0,
            pc_conviction,
            f"${opt_mark:.2f}"       if opt_mark      else 'n/a',
            f"${price_to_enter:.2f}" if price_to_enter else 'n/a',
        )
        return signal
