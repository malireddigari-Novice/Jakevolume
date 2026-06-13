"""Unit checks for the Flow Leadership Reversal Engine (V1)."""
from datetime import datetime, timedelta
from analysis.flow_reversal import volume_event, FlowReversalEngine

QUIET = [5] * 20                                  # flat low background -> no event
BURST = [5] * 15 + [200, 220, 210, 230, 240]      # quiet then a 5-bar burst -> valid event
PERSIST = [100] * 20                              # broad persistent flow -> rejected

# 1) Concentrated burst is a valid event.
ev = volume_event(BURST)
assert ev and ev['valid'] and ev['burst'] >= 5.0 and ev['share'] >= 0.60, ev
print(f"PASS  burst event valid (burst={ev['burst']} share={ev['share']} active={ev['active']})")

# 2) Quiet background -> not a valid event.
ev = volume_event(QUIET)
assert ev and not ev['valid'], ev
print(f"PASS  quiet background -> not valid (burst={ev['burst']} share={ev['share']})")

# 3) Persistent broad flow -> rejected even though volume is high.
ev = volume_event(PERSIST)
assert ev and ev['persistent_bg'] and not ev['valid'], ev
print(f"PASS  persistent background -> rejected (active_bg={ev['active_bg']} share={ev['share']})")

# 4) Full PUT->CALL reversal scenario.
eng = FlowReversalEngine()
t0 = datetime(2026, 6, 12, 12, 0)
def items(hist, low_dist):
    return [{'strike': 100.0, 'ev': volume_event(hist), 'low_dist': low_dist, 'mark': 1.0},
            {'strike': 102.5, 'ev': volume_event(hist), 'low_dist': low_dist, 'mark': 0.9}]

# t0: PUT (position side) has the burst; CALL (opp) quiet -> ACTIVE, seeds same peak.
r = eng.evaluate('TSLA', 'PUT', same_events=items(BURST, 1.2), opp_events=items(QUIET, 3.0), now=t0)
assert r['state'] == 'ACTIVE' and not r['reversal_confirmed'], r
print(f"PASS  t0: PUT active, no reversal (state={r['state']})")

# t0+11m: PUT now quiet (faded), CALL produces a dominant near-low burst across 2 strikes.
r = eng.evaluate('TSLA', 'PUT', same_events=items(QUIET, 3.0), opp_events=items(BURST, 1.2),
                 now=t0 + timedelta(minutes=11))
assert r['reversal_confirmed'], r
assert r['opp_type'] == 'CALL' and r['same_fading'] and r['opp_leadership'] >= 0.75, r
print(f"PASS  t0+11: CALL reversal CONFIRMED (opp_lead={r['opp_leadership']} "
      f"same_lead={r['same_leadership']} diff={r['leadership_diff']} fading={r['same_fading']})")

# 5) Mirror: a quiet opposite side does NOT trigger a reversal.
eng2 = FlowReversalEngine()
eng2.evaluate('AAPL', 'CALL', same_events=items(BURST, 1.2), opp_events=items(QUIET, 3.0), now=t0)
r = eng2.evaluate('AAPL', 'CALL', same_events=items(QUIET, 3.0), opp_events=items(QUIET, 3.0),
                  now=t0 + timedelta(minutes=11))
assert not r['reversal_confirmed'], r
print(f"PASS  quiet opposite side -> no reversal (state={r['state']})")

print("\nAll flow-reversal unit checks passed.")
