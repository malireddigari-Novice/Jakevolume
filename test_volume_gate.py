"""Unit checks for the ENTRY VOLUME GATE FIX three-rule _eval_volume."""
from analysis.signal_detector import SignalDetector

D = SignalDetector()
EXC = 0.70  # excitation_min (stair-step)

def ev(symbol, prior, cur, low=1.2):
    """history = prior vols + current; returns the _eval_volume dict."""
    hist = list(prior) + [cur]
    return D._eval_volume(symbol, hist, cur, low, 300, 300, 3.0, EXC)

# 1) AMZN vol 43 → LOW_ABSOLUTE_VOLUME (tiny current, tiny window).
r = ev('AMZN', [2, 1, 0, 3, 2, 1], 43)
assert not r['valid'] and r['block_reason'] == 'LOW_ABSOLUTE_VOLUME', r
print(f"PASS  AMZN 43 -> not valid ({r['block_reason']})")

# 2) MSFT vol 26 → LOW_ABSOLUTE_VOLUME.
r = ev('MSFT', [20, 22, 18, 25, 21], 26)
assert not r['valid'] and r['block_reason'] == 'LOW_ABSOLUTE_VOLUME', r
print(f"PASS  MSFT 26 -> not valid ({r['block_reason']})")

# 3) Genuine single-bar spike near lows → SingleBarValid, trigger SINGLE_BAR.
r = ev('AAPL', [10, 5, 8, 12, 6, 9, 4, 7, 11, 3], 1200, low=1.2)
assert r['valid'] and r['A'] and r['trigger_type'] == 'SINGLE_BAR', r
assert r['trigger_volume'] == 1200
print(f"PASS  single-bar 1200 -> valid (A, trig={r['trigger_type']} vol={r['trigger_volume']} ratio={r['trigger_ratio']})")

# 4) Real 5-bar cluster, moderate current bar → ClusterValid, trigger FIVE_BAR_WINDOW.
#    prior bars quiet so prior 5-windows are small (high WindowRatio5 + dominance);
#    last 5 each >= active threshold and sum >= 300.
prior = [5, 4, 6, 3, 5, 4, 6, 3, 5, 4]
last5 = [90, 95, 88, 92, 96]                 # win5=461, 5 active bars
hist = prior + last5
r = D._eval_volume('AAPL', hist, last5[-1], 1.2, 300, 300, 3.0, EXC)
assert r['valid'] and r['B'] and r['trigger_type'] == 'FIVE_BAR_WINDOW', r
assert r['trigger_volume'] == sum(last5)
print(f"PASS  cluster -> valid (B, trig={r['trigger_type']} win5={r['trigger_volume']} ratio={r['trigger_ratio']})")

# 5) Cheap premium (low_dist good) but weak volume → NO alert (contract-low is not a signal).
r = ev('AAPL', [50, 55, 48, 60, 52], 70, low=1.05)
assert not r['valid'], r
print(f"PASS  cheap premium + weak vol -> not valid ({r['block_reason']})")

# 6) NVDA higher floor: current 200 (< 250) with small window → LOW_ABSOLUTE_VOLUME.
r = ev('NVDA', [3, 2, 4, 1, 2], 200)
assert not r['valid'] and r['block_reason'] == 'LOW_ABSOLUTE_VOLUME', r
print(f"PASS  NVDA 200 (<250 floor) -> not valid ({r['block_reason']})")

print("\nAll entry-volume-gate unit checks passed.")
