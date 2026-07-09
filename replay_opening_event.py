"""
Replay validation for Fix (2) Option C (opening event-time production promotion).

Drives REAL OPRA 1-min bars for the opening window through SignalDetector.check() with
OPENING_SCAN_PRODUCTION_ENABLED on, and reports whether the opening event fires + how the
Gold gate rules on it. Read-only (no DB writes, no Discord, no trades).

Default case: NVDA 2026-07-09 — 202.5P printed 1,638 @ 08:40 with spot falling 203->201.85
(genuine put demand). Expected: OPENING-PROD fires PUT 202.5, priced at the commit mark.
Also runs a synthetic put-SUPPLY variant (spot forced rising) — expected: blocked.

Run:  python replay_opening_event.py
"""
import sys
import logging
from collections import Counter
from datetime import date, datetime

sys.path.insert(0, r"C:\Users\malir\Projects\Python\Jakevolume")
try:
    sys.stdout.reconfigure(encoding='utf-8')       # audit summaries contain '->' arrows
except Exception:
    pass
import config
# Enable the full opening production stack for this replay only.
config.GOLD_ONLY_PRODUCTION_MODE = True
config.EVENT_TIME_ELIGIBILITY_ENABLED = True
config.OPENING_SCAN_PRODUCTION_ENABLED = True
config.INTENT_VALIDATION_ENABLED = False   # intent confirmation is a separate main-loop step
config.OPPOSITE_SIDE_VETO_ENABLED = True

import pytz
from data.alpaca_data_client import AlpacaDataClient
from analysis.signal_detector import SignalDetector
from analysis import gold_mode

CST = pytz.timezone('America/Chicago')
SYMBOL = 'NVDA'
EXPIRY = date(2026, 7, 10)
STRIKES = [200.0, 202.5, 205.0]
LEVELS = [{'level_type': 'SUPPORT', 'rank': 1, 'strike': 195.0},
          {'level_type': 'SUPPORT', 'rank': 2, 'strike': 197.5},
          {'level_type': 'SUPPORT', 'rank': 3, 'strike': 200.0},
          {'level_type': 'RESISTANCE', 'rank': 2, 'strike': 207.5},
          {'level_type': 'RESISTANCE', 'rank': 1, 'strike': 210.0},
          {'level_type': 'RESISTANCE', 'rank': 3, 'strike': 212.5}]


def _occ(strike, ot):
    cp = 'P' if ot == 'PUT' else 'C'
    return f"NVDA260710{cp}{int(round(strike*1000)):08d}"


def _cst(iso_z):
    return datetime.fromisoformat(iso_z.replace('Z', '+00:00')).astimezone(CST)


def fetch():
    """Return {minute 'HH:MM': {'spot':x, 'opt':{(strike,ot):{mark,vol_cum,low}}}} 08:30-08:45."""
    c = AlpacaDataClient()
    # option bars per contract
    opt = {}
    for s in STRIKES:
        for ot in ('PUT', 'CALL'):
            bars = c._option_bars(_occ(s, ot), '1Min', '2026-07-09T13:25:00Z')
            cum = 0
            lo = None
            for b in bars:
                t = b.get('bar_time'); v = int(b.get('volume') or b.get('v') or 0)
                cl = b.get('close') or b.get('c')
                cum += v
                lo = cl if lo is None else min(lo, cl)
                hhmm = t.strftime('%H:%M') if hasattr(t, 'strftime') else None
                if hhmm:
                    opt.setdefault(hhmm, {})[(s, ot)] = {
                        'mark': cl, 'bid': round(cl * 0.985, 2), 'ask': round(cl * 1.015, 2),
                        'volume': cum, 'day_low': lo, 'open_interest': 0}
    # underlying
    ub = c._get('https://data.alpaca.markets', '/v2/stocks/NVDA/bars',
                {'timeframe': '1Min', 'start': '2026-07-09T13:25:00Z',
                 'end': '2026-07-09T14:00:00Z', 'feed': 'sip', 'limit': 100}) or {}
    spot = {}
    for b in ub.get('bars', []):
        spot[_cst(b['t']).strftime('%H:%M')] = float(b['c'])
    return opt, spot


class _OpeningProbe(logging.Handler):
    """Capture OPENING-PROD block reasons emitted by the detector during the replay."""
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.reasons = Counter()
    def emit(self, rec):
        m = rec.getMessage()
        if m.startswith('OPENING-PROD') and '→' in m:
            self.reasons[m.split('→', 1)[1].split('(')[0].strip()] += 1


def replay(opt, spot, force_spot_rising=False):
    probe = _OpeningProbe()
    lg = logging.getLogger('analysis.signal_detector')
    lg.setLevel(logging.INFO); lg.addHandler(probe)
    d = SignalDetector()
    d._history_date = date(2026, 7, 9)
    ubars = []
    fired_all = []
    try:
        for m in range(30, 46):                   # 08:30 .. 08:45 CST
            hhmm = f"08:{m:02d}"
            oq = opt.get(hhmm)
            sp = spot.get(hhmm)
            if oq is None or sp is None:
                continue
            if force_spot_rising:                 # synthetic supply variant
                sp = 201.0 + (m - 30) * 0.5       # steadily rising spot
            bt = CST.localize(datetime(2026, 7, 9, 8, m))
            ubars.append({'close': sp, 'bar_time': bt})
            fired = d.check(SYMBOL, ubars, LEVELS, dict(oq), expiry=EXPIRY, opening_range=True)
            for sig in fired:
                fired_all.append((hhmm, sig))
    finally:
        lg.removeHandler(probe)
    return fired_all, probe.reasons


def report(tag, result):
    fired, block_reasons = result
    if not fired:
        print(f"  [{tag}] no signal fired")
    for hhmm, sig in fired:
        gold_mode.annotate_and_gate(sig)
        aud = sig.get('gate_audit', {})
        path = 'OPENING-EVENT' if sig.get('opening_event') else sig.get('signal_context')
        print(f"  [{tag}] {hhmm} FIRED [{path}] {sig['symbol']} {sig['option_type']} "
              f"{sig.get('traded_strike')} @ {sig.get('price_to_enter')} "
              f"({sig.get('paper_fill_method')})")
        print(f"        story={sig.get('opening_story')} retro={sig.get('no_retro_label')} "
              f"gold={sig.get('gold_grade')} prod_allowed={sig.get('production_allowed')}")
        print(f"        gate: {gold_mode.audit_summary(aud)}")
    if block_reasons:
        print(f"  [{tag}] opening-promotion blocks: "
              + ", ".join(f"{r}×{n}" for r, n in block_reasons.most_common()))


def main():
    print("Fetching real OPRA + underlying bars (08:30-08:45 CST, 2026-07-09)...")
    try:
        opt, spot = fetch()
    except Exception as e:
        print("FETCH FAILED:", e); return 2
    print(f"  got {len(opt)} minute-slices, {len(spot)} underlying bars\n")

    print("POSITIVE — real data (spot falling into 08:40 put print = put demand):")
    report('real', replay(opt, spot))
    print()
    print("NEGATIVE — synthetic put-SUPPLY variant (spot forced rising):")
    report('supply', replay(opt, spot, force_spot_rising=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
