"""
Stateful intraday signal detector — Simplified V1 entry engine.

Goal (Mag-7 only): every minute, decide whether to alert a CALL or a PUT.

Signal semantics
-----------------
  SUPPORT    level (S1/S2/S3) → BULLISH / "Call-side bias"  — confirmed by CALL volume
  RESISTANCE level (R1/R2/R3) → BEARISH / "Put-side bias"   — confirmed by PUT  volume

A CALL/PUT alert fires when ALL hold (§17/§18):
  • spot is NEAR a same-side level                        (§4 proximity, binary)
  • the entry is WITH the intraday trend vs VWAP          (§16 VWAP trend gate)
  • correct side is being watched                         (§5)
  • a valid volume signal exists                          (§8 = §9 OR §10 OR §11)
  • the contract is cheap / not chased                    (§12 contract-low)
  • the contract is not historically rich                 (§13 historical percentile)
  • there is no short-cover risk                          (§14)
  • this symbol has not already alerted this direction    (§19 — 1 call + 1 put/day)

Valid volume signal (any one — the kind is NOT surfaced to Discord, §1):
  §9  Extreme single print : vol >= MinSingle AND ratio >= 8.0 AND low_dist <= 1.75
  §10 Volume cluster       : WindowVol5 >= MinCluster AND WindowRatio5 >= 3.0
                             AND ActiveBars5 >= 3 AND low_dist <= 1.75
  §11 Stair-step accum.    : ExcitationScore >= 0.70 AND WindowRatio5 >= 2.5
                             AND ActiveBars5 >= 3 AND low_dist <= 2.0

Opening range (§15): first 15 min are NOT blocked but need stronger evidence —
MinSingle × 1.5, cluster WindowRatio5 >= 4.0, stair-step ExcitationScore >= 0.80.

Removed for V1 (§1): dynamic S/R flipping, spread filter, target-room filter,
confidence tiers, WATCH/upgrade alerts, volume-shape labels in Discord.
"""
import logging
from collections import defaultdict, deque
from datetime import date, datetime
from typing import Optional

import config
from data.alpaca_client import occ_symbol
from data.market_utils import CST

logger = logging.getLogger(__name__)

_FiredKey = tuple[str, str]          # (symbol, signal_type)
_OptKey   = tuple[str, float, str]   # (symbol, strike, opt_type)


# ── Pure helper functions ─────────────────────────────────────────────────────

def _spike_ratio(history: list[int], delta: int) -> float:
    """1-bar ratio: delta / max(rolling_avg, 10). Returns 0.0 with no prior history."""
    prior = history[:-1] if len(history) > 1 else []
    if not prior:
        return 0.0
    baseline = max(sum(prior) / len(prior), 10)   # spec: max(AvgVol, 10)
    return round(delta / baseline, 2)


def _window_ratio(history: list[int]) -> tuple[int, float]:
    """
    3-bar window sum vs prior rolling windows (used by check_opposite_side).
    Returns (window_vol, window_spike_ratio). Needs >=4 bars; else (sum, 0.0).
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
    exclude_last=1 → bars just before the current one (single print);
    exclude_last=N → bars before the trailing N-bar window (cluster baseline).
    Returns 0.0 when no prior bars are available.
    """
    end = len(history) - exclude_last
    if end <= 0:
        return 0.0
    seg = history[max(0, end - lookback):end]
    return sum(seg) / len(seg) if seg else 0.0


def _single_print(history: list[int], delta: int, min_vol: int) -> tuple[bool, float]:
    """
    §9 raw single print (contract-low filter applied by caller):
      delta >= min_vol  AND  delta / max(AvgPrior10, 10) >= OPT_SINGLE_PRINT_RATIO.
    Returns (valid, ratio).
    """
    base  = max(_avg_prior(history, 1, config.OPT_PRIOR_LOOKBACK), 10.0)
    ratio = round(delta / base, 2)
    valid = delta >= min_vol and ratio >= config.OPT_SINGLE_PRINT_RATIO
    return valid, ratio


def _cluster_metrics(history: list[int]) -> dict:
    """
    Rolling N-bar window metrics (N = OPT_CLUSTER_WINDOW, default 5).

    Returns a dict with:
      vol        : sum of the last N bar deltas (WindowVol5)
      ratio      : WindowVol5 / (N * base_unit)  (WindowRatio5)
      active     : bars with per-bar ratio >= OPT_CLUSTER_ACTIVE_RATIO
      burst      : bars with per-bar ratio >= OPT_CLUSTER_BURST_RATIO
      base_unit  : max(AvgPrior10 before the window, 10)
      window     : the last N bar deltas (for the stair-step excitation)
    A short history (< N bars) yields zeroed metrics so nothing fires early.
    """
    n = config.OPT_CLUSTER_WINDOW
    window = list(history[-n:])
    if len(window) < n:
        return {'vol': sum(window), 'ratio': 0.0, 'active': 0, 'burst': 0,
                'base_unit': 10.0, 'window': window}
    base_unit  = max(_avg_prior(history, n, config.OPT_PRIOR_LOOKBACK), 10.0)
    window_vol = sum(window)
    ratio      = round(window_vol / (n * base_unit), 2)
    active = sum(1 for b in window if b / base_unit >= config.OPT_CLUSTER_ACTIVE_RATIO)
    burst  = sum(1 for b in window if b / base_unit >= config.OPT_CLUSTER_BURST_RATIO)
    return {'vol': window_vol, 'ratio': ratio, 'active': active, 'burst': burst,
            'base_unit': base_unit, 'window': window}


def _excitation(window: list[int], base_unit: float) -> float:
    """
    §11 ExcitationScore from the most-recent-first per-bar ratios:
      ExcitationRaw   = Σ STAIRSTEP_WEIGHTS[i] * VolumeRatio[t-i]
      ExcitationScore = min(ExcitationRaw, 10) / 10
    where VolumeRatio[t-i] = window[-1-i] / base_unit. 0.0 with no window.
    """
    if not window or base_unit <= 0:
        return 0.0
    weights = config.STAIRSTEP_WEIGHTS
    raw = 0.0
    for i, w in enumerate(weights):
        if i >= len(window):
            break
        raw += w * (window[-1 - i] / base_unit)
    return round(min(raw, 10.0) / 10.0, 4)


def _timing_score(atm_hist: list[int], itm_hist: list[int]) -> float:
    """Informational only — feeds the stored cluster_strength field."""
    n = min(3, len(atm_hist), len(itm_hist))
    if n == 0:
        return 0.0
    atm_3, itm_3 = atm_hist[-n:], itm_hist[-n:]
    if max(atm_3) == 0 or max(itm_3) == 0:
        return 0.0
    return 1.00 if atm_3.index(max(atm_3)) == itm_3.index(max(itm_3)) else 0.70


def _pc_conviction(signal_type: str, pc_ratio: Optional[float]) -> str:
    """Informational P/C label (no longer a gate)."""
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


def _spread_pct(data: dict) -> Optional[float]:
    """(ask − bid) / mid — informational only in V1 (no longer a gate)."""
    bid, ask = data.get('bid'), data.get('ask')
    if bid is None or ask is None:
        return None
    mid = (bid + ask) / 2
    if mid <= 0:
        return None
    return (ask - bid) / mid


def _opposing_strikes(levels: list, spot: float, position_only: bool) -> tuple[list, list]:
    """
    Split level strikes into (above_spot, below_spot). Used to derive exit
    targets for the (separate) exit state machine, not for entry.
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


def compute_exit_targets(
    signal_type: str,
    spot: float,
    levels: list,
    position_only: bool = False,
) -> tuple[Optional[float], Optional[float]]:
    """
    Exit-target-shift rule. The nearest opposing level is usually too close to
    give the trade room, so skip it and use the 2nd/3rd opposing levels:

      CALL at support    (BULLISH) → Exit1 = 2nd resistance, Exit2 = 3rd
      PUT  at resistance (BEARISH) → Exit1 = 2nd support,    Exit2 = 3rd

    Fallbacks: when only one opposing level exists, use it (R1/S1). When only two
    exist, Exit2 is None. After the shift, any candidate still within
    EXIT_MIN_ROOM_PCT of spot is dropped (skip to the next farther level).

    Returns (exit1, exit2) as underlying price levels, either may be None.
    """
    above, below = _opposing_strikes(levels, spot, position_only)
    if signal_type == 'BULLISH':
        opp  = sorted(above)                       # nearest resistance first
        room = lambda lv: (lv - spot) / spot if spot > 0 else 0.0
    else:
        opp  = sorted(below, reverse=True)         # nearest support first
        room = lambda lv: (spot - lv) / spot if spot > 0 else 0.0

    if not opp:
        return None, None
    if len(opp) == 1:
        return opp[0], None                        # only the nearest → fallback to R1/S1

    candidates = opp[1:]                            # skip the nearest (R1/S1)
    spaced = [lv for lv in candidates if room(lv) >= config.EXIT_MIN_ROOM_PCT]
    chosen = spaced if spaced else candidates       # keep the shifted set if all too close
    return chosen[0], (chosen[1] if len(chosen) > 1 else None)


# ── Detector ──────────────────────────────────────────────────────────────────

class SignalDetector:

    def __init__(self) -> None:
        self._hist_maxlen = config.OPT_CLUSTER_WINDOW + config.OPT_PRIOR_LOOKBACK
        # One alert per direction per ticker per day.
        self._fired_today:  dict[_FiredKey, bool] = {}
        self._prev_opt_vol: dict[_OptKey, int]    = {}
        self._opt_vol_hist: dict[_OptKey, deque]  = defaultdict(
            lambda: deque(maxlen=self._hist_maxlen)
        )
        self._opt_mark_low: dict[_OptKey, float]      = {}
        self._opt_last_bar: dict[_OptKey, datetime]   = {}
        # §13 multi-day (low, high) per OCC, fetched once per contract per day.
        self._opt_hist_range: dict[str, Optional[tuple[float, float]]] = {}
        self._hist_range_fn = None
        # §14 prior major volume events per contract key → [(volume, price), …].
        self._major_events: dict[str, list[tuple[int, float]]] = defaultdict(list)
        self._history_date: Optional[date] = None

    # ── Public API ────────────────────────────────────────────────────────────

    def check(
        self,
        symbol: str,
        bars: list[dict],
        levels: list,
        option_quotes: dict | None = None,
        expiry: Optional[date] = None,
        pc_ratio: Optional[float] = None,
        opening_range: bool = False,
        hist_range_fn=None,
        fired_today_fn=None,
        session_vwap: Optional[float] = None,
    ) -> list[dict]:
        """
        Run the V1 entry pipeline for the latest 1-min bar; return [] or [signal].

        opening_range : True during the first OPENING_RANGE_MINUTES — not blocked,
                        but volume thresholds are raised (§15).
        hist_range_fn : callable(occ) -> (low, high) | None for the §13 gate.
        fired_today_fn: callable(symbol, day) -> {signal_type: [confidence]} so the
                        one-call/one-put-per-day dedup survives restarts/instances.
        session_vwap  : volume-weighted average underlying price over the session,
                        computed by the caller from full-session bars (the detector
                        only sees the trailing slice). Drives the §16 trend gate;
                        None disables it (early session / no volume).
        """
        if not bars:
            return []

        current     = bars[-1]
        close_price = current['close']
        today       = current['bar_time'].date()
        bar_time    = current['bar_time']
        self._hist_range_fn = hist_range_fn

        # Keep 1DTE expiry handling (target-strike pricing); roles stay frozen.
        next_day_mode = (config.NEXT_DAY_MODE_ENABLED and
                         expiry is not None and expiry > today)

        # Reset intraday state on a new trading day
        if self._history_date != today:
            self._history_date    = today
            self._fired_today     = {}
            self._prev_opt_vol    = {}
            self._opt_vol_hist    = defaultdict(lambda: deque(maxlen=self._hist_maxlen))
            self._opt_mark_low    = {}
            self._opt_last_bar    = {}
            self._opt_hist_range  = {}
            self._major_events    = defaultdict(list)

        # Durable dedup: fold in directions already fired today (DB).
        if fired_today_fn is not None:
            try:
                db_fired = fired_today_fn(symbol, today) or {}
            except Exception as exc:
                logger.warning("fired_today_fn failed for %s: %s", symbol, exc)
                db_fired = {}
            for st in db_fired:
                self._fired_today[(symbol, st)] = True

        if not option_quotes:
            return []

        # ── Step 1: deltas + rolling histories (a re-entry after a gap restarts) ─
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

        # ── Opening-range thresholds (§15) — raised, never suppressed ──────────
        single_mult     = config.OPENING_RANGE_VOL_MULT      if opening_range else 1.0
        cluster_ratio_min = (config.OPENING_RANGE_CLUSTER_RATIO if opening_range
                             else config.OPT_CLUSTER_WINDOW_RATIO)
        excitation_min  = (config.OPENING_RANGE_EXCITATION_MIN if opening_range
                           else config.STAIRSTEP_EXCITATION_MIN)
        base_single_vol = config.OPT_MIN_SINGLE_PRINT_VOL.get(
            symbol, config.OPT_MIN_SINGLE_PRINT_VOL['default'])
        min_single_vol  = int(round(base_single_vol * single_mult))
        min_cluster_vol = config.OPT_MIN_CLUSTER_WINDOW_VOL.get(
            symbol, config.OPT_MIN_CLUSTER_WINDOW_VOL['default'])
        near_thr = (config.NEAR_LEVEL_DIST_VOLATILE if symbol in config.VOLATILE_SYMBOLS
                    else config.NEAR_LEVEL_DIST_DEFAULT)

        candidates: list[dict] = []

        for level in levels:
            strike       = float(level['strike'])
            rank         = int(level.get('rank', 1))
            level_type   = level['level_type']                       # frozen (no flip)
            confirm_type = 'PUT' if level_type == 'RESISTANCE' else 'CALL'
            signal_type  = 'BEARISH' if level_type == 'RESISTANCE' else 'BULLISH'
            label        = ('R' if level_type == 'RESISTANCE' else 'S') + str(rank)

            # ── §4 Proximity (binary) ─────────────────────────────────────────
            dist = abs(close_price - strike) / close_price if close_price > 0 else 1.0
            if dist > near_thr:
                self._log_eval(symbol, label, strike, close_price, confirm_type,
                               reason='NOT_NEAR_LEVEL', dist=dist)
                continue

            # ── §16 VWAP trend gate — only trade WITH the intraday trend ──────
            # BULLISH (call/support-bounce) needs spot at/above VWAP; BEARISH
            # (put/resistance-fade) needs spot at/below it. No-op when VWAP is
            # unavailable (early session / no volume).
            if config.VWAP_GATE_ENABLED and session_vwap and session_vwap > 0:
                buf = config.VWAP_GATE_BUFFER_PCT
                aligned = (close_price >= session_vwap * (1 + buf)
                           if signal_type == 'BULLISH'
                           else close_price <= session_vwap * (1 - buf))
                if not aligned:
                    self._log_eval(symbol, label, strike, close_price, confirm_type,
                                   reason='AGAINST_VWAP_TREND', dist=dist)
                    continue

            # ── Identify ATM + 1-ITM confirm-side contracts ───────────────────
            ct_keys = [(s, ot) for (s, ot) in opt_data_map if ot == confirm_type]
            if not ct_keys:
                self._log_eval(symbol, label, strike, close_price, confirm_type,
                               reason='NO_QUOTES', dist=dist)
                continue
            atm_key = min(ct_keys, key=lambda k: abs(k[0] - close_price))
            if confirm_type == 'CALL':
                itm_cands = [k for k in ct_keys if k[0] < close_price and k != atm_key]
                itm_key   = max(itm_cands, key=lambda k: k[0]) if itm_cands else None
            else:
                itm_cands = [k for k in ct_keys if k[0] > close_price and k != atm_key]
                itm_key   = min(itm_cands, key=lambda k: k[0]) if itm_cands else None

            atm_data  = opt_data_map[atm_key]
            atm_delta = vol_deltas.get(atm_key, 0)
            atm_okey: _OptKey = (symbol, atm_key[0], confirm_type)
            atm_low   = self._contract_low_dist(atm_okey, atm_data)
            atm = self._eval_volume(list(self._opt_vol_hist[atm_okey]), atm_delta, atm_low,
                                    min_single_vol, min_cluster_vol, cluster_ratio_min,
                                    excitation_min)

            if itm_key:
                itm_data  = opt_data_map[itm_key]
                itm_delta = vol_deltas.get(itm_key, 0)
                itm_okey: _OptKey = (symbol, itm_key[0], confirm_type)
                itm_low   = self._contract_low_dist(itm_okey, itm_data)
                itm = self._eval_volume(list(self._opt_vol_hist[itm_okey]), itm_delta, itm_low,
                                        min_single_vol, min_cluster_vol, cluster_ratio_min,
                                        excitation_min)
            else:
                itm_data, itm_delta, itm_low = {}, 0, None
                itm = self._eval_volume([], 0, None, min_single_vol, min_cluster_vol,
                                        cluster_ratio_min, excitation_min)

            valid_volume = atm['valid'] or itm['valid']

            # ── §12 contract-low: hard block a chased ATM (both modes) ─────────
            if atm_low is not None and atm_low > config.CONTRACT_LOW_MAX_DIST:
                self._log_eval(symbol, label, strike, close_price, confirm_type,
                               reason='CONTRACT_CHASED', dist=dist, atm=atm, low=atm_low)
                continue

            if not valid_volume:
                self._log_eval(symbol, label, strike, close_price, confirm_type,
                               reason='NO_VALID_VOLUME_SIGNAL', dist=dist, atm=atm, low=atm_low)
                continue

            # Trade the ATM confirm-side contract (nearest spot ≈ the level). The
            # contract we price and trade is the one volume was detected on, so its
            # strike, quote, and alert label stay consistent in every mode. No OTM
            # target-shift — next-day (Tue/Thu) trades at the level just like 0DTE.
            day_mode      = 'NEXT_DAY' if next_day_mode else '0DTE'
            trade_data    = atm_data
            traded_strike = float(atm_key[0])
            target_level: Optional[float] = None

            # ── §13 historical value percentile ───────────────────────────────
            hv_pctile = self._historical_value_pctile(
                symbol, traded_strike, confirm_type, expiry, trade_data)
            if hv_pctile is not None and hv_pctile > config.HIST_VALUE_PCTILE_MAX:
                self._log_eval(symbol, label, strike, close_price, confirm_type,
                               reason='HISTORICAL_VALUE_TOO_HIGH', dist=dist, atm=atm,
                               low=atm_low, hv=hv_pctile)
                continue

            # ── §14 short-cover risk (on the volume-bearing ATM contract) ──────
            atm_mark = atm_data.get('mark') or 0.0
            if self._short_cover_risk(symbol, atm_key[0], confirm_type, expiry,
                                      atm_delta, atm_mark, min_single_vol):
                self._log_eval(symbol, label, strike, close_price, confirm_type,
                               reason='SHORT_COVER_RISK', dist=dist, atm=atm, low=atm_low)
                continue

            # ── §19 already alerted this direction today ──────────────────────
            if self._fired_today.get((symbol, signal_type)):
                self._log_eval(symbol, label, strike, close_price, confirm_type,
                               reason='ALREADY_ALERTED_TODAY', dist=dist, atm=atm, low=atm_low)
                continue

            # ── Passed — build the alert ──────────────────────────────────────
            shape = ('EXTREME_SINGLE' if (atm['A'] or itm['A']) else
                     'CLUSTER'        if (atm['B'] or itm['B']) else 'STAIRSTEP')
            pc_conviction = _pc_conviction(signal_type, pc_ratio)
            timing   = _timing_score(list(self._opt_vol_hist[atm_okey]),
                                     list(self._opt_vol_hist[itm_okey]) if itm_key else [])
            atm_norm = min(1.0, atm['ratio'] / max(config.OPT_CLUSTER_WINDOW_RATIO, 1))
            itm_norm = min(1.0, itm['ratio'] / max(config.OPT_CLUSTER_WINDOW_RATIO, 1))
            cluster_strength = round(0.45 * atm_norm + 0.35 * itm_norm + 0.20 * timing, 4)

            signal = self._build_signal(
                symbol=symbol, level=level, levels=levels, rank=rank, level_label=label,
                level_type=level_type, confirm_type=confirm_type, signal_type=signal_type,
                current_bar=current, expiry=expiry,
                atm_data=atm_data, atm=atm, atm_delta=atm_delta, atm_low_dist=atm_low,
                itm=itm, itm_delta=itm_delta,
                atm_itm_confirm=(atm['valid'] and itm['valid']),
                cluster_strength=cluster_strength, signal_shape=shape,
                hv_pctile=hv_pctile, pc_ratio=pc_ratio, pc_conviction=pc_conviction,
                next_day_mode=next_day_mode, day_mode=day_mode,
                trade_data=trade_data, traded_strike=traded_strike, target_level=target_level,
            )
            candidates.append(signal)

        if not candidates:
            return []

        # One alert this bar per direction. Across in-range levels keep the
        # strongest: highest-OI level (lowest rank) then largest ATM volume.
        best_by_dir: dict[str, dict] = {}
        for sig in candidates:
            st = sig['signal_type']
            cur = best_by_dir.get(st)
            key = (-sig['level_rank'], sig['atm_vol_1m'])
            if cur is None or key > (-cur['level_rank'], cur['atm_vol_1m']):
                best_by_dir[st] = sig

        fired: list[dict] = []
        for st, sig in best_by_dir.items():
            if self._fired_today.get((symbol, st)):
                continue
            self._fired_today[(symbol, st)] = True
            fired.append(sig)
        return fired

    # ── Per-contract volume evaluation (§9/§10/§11) ──────────────────────────

    def _eval_volume(
        self,
        history: list[int],
        delta: int,
        low_dist: Optional[float],
        min_single: int,
        min_cluster_vol: int,
        cluster_ratio_min: float,
        excitation_min: float,
    ) -> dict:
        """Evaluate the three volume signals for one contract; returns metrics + A/B/C."""
        single_raw, spike_ratio = _single_print(history, delta, min_single)
        clu        = _cluster_metrics(history)
        excitation = _excitation(clu['window'], clu['base_unit'])

        near_175 = (low_dist is None or low_dist <= config.NEAR_LOW_MAX_DIST)
        near_200 = (low_dist is None or low_dist <= config.STAIRSTEP_LOW_DIST_MAX)

        a_extreme = single_raw and near_175
        b_cluster = (clu['vol'] >= min_cluster_vol and clu['ratio'] >= cluster_ratio_min
                     and clu['active'] >= config.OPT_CLUSTER_ACTIVE_MIN and near_175)
        # Stair-step also requires the absolute WindowVol5 floor — otherwise a
        # quiet contract (tiny baseline) fires on ratios alone (e.g. 180 contracts
        # over 5 bars). "Need absolute volume and ratio", per the §9 principle.
        c_stair   = (excitation >= excitation_min
                     and clu['vol'] >= min_cluster_vol
                     and clu['ratio'] >= config.STAIRSTEP_WINDOW_RATIO_MIN
                     and clu['active'] >= config.STAIRSTEP_ACTIVE_MIN and near_200)
        return {
            'delta': delta, 'spike_ratio': spike_ratio,
            'vol': clu['vol'], 'ratio': clu['ratio'],
            'active': clu['active'], 'burst': clu['burst'], 'excitation': excitation,
            'A': a_extreme, 'B': b_cluster, 'C': c_stair,
            'valid': a_extreme or b_cluster or c_stair,
        }

    # ── §13 historical value percentile ───────────────────────────────────────

    def _historical_value_pctile(
        self, symbol: str, strike: float, opt_type: str,
        expiry: Optional[date], data: dict,
    ) -> Optional[float]:
        """
        (mark - HistLow) / (HistHigh - HistLow) over the multi-day window, or None
        when not evaluable (no fn, no expiry, no mark, or no prior history → 0DTE).
        """
        if self._hist_range_fn is None or expiry is None:
            return None
        mark = data.get('mark')
        if not mark or mark <= 0:
            return None
        occ = occ_symbol(symbol, expiry, strike, opt_type)
        if occ not in self._opt_hist_range:
            try:
                self._opt_hist_range[occ] = self._hist_range_fn(occ)
            except Exception as exc:
                logger.warning("hist_range_fn failed for %s: %s", occ, exc)
                self._opt_hist_range[occ] = None
        rng = self._opt_hist_range.get(occ)
        if not rng:
            return None
        low, high = rng
        span = high - low
        if span <= 0:
            span = 0.01
        return round((mark - low) / span, 4)

    # ── §14 short-cover risk ──────────────────────────────────────────────────

    def _short_cover_risk(
        self, symbol: str, strike: float, opt_type: str, expiry: Optional[date],
        cur_vol: int, cur_price: float, min_major_vol: int,
    ) -> bool:
        """
        True when the current major volume event mirrors an earlier event of
        similar size but at a much lower price (shorts covering, not fresh longs).

        Only "major" events (cur_vol >= min_major_vol) are compared and stored.
        """
        if not config.SHORT_COVER_FILTER:
            return False
        if cur_vol < min_major_vol or cur_price <= 0:
            return False
        key = f"{symbol}|{expiry}|{strike}|{opt_type}"
        risk = False
        for prior_vol, prior_price in self._major_events[key]:
            if prior_price <= 0:
                continue
            sim = cur_vol / max(prior_vol, 1)
            if config.SHORT_COVER_SIM_LOW <= sim <= config.SHORT_COVER_SIM_HIGH:
                if cur_price / prior_price <= config.SHORT_COVER_REPRICE_MAX:
                    risk = True
                    break
        self._major_events[key].append((cur_vol, cur_price))
        return risk

    # ── Opposite-side volume validation (exit state machine) ─────────────────

    def check_opposite_side(
        self,
        symbol: str,
        signal_type: str,
        option_quotes: dict,
        target_price: float,
    ) -> bool:
        """
        True if opposite-side volume shows cluster-level activity near target_price.
        Used by check_exits() for early exit2. BULLISH→watch PUTs, BEARISH→CALLs.
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
            delta              = hist[-1]
            ratio              = _spike_ratio(hist, delta)
            win_vol, win_ratio = _window_ratio(hist)
            burst   = delta >= min_spike_vol and ratio     >= config.OPT_SINGLE_SPIKE_RATIO
            cluster = win_ratio >= config.OPT_CONSEC_SPIKE_RATIO and win_vol >= min_clust_vol
            return burst or cluster

        atm_active = _side_active(opp_keys[0])
        itm_active = _side_active(opp_keys[1]) if len(opp_keys) > 1 else False
        result     = atm_active and itm_active
        if result:
            logger.info("OppSide ACTIVE  %s %s  opp=%s  near=%.2f",
                        symbol, signal_type, opp_type, target_price)
        return result

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _contract_low_dist(self, okey: _OptKey, data: dict) -> Optional[float]:
        """
        §12 ContractLowDistance = mark / max(IntradayLow, 0.01); None if unknown.
        Uses the lower of Schwab's day_low and our watched-session min.
        """
        mark = data.get('mark')
        candidates = [x for x in (self._opt_mark_low.get(okey), data.get('day_low'))
                      if x and x > 0]
        low = min(candidates) if candidates else None
        if mark and low and low > 0:
            return round(mark / low, 4)
        return None

    def _log_eval(self, symbol, label, strike, spot, confirm_type, *,
                  reason: str, dist: float, atm: dict = None,
                  low: Optional[float] = None, hv: Optional[float] = None) -> None:
        """§21 per-level evaluation log with the blocked reason."""
        a = atm or {}
        logger.info(
            "MONITOR  %s %s@%.2f  spot=%.2f  dist=%.3f%%  %s  "
            "1m=%d(x%.1f) win=%d(x%.1f,act=%d) exc=%.2f  low=%s  hv=%s  → %s",
            symbol, label, strike, spot, dist * 100, confirm_type,
            a.get('delta', 0), a.get('spike_ratio', 0.0),
            a.get('vol', 0), a.get('ratio', 0.0), a.get('active', 0), a.get('excitation', 0.0),
            f"{low:.2f}" if low is not None else "n/a",
            f"{hv:.2f}" if hv is not None else "n/a",
            reason,
        )

    def _build_signal(
        self,
        symbol: str,
        level,
        levels: list,
        rank: int,
        level_label: str,
        level_type: str,
        confirm_type: str,
        signal_type: str,
        current_bar: dict,
        expiry: Optional[date],
        atm_data: dict,
        atm: dict,
        atm_delta: int,
        atm_low_dist: Optional[float],
        itm: dict,
        itm_delta: int,
        atm_itm_confirm: bool,
        cluster_strength: float,
        signal_shape: str,
        hv_pctile: Optional[float],
        pc_ratio: Optional[float],
        pc_conviction: str,
        next_day_mode: bool,
        day_mode: str,
        trade_data: dict,
        traded_strike: float,
        target_level: Optional[float],
    ) -> dict:
        """Build the signal dict (DB/Sheets/exits compatible). confidence = 'HIGH'."""
        now    = datetime.now(CST)
        strike = float(level['strike'])
        spot   = current_bar['close']

        # Exit targets — shifted one level out (skip the too-close nearest level).
        # Roles are frozen (no flipping), so use the frozen SUPPORT/RESISTANCE types.
        exit1_price, exit2_price = compute_exit_targets(
            signal_type, spot, levels, position_only=False)

        bias = 'Call-side bias' if level_type == 'SUPPORT' else 'Put-side bias'

        opt_mark = trade_data.get('mark')
        opt_bid  = trade_data.get('bid')
        opt_ask  = trade_data.get('ask')
        price_to_enter = round(opt_ask, 2) if opt_ask else None
        price_to_exit  = round(opt_ask * 2, 2) if opt_ask else None

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
            'level_label':      level_label,      # 'R1'…'S3' (used in Discord/selection)
            'level_rank':       rank,
            'expiry':           expiry,
            'trigger_price':    spot,
            'option_type':      confirm_type,
            'day_mode':         day_mode,
            'traded_strike':    traded_strike,
            'target_level':     target_level,
            'opt_mark':         opt_mark,
            'opt_bid':          opt_bid,
            'opt_ask':          opt_ask,
            'price_to_enter':   price_to_enter,
            'price_to_exit':    price_to_exit,
            'prox_score':       1.0,              # binary near-level → always 1.0
            'cluster_strength': cluster_strength,
            'strong_cluster':   atm_itm_confirm,
            'flow_shape':       signal_shape,
            'signal_shape':     signal_shape,
            'confidence':       'HIGH',
            'upgrade':          False,
            'cluster_active_bars': atm.get('active', 0),
            'cluster_burst_bars':  atm.get('burst', 0),
            'atm_vol_1m':       atm_delta,
            'atm_spike_ratio':  atm.get('spike_ratio', 0.0),
            'atm_vol_3m':       atm.get('vol', 0),
            'itm_vol_1m':       itm_delta,
            'itm_spike_ratio':  itm.get('spike_ratio', 0.0),
            'itm_vol_3m':       itm.get('vol', 0),
            'spread_pct':       (round(_spread_pct(trade_data), 4)
                                 if _spread_pct(trade_data) is not None else None),
            'low_dist':         atm_low_dist,
            'room_score':       None,             # §1 target-room removed as a gate
            'room_pct':         None,
            'hv_pctile':        hv_pctile,
            'pc_ratio':         pc_ratio,
            'pc_conviction':    pc_conviction,
            'option_hl_flag':   option_hl_flag,
            'exit1_price':      exit1_price,
            'exit2_price':      exit2_price,
            # Legacy nullable columns expected by db.save_signal
            'opt_vol_delta':      atm_delta,
            'avg_volume_20':      None,
            'spike_volume':       None,
            'consecutive_spikes': None,
        }

        strike_note = (f"  [{day_mode} strike={traded_strike:.2f}→tgt {target_level:.2f}]"
                       if next_day_mode and target_level is not None else "")
        logger.info(
            "SIGNAL >> %s %s (%s)  %s@%.2f%s  %s:1m=%d(x%.1f) win=%d(x%.1f,act=%d) exc=%.2f  "
            "low=%.2f  hv=%s  shape=%s  mark=%s  enter=%s",
            symbol, signal_type, bias, level_label, strike, strike_note,
            confirm_type, atm_delta, atm.get('spike_ratio', 0.0),
            atm.get('vol', 0), atm.get('ratio', 0.0), atm.get('active', 0),
            atm.get('excitation', 0.0),
            atm_low_dist if atm_low_dist is not None else 0.0,
            f"{hv_pctile:.2f}" if hv_pctile is not None else 'n/a',
            signal_shape,
            f"${opt_mark:.2f}"       if opt_mark      else 'n/a',
            f"${price_to_enter:.2f}" if price_to_enter else 'n/a',
        )
        return signal
