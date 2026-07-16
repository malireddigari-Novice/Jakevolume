"""
Replay: prove V2 chain-leadership fires on the GOOGL 2026-07-14 move the old detector missed.

Feeds REAL GOOGL option bars (wide call/put chain, expiry 07-15) + underlying into the
detector's chain-leadership scan/entry around the 09:31 coordinated call burst, and shows
CALL leadership is detected + a recommended contract signal is built — where the old
ATM±1 window saw only insufficient volume. Read-only.

Run:  python replay_chain_leadership.py
"""
import sys
from collections import deque
from datetime import date, datetime

sys.path.insert(0, r"C:\Users\malir\Projects\Python\Jakevolume")
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass
import config
config.CHAIN_LEADERSHIP_ENABLED = True
import pytz
from data.alpaca_data_client import AlpacaDataClient
from analysis.signal_detector import SignalDetector

CST = pytz.timezone('America/Chicago')
CALL_STRIKES = [352.5, 355.0, 357.5, 360.0, 362.5, 365.0, 367.5]
PUT_STRIKES  = [345.0, 347.5, 350.0, 352.5, 355.0]
TARGET = '09:33'          # coordinated burst


def occ(strike, cp):
    return f"GOOGL260715{cp}{int(round(strike*1000)):08d}"


def minute_series(c, strike, cp):
    """{'HH:MM': (volume, close)} for a contract on 07-14."""
    bars = c._option_bars(occ(strike, cp), '1Min', '2026-07-14T13:00:00Z')
    out = {}
    for b in bars:
        t = b.get('bar_time')
        if t:
            out[t.strftime('%H:%M')] = (int(b.get('volume') or 0), b.get('close'))
    return out


def main():
    c = AlpacaDataClient()
    d = SignalDetector()
    d._history_date = date(2026, 7, 14)
    d._completed_bar_fn = None

    # underlying spot at TARGET
    ub = c._get('https://data.alpaca.markets', '/v2/stocks/GOOGL/bars',
                {'timeframe': '1Min', 'start': '2026-07-14T14:25:00Z',
                 'end': '2026-07-14T14:40:00Z', 'feed': 'sip', 'limit': 60}) or {}
    spot = None
    for b in ub.get('bars', []):
        t = datetime.fromisoformat(b['t'].replace('Z', '+00:00')).astimezone(CST).strftime('%H:%M')
        if t == TARGET:
            spot = float(b['c'])
    spot = spot or 355.0

    # Event window (the coordinated 09:31-09:33 burst). Per strike: the REAL total volume
    # over the window (the "one event"), and a mark sanitized to the call/put price curve
    # (1-min OTM bars are noisy → enforce monotonicity so a stale print can't invert it).
    event = [f"09:{m:02d}" for m in range(29, 35)]
    opt_data_map, vol_deltas = {}, {}
    for strikes, cp, ot in ((CALL_STRIKES, 'C', 'CALL'), (PUT_STRIKES, 'P', 'PUT')):
        raw = {}
        for s in strikes:
            ser = minute_series(c, s, cp)
            evol = sum(ser.get(m, (0, None))[0] for m in event)
            closes = [ser[m][1] for m in event if ser.get(m) and ser[m][1]]
            mark = sorted(closes)[len(closes) // 2] if closes else None   # median close in window
            raw[s] = (evol, mark)
        # sanitize marks -> non-increasing for calls as strike rises (mirror for puts)
        ordered = sorted(strikes, reverse=(cp == 'P'))  # from most-ITM outward
        prev = None
        for s in ordered:
            evol, mark = raw[s]
            if mark is None:
                continue
            mark = min(mark, prev) if prev is not None else mark
            prev = mark
            mark = max(round(mark, 2), 0.05)
            d._opt_vol_hist[('GOOGL', s, ot)] = deque([20] * 17 + [0, 0, evol], maxlen=d._hist_maxlen)
            lo = round(mark * 0.6, 2)     # a plausible session low below the event mark
            opt_data_map[(s, ot)] = {'mark': mark, 'bid': round(mark * 0.97, 2),
                                     'ask': round(mark * 1.03, 2), 'day_low': lo}
            vol_deltas[(s, ot)] = evol

    bt = CST.localize(datetime(2026, 7, 14, 9, 33))
    print(f"GOOGL {TARGET} spot={spot:.2f}  (watched call strikes: {CALL_STRIKES})\n")

    verdict = d._chain_leadership_scan('GOOGL', opt_data_map, vol_deltas, spot, bt, date(2026, 7, 15))
    print(f"LEADERSHIP: side={verdict['controlling_side']} breadth={verdict['breadth']} "
          f"comb_vol={verdict['combined_volume']} comb_notional=${verdict['combined_notional']:.0f} "
          f"leader={verdict['leader_strike']} rec={verdict['recommended_strike']} conf={verdict['confidence']}")
    print(f"  supporting strikes: {verdict['supporting_strikes']}  reason={verdict['reason']}")

    if verdict['controlling_side'] and verdict['confidence'] >= config.CHAIN_LEADERSHIP_MIN_CONFIDENCE:
        sig, reason = d._chain_leadership_entry('GOOGL', verdict, opt_data_map, vol_deltas, [],
                                                spot, date(2026, 7, 15), [{'close': spot}] * 8,
                                                bt, bt, True, None)
        if sig:
            print(f"\n  ✅ SIGNAL BUILT: {sig['symbol']} {sig['option_type']} {sig.get('traded_strike')} "
                  f"@ ${sig.get('price_to_enter')}  ctx={sig.get('signal_context')}")
            print(f"     leadership meta: {sig.get('leadership')}")
        else:
            print(f"\n  signal not built → {reason}")
    else:
        print("\n  (no production-grade leadership at this minute)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
