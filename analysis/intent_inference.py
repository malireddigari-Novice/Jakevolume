"""
Position Intent Inference — §34-§36.

Without trade-side data (who is the aggressor?) exact intent cannot be known.
These functions estimate probabilities from observable intraday signals using the
starting hypotheses in spec §35, scored with a simple weighted-evidence model.
The Bayesian layer (updating base rates from history) is a future enhancement
once sufficient session data has accumulated (~100+ sessions recommended).

Public API
----------
classify_intent(...)          : §34 — rule-based intent estimate for one event
compute_lifecycle_pairs(rows) : §36 — match open/close event pairs for a contract
"""
import logging

logger = logging.getLogger(__name__)

# ── Session constants ─────────────────────────────────────────────────────────
_SESSION_OPEN_HOUR  = 8
_SESSION_OPEN_MIN   = 30
_SESSION_TOTAL_MINS = 390      # 08:30 → 15:00 CST

# All valid intent labels (§34)
INTENT_LABELS = (
    'OPENING_CALL_BUYING',  'OPENING_CALL_SELLING',
    'CLOSING_CALL_BUYING',  'CLOSING_CALL_SELLING',
    'OPENING_PUT_BUYING',   'OPENING_PUT_SELLING',
    'CLOSING_PUT_BUYING',   'CLOSING_PUT_SELLING',
    'MIXED_OR_UNKNOWN',
)


def _tod_frac(event_time) -> float:
    """Fraction of the trading session elapsed at event_time (0 = open, 1 = close)."""
    try:
        from data.market_utils import CST
        et = event_time.astimezone(CST)
    except Exception:
        et = event_time
    mins = (et.hour - _SESSION_OPEN_HOUR) * 60 + (et.minute - _SESSION_OPEN_MIN)
    return max(0.0, min(1.0, mins / _SESSION_TOTAL_MINS))


def classify_intent(
    option_type: str,
    low_dist: float | None,
    high_ratio: float | None,
    volume_shape: str | None,
    event_type: str | None,
    time_of_day_frac: float,
) -> dict:
    """
    §34/§35 Rule-based position intent estimate for one volume event.

    Parameters
    ----------
    option_type       : 'CALL' or 'PUT'
    low_dist          : mark / session_low_so_far; 1.0 = contract at its low
                        (same definition as flow_reversal low_dist)
    high_ratio        : mark / session_high_so_far; 1.0 = contract at its high
    volume_shape      : §27 classification (ISOLATED_SPIKE | COMPACT_CLUSTER |
                        STAIRCASE | DISTRIBUTED | None)
    event_type        : §32 event type (SINGLE_PRINT | CLUSTER | STAIRCASE | None)
    time_of_day_frac  : 0.0 = session open, 1.0 = session close

    Returns
    -------
    dict with keys:
        live_intent            : one of INTENT_LABELS
        intent_probability     : float [0, 1]
        intent_confidence      : 'HIGH' | 'MEDIUM' | 'LOW'
        supporting_evidence    : str | None
        contradicting_evidence : str | None
        time_of_day_frac       : float (echoed for storage)
        high_ratio             : float | None (echoed for storage)
    """
    supporting:     list[str] = []
    contradicting:  list[str] = []

    # ── Opening vs Closing signals ────────────────────────────────────────────
    open_w  = 0.0
    close_w = 0.0

    if low_dist is not None:
        if low_dist <= 1.10:
            open_w  += 2.0
            supporting.append(f'very_near_contract_low(dist={low_dist:.2f})')
        elif low_dist <= 1.25:
            open_w  += 1.0
            supporting.append(f'contract_low_nearby(dist={low_dist:.2f})')
        elif low_dist >= 2.00:
            close_w += 2.0
            supporting.append(f'premium_far_above_low(dist={low_dist:.2f})')
        elif low_dist >= 1.50:
            close_w += 1.0
            supporting.append(f'premium_elevated(dist={low_dist:.2f})')

    if high_ratio is not None:
        if high_ratio >= 0.90:
            close_w += 2.0
            supporting.append(f'near_session_high(ratio={high_ratio:.2f})')
        elif high_ratio >= 0.75:
            close_w += 1.0
            supporting.append(f'approaching_session_high(ratio={high_ratio:.2f})')
        elif high_ratio <= 0.30:
            open_w  += 1.0
            supporting.append(f'far_below_session_high(ratio={high_ratio:.2f})')

    if time_of_day_frac >= 0.80:
        close_w += 1.5
        supporting.append('late_session(>=80pct)')
    elif time_of_day_frac >= 0.65:
        close_w += 0.5
        supporting.append('later_session')
    elif time_of_day_frac <= 0.20:
        open_w  += 1.0
        supporting.append('early_session(<=20pct)')

    if event_type == 'STAIRCASE' or volume_shape == 'STAIRCASE':
        open_w  += 1.0
        supporting.append('staircase_accumulation')
    elif event_type == 'SINGLE_PRINT':
        # Single decisive print — slightly favors opening (decisive entry)
        open_w  += 0.5
        supporting.append('single_print_decisive_entry')

    # ── Buying vs Selling signals ─────────────────────────────────────────────
    buy_w  = 0.5    # slight prior: retail options volume skews to buying
    sell_w = 0.0

    if low_dist is not None and low_dist <= 1.25:
        buy_w  += 2.0   # accumulation near low = buyers
        # already noted above

    if high_ratio is not None:
        if high_ratio >= 0.90:
            # Near session high: ambiguous — could be closing buyers taking profit
            # OR opening sellers initiating. Lean slightly to closing buyers.
            buy_w  += 0.5
            sell_w += 0.5
            contradicting.append('near_session_high_ambiguous_buy_vs_sell')
        elif high_ratio >= 0.75:
            # Elevated premium — opening sellers slightly more consistent
            sell_w += 0.5

    if volume_shape == 'STAIRCASE':
        buy_w  += 0.5   # gradual staircase = patient accumulation
    elif volume_shape == 'DISTRIBUTED':
        # Spread-out volume = less conviction; slightly more consistent with selling
        sell_w += 0.3
        contradicting.append('distributed_volume_low_conviction')

    # ── Resolve intent class ──────────────────────────────────────────────────
    total_oc = open_w + close_w
    p_open   = open_w / max(total_oc, 0.001)

    total_bs = buy_w + sell_w
    p_buy    = buy_w / max(total_bs, 0.001)

    is_opening = p_open >= 0.60
    is_closing = p_open <= 0.40
    is_buying  = p_buy  >= 0.60

    # Require at least some evidence before committing to a direction
    if not is_opening and not is_closing or total_oc < 0.5:
        intent = 'MIXED_OR_UNKNOWN'
        prob   = 0.5
        conf   = 'LOW'
    else:
        oc     = 'OPENING' if is_opening else 'CLOSING'
        action = 'BUYING'  if is_buying  else 'SELLING'
        intent = f'{oc}_{option_type}_{action}'

        # Probability = weighted combination of directional certainties
        oc_certainty = abs(p_open - 0.5) * 2     # 0=uncertain, 1=certain
        bs_certainty = abs(p_buy  - 0.5) * 2
        prob = round(0.60 * oc_certainty + 0.40 * bs_certainty, 4)
        prob = max(0.50, min(0.95, prob))          # clip to [0.50, 0.95]

        avg_cert = (oc_certainty + bs_certainty) / 2
        conf = ('HIGH' if avg_cert >= 0.60 else
                'MEDIUM' if avg_cert >= 0.35 else 'LOW')

    return {
        'live_intent':             intent,
        'intent_probability':      round(prob, 4),
        'intent_confidence':       conf,
        'supporting_evidence':     '; '.join(supporting) or None,
        'contradicting_evidence':  '; '.join(contradicting) or None,
        'time_of_day_frac':        round(time_of_day_frac, 4),
        'high_ratio':              round(high_ratio, 4) if high_ratio is not None else None,
    }


def compute_lifecycle_pairs(oi_event_rows: list[dict]) -> list[dict]:
    """
    §36 Position Lifecycle — pair opening and closing events for the same contract.

    oi_event_rows: list of dicts, each with keys:
        id, symbol, session_date, occ_symbol, strike, option_type, expiry,
        event_time, trigger_volume, mark_at_event, low_dist, high_ratio,
        live_intent, intent_probability, maximum_contract_price, minimum_contract_price

    Returns list of position_lifecycle dicts (one per matched or unmatched opening event).
    Contracts with only one event produce a lifecycle with close fields = None.
    """
    from collections import defaultdict

    # Group events by contract (symbol, strike, option_type)
    by_contract: dict = defaultdict(list)
    for row in oi_event_rows:
        key = (row['symbol'], float(row.get('strike') or 0), row.get('option_type'))
        by_contract[key].append(row)

    lifecycles: list[dict] = []
    for (sym, strike, otype), events in by_contract.items():
        events_sorted = sorted(events, key=lambda e: e['event_time'])

        # Separate opening / closing candidates
        opens  = [e for e in events_sorted if (e.get('live_intent') or '').startswith('OPENING')]
        closes = [e for e in events_sorted if (e.get('live_intent') or '').startswith('CLOSING')]

        if not opens:
            continue  # No opening event identified — skip lifecycle for this contract

        open_ev = opens[0]  # Earliest opening event

        # Find the best closing event: a CLOSING_* event after the open, or the last event if ambiguous
        close_ev = None
        for c in closes:
            if c['event_time'] > open_ev['event_time']:
                close_ev = c
                break
        if close_ev is None and len(events_sorted) >= 2:
            # Fallback: use last event as probable close (even if unclassified)
            last = events_sorted[-1]
            if last['event_time'] > open_ev['event_time']:
                close_ev = last

        open_price  = open_ev.get('mark_at_event')
        close_price = close_ev.get('mark_at_event') if close_ev else None

        lifecycle_ret = None
        if open_price and close_price and open_price > 0:
            lifecycle_ret = round((close_price / open_price - 1) * 100, 2)

        # Confidence: HIGH if we have a confirmed pair + strong open intent
        open_conf = open_ev.get('intent_confidence', 'LOW')
        if close_ev:
            conf = open_conf if open_conf == 'HIGH' else 'MEDIUM'
        else:
            conf = 'LOW'

        # Session high/low across all events for this contract
        prices = [e.get('mark_at_event') for e in events_sorted if e.get('mark_at_event')]
        max_price = max(prices) if prices else None
        min_price = min(prices) if prices else None

        lifecycles.append({
            'symbol':                sym,
            'session_date':          open_ev['session_date'],
            'occ_symbol':            open_ev.get('occ_symbol'),
            'strike':                strike,
            'option_type':           otype,
            'expiry':                open_ev.get('expiry'),
            'open_event_id':         open_ev.get('id'),
            'probable_open_time':    open_ev['event_time'],
            'probable_open_price':   open_price,
            'probable_open_volume':  open_ev.get('trigger_volume'),
            'probable_position_type': open_ev.get('live_intent'),
            'opening_probability':   open_ev.get('intent_probability'),
            'maximum_contract_price': max_price,
            'minimum_contract_price': min_price,
            'close_event_id':        close_ev.get('id') if close_ev else None,
            'probable_close_time':   close_ev['event_time'] if close_ev else None,
            'probable_close_price':  close_price,
            'probable_close_volume': close_ev.get('trigger_volume') if close_ev else None,
            'closing_probability':   close_ev.get('intent_probability') if close_ev else None,
            'confirmed_oi_change':   None,
            'lifecycle_return_pct':  lifecycle_ret,
            'confidence':            conf,
        })

    return lifecycles
