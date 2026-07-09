"""
Statistical Research Toolkit — §74-§77.

§74  Permutation tests + block bootstrap   (scipy.stats, arch)
§75  Monte Carlo — trade sequences, drawdown  (numpy)
§76  Hawkes process scaffold               (research stub — not production)
§77  Change-point detection                (ruptures)

All functions are pure (no DB I/O, no side effects).
Results are persisted by the caller via db.ops.
"""

from __future__ import annotations

import logging
import math
import warnings
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# §74  RANDOMIZED CONTROLS + PERMUTATION TESTS
# ─────────────────────────────────────────────────────────────────────────────

def _metric_fn(values: np.ndarray, metric: str) -> float:
    if metric == 'median':
        return float(np.median(values))
    if metric == 'sharpe':
        std = float(np.std(values, ddof=1))
        return float(np.mean(values) / std) if std > 0 else 0.0
    return float(np.mean(values))   # default: mean


def permutation_test(
    observed_values: list[float],
    control_values: list[float],
    n_permutations: int = 10_000,
    metric: str = 'mean',
    random_seed: int = 42,
) -> dict:
    """
    §74 One-sided permutation test: is observed_metric > null distribution?

    H0: both samples come from the same distribution.
    H1: observed_values have a higher metric than controls (one-sided).

    Parameters
    ----------
    observed_values : returns / outcomes for *real* signals
    control_values  : returns / outcomes for matched random controls
    n_permutations  : number of shuffle iterations
    metric          : 'mean' | 'median' | 'sharpe'

    Returns
    -------
    dict with keys:
        n_observed, n_control, n_permutations, metric,
        observed_metric, null_mean, null_std,
        p_value, effect_size, percentile_rank,
        ci_lower, ci_upper, significant
    """
    rng = np.random.default_rng(random_seed)

    obs = np.array(observed_values, dtype=float)
    ctrl = np.array(control_values, dtype=float)
    combined = np.concatenate([obs, ctrl])
    n_obs = len(obs)

    observed_metric = _metric_fn(obs, metric)

    null_distribution = np.empty(n_permutations)
    for i in range(n_permutations):
        rng.shuffle(combined)
        null_distribution[i] = _metric_fn(combined[:n_obs], metric)

    # One-sided p-value: fraction of null >= observed
    p_value = float(np.mean(null_distribution >= observed_metric))
    null_mean = float(np.mean(null_distribution))
    null_std  = float(np.std(null_distribution, ddof=1))
    percentile_rank = float(np.mean(null_distribution <= observed_metric)) * 100

    # Effect size: Cohen's d between observed and control
    pooled_std = float(np.std(np.concatenate([obs, ctrl]), ddof=1))
    effect_size = ((float(np.mean(obs)) - float(np.mean(ctrl))) / pooled_std
                   if pooled_std > 0 else 0.0)

    # 95% CI on the null distribution
    ci_lower = float(np.percentile(null_distribution, 2.5))
    ci_upper = float(np.percentile(null_distribution, 97.5))

    return {
        'n_observed':     n_obs,
        'n_control':      len(ctrl),
        'n_permutations': n_permutations,
        'metric':         metric,
        'observed_metric': round(observed_metric, 6),
        'null_mean':       round(null_mean,        6),
        'null_std':        round(null_std,         6),
        'p_value':         round(p_value,          6),
        'effect_size':     round(effect_size,      4),
        'percentile_rank': round(percentile_rank,  4),
        'ci_lower':        round(ci_lower,         6),
        'ci_upper':        round(ci_upper,         6),
        'significant':     p_value < 0.05,
    }


def block_bootstrap(
    values: list[float],
    n_resamples: int = 10_000,
    block_size: int = 5,
    metric: str = 'mean',
    confidence: float = 0.95,
    random_seed: int = 42,
) -> dict:
    """
    §74 Circular block bootstrap for time-series with serial correlation.

    Preserves short-range autocorrelation by resampling contiguous blocks
    rather than individual observations.

    Returns
    -------
    dict with keys:
        n_obs, block_size, n_resamples, metric,
        observed_metric, se_estimate, ci_lower, ci_upper, bias
    """
    try:
        from arch.bootstrap import CircularBlockBootstrap as _CBB
    except ImportError:
        logger.error("arch package required for block_bootstrap — pip install arch")
        return {}

    arr = np.array(values, dtype=float)
    observed_metric = _metric_fn(arr, metric)

    def _stat(x, axis=1):
        # arch passes 2-D array (n_resamples × n_obs); reduce along axis=1
        if metric == 'median':
            return np.median(x, axis=axis)
        if metric == 'sharpe':
            mu  = np.mean(x, axis=axis)
            std = np.std(x, axis=axis, ddof=1)
            return np.where(std > 0, mu / std, 0.0)
        return np.mean(x, axis=axis)

    bs = _CBB(block_size, arr, random_state=random_seed)
    alpha = 1.0 - confidence
    result = bs.conf_int(_stat, n_resamples, method='percentile',
                         size=1 - alpha, tail='two')
    ci_lower, ci_upper = float(result[0, 0]), float(result[1, 0])

    resample_metrics = np.array([_metric_fn(bs.apply(_stat, 1), 'mean')
                                  for _ in range(min(n_resamples, 1000))])
    se_estimate = float(np.std(resample_metrics, ddof=1))
    bias        = float(np.mean(resample_metrics)) - observed_metric

    return {
        'n_obs':           len(arr),
        'block_size':      block_size,
        'n_resamples':     n_resamples,
        'metric':          metric,
        'observed_metric': round(observed_metric, 6),
        'se_estimate':     round(se_estimate,     6),
        'ci_lower':        round(ci_lower,        6),
        'ci_upper':        round(ci_upper,        6),
        'bias':            round(bias,            6),
    }


# ─────────────────────────────────────────────────────────────────────────────
# §75  MONTE CARLO STUDIES
# ─────────────────────────────────────────────────────────────────────────────

def _max_drawdown(equity_curve: np.ndarray) -> float:
    """Peak-to-trough maximum drawdown as a fraction of peak equity."""
    running_max = np.maximum.accumulate(equity_curve)
    dd = (equity_curve - running_max) / np.maximum(running_max, 1e-9)
    return float(np.min(dd))   # negative


def monte_carlo_trades(
    trade_returns: list[float],
    n_simulations:    int   = 10_000,
    n_trades:         Optional[int] = None,
    starting_capital: float = 10_000.0,
    ruin_threshold:   float = -0.50,    # cumulative drawdown from start
    target_return:    float = 1.00,     # 100% gain from start
    random_seed:      int   = 42,
) -> dict:
    """
    §75 Monte Carlo simulation of trade-sequence variation.

    Resamples historical trade returns WITH REPLACEMENT to generate
    n_simulations synthetic sequences of n_trades trades.

    Parameters
    ----------
    trade_returns    : list of per-trade % returns (e.g. 0.25 = 25%)
    n_simulations    : number of synthetic paths
    n_trades         : sequence length per path (default: len(trade_returns))
    starting_capital : starting equity level
    ruin_threshold   : cumulative return (fraction) that constitutes ruin
    target_return    : cumulative return (fraction) that constitutes success

    Returns
    -------
    dict with expected_return, median_return, probability_of_loss,
    probability_of_ruin, target_hit_probability, drawdown percentiles (5/50/95),
    ci_lower_95, ci_upper_95
    """
    if not trade_returns:
        return {}

    rng = np.random.default_rng(random_seed)
    rets = np.array(trade_returns, dtype=float)
    n = n_trades or len(rets)
    if n < 1:
        return {}

    # Draw n_simulations paths, each of length n
    # Shape: (n_simulations, n)
    draws = rng.choice(rets, size=(n_simulations, n), replace=True)

    # Equity curve: starting_capital * cumprod(1 + r_i)
    equity = starting_capital * np.cumprod(1.0 + draws, axis=1)

    final_equity     = equity[:, -1]
    final_return_pct = (final_equity / starting_capital) - 1.0

    # Maximum drawdown per path (using equity curve prefixed with starting_capital)
    full_equity = np.hstack([np.full((n_simulations, 1), starting_capital), equity])
    max_dds = np.array([_max_drawdown(full_equity[i]) for i in range(n_simulations)])

    # Ruin: final equity below starting * (1 + ruin_threshold)
    ruin_floor  = starting_capital * (1.0 + ruin_threshold)
    target_ceil = starting_capital * (1.0 + target_return)

    prob_loss   = float(np.mean(final_equity < starting_capital))
    prob_ruin   = float(np.mean(np.min(equity, axis=1) <= ruin_floor))
    prob_target = float(np.mean(np.max(equity, axis=1) >= target_ceil))

    return {
        'n_trades':            n,
        'n_simulations':       n_simulations,
        'starting_capital':    starting_capital,
        'expected_return':     round(float(np.mean(final_return_pct)),       4),
        'median_return':       round(float(np.median(final_return_pct)),      4),
        'probability_of_loss': round(prob_loss,   4),
        'probability_of_ruin': round(prob_ruin,   4),
        'target_hit_probability': round(prob_target, 4),
        'max_drawdown_p5':     round(float(np.percentile(max_dds,  5)),  4),
        'max_drawdown_p50':    round(float(np.percentile(max_dds, 50)),  4),
        'max_drawdown_p95':    round(float(np.percentile(max_dds, 95)),  4),
        'ci_lower_95':         round(float(np.percentile(final_return_pct,  2.5)), 4),
        'ci_upper_95':         round(float(np.percentile(final_return_pct, 97.5)), 4),
    }


# ─────────────────────────────────────────────────────────────────────────────
# §76  HAWKES PROCESS RESEARCH SCAFFOLD
# ─────────────────────────────────────────────────────────────────────────────

# Event class labels per spec §76
HAWKES_EVENT_CLASSES = (
    'CALL_ATM_EVENT', 'CALL_ITM_EVENT', 'CALL_OTM_EVENT',
    'PUT_ATM_EVENT',  'PUT_ITM_EVENT',  'PUT_OTM_EVENT',
)


class HawkesResearchStub:
    """
    §76 Hawkes / point-process research scaffold.

    This is intentionally a RESEARCH STUB — not for production use.
    The spec recommends treating this as a 6-month research project.
    Multivariate self-exciting process fitting requires deep domain expertise
    and libraries (tick, hawkeslib, pyHawkes) that are research-grade.

    Usage path:
    1. Call prepare_event_stream() to convert volume_events rows.
    2. Call fit_poisson_baseline() to establish a non-exciting baseline.
    3. Extend fit_univariate_hawkes() / fit_multivariate_hawkes() once
       a suitable library is available and calibrated out-of-sample.

    Do not use Hawkes output in production until it materially beats
    simpler rules out of sample (per §76 spec).
    """

    # Parameters §76 expects to estimate
    PARAMETERS = (
        'baseline_intensity',
        'self_excitation',
        'cross_excitation',
        'decay_rate',
        'branching_ratio',
        'event_half_life',
    )

    def prepare_event_stream(self, volume_events: list[dict]) -> list[dict]:
        """
        Convert volume_events / oi_events DB rows into a normalized event stream.

        Each event dict gets:
            event_time_seconds : float — seconds since session open (08:30 CST)
            event_class        : one of HAWKES_EVENT_CLASSES
            event_size         : log(trigger_volume + 1) normalised magnitude
        """
        from datetime import timezone, timedelta

        _CST_OFFSET = timedelta(hours=-6)
        _CST = timezone(_CST_OFFSET)
        _SESSION_OPEN_HOUR = 8
        _SESSION_OPEN_MIN  = 30

        stream = []
        for ev in volume_events:
            et = ev.get('event_time')
            if et is None:
                continue
            try:
                et_cst = et.astimezone(_CST)
            except Exception:
                et_cst = et
            session_open = et_cst.replace(
                hour=_SESSION_OPEN_HOUR, minute=_SESSION_OPEN_MIN, second=0, microsecond=0
            )
            t_sec = (et_cst - session_open).total_seconds()
            if t_sec < 0:
                continue

            otype  = (ev.get('option_type') or '').upper()
            level  = ev.get('level_type') or ev.get('event_type') or ''
            if 'ATM' in level or 'SUPPORT' in level:
                moneyness = 'ATM'
            elif 'ITM' in level:
                moneyness = 'ITM'
            else:
                moneyness = 'OTM'

            if otype in ('CALL', 'PUT'):
                event_class = f'{otype}_{moneyness}_EVENT'
            else:
                event_class = 'CALL_ATM_EVENT'   # fallback

            vol = ev.get('trigger_volume') or 0
            stream.append({
                'event_time_seconds': round(t_sec, 1),
                'event_class':        event_class,
                'event_size':         round(math.log(vol + 1), 4),
                'strike':             ev.get('strike'),
                'contract_price':     ev.get('mark_at_event'),
                'distance_to_level':  ev.get('low_dist'),
            })

        return sorted(stream, key=lambda e: e['event_time_seconds'])

    def fit_poisson_baseline(self, event_stream: list[dict]) -> dict:
        """
        Fit a homogeneous Poisson process (non-exciting baseline).

        Rate = N events / T seconds. Used as the comparison model for
        any Hawkes fit: branching_ratio=0 means pure Poisson.
        """
        if not event_stream:
            return {}

        by_class: dict[str, list] = {c: [] for c in HAWKES_EVENT_CLASSES}
        for ev in event_stream:
            cls = ev['event_class']
            if cls in by_class:
                by_class[cls].append(ev['event_time_seconds'])

        session_duration = 6.5 * 3600   # 390 minutes in seconds
        result = {'model': 'Poisson_baseline', 'session_duration_sec': session_duration}

        for cls, times in by_class.items():
            n = len(times)
            rate = n / session_duration
            result[cls] = {
                'n_events':          n,
                'baseline_intensity': round(rate * 60, 6),  # per minute
                'expected_per_hour':  round(rate * 3600, 2),
                'inter_arrival_mean': round(1 / rate, 1) if rate > 0 else None,
            }

        return result

    def fit_univariate_hawkes(self, event_stream: list[dict],
                               event_class: str = 'CALL_ATM_EVENT') -> dict:
        """
        Univariate Hawkes fit stub for a single event class.

        Full implementation requires: pip install tick  (or hawkeslib).
        Returns a stub result with implementation notes.
        """
        logger.warning(
            "HawkesResearchStub.fit_univariate_hawkes: not implemented. "
            "Install `tick` (Linux/Mac) or `hawkeslib` and implement the "
            "HawkesExpKern fitting loop. See §76 spec for parameter targets."
        )
        times = [e['event_time_seconds'] for e in event_stream
                 if e['event_class'] == event_class]
        return {
            'model':        'univariate_Hawkes_ExpKern',
            'event_class':  event_class,
            'n_events':     len(times),
            'status':       'STUB_NOT_FITTED',
            'library_needed': 'tick or hawkeslib',
            'parameters':   {p: None for p in self.PARAMETERS},
            'research_questions': [
                'Does flow excite nearby strikes?',
                'Does put flow precede call flow during reversals?',
                'Are some patterns persistent baseline intensity?',
                'Are others discrete intensity bursts?',
                'Do secondary OI levels attract measurable excitation?',
            ],
        }

    def fit_multivariate_hawkes(self, event_stream: list[dict]) -> dict:
        """Multivariate Hawkes stub (all 6 event classes simultaneously)."""
        logger.warning(
            "HawkesResearchStub.fit_multivariate_hawkes: not implemented. "
            "Requires specialized library and out-of-sample validation."
        )
        return {
            'model':  'multivariate_Hawkes',
            'status': 'STUB_NOT_FITTED',
            'note':   '6-month research project per §76 spec',
        }


# ─────────────────────────────────────────────────────────────────────────────
# §77  CHANGE-POINT DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def detect_volume_change_points(
    volume_series: list[float],
    model:         str           = 'rbf',
    pen:           float         = 3.0,
    min_size:      int           = 3,
    max_bkps:      Optional[int] = None,
) -> dict:
    """
    §77 Change-point detection on an intraday volume series.

    Primary question: did volume shift from baseline behavior into a
    concentrated event regime?

    Uses ruptures.Pelt (pruned exact linear time) with the specified
    cost model. Falls back to ruptures.Binseg if Pelt fails.

    Parameters
    ----------
    volume_series : per-minute call or put volume (or combined)
    model         : ruptures cost model ('rbf' | 'l1' | 'l2' | 'normal')
    pen           : penalty parameter for Pelt (higher = fewer breakpoints)
    min_size      : minimum segment length in bars
    max_bkps      : hard cap on number of breakpoints (None = uncapped)

    Returns
    -------
    dict with breakpoint_indices, n_breakpoints, pre/post regime means,
    regime_change_ratio, concentrated_event_detected, model_used
    """
    try:
        import ruptures as rpt
    except ImportError:
        logger.error("ruptures package required — pip install ruptures")
        return {}

    if len(volume_series) < min_size * 2:
        return {
            'n_breakpoints': 0,
            'breakpoint_indices': [],
            'pre_regime_mean': float(np.mean(volume_series)) if volume_series else None,
            'post_regime_mean': None,
            'regime_change_ratio': None,
            'concentrated_event_detected': False,
            'model_used': model,
        }

    arr = np.array(volume_series, dtype=float).reshape(-1, 1)

    try:
        algo = rpt.Pelt(model=model, min_size=min_size).fit(arr)
        bkps = algo.predict(pen=pen)
    except Exception as e:
        logger.warning("Pelt failed (%s), falling back to Binseg", e)
        try:
            n_bkps = max_bkps or max(1, len(volume_series) // 10)
            algo = rpt.Binseg(model=model, min_size=min_size).fit(arr)
            bkps = algo.predict(n_bkps=n_bkps)
        except Exception as e2:
            logger.warning("Binseg also failed: %s", e2)
            return {}

    # ruptures always returns the final index (len) as the last element
    bkps_clean = [b for b in bkps if b < len(volume_series)]
    if max_bkps:
        bkps_clean = bkps_clean[:max_bkps]

    n_bkps = len(bkps_clean)

    # Compute pre/post regime statistics around the FIRST breakpoint
    if n_bkps == 0:
        pre_mean  = float(np.mean(volume_series))
        post_mean = None
        ratio     = None
        concentrated = False
    else:
        first_bkp = bkps_clean[0]
        pre  = volume_series[:first_bkp]
        post = volume_series[first_bkp:]
        pre_mean  = float(np.mean(pre))  if pre  else None
        post_mean = float(np.mean(post)) if post else None
        ratio = (round(post_mean / pre_mean, 4)
                 if (pre_mean and pre_mean > 0) else None)
        # "concentrated event regime" = post-break mean ≥ 2× pre-break mean
        concentrated = bool(ratio is not None and ratio >= 2.0)

    return {
        'n_breakpoints':               n_bkps,
        'breakpoint_indices':          bkps_clean,
        'pre_regime_mean':             round(pre_mean,  4) if pre_mean  is not None else None,
        'post_regime_mean':            round(post_mean, 4) if post_mean is not None else None,
        'regime_change_ratio':         ratio,
        'concentrated_event_detected': concentrated,
        'model_used':                  model,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Convenience: build matched random controls for §74 from a signal set
# ─────────────────────────────────────────────────────────────────────────────

def build_random_controls(
    signal_rows: list[dict],
    candidate_rows: list[dict],
    return_col:  str   = 'return_30m',
    n_controls:  int   = 5,
    random_seed: int   = 42,
) -> tuple[list[float], list[float]]:
    """
    §74 Build matched control sample from signal_candidates.

    For each signal, sample n_controls candidate rows that did NOT fire
    an alert (alert_fired=False) and share the same symbol + session_date.
    Returns (observed_returns, control_returns).
    """
    rng = np.random.default_rng(random_seed)

    # Index candidates by (symbol, session_date)
    ctrl_pool: dict[tuple, list] = {}
    for row in candidate_rows:
        key = (row.get('symbol'), str(row.get('session_date', '')))
        if not row.get('alert_fired', True):
            ctrl_pool.setdefault(key, []).append(row)

    observed: list[float] = []
    controls: list[float] = []

    for sig in signal_rows:
        ret = sig.get(return_col)
        if ret is None:
            continue
        observed.append(float(ret))
        key = (sig.get('symbol'), str(sig.get('session_date', '')))
        pool = ctrl_pool.get(key, [])
        if pool:
            sample = rng.choice(len(pool), size=min(n_controls, len(pool)),
                                replace=False)
            for idx in sample:
                ctrl_ret = pool[idx].get(return_col)
                if ctrl_ret is not None:
                    controls.append(float(ctrl_ret))

    return observed, controls
