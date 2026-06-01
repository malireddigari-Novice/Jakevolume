"""
Stateful intraday signal detector — full 0DTE pipeline.

Signal semantics
-----------------
  SUPPORT    level → BULLISH / "Call-side bias"  — confirmed by CALL volume
  RESISTANCE level → BEARISH / "Put-side bias"   — confirmed by PUT  volume

Single print vs cluster (per confirm-side contract, per 1-min bar)
------------------------------------------------------------------
  SinglePrintRatio = Current1MinVol / max(AvgPrior10, 10)
  ValidSinglePrint : Current1MinVol >= MinSinglePrintVol  AND
                     SinglePrintRatio >= 8×                AND  low_dist <= 1.75
  ValidCluster (rolling 5-bar window):
                     WindowRatio5 = WindowVol5 / (5 * max(AvgPrior10, 10)) >= 3×  AND
                     ActiveBars5  (per-bar ratio >= 2×) >= 3                       AND  low_dist <= 1.75
  ATM_valid = single OR cluster ; ITM_valid = single OR cluster (ITM = 1 strike in-the-money)

Classification + confidence
---------------------------
  HIGH        / ATM_ITM_CLUSTER       : ATM_valid AND ITM_valid AND cluster valid
  MEDIUM_HIGH / EXTREME_SINGLE_PRINT  : extreme single (near lows) at S2/S3 or R2/R3
  MEDIUM      / VOLUME_PRESSURE_CLUSTER or EXTREME_SINGLE_PRINT : single-side cluster, or
                                        extreme single at rank 1
  WATCH       / RANDOM_SINGLE_PRINT   : notable print but not near lows / no room / no ITM —
                                        recorded (EMIT_WATCH_ONLY) but never auto-traded

Surrounding filters (unchanged): proximity band, spread, target room, P/C conviction.
Contract low: NearLow (<=1.75) qualifies prints; TooChased (>2.50) hard-blocks the ATM.
"""
import logging
from collections import defaultdict, deque
from datetime import date, datetime
from typing import Optional

import config
from data.market_utils import CST

logger = logging.getLogger(__name__)

_FiredKey = tuple[str, str]          # (symbol, signal_type)
_OptKey   = tuple[str, float, str]   # (symbol, strike, opt_type)

# Confidence ordering for the cluster-upgrade path (higher = stronger).
_CONF_RANK = {'WATCH': 0, 'MEDIUM': 1, 'MEDIUM_HIGH': 2, 'HIGH': 3}


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


def _spike_ratio(history: list[int], delta: int) -> float:
    """1-bar ratio: delta / max(rolling_avg, 10). Returns 0.0 with no prior history."""
    prior = history[:-1] if len(history) > 1 else []
    if not prior:
        return 0.0
    baseline = max(sum(prior) / len(prior), 10)   # spec: max(AvgVol, 10)
    return round(delta / baseline, 2)


def _window_ratio(history: list[int]) -> tuple[int, float]:
    """
    3-bar window sum vs prior rolling windows.
    Returns (window_vol, window_spike_ratio).
    Needs at least 4 bars of history; returns (sum, 0.0) otherwise.
    """
    if len(history) < 4:
        return sum(history), 0.0
    window_vol    = sum(history[-3:])
    prior_windows = [sum(history[i:i+3]) for i in range(len(history) - 3)]
    prior_avg     = sum(prior_windows) / len(prior_windows) if prior_windows else 0
    ratio         = window_vol / max(prior_avg, 30)
    return window_vol, round(ratio, 2)


def _avg_prior(history: list[int], exclude_last: int, lookback: int) -> float:
    """
    Average of up to `lookback` bars ending `exclude_last` bars before the end.
    e.g. exclude_last=1 → the bars just before the current one (single print);
         exclude_last=5 → the bars before the trailing 5-bar window (cluster).
    Returns 0.0 when no prior bars are available.
    """
    end = len(history) - exclude_last
    if end <= 0:
        return 0.0
    seg = history[max(0, end - lookback):end]
    return sum(seg) / len(seg) if seg else 0.0


def _single_print(history: list[int], delta: int, min_vol: int) -> tuple[bool, float]:
    """
    Valid single print (ignoring contract-low filter, applied by the caller):
      delta >= min_vol  AND  delta / max(AvgPrior10, 10) >= ratio threshold.
    Returns (valid, ratio).
    """
    base  = max(_avg_prior(history, 1, config.OPT_PRIOR_LOOKBACK), 10.0)
    ratio = round(delta / base, 2)
    valid = delta >= min_vol and ratio >= config.OPT_SINGLE_PRINT_RATIO
    return valid, ratio


def _cluster5(history: list[int]) -> dict:
    """
    Rolling N-bar pressure cluster (N = OPT_CLUSTER_WINDOW, default 5).

    base_unit    = max(AvgPrior10 before the window, 10)
    window_vol   = sum of the last N bar deltas
    window_ratio = window_vol / (N * base_unit)
    active_bars  = bars in window with per-bar ratio >= OPT_CLUSTER_ACTIVE_RATIO
    burst_bars   = bars in window with per-bar ratio >= OPT_CLUSTER_BURST_RATIO
    valid_core   = window_ratio >= threshold AND active_bars >= OPT_CLUSTER_ACTIVE_MIN
                   (contract-low filter applied by the caller)
    """
    n = config.OPT_CLUSTER_WINDOW
    window = history[-n:]
    if len(window) < n:
        return {'vol': sum(window), 'ratio': 0.0, 'active': 0, 'burst': 0, 'valid_core': False}

    base_unit    = max(_avg_prior(history, n, config.OPT_PRIOR_LOOKBACK), 10.0)
    window_vol   = sum(window)
    window_ratio = round(window_vol / (n * base_unit), 2)
    active = sum(1 for b in window if b / base_unit >= config.OPT_CLUSTER_ACTIVE_RATIO)
    burst  = sum(1 for b in window if b / base_unit >= config.OPT_CLUSTER_BURST_RATIO)
    valid_core = (window_ratio >= config.OPT_CLUSTER_WINDOW_RATIO and
                  active >= config.OPT_CLUSTER_ACTIVE_MIN)
    return {'vol': window_vol, 'ratio': window_ratio, 'active': active,
            'burst': burst, 'valid_core': valid_core}


def _pc_conviction(signal_type: str, pc_ratio: Optional[float]) -> str:
    if pc_ratio is None:
        return 'NEUTRAL'
    if signal_type == 'BULLISH':
        if pc_ratio < config.PC_BULL_CUTOFF:
            return 'WITH_BIAS'
        if pc_ratio > config.PC_BEAR_CUTOFF:
            return 'AGAINST_BIAS'
    else:
        if pc_ratio > config.PC_BEAR_CUTOFF:
            return 'WITH_BIAS'
        if pc_ratio < config.PC_BULL_CUTOFF:
            return 'AGAINST_BIAS'
    return 'NEUTRAL'


def _target_room(
    signal_type: str,
    spot: float,
    levels: list,
    position_only: bool = False,
) -> tuple[float, float]:
    """
    Step 10: nearest opposing level distance from spot.
    BULLISH → nearest RESISTANCE above spot.
    BEARISH → nearest SUPPORT below spot.

    position_only=True (next-day mode) treats any level above spot as resistance
    and any below as support, ignoring the frozen morning level_type.
    Returns (score, room_pct). Unlimited room → (1.00, inf).
    """
    above, below = _opposing_strikes(levels, spot, position_only)
    if signal_type == 'BULLISH':
        if not above:
            return 1.00, float('inf')
        room = min(above) - spot
    else:
        if not below:
            return 1.00, float('inf')
        room = spot - max(below)

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


def _spread_pct(data: dict) -> Optional[float]:
    """(ask − bid) / mid for a contract; None when quotes are missing or non-positive."""
    bid = data.get('bid')
    ask = data.get('ask')
    if bid is None or ask is None:
        return None
    mid = (bid + ask) / 2
    if mid <= 0:
        return None
    return (ask - bid) / mid


def _opposing_strikes(levels: list, spot: float, position_only: bool) -> tuple[list, list]:
    """
    Split level strikes into (above_spot, below_spot).

    In 0DTE mode (position_only=False) only the frozen RESISTANCE strikes count
    as "above" and SUPPORT strikes as "below". In next-day mode (position_only=
    True) levels are interchangeable: any strike above spot is resistance, any
    below is support — so selling into S3 makes S2/S1 act as overhead resistance.
    """
    above, below = [], []
    for l in levels:
        s = float(l['strike'])
        if position_only:
            (above if s > spot else below).append(s)
        elif l['level_type'] == 'RESISTANCE' and s > spot:
            above.append(s)
        elif l['level_type'] == 'SUPPORT' and s < spot:
            below.append(s)
    return above, below


def _otm_target_contract(
    signal_type: str,
    confirm_type: str,
    spot: float,
    levels: list,
    chain_quotes: dict,
    depth: int,
) -> Optional[tuple[float, float, dict]]:
    """
    Next-day strike pick: the ATM strike at the target level (OTM vs spot).

    BULLISH → target the depth-th level above spot (e.g. S3 → S2); buy the call
    strike nearest that level. BEARISH → depth-th level below spot (R3 → R2).
    Returns (target_level_price, otm_strike, contract_dict) or None when the
    target level or a matching chain contract is unavailable.
    """
    above, below = _opposing_strikes(levels, spot, position_only=True)
    ladder = sorted(above) if signal_type == 'BULLISH' else sorted(below, reverse=True)
    if not ladder:
        return None
    target_price = ladder[min(depth - 1, len(ladder) - 1)]

    strikes = [s for (s, ot) in chain_quotes if ot == confirm_type]
    if not strikes:
        return None
    otm_strike = min(strikes, key=lambda s: abs(s - target_price))
    return target_price, otm_strike, chain_quotes[(otm_strike, confirm_type)]


# ── Detector ──────────────────────────────────────────────────────────────────

class SignalDetector:

    def __init__(self) -> None:
        # Hold enough bars for a 5-bar window plus its prior-10 baseline.
        self._hist_maxlen = config.OPT_CLUSTER_WINDOW + config.OPT_PRIOR_LOOKBACK
        # One alert per direction per ticker per day: (symbol, signal_type) → best
        # confidence rank fired. Yields at most one CALL and one PUT symbol/ticker.
        self._fired_today:  dict[_FiredKey, int] = {}
        self._prev_opt_vol: dict[_OptKey, int]   = {}
        self._opt_vol_hist: dict[_OptKey, deque] = defaultdict(
            lambda: deque(maxlen=self._hist_maxlen)
        )
        self._opt_mark_low: dict[_OptKey, float] = {}
        self._opt_last_bar: dict[_OptKey, datetime] = {}   # last bar a contract was seen
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
        chain_quotes: dict | None = None,
    ) -> list[dict]:
        """
        Run the full signal detection pipeline for the latest 1-min bar.

        Parameters
        ----------
        bars          : 1-min bars sorted oldest-first
        levels        : rows from db.get_today_levels() — all S/R levels
        option_quotes : {(strike, opt_type): contract_dict} from get_watched_contracts().
                        If empty/None no alert fires (hard gate).
        pc_ratio      : morning P/C OI ratio — used for conviction label.
        chain_quotes  : full {(strike, opt_type): contract_dict} chain. Only needed
                        in next-day mode to price the OTM target strike.
        """
        if not bars:
            return []

        current     = bars[-1]
        close_price = current['close']
        today       = current['bar_time'].date()

        # Next-day mode: no 0DTE today (nearest expiry is a future date) → Tue/Thu.
        next_day_mode = (config.NEXT_DAY_MODE_ENABLED and
                         expiry is not None and expiry > today)

        # Reset intraday state on a new trading day
        if self._history_date != today:
            self._history_date   = today
            self._fired_today    = {}
            self._prev_opt_vol   = {}
            self._opt_vol_hist   = defaultdict(lambda: deque(maxlen=self._hist_maxlen))
            self._opt_mark_low   = {}
            self._opt_last_bar   = {}

        if not option_quotes:
            return []

        # ── Step 1: Compute deltas, update rolling histories ──────────────────
        # Watched strikes rotate as spot moves. If a contract was not seen on the
        # immediately preceding bar, its cumulative volume jumped while unwatched —
        # treat re-entry as a fresh start (delta 0, history cleared) so we don't
        # manufacture a fake 1-min spike from accumulated volume.
        bar_time = current['bar_time']
        gap_limit = config.POLL_INTERVAL_SECONDS * 1.5

        opt_data_map: dict[tuple[float, str], dict] = {}
        vol_deltas:   dict[tuple[float, str], int]  = {}

        for (s, ot), data in option_quotes.items():
            opt_key: _OptKey = (symbol, s, ot)
            cur_vol  = int(data.get('volume', 0) or 0)
            last_bar = self._opt_last_bar.get(opt_key)
            discontinuous = (last_bar is not None and
                             (bar_time - last_bar).total_seconds() > gap_limit)

            if last_bar is None or discontinuous:
                # First sight or re-entry after a gap → no spurious delta.
                delta = 0
                if discontinuous:
                    self._opt_vol_hist[opt_key].clear()
            else:
                prev_vol = self._prev_opt_vol.get(opt_key, cur_vol)
                delta    = max(0, cur_vol - prev_vol)
            self._prev_opt_vol[opt_key] = cur_vol
            self._opt_last_bar[opt_key] = bar_time

            self._opt_vol_hist[opt_key].append(delta)

            mark = data.get('mark')
            if mark is not None and mark > 0:
                prev_low = self._opt_mark_low.get(opt_key)
                self._opt_mark_low[opt_key] = mark if prev_low is None else min(prev_low, mark)

            opt_data_map[(s, ot)] = data
            vol_deltas[(s, ot)]   = delta

        # (symbol, signal_dict, actionable) candidates collected across levels
        candidates: list[tuple[dict, bool]] = []

        for level in levels:
            strike     = float(level['strike'])
            rank       = int(level.get('rank', 1))
            # Next-day mode: levels are interchangeable — role is set by spot
            # position each bar. A deadband around the strike avoids bull/bear
            # whipsaw right at the level (keep the frozen role inside the band).
            # 0DTE mode always keeps the frozen morning level_type.
            if next_day_mode:
                band = strike * config.LEVEL_FLIP_DEADBAND_PCT
                if close_price > strike + band:
                    level_type = 'SUPPORT'        # spot clearly above → level below
                elif close_price < strike - band:
                    level_type = 'RESISTANCE'     # spot clearly below → level overhead
                else:
                    level_type = level['level_type']
            else:
                level_type = level['level_type']
            confirm_type = 'PUT' if level_type == 'RESISTANCE' else 'CALL'
            signal_type  = 'BEARISH' if level_type == 'RESISTANCE' else 'BULLISH'

            # ── Step 2: Proximity score ───────────────────────────────────────
            prox_score = _proximity_score(close_price, strike)
            if prox_score == 0.0:
                pct_away = abs(close_price - strike) / strike * 100
                logger.info(
                    "MONITOR  %s %s@%.2f  spot=%.2f  pct_away=%.2f%%  OUT_OF_RANGE",
                    symbol, level_type, strike, close_price, pct_away,
                )
                continue

            # ── Identify ATM + 1-ITM confirm-side contracts (directional) ─────
            ct_keys = [(s, ot) for (s, ot) in opt_data_map if ot == confirm_type]
            if not ct_keys:
                logger.info(
                    "MONITOR  %s %s@%.2f  spot=%.2f  prox=%.2f  NO_%s_QUOTES",
                    symbol, level_type, strike, close_price, prox_score, confirm_type,
                )
                continue

            atm_key = min(ct_keys, key=lambda k: abs(k[0] - close_price))
            # ITM = one strike in-the-money: CALL → below spot, PUT → above spot
            if confirm_type == 'CALL':
                itm_cands = [k for k in ct_keys if k[0] < close_price and k != atm_key]
                itm_key   = max(itm_cands, key=lambda k: k[0]) if itm_cands else None
            else:
                itm_cands = [k for k in ct_keys if k[0] > close_price and k != atm_key]
                itm_key   = min(itm_cands, key=lambda k: k[0]) if itm_cands else None

            atm_data  = opt_data_map[atm_key]
            atm_delta = vol_deltas.get(atm_key, 0)
            atm_okey: _OptKey = (symbol, atm_key[0], confirm_type)
            atm_hist  = list(self._opt_vol_hist[atm_okey])

            itm_delta = vol_deltas.get(itm_key, 0) if itm_key else 0
            itm_data  = opt_data_map[itm_key]      if itm_key else {}
            itm_okey: _OptKey = (symbol, itm_key[0], confirm_type) if itm_key else ('', 0.0, '')
            itm_hist  = list(self._opt_vol_hist[itm_okey]) if itm_key else []

            min_single_vol = config.OPT_MIN_SINGLE_PRINT_VOL.get(
                symbol, config.OPT_MIN_SINGLE_PRINT_VOL['default'])

            # ── Single print + 5-bar pressure cluster, per contract ───────────
            atm_single_raw, atm_spike_ratio = _single_print(atm_hist, atm_delta, min_single_vol)
            atm_clu = _cluster5(atm_hist)
            atm_window_vol, atm_window_ratio = atm_clu['vol'], atm_clu['ratio']

            if itm_key:
                itm_single_raw, itm_spike_ratio = _single_print(itm_hist, itm_delta, min_single_vol)
                itm_clu = _cluster5(itm_hist)
            else:
                itm_single_raw, itm_spike_ratio = False, 0.0
                itm_clu = {'vol': 0, 'ratio': 0.0, 'active': 0, 'burst': 0, 'valid_core': False}
            itm_window_vol, itm_window_ratio = itm_clu['vol'], itm_clu['ratio']

            # Per-contract distance above session low
            atm_low_dist = self._contract_low_dist(atm_okey, atm_data)
            itm_low_dist = self._contract_low_dist(itm_okey, itm_data) if itm_key else None
            atm_near_low = (atm_low_dist is None or atm_low_dist <= config.NEAR_LOW_MAX_DIST)
            itm_near_low = (itm_low_dist is not None and itm_low_dist <= config.NEAR_LOW_MAX_DIST)
            atm_chased   = (atm_low_dist is not None and atm_low_dist > config.CONTRACT_LOW_MAX_DIST)
            low_dist     = atm_low_dist
            near_low     = atm_near_low

            # Near-low-qualified validity (single OR cluster, per spec)
            atm_single  = atm_single_raw and atm_near_low
            atm_cluster = atm_clu['valid_core'] and atm_near_low
            itm_single  = itm_single_raw and itm_near_low
            itm_cluster = itm_clu['valid_core'] and itm_near_low

            atm_valid       = atm_single or atm_cluster
            itm_valid       = itm_single or itm_cluster
            atm_itm_confirm = atm_valid and itm_valid
            cluster_valid   = atm_cluster or itm_cluster
            extreme_single  = atm_single or itm_single          # near-low extreme print
            extreme_raw     = atm_single_raw or itm_single_raw   # ignores near-low
            cluster_core    = atm_clu['valid_core'] or itm_clu['valid_core']

            # Informational ClusterStrength (kept for storage/logging; no longer a gate)
            timing   = _timing_score(atm_hist, itm_hist)
            atm_norm = min(1.0, atm_window_ratio / max(config.OPT_CLUSTER_WINDOW_RATIO, 1))
            itm_norm = min(1.0, itm_window_ratio / max(config.OPT_CLUSTER_WINDOW_RATIO, 1))
            cluster_strength = round(0.45 * atm_norm + 0.35 * itm_norm + 0.20 * timing, 4)

            # ── MONITOR log (every bar, every in-range level) ─────────────────
            logger.info(
                "MONITOR  %s %s@%.2f  rank=%d  spot=%.2f  prox=%.2f  "
                "%s:1m=%d(x%.1f) win=%d(x%.1f,act=%d) itm:1m=%d(x%.1f) win=%d(x%.1f,act=%d)  "
                "low=%s  atm_itm=%s clust=%s extreme=%s",
                symbol, level_type, strike, rank, close_price, prox_score, confirm_type,
                atm_delta, atm_spike_ratio, atm_window_vol, atm_window_ratio, atm_clu['active'],
                itm_delta, itm_spike_ratio, itm_window_vol, itm_window_ratio, itm_clu['active'],
                f"{low_dist:.2f}" if low_dist is not None else "n/a",
                atm_itm_confirm, cluster_valid, extreme_single,
            )

            # Nothing notable at all → skip; chased ATM contract → hard block
            if not (extreme_raw or cluster_core):
                continue
            if atm_chased:
                logger.debug("%s %s@%.2f: TooChased — low_dist=%.2f",
                             symbol, level_type, strike, low_dist)
                continue

            # ── Next-day OTM strike: buy the ATM contract at the target level ─
            # (detection volume stays on the spot-side contracts above).
            day_mode      = 'NEXT_DAY' if next_day_mode else '0DTE'
            trade_data    = atm_data
            traded_strike = strike
            target_level: Optional[float] = None
            if next_day_mode and chain_quotes:
                otm = _otm_target_contract(signal_type, confirm_type, close_price,
                                           levels, chain_quotes, config.NEXT_DAY_TARGET_DEPTH)
                if otm:
                    target_level, traded_strike, trade_data = otm

            # ── Spread + target room — downgrade to WATCH, never silently drop ─
            # Spread/room follow the contract we'd actually trade (OTM in next-day mode).
            spread_pct = _spread_pct(trade_data)
            spread_ok  = spread_pct is None or spread_pct <= config.MAX_SPREAD_PCT
            room_score, room_pct = _target_room(signal_type, close_price, levels,
                                                position_only=next_day_mode)
            room_ok    = room_score > 0.0
            gates_ok   = spread_ok and room_ok

            # ── Classification + confidence tier ──────────────────────────────
            confidence, signal_shape, actionable = 'WATCH', 'RANDOM_SINGLE_PRINT', False
            if atm_itm_confirm and cluster_valid and gates_ok:
                confidence, signal_shape, actionable = 'HIGH', 'ATM_ITM_CLUSTER', True
            elif extreme_single and rank in config.SINGLE_PRINT_RANKS and gates_ok:
                confidence, signal_shape, actionable = 'MEDIUM_HIGH', 'EXTREME_SINGLE_PRINT', True
            elif cluster_valid and gates_ok:
                confidence, signal_shape, actionable = 'MEDIUM', 'VOLUME_PRESSURE_CLUSTER', True
            elif extreme_single and gates_ok:
                confidence, signal_shape, actionable = 'MEDIUM', 'EXTREME_SINGLE_PRINT', True

            if not actionable and not config.EMIT_WATCH_ONLY:
                continue

            pc_conviction = _pc_conviction(signal_type, pc_ratio)

            signal = self._build_signal(
                symbol=symbol, level=level, levels=levels, rank=rank,
                level_type=level_type, confirm_type=confirm_type, signal_type=signal_type,
                current_bar=current, expiry=expiry, prox_score=prox_score,
                atm_data=atm_data, atm_delta=atm_delta, atm_spike_ratio=atm_spike_ratio,
                atm_window_vol=atm_window_vol, atm_window_ratio=atm_window_ratio,
                itm_delta=itm_delta, itm_spike_ratio=itm_spike_ratio,
                itm_window_vol=itm_window_vol, itm_window_ratio=itm_window_ratio,
                cluster_active=atm_clu['active'], cluster_burst=atm_clu['burst'],
                atm_itm_confirm=atm_itm_confirm, cluster_strength=cluster_strength,
                confidence=confidence, signal_shape=signal_shape,
                spread_pct=spread_pct, low_dist=low_dist,
                room_score=room_score, room_pct=room_pct,
                pc_ratio=pc_ratio, pc_conviction=pc_conviction,
                next_day_mode=next_day_mode, day_mode=day_mode,
                trade_data=trade_data, traded_strike=traded_strike, target_level=target_level,
            )
            candidates.append((signal, actionable))

        # ── Select one alert this bar — at most one CALL and one PUT per ticker ──
        # Dedup is per DIRECTION per day (not per level), so the best bullish setup
        # yields a single call symbol and the best bearish setup a single put
        # symbol. Across the day's levels we keep the strongest, not one each.
        eligible: list[tuple[dict, bool, bool]] = []   # (sig, actionable, is_upgrade)
        for sig, actionable in candidates:
            decision, is_upgrade = self._fire_decision(sig)
            if decision != 'skip':
                eligible.append((sig, actionable, is_upgrade))
        if not eligible:
            return []

        # Prefer actionable, then confidence, then room.
        act = [e for e in eligible if e[1]]
        pool = act if act else eligible
        sig, _, is_upgrade = max(
            pool, key=lambda e: (_CONF_RANK[e[0]['confidence']], e[0]['room_score'])
        )

        key: _FiredKey = (symbol, sig['signal_type'])
        prev_rank = self._fired_today.get(key, -1)
        self._fired_today[key] = max(_CONF_RANK[sig['confidence']], prev_rank)
        sig['upgrade'] = is_upgrade
        if is_upgrade:
            logger.info("UPGRADE  %s %s  → %s", symbol, sig['signal_type'], sig['confidence'])
        return [sig]

    def _fire_decision(self, sig: dict) -> tuple[str, bool]:
        """
        One alert per direction per day. Returns (action, is_upgrade) where action
        is 'fire' | 'upgrade' | 'skip'.
          - never fired this direction         → fire
          - actionable after a prior WATCH     → fire (the real call/put)
          - stronger actionable, upgrades on   → upgrade (alert only; not re-traded)
          - otherwise (already fired)          → skip
        Result: at most one CALL and one PUT symbol per ticker per day (plus an
        optional upgrade follow-up when EMIT_UPGRADE_ALERT is enabled).
        """
        key  = (sig['symbol'], sig['signal_type'])
        rank = _CONF_RANK[sig['confidence']]
        prev = self._fired_today.get(key)
        if prev is None:
            return 'fire', False
        if rank > prev:
            if prev < _CONF_RANK['MEDIUM']:
                return 'fire', False          # prior was WATCH → this is the real entry
            if config.EMIT_UPGRADE_ALERT and config.CLUSTER_UPGRADE_ENABLED:
                return 'upgrade', True         # optional stronger-signal follow-up
        return 'skip', False

    # ── Opposite-side volume validation (for exit state machine) ─────────────

    def check_opposite_side(
        self,
        symbol: str,
        signal_type: str,
        option_quotes: dict,
        target_price: float,
    ) -> bool:
        """
        Returns True if opposite-side volume shows TrueCluster-level activity
        near target_price.  Used by check_exits() for early exit2 after exit1 fills.

        BULLISH trade → watches PUT volume (put cluster at R1 = reversal signal)
        BEARISH trade → watches CALL volume (call cluster at S1 = reversal signal)

        Requires both ATM and ITM opposite contracts active (mirrors TrueCluster gate).
        History is already current because check() runs before check_exits() each bar.
        """
        opp_type      = 'PUT' if signal_type == 'BULLISH' else 'CALL'
        min_spike_vol = config.OPT_MIN_SPIKE_VOL.get(symbol, config.OPT_MIN_SPIKE_VOL['default'])
        min_clust_vol = config.OPT_MIN_CLUSTER_VOL.get(symbol, config.OPT_MIN_CLUSTER_VOL['default'])

        opp_keys = sorted(
            [(s, ot) for (s, ot) in option_quotes if ot == opp_type],
            key=lambda k: abs(k[0] - target_price),
        )
        if not opp_keys:
            return False

        def _side_active(key: tuple) -> bool:
            okey: _OptKey = (symbol, key[0], opp_type)
            hist = list(self._opt_vol_hist[okey])
            if not hist:
                return False
            delta              = hist[-1]   # current bar delta, pushed by check()
            ratio              = _spike_ratio(hist, delta)
            win_vol, win_ratio = _window_ratio(hist)
            burst   = delta >= min_spike_vol and ratio     >= config.OPT_SINGLE_SPIKE_RATIO
            cluster = win_ratio >= config.OPT_CONSEC_SPIKE_RATIO and win_vol >= min_clust_vol
            return burst or cluster

        atm_active = _side_active(opp_keys[0])
        itm_active = _side_active(opp_keys[1]) if len(opp_keys) > 1 else False
        result     = atm_active and itm_active
        if result:
            logger.info(
                "OppSide ACTIVE  %s %s  opp=%s  near=%.2f",
                symbol, signal_type, opp_type, target_price,
            )
        return result

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _contract_low_dist(self, okey: _OptKey, data: dict) -> Optional[float]:
        """
        Ratio of current mark to the contract's true session low; None if unknown.

        Uses the lower of Schwab's reported day_low and our watched-session min, so
        a low that printed before we started watching still counts (a higher, stale
        watched-min would otherwise understate how chased the contract is).
        """
        mark = data.get('mark')
        candidates = [x for x in (self._opt_mark_low.get(okey), data.get('day_low'))
                      if x and x > 0]
        low = min(candidates) if candidates else None
        if mark and low and low > 0:
            return round(mark / low, 4)
        return None

    def _build_signal(
        self,
        symbol: str,
        level,
        levels: list,
        rank: int,
        level_type: str,
        confirm_type: str,
        signal_type: str,
        current_bar: dict,
        expiry: Optional[date],
        prox_score: float,
        atm_data: dict,
        atm_delta: int,
        atm_spike_ratio: float,
        atm_window_vol: int,
        atm_window_ratio: float,
        itm_delta: int,
        itm_spike_ratio: float,
        itm_window_vol: int,
        itm_window_ratio: float,
        cluster_active: int,
        cluster_burst: int,
        atm_itm_confirm: bool,
        cluster_strength: float,
        confidence: str,
        signal_shape: str,
        spread_pct: Optional[float],
        low_dist: Optional[float],
        room_score: float,
        room_pct: float,
        pc_ratio: Optional[float],
        pc_conviction: str,
        next_day_mode: bool,
        day_mode: str,
        trade_data: dict,
        traded_strike: float,
        target_level: Optional[float],
    ) -> dict:
        """
        Build the signal dict for a classified candidate. No state mutation —
        dedup and one-per-direction selection happen in check().

        `level_type` is the effective role (spot-based in next-day mode). The
        traded contract (`trade_data`/`traded_strike`) is the OTM target strike
        in next-day mode, else the spot-side ATM contract.
        """
        now        = datetime.now(CST)
        strike     = float(level['strike'])
        spot       = current_bar['close']

        # Exit targets from opposing levels — position-based in next-day mode.
        above, below = _opposing_strikes(levels, spot, position_only=next_day_mode)
        if signal_type == 'BULLISH':
            _exits = sorted(above)
        else:
            _exits = sorted(below, reverse=True)
        exit1_price = _exits[0] if _exits else None
        exit2_price = _exits[1] if len(_exits) > 1 else None

        bias = 'Call-side bias' if level_type == 'SUPPORT' else 'Put-side bias'

        opt_mark = trade_data.get('mark')
        opt_bid  = trade_data.get('bid')
        opt_ask  = trade_data.get('ask')
        price_to_enter = round(opt_ask, 2) if opt_ask else None
        price_to_exit  = round(opt_ask * 2, 2) if opt_ask else None

        # Option H/L flag (on the traded contract)
        option_hl_flag: Optional[str] = None
        day_high = trade_data.get('day_high')
        day_low  = trade_data.get('day_low')
        mark     = opt_mark or 0
        if mark > 0:
            if day_high and day_high > 0:
                if mark >= day_high * 0.97:
                    option_hl_flag = 'AT_HIGH'
                elif mark >= day_high * 0.90:
                    option_hl_flag = 'NEAR_HIGH'
            if option_hl_flag is None and day_low and day_low > 0:
                if mark <= day_low * 1.03:
                    option_hl_flag = 'AT_LOW'
                elif mark <= day_low * 1.10:
                    option_hl_flag = 'NEAR_LOW'

        signal = {
            'symbol':           symbol,
            'signal_time':      now,
            'signal_type':      signal_type,
            'bias':             bias,
            'level_type':       level_type,
            'level_price':      strike,
            'expiry':           expiry,
            'trigger_price':    spot,
            'option_type':      confirm_type,
            'day_mode':         day_mode,        # '0DTE' | 'NEXT_DAY'
            'traded_strike':    traded_strike,   # OTM target strike in next-day mode
            'target_level':     target_level,
            'opt_mark':         opt_mark,
            'opt_bid':          opt_bid,
            'opt_ask':          opt_ask,
            'price_to_enter':   price_to_enter,
            'price_to_exit':    price_to_exit,
            'prox_score':       prox_score,
            'cluster_strength': cluster_strength,
            'strong_cluster':   atm_itm_confirm,
            'flow_shape':       signal_shape,
            'signal_shape':     signal_shape,
            'confidence':       confidence,
            'upgrade':          False,   # set True in check() on a cluster upgrade
            'cluster_active_bars': cluster_active,
            'cluster_burst_bars':  cluster_burst,
            'atm_vol_1m':       atm_delta,
            'atm_spike_ratio':  atm_spike_ratio,
            'atm_vol_3m':       atm_window_vol,
            'itm_vol_1m':       itm_delta,
            'itm_spike_ratio':  itm_spike_ratio,
            'itm_vol_3m':       itm_window_vol,
            'spread_pct':       round(spread_pct, 4) if spread_pct is not None else None,
            'low_dist':         low_dist,
            'room_score':       room_score,
            'room_pct':         round(room_pct, 6)   if room_pct != float('inf') else None,
            'pc_ratio':         pc_ratio,
            'pc_conviction':    pc_conviction,
            'option_hl_flag':   option_hl_flag,
            'exit1_price':        exit1_price,
            'exit2_price':        exit2_price,
            # Legacy nullable columns expected by db.save_signal
            'opt_vol_delta':      atm_delta,
            'avg_volume_20':      None,
            'spike_volume':       None,
            'consecutive_spikes': None,
        }

        tag = 'SIGNAL' if confidence != 'WATCH' else 'WATCH '
        strike_note = (f"  [{day_mode} strike={traded_strike:.2f}→tgt {target_level:.2f}]"
                       if next_day_mode and target_level is not None else "")
        logger.info(
            "%s >> %s %s (%s)  %s@%.2f  rank=%d  prox=%.2f  conf=%s  shape=%s%s  "
            "%s:1m=%d(x%.1f) win=%d(x%.1f)  itm:1m=%d(x%.1f) win=%d(x%.1f)  "
            "CS=%.2f  low=%.2f  room=%.3f%%  pc=%.2f[%s]  mark=%s  enter=%s",
            tag, symbol, signal_type, bias, level_type, strike, rank, prox_score,
            confidence, signal_shape, strike_note,
            confirm_type,
            atm_delta, atm_spike_ratio, atm_window_vol, atm_window_ratio,
            itm_delta, itm_spike_ratio, itm_window_vol, itm_window_ratio,
            cluster_strength,
            low_dist if low_dist is not None else 0.0,
            (room_pct * 100) if room_pct != float('inf') else 999,
            pc_ratio if pc_ratio is not None else 0.0,
            pc_conviction,
            f"${opt_mark:.2f}"       if opt_mark      else 'n/a',
            f"${price_to_enter:.2f}" if price_to_enter else 'n/a',
        )
        return signal
