"""
Counterfactual: would the V2 premium + price confirmation layers have blocked the
penny-option flips that got the reversal engine disabled?

Replays each historical flow_reversals row against the two new layers using REAL fetched
data (the taking-control contract's 1-min premium trajectory + the underlying's VWAP):

  Premium: opp mark at flip must be >= streak-low mark * (1 + REVERSAL_PREMIUM_EXPANSION_PCT).
  Price:   VWAP loss for a call->put flip / VWAP reclaim for a put->call flip.

New engine flips only if BOTH pass (they default on). Read-only.
Run:  python replay_reversal_confirmation.py
"""
import sys
from datetime import timedelta

sys.path.insert(0, r"C:\Users\malir\Projects\Python\Jakevolume")
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass
import config
import psycopg2
from psycopg2.extras import RealDictCursor
from data.alpaca_data_client import AlpacaDataClient

PENNY = 0.20          # hypo entry below this = a penny flip we want to avoid
WIN_MIN = config.REVERSAL_WINDOW_MIN


def _conn():
    return psycopg2.connect(host=config.DB_HOST, port=config.DB_PORT, dbname=config.DB_NAME,
                            user=config.DB_USER, password=config.DB_PASSWORD)


def _iso_utc_open(dt):
    # day's ~08:00 CST as UTC (CDT = UTC-5): 13:00Z
    return dt.strftime('%Y-%m-%dT13:00:00Z')


def premium_trajectory(c, occ, detected_at):
    """Streak-low and at-flip premium for the taking-control contract over the reversal
    window ending at the flip. Returns (streak_low, mark_at_flip) or (None, None)."""
    try:
        bars = c._option_bars(occ, '1Min', _iso_utc_open(detected_at))
    except Exception:
        return None, None
    lo, hi = detected_at - timedelta(minutes=WIN_MIN), detected_at + timedelta(minutes=1)
    win = [b for b in bars if b.get('bar_time') and lo <= b['bar_time'] <= hi and (b.get('close') or 0) > 0]
    if not win:
        return None, None
    streak_low = min(b['close'] for b in win)
    mark_at_flip = win[-1]['close']
    return streak_low, mark_at_flip


def underlying_vwap(c, symbol, detected_at):
    """Session VWAP up to the flip, from Alpaca 1-min stock bars."""
    j = c._get('https://data.alpaca.markets', f'/v2/stocks/{symbol}/bars',
               {'timeframe': '1Min', 'start': _iso_utc_open(detected_at),
                'end': detected_at.astimezone(__import__('pytz').UTC).strftime('%Y-%m-%dT%H:%M:%SZ'),
                'feed': 'sip', 'limit': 500}) or {}
    num = den = 0.0
    for b in j.get('bars', []):
        v = b.get('v') or 0
        tp = (b['h'] + b['l'] + b['c']) / 3.0
        num += tp * v; den += v
    return round(num / den, 4) if den > 0 else None


def main():
    c = AlpacaDataClient()
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM flow_reversals ORDER BY detected_at")
        flips = cur.fetchall()
    conn.close()

    print(f"Replaying {len(flips)} historical flips against V2 premium+price layers "
          f"(expansion>={config.REVERSAL_PREMIUM_EXPANSION_PCT:.0%}, VWAP price)\n")
    blocked_penny = kept_real = penny_total = 0
    for f in flips:
        occ, spot, det = f['hypo_occ'], float(f['spot']), f['detected_at']
        entry = float(f['hypo_entry_price'])
        is_penny = entry < PENNY
        penny_total += is_penny

        slow, mark = premium_trajectory(c, occ, det)
        prem_ok = bool(slow and mark and mark >= slow * (1 + config.REVERSAL_PREMIUM_EXPANSION_PCT))
        vwap = underlying_vwap(c, f['symbol'], det)
        if vwap is None:
            price_ok = None
        else:
            price_ok = (spot < vwap) if f['to_side'] == 'PUT' else (spot > vwap)
        new_flips = bool(prem_ok and price_ok)

        tag = 'PENNY' if is_penny else 'real '
        verdict = 'FLIP' if new_flips else 'BLOCK'
        if is_penny and not new_flips:
            blocked_penny += 1
        if (not is_penny) and new_flips:
            kept_real += 1
        pm = f"prem {slow}->{mark} ({'ok' if prem_ok else 'no'})" if slow else "prem n/a"
        pr = f"vwap {vwap} spot {spot} ({'ok' if price_ok else 'no' if price_ok is not None else 'n/a'})"
        print(f"  [{tag}] {det.strftime('%m-%d %H:%M')} {f['from_side']}->{f['to_side']} "
              f"{occ} ${entry:.2f}  ->  NEW: {verdict}   {pm} · {pr}")

    print(f"\nSummary: {penny_total} penny flips, {blocked_penny} of them BLOCKED by the new layers; "
          f"{kept_real} real flips KEPT.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
