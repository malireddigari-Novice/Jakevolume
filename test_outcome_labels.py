"""Unit checks for daily_review._outcome_labels (objective outcome labels §20-§24)."""
from datetime import datetime, timedelta
from analysis.daily_review import _outcome_labels

t0 = datetime(2026, 6, 12, 10, 0)
def path(bars):
    # bars: list of (minute_offset, high, low, close); prepend the entry bar at t0.
    out = [(t0, 1.0, 1.0, 1.0)]
    for m, h, l, c in bars:
        out.append((t0 + timedelta(minutes=m), h, l, c))
    return out

# 1) Winner: +50% reached before -35%, within 30m -> entry_success.
lab = _outcome_labels(t0, path([(2, 1.20, 0.98, 1.15), (5, 1.60, 1.10, 1.55), (40, 3.0, 1.0, 2.8)]))
assert lab['entry_success'] and not lab['false_positive'] and lab['reached_100pct'], lab
print(f"PASS  winner -> entry_success (mfe={lab['mfe_pct']}% r5={lab['return_5m']}% reached100={lab['reached_100pct']})")

# 2) False positive: hits -35% before +25% within 30m.
lab = _outcome_labels(t0, path([(2, 1.10, 0.90, 0.95), (5, 1.00, 0.60, 0.62), (20, 0.8, 0.4, 0.5)]))
assert lab['false_positive'] and not lab['entry_success'], lab
print(f"PASS  false positive (mae={lab['mae_pct']}% fp={lab['false_positive']})")

# 3) Peak arrives AFTER 30m -> not entry_success even though mfe is high.
lab = _outcome_labels(t0, path([(5, 1.10, 0.95, 1.05), (45, 2.0, 0.9, 1.9)]))
assert not lab['entry_success'] and lab['mfe_pct'] >= 50, lab
print(f"PASS  late peak -> not success (mfe={lab['mfe_pct']}% entry_success={lab['entry_success']})")

print("\nAll outcome-label checks passed.")
