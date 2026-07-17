"""
Alert taxonomy — every fired alert = Market State × Leadership Type × Direction.

Philosophy: JakeVolume is a deterministic state machine, not a scanner. If an alert
fires it is already the highest-conviction opportunity the engine can identify — there
are no tiers, stars, confidence scores, or "how good is it" grades on Discord. The
quality decision was made upstream (the gates + the Gold chokepoint). This module's
job is only to say, consistently, WHAT fired: which market state, which leadership
type, which direction, and WHY (a plain list of trigger reasons).

It replaces the old grab-bag of overlapping signal names (Chain-Led, Primary Bounce,
Gold, Transition, Reversal, …) — which mixed market state, trigger, and quality — with
three orthogonal axes:

  Market State   : COMPRESSION · TRANSITION · TREND_EXPANSION · REVERSAL · BREAKOUT
  Leadership Type: CHAIN_LEADER · PRIMARY_LEVEL · GAMMA_LEADER · VOLUME_LEADER
  Direction      : CALL · PUT  (already on the signal as option_type / signal_type)

All derivation is deterministic and read-only over fields the detector already stamps
plus the poll's bars / trend / option quotes. No I/O, no trade gating.
"""
import logging
from statistics import fmean

import config

logger = logging.getLogger(__name__)

# ── Market states ──────────────────────────────────────────────────────────────
COMPRESSION     = 'COMPRESSION'
TRANSITION      = 'TRANSITION'
TREND_EXPANSION = 'TREND_EXPANSION'
REVERSAL        = 'REVERSAL'
BREAKOUT        = 'BREAKOUT'

# ── Leadership types ───────────────────────────────────────────────────────────
CHAIN_LEADER  = 'CHAIN_LEADER'
PRIMARY_LEVEL = 'PRIMARY_LEVEL'
GAMMA_LEADER  = 'GAMMA_LEADER'
VOLUME_LEADER = 'VOLUME_LEADER'

STATE_LABEL = {
    COMPRESSION: 'Compression', TRANSITION: 'Transition',
    TREND_EXPANSION: 'Trend Expansion', REVERSAL: 'Reversal', BREAKOUT: 'Breakout',
}
LEADERSHIP_LABEL = {
    CHAIN_LEADER: 'Chain Leader', PRIMARY_LEVEL: 'Primary Level',
    GAMMA_LEADER: 'Gamma Leader', VOLUME_LEADER: 'Volume Leader',
}


def state_label(state):
    return STATE_LABEL.get(state, (state or '').replace('_', ' ').title() or 'Unknown')


def leadership_label(lead):
    return LEADERSHIP_LABEL.get(lead, (lead or '').replace('_', ' ').title() or 'Unknown')


# ── Range dynamics from the underlying bars ────────────────────────────────────
# All bar access is defensive: bars may lack high/low/close (test fixtures, or the
# Databento fallback feed), in which case the range/gamma logic degrades gracefully
# to "unknown" rather than raising — a market state is never worth a crash.

def _bar_range(b):
    hi, lo = b.get('high'), b.get('low')
    if hi is None or lo is None:
        return None
    return float(hi) - float(lo)


def _range_ratio(bars, window):
    """recent-window mean bar range ÷ prior-window mean bar range, or None if there
    aren't 2×window usable bars. >1 = expanding, <1 = contracting."""
    if not bars or len(bars) < 2 * window:
        return None
    def _rng(bs):
        vals = [r for r in (_bar_range(b) for b in bs) if r is not None]
        return fmean(vals) if vals else 0.0
    prior = _rng(bars[-2 * window:-window])
    recent = _rng(bars[-window:])
    if prior <= 0:
        return None
    return recent / prior


def _directional(bars, window, direction):
    """True if close moved `window` bars ago → now in the signal's direction."""
    if not bars or len(bars) < window + 1:
        return False
    a, b = bars[-1 - window].get('close'), bars[-1].get('close')
    if a is None or b is None:
        return False
    move = float(b) - float(a)
    return move > 0 if direction == 'BULLISH' else move < 0


# ── Market state ───────────────────────────────────────────────────────────────

def _market_state(sig, bars, direction, trend_dir, trend_working):
    # 1) Reversal — the signal itself is a confirmed flow reversal / countertrend.
    tags = " ".join(str(sig.get(k) or '') for k in
                    ('signal_shape', 'flow_shape', 'signal_context')).upper()
    if 'REVERSAL' in tags:
        return REVERSAL
    # 2) Breakout / breakdown — price accepted beyond a primary level.
    la = (sig.get('level_action') or '').upper()
    if 'BREAKOUT' in la or 'BREAKDOWN' in la:
        return BREAKOUT
    # 3) Trend expansion — range widening AND price moving in the signal direction,
    #    or a leadership-confirmed still-working trend on the same side.
    w = config.MARKET_STATE_RANGE_WINDOW
    ratio = _range_ratio(bars, w)
    expanding = ratio is not None and ratio >= config.MARKET_STATE_EXPANSION_MULT
    if (expanding and _directional(bars, w, direction)) or (trend_dir == direction and trend_working):
        return TREND_EXPANSION
    # 4) Compression — range coiling (contracted vs the prior window).
    if ratio is not None and ratio <= config.MARKET_STATE_COMPRESSION_MULT:
        return COMPRESSION
    # 5) Default — something changed but it isn't yet expansion/coil/break/reversal.
    return TRANSITION


# ── Leadership type ────────────────────────────────────────────────────────────

def _gamma_ramp(sig, bars, quotes, direction):
    """A gamma ramp: an accelerating directional move (each of the last N bar ranges
    larger than the previous, in the signal direction) into a strike carrying near-peak
    same-side gamma. Requires acceleration so it does not swallow every ATM entry."""
    if not config.GAMMA_LEADERSHIP_ENABLED or not quotes or not bars:
        return False
    n = config.GAMMA_RAMP_ACCEL_BARS
    if len(bars) < n + 1:
        return False
    ranges = [_bar_range(b) for b in bars[-n:]]
    if any(r is None for r in ranges):
        return False
    if not all(ranges[i] > ranges[i - 1] for i in range(1, len(ranges))):
        return False
    if not _directional(bars, n, direction):
        return False
    side = sig.get('option_type')
    tstrike = sig.get('traded_strike') or sig.get('level_price')
    if tstrike is None:
        return False
    gammas = [(float(k[0]), float(q.get('gamma') or 0.0))
              for k, q in quotes.items() if k[1] == side]
    peak = max((g for _, g in gammas), default=0.0)
    if peak <= 0:
        return False
    traded_g = next((g for s, g in gammas if abs(s - float(tstrike)) < 1e-6), 0.0)
    return traded_g >= config.GAMMA_PEAK_RATIO * peak


def _leadership_type(sig, bars, quotes, direction):
    # Gamma ramp is the strongest mechanic when present (selective — needs acceleration).
    if _gamma_ramp(sig, bars, quotes, direction):
        return GAMMA_LEADER
    ctx = (sig.get('signal_context') or '').upper()
    shape = (sig.get('signal_shape') or '').upper()
    if 'CHAIN' in ctx or 'CHAIN' in shape:
        return CHAIN_LEADER
    # A named primary OI level (R1..S3) — emergent chain entries use 'EMERGENT' and are
    # already caught above, so a real rank label here means a primary-level entry.
    if sig.get('level_label') and (sig.get('level_label') or '').upper() != 'EMERGENT':
        return PRIMARY_LEVEL
    return VOLUME_LEADER


# ── Why it triggered ───────────────────────────────────────────────────────────

def _reasons(sig, state, lead, direction):
    side = sig.get('option_type')
    who = 'Calls' if side == 'CALL' else 'Puts'
    lvl = (sig.get('level_label') or '').strip()
    if lvl.upper() == 'EMERGENT':      # emergent chain locations have no rank label
        lvl = ''
    out = []

    # Leadership — what took control.
    if lead == CHAIN_LEADER:
        n = len(sig.get('chain_strikes') or [])
        out.append(f"{who} became chain leader" + (f" across {n} strikes" if n else ""))
    elif lead == GAMMA_LEADER:
        out.append(f"{who} drove a gamma ramp (accelerating move)")
    elif lead == PRIMARY_LEVEL:
        out.append(f"{who} led at primary level {lvl}".rstrip())
    else:
        out.append(f"{who} took volume leadership")

    # Level interaction — what price did at the level.
    la = (sig.get('level_action') or '').upper()
    lt = (sig.get('level_type') or '').upper()
    if 'BREAKOUT' in la:
        out.append(f"Spot broke out above {lvl}".rstrip())
    elif 'BREAKDOWN' in la:
        out.append(f"Spot broke down below {lvl}".rstrip())
    elif 'BOUNCE' in la or lt == 'SUPPORT':
        out.append(f"Spot reclaimed support {lvl}".rstrip())
    elif 'REJECTION' in la or lt == 'RESISTANCE':
        out.append(f"Spot rejected at resistance {lvl}".rstrip())

    if sig.get('trigger_volume'):
        out.append("Volume expanded")
    if sig.get('premium_notional'):
        out.append("Premium exceeded threshold")

    # State transition — the coil/expansion/turn context.
    if state == TREND_EXPANSION:
        out.append("Range expanded in the move direction")
    elif state == COMPRESSION:
        out.append("Range compressed into a coil")
    elif state == TRANSITION:
        out.append("Leadership transitioned sides")
    elif state == REVERSAL:
        out.append("Opposite-side flow took control")

    if sig.get('pds_class') in ('VIRGIN_DISCOVERY', 'FRESH_ACCUMULATION'):
        out.append("Fresh premium discovery (not recycled)")
    ld = sig.get('low_dist')
    if ld is not None and ld <= config.CLOW_STRONG_MAX:
        out.append("Entered near contract value low")
    return out


# ── Public entry point ─────────────────────────────────────────────────────────

def classify(sig, *, bars=None, quotes=None, trend_dir=None, trend_working=False):
    """
    Stamp sig['market_state'], sig['leadership_type'], sig['trigger_reasons'] and
    return them. Deterministic; safe to call with missing context (falls back to
    TRANSITION / VOLUME_LEADER and a still-useful reason list).
    """
    direction = sig.get('signal_type')
    bars = bars or []
    quotes = quotes or {}
    state = _market_state(sig, bars, direction, trend_dir, trend_working)
    lead = _leadership_type(sig, bars, quotes, direction)
    reasons = _reasons(sig, state, lead, direction)
    sig['market_state'] = state
    sig['leadership_type'] = lead
    sig['trigger_reasons'] = reasons
    return {'market_state': state, 'leadership_type': lead, 'trigger_reasons': reasons}
