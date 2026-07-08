"""
Gold-only production gate (§1 / §18 / §19) — P1 foundation.

The single production chokepoint. classify() labels a fired signal with its Gold
subtype, grade, and value / contract-low regions; production_allowed() decides
whether it may create a Discord alert + paper trade.

When config.GOLD_ONLY_PRODUCTION_MODE is FALSE (the default), production_allowed()
is a pass-through, so behavior is identical to today. When TRUE, only Gold-graded
subtypes pass; everything else is stored research-only (§19).

Deeper Gold conditions are STAGED and their P1 hooks are non-blocking (return True),
marked TODO:
  - directional-intent validation (§5-§8)  → P2
  - opposite-side leadership veto (§9)      → P2
  - opening full-chain scan/story (§10-§11) → P3
  - exceptional single-strike Route B (§ July-1) → P3
  - same-direction upgrade / countertrend-strict (§14-§15) → P4
Reversals are handled by analysis/flow_reversal and are exempt from this gate.
"""
import logging

import config
from analysis.intent_validation import is_directional_demand

logger = logging.getLogger(__name__)

# Gold production subtypes (§1)
GOLD_PRIMARY_LEVEL    = 'GOLD_PRIMARY_LEVEL'
GOLD_CHAIN_LED        = 'GOLD_CHAIN_LED'
PRIMARY_AND_CHAIN     = 'PRIMARY_AND_CHAIN_CONFIRMED'
SAME_DIR_UPGRADE      = 'HIGH_CONVICTION_SAME_DIRECTION_UPGRADE'
COUNTERTREND_REVERSAL = 'CONFIRMED_COUNTERTREND_REVERSAL'
# P-BD primary-level continuation subtypes
GOLD_PRIMARY_BOUNCE_CALL   = 'GOLD_PRIMARY_BOUNCE_CALL'
GOLD_PRIMARY_REJECTION_PUT = 'GOLD_PRIMARY_REJECTION_PUT'
GOLD_PRIMARY_BREAKOUT_CALL = 'GOLD_PRIMARY_BREAKOUT_CALL'
GOLD_PRIMARY_BREAKDOWN_PUT = 'GOLD_PRIMARY_BREAKDOWN_PUT'
# P3 Route B exceptional single-strike
GOLD_EXCEPTIONAL_SINGLE_STRIKE_CALL = 'GOLD_EXCEPTIONAL_SINGLE_STRIKE_CALL'
GOLD_EXCEPTIONAL_SINGLE_STRIKE_PUT  = 'GOLD_EXCEPTIONAL_SINGLE_STRIKE_PUT'
_GOLD_SUBTYPES = {GOLD_PRIMARY_LEVEL, GOLD_CHAIN_LED, PRIMARY_AND_CHAIN,
                  SAME_DIR_UPGRADE, COUNTERTREND_REVERSAL,
                  GOLD_PRIMARY_BOUNCE_CALL, GOLD_PRIMARY_REJECTION_PUT,
                  GOLD_PRIMARY_BREAKOUT_CALL, GOLD_PRIMARY_BREAKDOWN_PUT,
                  GOLD_EXCEPTIONAL_SINGLE_STRIKE_CALL, GOLD_EXCEPTIONAL_SINGLE_STRIKE_PUT}
# Map a breakout.classify_interaction() result to its Gold subtype.
_INTERACTION_SUBTYPE = {
    'BOUNCE_CALL':   GOLD_PRIMARY_BOUNCE_CALL,
    'REJECTION_PUT': GOLD_PRIMARY_REJECTION_PUT,
    'BREAKOUT_CALL': GOLD_PRIMARY_BREAKOUT_CALL,
    'BREAKDOWN_PUT': GOLD_PRIMARY_BREAKDOWN_PUT,
}


def subtype_for_interaction(interaction: str):
    """Gold subtype for a breakout.classify_interaction() label, or None if not actionable."""
    return _INTERACTION_SUBTYPE.get(interaction)


# ── Value-location classifiers (§12 / §13) ────────────────────────────────────

def value_region(pctile):
    """§12 historical-value percentile → region label (None when unknown)."""
    if pctile is None:
        return None
    if pctile <= config.HV_REGION_EXCELLENT_MAX:
        return 'EXCELLENT_VALUE_REGION'
    if pctile <= config.HV_REGION_ACCEPTABLE_MAX:
        return 'ACCEPTABLE_VALUE_REGION'
    if pctile <= config.HV_REGION_NEUTRAL_MAX:
        return 'NEUTRAL_VALUE_REGION'
    return 'ELEVATED_VALUE_REGION'


def contract_low_region(clow):
    """§13 ContractLowDistance → region label (None when unknown)."""
    if clow is None:
        return None
    if clow <= config.CLOW_GOLD_MAX:
        return 'GOLD_VALUE_LOCATION'
    if clow <= config.CLOW_STRONG_MAX:
        return 'STRONG_VALUE_LOCATION'
    if clow <= config.CLOW_ACCEPTABLE_MAX:
        return 'ACCEPTABLE_ONLY_WITH_EXCEPTIONAL_EVIDENCE'
    return 'LIKELY_CHASED_OR_LATE'


def _subtype_from_context(sig) -> str:
    ctx = (sig.get('signal_context') or '').upper()
    if sig.get('upgrade'):
        return SAME_DIR_UPGRADE
    if 'CHAIN_LED' in ctx:
        return GOLD_CHAIN_LED
    if 'COUNTERTREND' in ctx or 'REVERSAL' in ctx:
        return COUNTERTREND_REVERSAL
    return GOLD_PRIMARY_LEVEL   # PRIMARY_LEVEL_CONTINUATION default


# ── Classification + gate ─────────────────────────────────────────────────────

def classify(sig) -> dict:
    """
    Annotate `sig` in place with Gold fields and return them:
      gold_subtype, gold_grade ('GOLD' | 'RESEARCH'), value_region, clow_region.

    P1 grade is structural + value-location quality only: acceptable historical-value
    region AND a non-chased contract-low region. Intent (P2), opposite-side veto (P2),
    and the opening story (P3) tighten this further in later phases.
    """
    subtype = _subtype_from_context(sig)
    # P-BD: a classified level interaction (bounce/rejection/breakout/breakdown)
    # refines the primary subtype when present.
    _mapped = subtype_for_interaction(sig.get('level_action'))
    if _mapped:
        subtype = _mapped
    vr = value_region(sig.get('hv_pctile'))
    cr = contract_low_region(sig.get('low_dist'))
    # Missing (None) regions are treated as non-blocking here — the existing
    # production gate already enforced hard value/low limits before this point.
    value_ok = vr in (None, 'EXCELLENT_VALUE_REGION', 'ACCEPTABLE_VALUE_REGION')
    clow_ok  = cr in (None, 'GOLD_VALUE_LOCATION', 'STRONG_VALUE_LOCATION')
    grade = 'GOLD' if (value_ok and clow_ok) else 'RESEARCH'

    sig['gold_subtype'] = subtype
    sig['value_region'] = vr
    sig['clow_region']  = cr
    sig['gold_grade']   = grade
    return {'gold_subtype': subtype, 'gold_grade': grade,
            'value_region': vr, 'clow_region': cr}


# ── P2 deeper checks — directional intent (§5-§8) + opposite-side veto (§9) ──
# Reversals are exempt (they carry their own activation proof via flow_reversal).
_REVERSAL_EXEMPT = {COUNTERTREND_REVERSAL}


def _intent_ok(sig) -> bool:
    """Require a validated LIKELY_DIRECTIONAL_*_DEMAND verdict (§8). Absent = not
    confirmed = blocked (default NO_TRADE). Reversals and disabled mode pass."""
    if not config.INTENT_VALIDATION_ENABLED:
        return True
    if sig.get('gold_subtype') in _REVERSAL_EXEMPT:
        return True
    return is_directional_demand(sig.get('intent_class'))


def _veto_ok(sig) -> bool:
    """Block when the opposite side shows dominant validated demand (§9)."""
    if not config.OPPOSITE_SIDE_VETO_ENABLED:
        return True
    return not sig.get('opp_veto')


def production_allowed(sig) -> bool:
    """
    §18 production gate.

    GOLD_ONLY_PRODUCTION_MODE off → pass-through (True): unchanged behavior.
    On → only a Gold-graded, recognized Gold subtype passes the P1-available
    conditions; everything else is research-only (§19).
    """
    if not config.GOLD_ONLY_PRODUCTION_MODE:
        return True
    if sig.get('gold_grade') != 'GOLD':
        return False
    if sig.get('gold_subtype') not in _GOLD_SUBTYPES:
        return False
    return _intent_ok(sig) and _veto_ok(sig)


def annotate_and_gate(sig) -> bool:
    """Classify (always) then return production_allowed; stamps sig['production_allowed']."""
    classify(sig)
    allowed = production_allowed(sig)
    sig['production_allowed'] = allowed
    return allowed


def merge(signals: list) -> list:
    """
    §4 — when the same trade qualified via BOTH the primary-level and chain-led paths
    (same symbol + direction + within one strike), emit ONE signal labeled
    PRIMARY_AND_CHAIN_CONFIRMED rather than two. In practice the one-per-direction
    dedup already collapses same-side duplicates, so this is usually a no-op; it exists
    so a genuine dual-qualification is relabeled, never double-alerted.
    """
    if not config.PRIMARY_CHAIN_MERGE_ENABLED or len(signals) < 2:
        return signals
    out, used = [], set()
    for i, a in enumerate(signals):
        if i in used:
            continue
        for j in range(i + 1, len(signals)):
            b = signals[j]
            if j in used:
                continue
            same = (a.get('signal_type') == b.get('signal_type')
                    and abs(float(a.get('traded_strike') or 0) - float(b.get('traded_strike') or 0)) <= 1e-6)
            ctxs = {(a.get('signal_context') or ''), (b.get('signal_context') or '')}
            is_pair = same and any('CHAIN_LED' in c.upper() for c in ctxs) \
                and any('PRIMARY' in c.upper() for c in ctxs)
            if is_pair:
                primary = a if 'PRIMARY' in (a.get('signal_context') or '').upper() else b
                primary['signal_context'] = PRIMARY_AND_CHAIN
                primary['gold_subtype'] = PRIMARY_AND_CHAIN
                used.add(j)
                logger.info("GOLD-MODE merge: %s %s -> PRIMARY_AND_CHAIN_CONFIRMED",
                            primary.get('symbol'), primary.get('signal_type'))
                a = primary
        out.append(a)
    return out
