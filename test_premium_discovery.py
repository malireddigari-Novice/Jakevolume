"""
§13b Premium Discovery Score tests. Run: python test_premium_discovery.py

Covers the pure classifier (all five classes + the two GOOGL cases + edges) and the
gold_mode PDS gate integration (gate off/on, fresh vs recycled, chain-led, reversal
exemption, unknown-history in lenient vs strict mode).
"""
import sys
sys.path.insert(0, r"C:\Users\malir\Projects\Python\Jakevolume")
import config
from analysis import premium_discovery as pd
from analysis import gold_mode

fails = 0
def ck(name, cond):
    global fails
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails += 1

def bars(*specs):
    """specs: (low, high, volume) tuples → history bar dicts (close = midpoint)."""
    return [{'low': lo, 'high': hi, 'close': (lo + hi) / 2, 'volume': v}
            for (lo, hi, v) in specs]

# Pin PDS thresholds to their documented defaults so the test is independent of env.
config.PDS_BAND_PCT = 0.10
config.PDS_ABOVE_MARGIN = 0.15
config.PDS_VIRGIN_MAX_HIST_VOL = 500
config.PDS_EXHAUSTED_PCTILE = 0.85
config.PDS_RECYCLED_ABOVE_SHARE = 0.40
config.PDS_ACCEPTED_SHARE = 0.35
config.PDS_FRESH_MAX_PCTILE = 0.35
config.PDS_FRESH_MIN_EVENT_SHARE = 0.50
config.PDS_REQUIRE_HISTORY = False

# ── Pure classifier ───────────────────────────────────────────────────────────

# GOOGL 370C (BAD): heavy history at $4-6, alert at $3.55 — looks cheap to a min/max
# range gate but the premium was already discovered richer → recycled, not eligible.
c = pd.score(bars((3.0, 4.0, 800), (3.0, 4.0, 800), (3.0, 4.0, 800),
                  *[(4.0, 6.0, 4000)] * 20), mark=3.55, event_volume=1500)
ck("370C -> REPRICED_RECYCLED", c['pds_class'] == pd.REPRICED_RECYCLED)
ck("370C not eligible", c['eligible'] is False)
ck("370C looks cheap (pctile low) yet vol_above high",
   c['price_pctile'] < 0.35 and c['vol_above'] > 0.40)

# GOOGL 370P (GOOD): near lows, tiny prior volume, then ~2000 trade → first footprint.
p = pd.score(bars(*[(0.8, 1.1, 40)] * 12), mark=1.05, event_volume=2000)
ck("370P -> VIRGIN_DISCOVERY", p['pds_class'] == pd.VIRGIN_DISCOVERY)
ck("370P eligible", p['eligible'] is True)

# VIRGIN: near-zero cumulative participation regardless of price.
v = pd.score(bars((0.5, 0.7, 50), (0.5, 0.7, 50)), mark=0.6, event_volume=1000)
ck("low cum vol -> VIRGIN", v['pds_class'] == pd.VIRGIN_DISCOVERY and v['cum_vol'] <= 500)

# EXHAUSTED: enough history to be non-virgin, mark above the historical range.
e = pd.score(bars(*[(0.10, 0.20, 60)] * 12), mark=0.50, event_volume=1000)
ck("mark above range -> EXHAUSTED", e['pds_class'] == pd.EXHAUSTED and e['price_pctile'] >= 0.85)

# ACCEPTED: non-virgin, in-range, most volume sits AT the current premium band.
a = pd.score(bars(*[(0.18, 0.22, 30)] * 20, (0.40, 0.45, 8), (0.40, 0.45, 8), (0.40, 0.45, 8)),
             mark=0.20, event_volume=100)
ck("heavy vol at current band -> ACCEPTED_VALUE", a['pds_class'] == pd.ACCEPTED_VALUE)

# FRESH: non-virgin but prior volume sits BELOW current, mark cheap, event dominates.
f = pd.score(bars(*[(0.08, 0.12, 15)] * 40, (0.55, 0.60, 5), (0.55, 0.60, 5)),
             mark=0.20, event_volume=2000)
ck("cheap + event-dominant + little vol at/above -> FRESH_ACCUMULATION",
   f['pds_class'] == pd.FRESH_ACCUMULATION)
ck("FRESH eligible", f['eligible'] is True)
ck("FRESH event_share >= 0.50", f['event_share'] >= 0.50)

# Edges.
ck("empty history -> UNKNOWN dict",
   pd.score([], mark=1.0, event_volume=100)['pds_class'] == pd.UNKNOWN_INSUFFICIENT_HISTORY)
ck("no mark -> None", pd.score(bars((1, 2, 100)), mark=0, event_volume=100) is None)
ck("zero cum vol history still scores",
   pd.score(bars((1.0, 1.0, 0)), mark=1.0, event_volume=100)['pds_class'] == pd.VIRGIN_DISCOVERY)

# ── is_gold_eligible: only virgin/fresh; None/unknown non-blocking unless strict ──
ck("eligible: VIRGIN", pd.is_gold_eligible(pd.VIRGIN_DISCOVERY) is True)
ck("eligible: FRESH", pd.is_gold_eligible(pd.FRESH_ACCUMULATION) is True)
ck("not eligible: RECYCLED", pd.is_gold_eligible(pd.REPRICED_RECYCLED) is False)
ck("not eligible: ACCEPTED", pd.is_gold_eligible(pd.ACCEPTED_VALUE) is False)
ck("not eligible: EXHAUSTED", pd.is_gold_eligible(pd.EXHAUSTED) is False)

config.PDS_REQUIRE_HISTORY = False
ck("lenient: None non-blocking", pd.is_gold_eligible(None) is True)
ck("lenient: UNKNOWN non-blocking", pd.is_gold_eligible(pd.UNKNOWN_INSUFFICIENT_HISTORY) is True)
config.PDS_REQUIRE_HISTORY = True
ck("strict: None blocks", pd.is_gold_eligible(None) is False)
ck("strict: UNKNOWN blocks", pd.is_gold_eligible(pd.UNKNOWN_INSUFFICIENT_HISTORY) is False)
ck("strict: FRESH still passes", pd.is_gold_eligible(pd.FRESH_ACCUMULATION) is True)
config.PDS_REQUIRE_HISTORY = False

# ── gold_mode gate integration ────────────────────────────────────────────────

def gsig(**kw):
    s = {'gold_grade': 'GOLD', 'gold_subtype': gold_mode.GOLD_PRIMARY_LEVEL,
         'value_region': 'EXCELLENT_VALUE_REGION', 'clow_region': 'GOLD_VALUE_LOCATION',
         'signal_context': 'PRIMARY_LEVEL_CONTINUATION'}
    s.update(kw)
    return s

config.GOLD_ONLY_PRODUCTION_MODE = True
config.INTENT_VALIDATION_ENABLED = False
config.OPPOSITE_SIDE_VETO_ENABLED = False

# Gate OFF → PDS is SKIP; a recycled signal is NOT blocked by PDS.
config.PREMIUM_DISCOVERY_GATE_ENABLED = False
au = gold_mode.gate_audit(gsig(pds_class=pd.REPRICED_RECYCLED))
ck("gate off -> PDS SKIP", any(g['gate'] == 'PDS' and g['verdict'] == 'SKIP' for g in au['gates']))
ck("gate off -> recycled still allowed", gold_mode.production_allowed(gsig(pds_class=pd.REPRICED_RECYCLED)) is True)

# Gate ON.
config.PREMIUM_DISCOVERY_GATE_ENABLED = True
ck("gate on: FRESH -> allowed", gold_mode.production_allowed(gsig(pds_class=pd.FRESH_ACCUMULATION)) is True)
ck("gate on: VIRGIN -> allowed", gold_mode.production_allowed(gsig(pds_class=pd.VIRGIN_DISCOVERY)) is True)
ck("gate on: RECYCLED -> blocked", gold_mode.production_allowed(gsig(pds_class=pd.REPRICED_RECYCLED)) is False)
au = gold_mode.gate_audit(gsig(pds_class=pd.REPRICED_RECYCLED))
ck("gate on: recycled blocked AT PDS", au['blocking_gate'] == 'PDS' and au['decision'] == 'RESEARCH')
ck("gate on: ACCEPTED -> blocked", gold_mode.production_allowed(gsig(pds_class=pd.ACCEPTED_VALUE)) is False)
ck("gate on: EXHAUSTED -> blocked", gold_mode.production_allowed(gsig(pds_class=pd.EXHAUSTED)) is False)

# Unknown / unevaluated: lenient allows, strict blocks (covers chain-led with no history).
config.PDS_REQUIRE_HISTORY = False
ck("gate on lenient: no pds_class -> allowed", gold_mode.production_allowed(gsig()) is True)
config.PDS_REQUIRE_HISTORY = True
ck("gate on strict: no pds_class -> blocked", gold_mode.production_allowed(gsig()) is False)
config.PDS_REQUIRE_HISTORY = False

# Reversals are exempt (they carry their own activation proof).
au = gold_mode.gate_audit(gsig(gold_subtype=gold_mode.COUNTERTREND_REVERSAL,
                               signal_context='PRIMARY_LEVEL_COUNTERTREND_REVERSAL',
                               pds_class=pd.REPRICED_RECYCLED))
ck("reversal -> PDS SKIP (exempt)", any(g['gate'] == 'PDS' and g['verdict'] == 'SKIP' for g in au['gates']))
ck("reversal recycled still allowed",
   gold_mode.production_allowed(gsig(gold_subtype=gold_mode.COUNTERTREND_REVERSAL,
                                     signal_context='REVERSAL', pds_class=pd.REPRICED_RECYCLED)) is True)

# Audit summary renders the block point.
config.PREMIUM_DISCOVERY_GATE_ENABLED = True
s = gold_mode.audit_summary(gold_mode.gate_audit(gsig(pds_class=pd.REPRICED_RECYCLED)))
ck("summary shows blocked at PDS", 'PDS:FAIL' in s and 'blocked at PDS' in s)

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)
