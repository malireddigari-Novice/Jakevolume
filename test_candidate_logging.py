"""Unit check for §73 — every evaluated level is captured in detector.last_candidates."""
import logging; logging.disable(logging.CRITICAL)
from datetime import datetime
from data.market_utils import CST
from analysis.signal_detector import SignalDetector

d = SignalDetector()
bt = datetime(2026, 6, 15, 10, 0, tzinfo=CST)
bars = [{'bar_time': bt, 'open': 100, 'high': 100, 'low': 100, 'close': 100, 'volume': 1000}]
levels = [{'level_type': 'SUPPORT', 'rank': 1, 'strike': 100.0},      # near spot
          {'level_type': 'RESISTANCE', 'rank': 1, 'strike': 120.0}]   # 20% away
quotes = {(100.0, 'CALL'): {'volume': 40, 'mark': 1.0, 'bid': 0.9, 'ask': 1.1,
                            'open_interest': 100, 'day_low': 0.95}}

d.check('AAPL', bars, levels, quotes)
cands = {c['level_label']: c for c in d.last_candidates}
assert len(d.last_candidates) == 2, d.last_candidates
# near support, tiny volume -> recorded, blocked on volume, not fired
assert cands['S1']['near_level'] and cands['S1']['blocked_reason'].startswith('NO_VALID_VOLUME')
assert cands['S1']['candidate_side'] == 'CALL' and not cands['S1']['alert_fired']
# far resistance -> recorded as NOT_NEAR_LEVEL
assert not cands['R1']['near_level'] and cands['R1']['blocked_reason'] == 'NOT_NEAR_LEVEL'
assert all(not c['alert_fired'] for c in d.last_candidates)
print("PASS  §73: blocked + far candidates captured")
print(f"  S1: near={cands['S1']['near_level']} reason={cands['S1']['blocked_reason']}")
print(f"  R1: near={cands['R1']['near_level']} reason={cands['R1']['blocked_reason']}")

# Re-running resets the buffer (per-poll, no accumulation across polls)
d.check('AAPL', bars, levels, quotes)
assert len(d.last_candidates) == 2, "last_candidates must reset each poll"
print("PASS  §73: last_candidates resets per poll")
print("\nAll candidate-logging checks passed.")
