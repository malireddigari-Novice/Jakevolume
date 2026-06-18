"""
§16-17 — persistent positive/negative control libraries + regression runner.

A control is a frozen volume event with a known intended verdict. Positive controls
(e.g. NVDA 210P GOLD_STANDARD_LEVEL_REJECTION) MUST keep passing; negative controls
(spam) MUST keep being blocked. Every gate change re-runs `run_controls()` — the answer
to "would this rule still preserve NVDA 210P?" is now mechanical.

The gate verdict comes from the ONE canonical SignalDetector._eval_volume.

Usage:
    python gate_controls.py --seed     # insert the starter library (idempotent)
    python gate_controls.py            # run all controls, report PASS/FAIL
"""
import sys
import json

sys.path.insert(0, r"C:\Users\malir\Projects\Python\Jakevolume")
import config
import psycopg2
from psycopg2.extras import Json, RealDictCursor
from analysis.signal_detector import SignalDetector

_D   = SignalDetector()
_EXC = config.STAIRSTEP_EXCITATION_MIN

_COLS = ('control_type', 'control_label', 'symbol', 'strike', 'option_type', 'alert_time',
         'spot', 'level_label', 'level_price', 'entry_price', 'vols', 'observed_vol',
         'completed_vol', 'ratio', 'event_share', 'premium_notional', 'low_dist', 'is_atm',
         'next_day_mode', 'expected_pass', 'expected_path', 'expected_gold', 'target1',
         'target2', 'target1_reached', 'target2_reached', 'bid_mfe_pct', 'bid_mae_pct',
         'time_to_mfe_min', 'notes')


def _conn():
    return psycopg2.connect(host=config.DB_HOST, port=config.DB_PORT, dbname=config.DB_NAME,
                            user=config.DB_USER, password=config.DB_PASSWORD)


def add_control(**kw) -> None:
    """Upsert one control row (keyed by control_label + symbol + strike + alert_time)."""
    kw.setdefault('option_type', None)
    row = {c: kw.get(c) for c in _COLS}
    row['vols'] = Json(row['vols'])
    cols = ','.join(_COLS)
    ph   = ','.join(f'%({c})s' for c in _COLS)
    sql  = (f"INSERT INTO gate_controls ({cols}) VALUES ({ph}) "
            f"ON CONFLICT (control_label, symbol, strike, alert_time) DO UPDATE SET "
            + ','.join(f"{c}=EXCLUDED.{c}" for c in _COLS if c not in
                       ('control_label', 'symbol', 'strike', 'alert_time')))
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, row)
        conn.commit()
    finally:
        conn.close()


def seed_controls() -> None:
    """Insert the starter library — the NVDA 210P positive control + spam negatives."""
    quiet = [10] * 19
    # ── POSITIVE: NVDA 210P gold-standard level rejection (§16) ──
    add_control(
        control_type='POSITIVE', control_label='GOLD_STANDARD_LEVEL_REJECTION',
        symbol='NVDA', strike=210.0, option_type='PUT', alert_time='2026-06-17 08:51:00-05',
        spot=209.09, level_label='R1', level_price=210.0, entry_price=1.72,
        vols=quiet + [456], observed_vol=456, completed_vol=508, ratio=45.6,
        event_share=0.77, premium_notional=87376, low_dist=1.00, is_atm=True,
        next_day_mode=False, expected_pass=True, expected_path='B', expected_gold=True,
        target1=207.5, target2=205.0, target1_reached=True, target2_reached=True,
        bid_mfe_pct=300.0, bid_mae_pct=-10.0, time_to_mfe_min=20,
        notes='ATM==R1, spot at resistance, contract at its low, 45x, ~$86k notional, '
              'ran cleanly through both targets. The canonical preservation test.')
    # ── NEGATIVES (§17) ──
    add_control(  # thin volume, huge ratio off a near-zero baseline
        control_type='NEGATIVE', control_label='SPAM_THIN_HIGH_RATIO',
        symbol='AMZN', strike=247.5, option_type='PUT', alert_time='2026-01-01 09:00:00-05',
        spot=247.0, level_label='R1', level_price=247.5, entry_price=1.50,
        vols=[38] * 19 + [143], observed_vol=143, completed_vol=143, ratio=3.8,
        event_share=0.34, premium_notional=21450, low_dist=1.10, is_atm=True,
        next_day_mode=False, expected_pass=False, notes='143 contracts — below the 500 floor.')
    add_control(  # 444 @ 44x but completed bar still < 500
        control_type='NEGATIVE', control_label='SPAM_NEARMISS_COMPLETED',
        symbol='NVDA', strike=210.0, option_type='PUT', alert_time='2026-01-02 09:00:00-05',
        spot=209.0, level_label='R1', level_price=210.0, entry_price=1.72,
        vols=[10] * 19 + [444], observed_vol=444, completed_vol=444, ratio=44.4,
        event_share=0.76, premium_notional=76368, low_dist=1.05, is_atm=True,
        next_day_mode=False, expected_pass=False,
        notes='Completed bar 444 < 500 floor — must block (only passes if completed ≥500).')
    add_control(  # big volume but worthless premium → notional floor blocks
        control_type='NEGATIVE', control_label='SPAM_LOW_NOTIONAL',
        symbol='AAPL', strike=300.0, option_type='CALL', alert_time='2026-01-03 09:00:00-05',
        spot=300.0, level_label='S1', level_price=300.0, entry_price=0.05,
        vols=[10] * 19 + [800], observed_vol=800, completed_vol=800, ratio=80.0,
        event_share=0.80, premium_notional=4000, low_dist=1.0, is_atm=True,
        next_day_mode=False, expected_pass=False,
        notes='800 contracts but $0.05 premium = $4k notional < $50k floor.')
    print("Seeded gate controls (idempotent).")


def run_controls() -> int:
    """Re-run the canonical gate on every control; return the number of FAILURES."""
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM gate_controls ORDER BY control_type DESC, control_label")
        controls = cur.fetchall()
    conn.close()
    if not controls:
        print("No controls seeded — run:  python gate_controls.py --seed")
        return 0

    print(f"GATE CONTROL REGRESSION — {len(controls)} controls")
    print("=" * 92)
    failures = 0
    for c in controls:
        vols = [int(v) for v in c['vols']]
        ev = _D._eval_volume(
            c['symbol'], vols, vols[-1], float(c['low_dist']) if c['low_dist'] is not None else None,
            300, 300, 3.0, _EXC, mark=float(c['entry_price']) if c['entry_price'] else None,
            is_atm=bool(c['is_atm']), next_day_mode=bool(c['next_day_mode']),
            completed_vol=int(c['completed_vol']) if c['completed_vol'] is not None else None)
        ok = (ev['valid'] == c['expected_pass'])
        if c['expected_pass'] and c.get('expected_gold') and not ev['gold_standard']:
            ok = False
        if c['expected_pass'] and c.get('expected_path') and ev['path'] != c['expected_path']:
            ok = False
        tag = "PASS" if ok else "**FAIL**"
        if not ok:
            failures += 1
        want = (f"pass(path {c['expected_path']}{'/gold' if c['expected_gold'] else ''})"
                if c['expected_pass'] else "block")
        got = (f"valid path={ev['path']} gold={ev['gold_standard']}" if ev['valid']
               else f"blocked {ev['block_reason']}")
        print(f"  [{tag}] {c['control_type']:8} {c['control_label']:32} {c['symbol']:5} "
              f"want={want:22} got={got}")
    print("-" * 92)
    print(f"  {len(controls) - failures}/{len(controls)} controls OK"
          + (f"  — {failures} FAILURE(S)" if failures else "  — all green"))
    return failures


if __name__ == "__main__":
    if '--seed' in sys.argv:
        seed_controls()
    else:
        sys.exit(1 if run_controls() else 0)
