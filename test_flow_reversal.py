"""Unit checks for the Flow Leadership Reversal Engine + V2 confirmation layers.

The reversal-confirmed exit now also requires (both default-on):
  - PREMIUM confirmation: the taking-control side's premium expands during the takeover
    (>= REVERSAL_PREMIUM_EXPANSION_PCT off the streak low — so it needs >=2 opp polls).
  - PRICE confirmation: the caller's price verdict (VWAP loss/reclaim) is True.
"""
import sys
from datetime import datetime, timedelta
sys.path.insert(0, r"C:\Users\malir\Projects\Python\Jakevolume")
from analysis.flow_reversal import volume_event, FlowReversalEngine

QUIET = [5] * 20
BURST = [5] * 15 + [200, 220, 210, 230, 240]
PERSIST = [100] * 20

# 1) Concentrated burst is a valid event.
ev = volume_event(BURST)
assert ev and ev['valid'] and ev['burst'] >= 5.0 and ev['share'] >= 0.60, ev
print(f"PASS  burst event valid (burst={ev['burst']} share={ev['share']} active={ev['active']})")

# 2) Quiet background -> not a valid event.
ev = volume_event(QUIET)
assert ev and not ev['valid'], ev
print("PASS  quiet background -> not valid")

# 3) Persistent broad flow -> rejected.
ev = volume_event(PERSIST)
assert ev and ev['persistent_bg'] and not ev['valid'], ev
print("PASS  persistent background -> rejected")

t0 = datetime(2026, 6, 12, 12, 0)
def items(hist, low_dist, mark):
    return [{'strike': 100.0, 'ev': volume_event(hist), 'low_dist': low_dist, 'mark': mark},
            {'strike': 102.5, 'ev': volume_event(hist), 'low_dist': low_dist, 'mark': mark - 0.1}]

# 4) PUT->CALL reversal that CONFIRMS: same fades, opp leads with EXPANDING premium and
#    price confirmation. Needs >=2 opp polls for premium expansion to register.
eng = FlowReversalEngine()
r = eng.evaluate('TSLA', 'PUT', items(BURST, 1.2, 1.0), items(QUIET, 3.0, 0.5), now=t0)
assert r['state'] == 'ACTIVE' and not r['reversal_confirmed'], r
# opp first appears (premium baseline set) — not yet confirmed (no expansion on poll 1)
r = eng.evaluate('TSLA', 'PUT', items(QUIET, 3.0, 0.5), items(BURST, 1.2, 1.00),
                 now=t0 + timedelta(minutes=11), price_confirmed=True)
assert not r['reversal_confirmed'] and not r['premium_confirmed'], r
# opp premium expands +20% off the baseline, price confirms -> CONFIRMED
r = eng.evaluate('TSLA', 'PUT', items(QUIET, 3.0, 0.5), items(BURST, 1.2, 1.20),
                 now=t0 + timedelta(minutes=12), price_confirmed=True)
assert r['reversal_confirmed'] and r['premium_confirmed'] and r['price_confirmed'], r
print(f"PASS  reversal CONFIRMED with premium expansion + price (opp_lead={r['opp_leadership']} "
      f"prem={r['premium_confirmed']} price={r['price_confirmed']})")

# 5) Same takeover but premium does NOT expand (opp mark flat) -> BLOCKED.
eng2 = FlowReversalEngine()
eng2.evaluate('AAPL', 'PUT', items(BURST, 1.2, 1.0), items(QUIET, 3.0, 0.5), now=t0)
eng2.evaluate('AAPL', 'PUT', items(QUIET, 3.0, 0.5), items(BURST, 1.2, 1.00),
              now=t0 + timedelta(minutes=11), price_confirmed=True)
r = eng2.evaluate('AAPL', 'PUT', items(QUIET, 3.0, 0.5), items(BURST, 1.2, 1.00),
                  now=t0 + timedelta(minutes=12), price_confirmed=True)
assert not r['reversal_confirmed'] and not r['premium_confirmed'], r
print("PASS  premium NOT expanding -> reversal blocked")

# 6) Premium expands but PRICE does not confirm (price_confirmed=False) -> BLOCKED.
eng3 = FlowReversalEngine()
eng3.evaluate('NVDA', 'PUT', items(BURST, 1.2, 1.0), items(QUIET, 3.0, 0.5), now=t0)
eng3.evaluate('NVDA', 'PUT', items(QUIET, 3.0, 0.5), items(BURST, 1.2, 1.00),
              now=t0 + timedelta(minutes=11), price_confirmed=False)
r = eng3.evaluate('NVDA', 'PUT', items(QUIET, 3.0, 0.5), items(BURST, 1.2, 1.20),
                  now=t0 + timedelta(minutes=12), price_confirmed=False)
assert not r['reversal_confirmed'] and r['premium_confirmed'] and not r['price_confirmed'], r
print("PASS  price NOT confirming -> reversal blocked")

# 7) Quiet opposite side -> never reverses.
eng4 = FlowReversalEngine()
eng4.evaluate('MSFT', 'CALL', items(BURST, 1.2, 1.0), items(QUIET, 3.0, 0.5), now=t0)
r = eng4.evaluate('MSFT', 'CALL', items(QUIET, 3.0, 0.5), items(QUIET, 3.0, 0.5),
                  now=t0 + timedelta(minutes=11), price_confirmed=True)
assert not r['reversal_confirmed'], r
print("PASS  quiet opposite side -> no reversal")

print("\nAll flow-reversal + V2 confirmation checks passed.")
