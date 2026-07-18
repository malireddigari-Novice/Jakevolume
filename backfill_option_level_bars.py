"""
Backfill option_level_bars from Alpaca's options market-data API.

Schwab's /pricehistory does not serve option candles (returns empty), so the
table has always been empty. Alpaca DOES serve 1-Min option OHLCV keyed by the
same unpadded OCC symbol occ_symbol() builds — this script pulls that history
for every S/R level contract we have on record and populates the table.

Which contracts / days:
  - levels   : oi_levels (the 6 S/R contracts per symbol per day: strike, type, rank)
  - expiry   : option_chain_snapshots.expiry_date  (the expiry that day's levels were
               anchored to — correctly handles next-day mode, e.g. Tue levels → Wed expiry)

Alpaca endpoint (same APCA keys as trading):
  GET https://data.alpaca.markets/v1beta1/options/bars
      ?symbols={occ,occ,...}&timeframe=1Min&start=..&end=..   (NO `feed` param)

Idempotent: save_option_level_bars upserts on (occ_symbol, bar_time). By default a
(symbol, day) already present in the table is skipped; pass --force to refetch.

Usage:
  python backfill_option_level_bars.py [--dry-run] [--force] [--since YYYY-MM-DD]
"""
import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone

import psycopg2
import requests

import config
import db.ops as db
from data.alpaca_client import occ_symbol
from data.market_utils import CST

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)-7s %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger('backfill')

DATA_URL = 'https://data.alpaca.markets/v1beta1/options/bars'
HEADERS  = {'APCA-API-KEY-ID': config.ALPACA_API_KEY,
            'APCA-API-SECRET-KEY': config.ALPACA_SECRET_KEY}
BATCH       = 6      # OCC symbols per request (the 6 levels of one symbol/day)
FETCH_CHUNK = 40     # candidates mode: OCC symbols per Alpaca request (a day can hold many)
PAGE_LIMIT  = 10000


def _alpaca_bars(occ_syms: list[str], day) -> dict:
    """Return {occ: [bar,...]} of 1-Min bars for the given UTC calendar day."""
    start = f"{day.isoformat()}T00:00:00Z"
    end   = f"{day.isoformat()}T23:59:59Z"
    out: dict[str, list] = {s: [] for s in occ_syms}
    page = None
    while True:
        params = {'symbols': ','.join(occ_syms), 'timeframe': '1Min',
                  'start': start, 'end': end, 'limit': PAGE_LIMIT}
        if page:
            params['page_token'] = page
        for attempt in range(5):
            r = requests.get(DATA_URL, headers=HEADERS, params=params, timeout=30)
            if r.status_code == 429:
                wait = 2 ** attempt
                log.warning("429 rate-limited — sleeping %ds", wait); time.sleep(wait); continue
            r.raise_for_status()
            break
        else:
            raise RuntimeError("repeated 429s from Alpaca")
        j = r.json()
        for sym, bars in (j.get('bars') or {}).items():
            out.setdefault(sym, []).extend(bars)
        page = j.get('next_page_token')
        if not page:
            return out


def _to_cst(iso_z: str) -> datetime:
    """'2026-06-10T13:30:00Z' -> tz-aware CST datetime (matches the live pipeline)."""
    dt = datetime.fromisoformat(iso_z.replace('Z', '+00:00')).astimezone(timezone.utc)
    return dt.astimezone(CST)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true', help='report only, no writes')
    ap.add_argument('--force', action='store_true', help='refetch days already present')
    ap.add_argument('--since', help='only backfill level_date >= this (YYYY-MM-DD)')
    ap.add_argument('--signals', action='store_true',
                    help='backfill the contracts that SIGNALS actually traded '
                         '(ATM confirm-side strikes) instead of the 6 S/R level contracts')
    ap.add_argument('--candidates', action='store_true',
                    help='backfill NEAR-LOW candidate contracts (non-level strikes) from '
                         'signal_candidates into option_candidate_bars — so the absorption / '
                         'PDS outcome tests can price contracts option_level_bars never stored')
    ap.add_argument('--near-low-max', type=float, default=1.75,
                    help='candidates mode: max contract_low_distance to include (default 1.75)')
    ap.add_argument('--min-vol', type=int, default=300,
                    help='candidates mode: min trigger_volume to include (default 300)')
    args = ap.parse_args()

    db.init_pool()

    conn = psycopg2.connect(host=config.DB_HOST, port=config.DB_PORT, dbname=config.DB_NAME,
                            user=config.DB_USER, password=config.DB_PASSWORD)
    cur = conn.cursor()

    # Expiry map: (symbol, day) -> expiry_date
    cur.execute("""SELECT symbol, snap_date, MIN(expiry_date)
                   FROM option_chain_snapshots GROUP BY symbol, snap_date""")
    expiry_map = {(s, d): e for s, d, e in cur.fetchall()}

    by_day: dict = {}
    if args.candidates:
        # Near-low candidate contracts (non-level strikes) the outcome tests can't
        # price today. One entry per distinct (symbol, session_date, strike, side).
        where = "AND session_date >= %s" if args.since else ""
        params = [args.near_low_max, args.min_vol]
        if args.since:
            params.append(args.since)
        cur.execute(f"""SELECT DISTINCT symbol, session_date, strike, candidate_side
                        FROM signal_candidates
                        WHERE contract_low_distance <= %s
                          AND (valid_volume_event OR trigger_volume >= %s)
                          {where}
                        ORDER BY session_date, symbol""", tuple(params))
        for symbol, day, strike, otype in cur.fetchall():
            by_day.setdefault((symbol, day), []).append(
                dict(strike=float(strike), option_type=otype))
    elif args.signals:
        # Contracts the engine actually traded (ATM confirm-side strikes). Tag each
        # with the originating level's type and rank (rank looked up by matching the
        # signal's level_price to oi_levels; the level side is CALL for RESISTANCE,
        # PUT for SUPPORT).
        cur.execute("""SELECT symbol, level_date, strike, option_type, rank
                       FROM oi_levels""")
        rank_map = {(s, d, float(k), ot): r for s, d, k, ot, r in cur.fetchall()}
        where = "AND signal_time::date >= %s" if args.since else ""
        cur.execute(f"""SELECT DISTINCT symbol, signal_time::date, traded_strike,
                               option_type, level_type, level_price
                        FROM signals
                        WHERE traded_strike IS NOT NULL {where}
                        ORDER BY 2, 1""",
                    ((args.since,) if args.since else ()))
        for symbol, day, tstrike, otype, ltype, lprice in cur.fetchall():
            level_side = 'CALL' if ltype == 'RESISTANCE' else 'PUT'
            rank = rank_map.get((symbol, day, float(lprice), level_side), 1)
            by_day.setdefault((symbol, day), []).append(
                dict(level_type=ltype, rank=rank, strike=float(tstrike), option_type=otype))
    else:
        # The 6 S/R level contracts (oi_levels) — the table's defined purpose.
        where = "WHERE level_date >= %s" if args.since else ""
        cur.execute(f"""SELECT symbol, level_date, level_type, rank, strike, option_type
                        FROM oi_levels {where}
                        ORDER BY level_date, symbol, level_type, rank""",
                    ((args.since,) if args.since else ()))
        for symbol, day, ltype, rank, strike, otype in cur.fetchall():
            by_day.setdefault((symbol, day), []).append(
                dict(level_type=ltype, rank=rank, strike=float(strike), option_type=otype))

    source = 'candidates' if args.candidates else 'signals' if args.signals else 'levels'
    log.info("%d (symbol,day) groups to consider [source=%s]%s", len(by_day), source,
             f" since {args.since}" if args.since else "")

    tot_rows = tot_bars = skipped_present = skipped_noexp = 0
    days_done = 0
    for (symbol, day), levels in sorted(by_day.items()):
        expiry = expiry_map.get((symbol, day))
        if expiry is None:
            skipped_noexp += 1
            log.warning("%s %s: no expiry in option_chain_snapshots — skipping", symbol, day)
            continue

        occ_to_level = {occ_symbol(symbol, expiry, lv['strike'], lv['option_type']): lv
                        for lv in levels}

        # Skip OCC symbols already present unless --force (per-contract, so a
        # signals-mode run still fills traded strikes on days whose level
        # contracts are already stored).
        if not args.force:
            if args.candidates:
                # Already priceable if stored in EITHER table (level contracts don't
                # need re-fetching into the candidate table).
                cur.execute("""SELECT occ_symbol FROM option_candidate_bars
                               WHERE symbol=%s AND session_date=%s
                               UNION SELECT occ_symbol FROM option_level_bars
                               WHERE symbol=%s AND level_date=%s""",
                            (symbol, day, symbol, day))
            else:
                cur.execute("""SELECT DISTINCT occ_symbol FROM option_level_bars
                               WHERE symbol=%s AND level_date=%s""", (symbol, day))
            present = {r[0] for r in cur.fetchall()}
            for occ in list(occ_to_level):
                if occ in present:
                    del occ_to_level[occ]
            if not occ_to_level:
                skipped_present += 1
                continue
        occ_syms = list(occ_to_level)

        if args.dry_run:
            log.info("[dry-run] %s %s exp=%s -> %d contracts: %s",
                     symbol, day, expiry, len(occ_syms), occ_syms)
            days_done += 1
            continue

        # A candidate day can carry many strikes — chunk the fetch (levels = 6, one call).
        try:
            bars_by_occ = {}
            for i in range(0, len(occ_syms), FETCH_CHUNK):
                chunk = occ_syms[i:i + FETCH_CHUNK]
                bars_by_occ.update(_alpaca_bars(chunk, day))
                if len(occ_syms) > FETCH_CHUNK:
                    time.sleep(0.25)
        except Exception as exc:
            log.error("%s %s: Alpaca fetch failed: %s", symbol, day, exc)
            continue

        rows = []
        nb = 0
        for occ, bars in bars_by_occ.items():
            lv = occ_to_level[occ]
            for b in bars:
                nb += 1
                if args.candidates:
                    rows.append({
                        'symbol': symbol, 'session_date': day,
                        'strike': lv['strike'], 'option_type': lv['option_type'],
                        'expiry': expiry, 'occ_symbol': occ,
                        'bar_time': _to_cst(b['t']),
                        'open': b['o'], 'high': b['h'], 'low': b['l'],
                        'close': b['c'], 'volume': b['v'],
                    })
                else:
                    rows.append({
                        'symbol': symbol, 'level_date': day,
                        'level_type': lv['level_type'], 'rank': lv['rank'],
                        'strike': lv['strike'], 'option_type': lv['option_type'],
                        'expiry': expiry, 'occ_symbol': occ,
                        'bar_time': _to_cst(b['t']),
                        'open': b['o'], 'high': b['h'], 'low': b['l'],
                        'close': b['c'], 'volume': b['v'],
                    })
        if args.candidates:
            db.save_option_candidate_bars(rows)
        else:
            db.save_option_level_bars(rows)
        tot_rows += len(rows); tot_bars += nb; days_done += 1
        log.info("%s %s exp=%s: %d bars across %d/%d contracts",
                 symbol, day, expiry, nb, sum(1 for o in bars_by_occ if bars_by_occ[o]),
                 len(occ_syms))
        time.sleep(0.25)   # gentle on the rate limit

    conn.close()
    log.info("DONE. days=%d  rows_written=%d  (skipped: present=%d, no_expiry=%d)%s",
             days_done, tot_rows, skipped_present, skipped_noexp,
             "  [DRY-RUN]" if args.dry_run else "")


if __name__ == '__main__':
    main()
