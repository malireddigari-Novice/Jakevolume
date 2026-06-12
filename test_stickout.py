"""Unit checks for analysis.volume_stickout.compute_stickout."""
from analysis.volume_stickout import compute_stickout

def _mk(cur, prior, **kw):
    """Helper: build inputs from a current vol + prior-vol list (oldest→newest, excl current)."""
    hist = list(prior) + [cur]
    return compute_stickout(
        current_vol=cur, prior_vols=prior, session_vols=prior,
        win5=sum(hist[-5:]), last5_vols=hist[-5:],
        prior5m_windows=[sum(hist[i:i+5]) for i in range(0, max(0, len(hist)-5))],
        contract_low_distance=kw.get('cld', 1.2), symbol=kw.get('symbol', 'AAPL'))

# 1) Hard floor: tiny volume blocked regardless of ratio (AMZN vol 43 example).
r = _mk(43, [2, 1, 0, 3, 2, 1, 0, 0])
assert not r['valid'] and r['reason'] == 'BELOW_VOLUME_FLOOR', r
print("PASS  tiny volume (43) -> BELOW_VOLUME_FLOOR")

# 2) MSFT vol 26 ratio 1.2x style -> blocked by floor.
r = _mk(26, [20, 22, 18, 25, 21])
assert not r['valid'], r
print("PASS  small volume (26) -> not valid")

# 3) Genuine right-tail spike on a quiet contract: big bar vs small history, near low.
r = _mk(1200, [10, 5, 8, 12, 6, 9, 4, 7, 11, 3], cld=1.2)
assert r['valid'] and r['right_tail_ok'], r
assert r['visual_dom'] >= 1.0
print(f"PASS  1200 vs quiet prior -> valid (score={r['score']}, visual_dom={r['visual_dom']})")

# 4) Right-tail guard: decent score but NOT exceeding recent max and percentile low -> blocked.
#    current 300 but prior bars are larger (max 900) and many, so not right-tail.
prior = [900, 850, 800, 700, 600, 500, 400, 350, 300, 250,
         900, 800, 700, 600, 500, 400, 300, 250, 900, 800]
r = _mk(300, prior, cld=1.2)
assert not r['valid'], r            # visual_dom = 300/900 < 1, percentile low -> not right tail
print(f"PASS  300 below recent max (right-tail fail) -> not valid (vdom={r['visual_dom']}, pct={r['session_pctile']})")

# 5) NVDA higher floor: 300 (would pass default) is still below NVDA floor when window thin.
r = _mk(300, [5, 4, 6, 3], symbol='NVDA')   # cur 300<? no, 300>=250 floor 'cur' -> passes floor
assert r['reason'] != 'BELOW_VOLUME_FLOOR'
r2 = _mk(120, [2, 1, 0, 3], symbol='NVDA')  # 120<250 and win5<600 -> floor block
assert not r2['valid'] and r2['reason'] == 'BELOW_VOLUME_FLOOR', r2
print("PASS  NVDA floor (120) -> BELOW_VOLUME_FLOOR")

print("\nAll volume-stickout unit checks passed.")
