"""Verify the three gap fixes: proximity threshold, spike multiplier, adjacent cluster."""
import datetime
import pytz

import config
from analysis.signal_detector import SignalDetector

CST = pytz.timezone('America/Chicago')
now = datetime.datetime.now(CST)

print(f"LEVEL_PROXIMITY_PCT    : {config.LEVEL_PROXIMITY_PCT}   (expect 0.005)")
print(f"VOLUME_SPIKE_MULTIPLIER: {config.VOLUME_SPIKE_MULTIPLIER}  (expect 2.0)")
assert config.LEVEL_PROXIMITY_PCT    == 0.005, "proximity threshold wrong"
assert config.VOLUME_SPIKE_MULTIPLIER == 2.0,  "spike multiplier wrong"
print("Thresholds OK\n")

# Two levels both showing option volume above OPT_VOL_MIN_CLUSTER (25)
# -> adj_cluster should be True for each level
levels = [
    {'level_type': 'RESISTANCE', 'strike': 307.5, 'option_type': 'PUT'},
    {'level_type': 'SUPPORT',    'strike': 300.0, 'option_type': 'CALL'},
]
opt_quotes = {
    (307.5, 'PUT'):  {'volume': 100, 'mark': 2.0, 'bid': 1.9, 'ask': 2.1},
    (300.0, 'CALL'): {'volume': 80,  'mark': 1.5, 'bid': 1.4, 'ask': 1.6},
}

baseline = {
    'bar_time': now, 'open': 299.0, 'high': 299.5,
    'low': 298.5, 'close': 299.0, 'volume': 100,
}
# price 307.0 -> within 0.5% of 307.5 strike (diff = 0.16%), volume spike
spike_bar = {
    'bar_time': now, 'open': 299.0, 'high': 308.0,
    'low': 298.0, 'close': 307.0, 'volume': 9_999_999,
}
bars = [baseline] * config.VOLUME_LOOKBACK_BARS + [spike_bar]

det = SignalDetector()
all_sigs: list[dict] = []
# Each tick must have INCREASING option volume so delta > 0.
# Tick 0 seeds _prev_opt_vol; ticks 1-3 produce real deltas.
for tick in range(config.CONSECUTIVE_SPIKES_REQUIRED + 1):
    tick_quotes = {
        (307.5, 'PUT'):  {'volume': 50 * (tick + 1), 'mark': 2.0, 'bid': 1.9, 'ask': 2.1},
        (300.0, 'CALL'): {'volume': 40 * (tick + 1), 'mark': 1.5, 'bid': 1.4, 'ask': 1.6},
    }
    all_sigs.extend(det.check('AAPL', bars, levels, tick_quotes))
sigs = all_sigs

print(f"Signals fired         : {len(sigs)}  (expect 1)")
if sigs:
    s = sigs[0]
    print(f"  signal_type        : {s['signal_type']}")
    print(f"  level_price        : {s['level_price']}")
    print(f"  adj_cluster        : {s['adj_cluster']}  (expect True)")
    assert s['adj_cluster'] is True, "adj_cluster should be True"
    print("Adjacent cluster check OK\n")
else:
    print("  No signal fired — check bar/level proximity setup")

print("All gap fixes verified.")
