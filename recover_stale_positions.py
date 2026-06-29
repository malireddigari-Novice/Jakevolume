"""
Recovery sweep for positions that a missed/crashed session left open.

When the system is down at EOD it never runs its liquidation, so a position opened
that day (and any reversal that should have closed it) is left dangling. This script
reconciles broker truth (Alpaca) against system state (the trades table), classifies
anything still open, and — only with --execute — flattens it and reconciles the DB.

USAGE
  python recover_stale_positions.py            # DRY-RUN: report only, places NO orders
  python recover_stale_positions.py --execute  # actually market-close stale live positions
                                               # (run during market hours; day orders reject when closed)

CLASSIFICATION (per currently-held Alpaca position / open DB trade)
  EXPIRED    expiry < today  → already settled; cannot sell, just reconcile the DB row
  STALE_LIVE expiry >= today, still held → flatten at market on --execute
  STALE_DB   trades.status='placed' but Alpaca holds nothing → reconcile DB (no order)

REVERSAL: a reversal that was missed cannot be re-detected without that session's
intraday option flow, so this does NOT retroactively flip into a new trade. It
flattens the stale position and REPORTS any opposite-side signal / recorded reversal
since entry, so you can decide whether to re-enter manually.
"""
import sys
from datetime import date, datetime, timedelta

import pytz

import config
import db.ops as db
from data.alpaca_client import AlpacaClient, parse_occ_symbol as parse_occ
from data.market_utils import today_cst

CST = pytz.timezone('America/Chicago')


def reversal_context(symbol: str, since: date) -> dict:
    """Opposite-side signals + recorded reversals for `symbol` on/after `since` (informational)."""
    out = {'reversals': 0, 'opp_signals': []}
    conn = db._get()
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT COUNT(*) FROM flow_reversals "
                            "WHERE symbol=%s AND detected_at::date >= %s", (symbol, since))
                out['reversals'] = cur.fetchone()[0]
            except Exception:
                conn.rollback()
            cur.execute("SELECT signal_time, signal_type, option_type, level_price "
                        "FROM signals WHERE symbol=%s AND signal_time::date >= %s "
                        "ORDER BY signal_time", (symbol, since))
            out['opp_signals'] = cur.fetchall()
    finally:
        db._put(conn)
    return out


def main(execute: bool) -> int:
    db.init_pool()
    ac = AlpacaClient()
    if not ac.verify():
        print("Alpaca verify failed — aborting.")
        return 1

    today     = today_cst()
    now       = datetime.now(CST)
    positions = ac.list_positions()
    db_open   = {t['occ_symbol']: t for t in db.get_open_trades()}
    pos_occ   = {p['occ'] for p in positions}

    mode = "EXECUTE" if execute else "DRY-RUN (no orders placed)"
    print(f"\n=== Stale-position recovery — {mode} | session {today} ===\n")

    plan = []   # (kind, occ, detail, trade_id_or_None)

    # 1. Currently-held Alpaca positions
    for p in positions:
        occ = p['occ']
        try:
            sym, expiry, otype, strike = parse_occ(occ)
        except Exception:
            sym, expiry, otype, strike = occ, None, '?', 0.0
        trade = db_open.get(occ)
        tid   = trade['id'] if trade else None
        if expiry and expiry < today:
            plan.append(('EXPIRED', occ, f"{sym} {otype} {strike:g} exp {expiry} "
                         f"qty={p['qty']} uPL=${p['unrealized_pl']:+.2f}", tid))
        else:
            plan.append(('STALE_LIVE', occ, f"{sym} {otype} {strike:g} exp {expiry} "
                         f"qty={p['qty']} now=${p['current_price']:.2f} "
                         f"uPL=${p['unrealized_pl']:+.2f}", tid))

    # 2. DB trades marked open but with no Alpaca position
    for occ, t in db_open.items():
        if occ not in pos_occ:
            plan.append(('STALE_DB', occ, f"{t['symbol']} {t['signal_type']} qty={t['qty']} "
                         f"(status=placed, broker flat)", t['id']))

    if not plan:
        print("  Nothing open on either side — flat and reconciled. No recovery needed.\n")
        return 0

    # Report + (optionally) act
    for kind, occ, detail, tid in plan:
        print(f"  [{kind}] {occ}  {detail}")

        sym   = parse_occ(occ)[0] if len(occ) > 15 else occ[:4]
        since = today - timedelta(days=7)   # wide enough to include a prior session's entry
        rc    = reversal_context(sym, since)
        if rc['reversals'] or rc['opp_signals']:
            print(f"       reversal context: {rc['reversals']} recorded reversal(s); "
                  f"{len(rc['opp_signals'])} signal(s) since {since}:")
            for st, styp, otype, lvl in rc['opp_signals']:
                print(f"         - {st:%m-%d %H:%M} {styp} {otype} @ {lvl}")

        if not execute:
            if kind == 'STALE_LIVE':
                print("       → would MARKET-CLOSE this position and mark trade eod_closed")
            elif kind == 'STALE_DB':
                print("       → would reconcile DB: mark trade eod_closed (broker already flat)")
            elif kind == 'EXPIRED':
                print("       → expired/settled: cannot sell; would reconcile DB row if tracked")
            continue

        # --- EXECUTE ---
        if kind == 'STALE_LIVE':
            held = ac.position_qty(occ)
            if held > 0:
                order = ac.close_position_qty(occ, held)
                if order:
                    print(f"       ✓ close order placed (id={order.get('id','?')[:8]})")
                    if tid:
                        db.mark_trade_eod_closed(tid, now)
                else:
                    print("       ✗ close order FAILED (market closed? see log) — DB left as-is")
            else:
                print("       position already gone at execute time; reconciling DB")
                if tid:
                    db.mark_trade_eod_closed(tid, now)
        elif kind == 'STALE_DB':
            if tid:
                db.mark_trade_eod_closed(tid, now)
                print("       ✓ DB reconciled (eod_closed)")
        elif kind == 'EXPIRED':
            if tid:
                db.mark_trade_eod_closed(tid, now)
                print("       ✓ DB reconciled (expired/settled)")

    print(f"\n=== {'Recovery applied.' if execute else 'Dry-run complete — re-run with --execute to act.'} ===\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(execute='--execute' in sys.argv))
