"""
Pre-market sentiment scoring for a single symbol.

Two signals, each scored -1 / 0 / +1:
  1. Pre-market drift   — (pm_price - prev_close) / prev_close
  2. Put/Call OI ratio  — near-ATM put OI vs call OI (< 0.85 bullish, > 1.15 bearish)

Combined score → label:
  +2  STRONGLY BULLISH
  +1  BULLISH
   0  NEUTRAL
  -1  BEARISH
  -2  STRONGLY BEARISH
"""
import logging

import config

logger = logging.getLogger(__name__)

_DRIFT_THRESHOLD = 0.001   # 0.1 % — below this pm drift is noise
_PC_BULL_CUTOFF  = 0.85
_PC_BEAR_CUTOFF  = 1.15

_LABEL = {
    2:  'STRONGLY BULLISH',
    1:  'BULLISH',
    0:  'NEUTRAL',
    -1: 'BEARISH',
    -2: 'STRONGLY BEARISH',
}


def compute_sentiment(
    chain: dict,
    pm_price: float,
    prev_close: float,
) -> dict:
    """
    Return a sentiment dict for one symbol.

    Keys: symbol, prev_close, pm_price, pm_change_pct,
          call_oi, put_oi, pc_ratio, drift_score, pc_score, total_score, bias
    """
    symbol = chain['symbol']

    # ── Pre-market drift ──────────────────────────────────────────────────────
    if prev_close > 0:
        pm_change_pct = (pm_price - prev_close) / prev_close * 100
    else:
        pm_change_pct = 0.0

    if pm_change_pct > _DRIFT_THRESHOLD * 100:
        drift_score = 1
    elif pm_change_pct < -_DRIFT_THRESHOLD * 100:
        drift_score = -1
    else:
        drift_score = 0

    # ── Near-ATM Put/Call OI ratio ────────────────────────────────────────────
    # Band centred on the pre-market SPOT (not prev close) so the P/C ratio reflects
    # where price actually is at 08:20 — consistent with the spot-anchored S/R levels.
    anchor = pm_price if pm_price and pm_price > 0 else prev_close
    lo = anchor * (1 - config.ATM_RANGE_PCT)
    hi = anchor * (1 + config.ATM_RANGE_PCT)

    call_oi = sum(c['open_interest'] for c in chain['calls'] if lo <= c['strike'] <= hi)
    put_oi  = sum(p['open_interest'] for p in chain['puts']  if lo <= p['strike'] <= hi)

    pc_ratio = round(put_oi / call_oi, 3) if call_oi > 0 else 0.0

    if 0 < pc_ratio < _PC_BULL_CUTOFF:
        pc_score = 1
    elif pc_ratio > _PC_BEAR_CUTOFF:
        pc_score = -1
    else:
        pc_score = 0

    total_score = max(-2, min(2, drift_score + pc_score))
    bias = _LABEL[total_score]

    logger.info(
        "%s  pm=%.2f%%  drift=%+d  P/C=%.2f  pc=%+d  => %s",
        symbol, pm_change_pct, drift_score, pc_ratio, pc_score, bias,
    )

    return {
        'symbol':        symbol,
        'prev_close':    round(prev_close, 4),
        'pm_price':      round(pm_price, 4),
        'pm_change_pct': round(pm_change_pct, 3),
        'call_oi':       call_oi,
        'put_oi':        put_oi,
        'pc_ratio':      pc_ratio,
        'drift_score':   drift_score,
        'pc_score':      pc_score,
        'total_score':   total_score,
        'bias':          bias,
    }
