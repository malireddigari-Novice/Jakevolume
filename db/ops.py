"""
PostgreSQL operations — thin CRUD layer over the jakevolume schema.
All public functions borrow/return connections from the module-level pool.
"""
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import psycopg2
from psycopg2 import pool as pg_pool
from psycopg2.extras import execute_values, RealDictCursor

import config

logger = logging.getLogger(__name__)

_pool: Optional[pg_pool.SimpleConnectionPool] = None


# ── Pool lifecycle ────────────────────────────────────────────────────────────

def init_pool() -> None:
    """Create the module-level psycopg2 connection pool from config credentials."""
    global _pool
    _pool = pg_pool.SimpleConnectionPool(
        minconn=1,
        maxconn=8,
        host=config.DB_HOST,
        port=config.DB_PORT,
        dbname=config.DB_NAME,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
    )
    logger.info("DB connection pool initialised (%s/%s)", config.DB_HOST, config.DB_NAME)


def _get() -> psycopg2.extensions.connection:
    return _pool.getconn()


def _put(conn) -> None:
    _pool.putconn(conn)


# ── Schema bootstrap ──────────────────────────────────────────────────────────

def init_schema() -> None:
    """Execute schema.sql once; safe to call on every startup (idempotent)."""
    sql_path = Path(__file__).parent / 'schema.sql'
    sql = sql_path.read_text(encoding='utf-8')
    conn = _get()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        logger.info("Schema verified / initialised")
    finally:
        _put(conn)


# ── Option chain snapshots ────────────────────────────────────────────────────

def save_option_chain(
    symbol: str,
    snap_date: date,
    snap_time: datetime,
    expiry_date: date,
    contracts: list,
    underlying_price: float,
) -> None:
    """Bulk-insert all contracts from one option chain snapshot; ignores duplicates."""
    rows = [
        (
            symbol,
            snap_date,
            snap_time,
            expiry_date,
            float(c['strike']),
            c['option_type'],
            int(c.get('open_interest', 0)),
            int(c.get('volume', 0)),
            c.get('bid'),
            c.get('ask'),
            c.get('mark'),
            underlying_price,
            c.get('delta'),
            c.get('gamma'),
            c.get('vega'),
            c.get('theta'),
            c.get('rho'),
            c.get('implied_vol'),
        )
        for c in contracts
    ]
    sql = """
        INSERT INTO option_chain_snapshots
            (symbol, snap_date, snap_time, expiry_date, strike, option_type,
             open_interest, volume, bid, ask, mark, underlying_price,
             delta, gamma, vega, theta, rho, implied_vol)
        VALUES %s
        ON CONFLICT DO NOTHING
    """
    conn = _get()
    try:
        with conn.cursor() as cur:
            execute_values(cur, sql, rows)
        conn.commit()
        logger.debug("Saved %d option chain rows for %s", len(rows), symbol)
    finally:
        _put(conn)


def reconcile_oi_changes(symbol: str, snap_date: date) -> int:
    """
    For each row in today's option_chain_snapshots, look up yesterday's OI for the
    same (symbol, expiry_date, strike, option_type) and compute:
        prev_open_interest = yesterday's OI
        oi_change          = today_oi - yesterday_oi
        oi_change_pct      = oi_change / max(yesterday_oi, 1)
        volume_to_oi       = today_volume / max(yesterday_oi, 1)

    Run once per morning after save_option_chain completes.  Rows with no prior-day
    match (new strikes, first run) are left with NULL oi_change columns.
    Returns the number of rows updated.
    """
    sql = """
        UPDATE option_chain_snapshots AS t
        SET
            prev_open_interest = y.open_interest,
            oi_change          = t.open_interest - y.open_interest,
            oi_change_pct      = ROUND(
                (t.open_interest - y.open_interest)::numeric
                / GREATEST(y.open_interest, 1), 4
            ),
            volume_to_oi       = ROUND(
                t.volume::numeric / GREATEST(y.open_interest, 1), 4
            )
        FROM (
            SELECT DISTINCT ON (symbol, expiry_date, strike, option_type)
                symbol, expiry_date, strike, option_type, open_interest
            FROM option_chain_snapshots
            WHERE symbol    = %s
              AND snap_date = %s - INTERVAL '1 day'
            ORDER BY symbol, expiry_date, strike, option_type, snap_time DESC
        ) y
        WHERE t.symbol      = %s
          AND t.snap_date   = %s
          AND y.symbol      = t.symbol
          AND y.expiry_date = t.expiry_date
          AND y.strike      = t.strike
          AND y.option_type = t.option_type
    """
    conn = _get()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (symbol, snap_date, symbol, snap_date))
            count = cur.rowcount
        conn.commit()
        logger.info("OI change reconciled: %s %s — %d rows updated", symbol, snap_date, count)
        return count
    finally:
        _put(conn)


# ── OI levels ─────────────────────────────────────────────────────────────────

def save_oi_levels(
    symbol: str,
    level_date: date,
    computed_at: datetime,
    levels: list,
) -> None:
    """Upsert computed S/R levels, refreshing strike and OI if already present for the day."""
    rows = [
        (
            symbol,
            level_date,
            lv['level_type'],
            lv['rank'],
            lv['strike'],
            lv['open_interest'],
            lv['option_type'],
            computed_at,
        )
        for lv in levels
    ]
    sql = """
        INSERT INTO oi_levels
            (symbol, level_date, level_type, rank, strike, open_interest, option_type, computed_at)
        VALUES %s
        ON CONFLICT (symbol, level_date, level_type, rank) DO UPDATE SET
            strike        = EXCLUDED.strike,
            open_interest = EXCLUDED.open_interest,
            option_type   = EXCLUDED.option_type,
            computed_at   = EXCLUDED.computed_at
    """
    conn = _get()
    try:
        with conn.cursor() as cur:
            execute_values(cur, sql, rows)
        conn.commit()
        logger.info("Saved %d OI levels for %s", len(rows), symbol)
    finally:
        _put(conn)


def get_today_levels(symbol: str, level_date: date) -> list:
    """Return all S/R levels for a symbol on the given date, ordered by type then rank."""
    sql = """
        SELECT level_type, rank, strike, open_interest, option_type
        FROM   oi_levels
        WHERE  symbol = %s AND level_date = %s
        ORDER  BY level_type, rank
    """
    conn = _get()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, (symbol, level_date))
            return cur.fetchall()
    finally:
        _put(conn)


# ── Price bars ────────────────────────────────────────────────────────────────

def save_bars(symbol: str, bars: list, full_session: bool = True) -> None:
    """
    Bulk-upsert 1-min equity bars.

    Each bar carries the 7 stored fields: open, high (max), low (min), close,
    volume (per-minute candle volume), plus spot_price (underlying spot at the
    bar) and cum_volume (running session total).

    cum_volume is only meaningful when the full session is passed in. With
    full_session=False (e.g. a partial rolling buffer) it is stored as NULL
    rather than a misleading partial total.

    ON CONFLICT updates the row so the forming minute settles to its final
    values and rows predating the spot_price/cum_volume columns get backfilled.
    """
    if not bars:
        return

    running = 0
    rows = []
    for b in bars:
        running += int(b['volume'])
        if not full_session:
            cum = None
        else:
            cum = b.get('cum_volume')
            if cum is None:
                cum = running
        spot = b.get('spot_price', b['close'])
        rows.append((
            symbol, b['bar_time'], b['open'], b['high'], b['low'], b['close'],
            b['volume'], spot, cum,
        ))

    sql = """
        INSERT INTO price_bars
            (symbol, bar_time, open, high, low, close, volume, spot_price, cum_volume)
        VALUES %s
        ON CONFLICT (symbol, bar_time) DO UPDATE SET
            open       = EXCLUDED.open,
            high       = EXCLUDED.high,
            low        = EXCLUDED.low,
            close      = EXCLUDED.close,
            volume     = EXCLUDED.volume,
            spot_price = EXCLUDED.spot_price,
            cum_volume = EXCLUDED.cum_volume
    """
    conn = _get()
    try:
        with conn.cursor() as cur:
            execute_values(cur, sql, rows)
        conn.commit()
    finally:
        _put(conn)


def save_option_level_bars(rows: list) -> None:
    """
    Bulk-upsert 1-min OHLCV bars for S/R level option contracts.

    Each row dict must carry: symbol, level_date, level_type, rank, strike,
    option_type, expiry, occ_symbol, bar_time, open, high, low, close, volume.
    ON CONFLICT (occ_symbol, bar_time) updates so the forming minute settles.
    """
    if not rows:
        return
    values = [
        (
            r['symbol'], r['level_date'], r['level_type'], r['rank'], r['strike'],
            r['option_type'], r['expiry'], r['occ_symbol'], r['bar_time'],
            r['open'], r['high'], r['low'], r['close'], r['volume'],
            r.get('cum_option_volume'),
            r.get('implied_vol'), r.get('delta'), r.get('gamma'),
            r.get('vega'), r.get('theta'), r.get('rho'),
        )
        for r in rows
    ]
    sql = """
        INSERT INTO option_level_bars
            (symbol, level_date, level_type, rank, strike, option_type, expiry,
             occ_symbol, bar_time, open, high, low, close, volume,
             cum_option_volume, implied_vol, delta, gamma, vega, theta, rho)
        VALUES %s
        ON CONFLICT (occ_symbol, bar_time) DO UPDATE SET
            open              = EXCLUDED.open,
            high              = EXCLUDED.high,
            low               = EXCLUDED.low,
            close             = EXCLUDED.close,
            volume            = EXCLUDED.volume,
            cum_option_volume = EXCLUDED.cum_option_volume,
            implied_vol = COALESCE(EXCLUDED.implied_vol, option_level_bars.implied_vol),
            delta       = COALESCE(EXCLUDED.delta,       option_level_bars.delta),
            gamma       = COALESCE(EXCLUDED.gamma,       option_level_bars.gamma),
            vega        = COALESCE(EXCLUDED.vega,        option_level_bars.vega),
            theta       = COALESCE(EXCLUDED.theta,       option_level_bars.theta),
            rho         = COALESCE(EXCLUDED.rho,         option_level_bars.rho)
    """
    conn = _get()
    try:
        with conn.cursor() as cur:
            execute_values(cur, sql, values)
        conn.commit()
    finally:
        _put(conn)


def prune_old_bars(keep_days: int = 10) -> dict:
    """
    Retain only the most recent `keep_days` trading days of 1-min bar data.

    Deletes older rows from price_bars and option_level_bars. The cutoff is the
    oldest of the `keep_days` most recent distinct trading dates actually present
    in price_bars, so market holidays are handled automatically (no calendar
    math). If fewer than `keep_days` trading days exist yet, nothing is deleted.

    Alerts (signals), trades, and the daily tables (oi_levels, morning_sentiment,
    option_chain_snapshots, volume_clusters) are never touched.

    Returns {'cutoff': date|None, 'price_bars': int, 'option_level_bars': int}.
    """
    conn = _get()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT (bar_time AT TIME ZONE 'America/Chicago')::date AS d
                FROM   price_bars
                ORDER  BY d DESC
                LIMIT  %s
                """,
                (keep_days,),
            )
            dates = [r[0] for r in cur.fetchall()]
            if len(dates) < keep_days:
                logger.info(
                    "prune_old_bars: only %d trading day(s) stored (< %d) — nothing to prune",
                    len(dates), keep_days,
                )
                return {'cutoff': None, 'price_bars': 0, 'option_level_bars': 0}

            cutoff = min(dates)   # oldest date we keep

            cur.execute(
                "DELETE FROM price_bars "
                "WHERE (bar_time AT TIME ZONE 'America/Chicago')::date < %s",
                (cutoff,),
            )
            pb = cur.rowcount

            cur.execute(
                "DELETE FROM option_level_bars WHERE level_date < %s",
                (cutoff,),
            )
            olb = cur.rowcount

            cur.execute(
                "DELETE FROM signal_candidates WHERE session_date < %s",
                (cutoff,),
            )
            sc = cur.rowcount

        conn.commit()
        logger.info(
            "prune_old_bars: kept %d trading days (>= %s); deleted price_bars=%d, "
            "option_level_bars=%d, signal_candidates=%d",
            keep_days, cutoff, pb, olb, sc,
        )
        return {'cutoff': cutoff, 'price_bars': pb, 'option_level_bars': olb,
                'signal_candidates': sc}
    finally:
        _put(conn)


def get_recent_bars(symbol: str, limit: int = 40) -> list:
    """Return bars sorted oldest-first."""
    sql = """
        SELECT bar_time, open, high, low, close, volume
        FROM   price_bars
        WHERE  symbol = %s
        ORDER  BY bar_time DESC
        LIMIT  %s
    """
    conn = _get()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, (symbol, limit))
            rows = cur.fetchall()
        return list(reversed(rows))
    finally:
        _put(conn)


# ── Signals ───────────────────────────────────────────────────────────────────

def save_signal(signal: dict) -> int:
    """Insert a fired signal and return its new row id for downstream logging."""
    sql = """
        INSERT INTO signals
            (symbol, signal_time, signal_type, bias, level_type, level_price,
             trigger_price, option_type, opt_mark, opt_bid, opt_ask,
             price_to_enter, price_to_exit,
             prox_score, cluster_strength, strong_cluster, flow_shape,
             signal_shape, confidence, upgrade, cluster_active_bars, cluster_burst_bars,
             day_mode, traded_strike, target_level,
             atm_vol_1m, atm_spike_ratio, atm_vol_3m,
             itm_vol_1m, itm_spike_ratio, itm_vol_3m,
             spread_pct, low_dist, room_score, room_pct,
             pc_ratio, pc_conviction, option_hl_flag,
             opt_vol_delta, avg_volume_20, spike_volume, consecutive_spikes)
        VALUES
            (%(symbol)s, %(signal_time)s, %(signal_type)s, %(bias)s,
             %(level_type)s, %(level_price)s, %(trigger_price)s,
             %(option_type)s, %(opt_mark)s, %(opt_bid)s, %(opt_ask)s,
             %(price_to_enter)s, %(price_to_exit)s,
             %(prox_score)s, %(cluster_strength)s, %(strong_cluster)s, %(flow_shape)s,
             %(signal_shape)s, %(confidence)s, %(upgrade)s, %(cluster_active_bars)s, %(cluster_burst_bars)s,
             %(day_mode)s, %(traded_strike)s, %(target_level)s,
             %(atm_vol_1m)s, %(atm_spike_ratio)s, %(atm_vol_3m)s,
             %(itm_vol_1m)s, %(itm_spike_ratio)s, %(itm_vol_3m)s,
             %(spread_pct)s, %(low_dist)s, %(room_score)s, %(room_pct)s,
             %(pc_ratio)s, %(pc_conviction)s, %(option_hl_flag)s,
             %(opt_vol_delta)s, %(avg_volume_20)s, %(spike_volume)s, %(consecutive_spikes)s)
        RETURNING id
    """
    conn = _get()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, signal)
            sig_id = cur.fetchone()[0]
        conn.commit()
        return sig_id
    finally:
        _put(conn)


def save_morning_sentiment(
    symbol: str,
    snap_date: date,
    pc_ratio: float,
    bias: str,
    computed_at: datetime,
) -> None:
    """Upsert daily P/C ratio and bias for one symbol."""
    sql = """
        INSERT INTO morning_sentiment (symbol, snap_date, pc_ratio, bias, computed_at)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (symbol, snap_date) DO UPDATE SET
            pc_ratio    = EXCLUDED.pc_ratio,
            bias        = EXCLUDED.bias,
            computed_at = EXCLUDED.computed_at
    """
    conn = _get()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (symbol, snap_date, pc_ratio, bias, computed_at))
        conn.commit()
        logger.debug("Saved morning sentiment for %s: pc=%.3f %s", symbol, pc_ratio, bias)
    finally:
        _put(conn)


def save_flow_reversal(r: dict) -> Optional[int]:
    """Insert a flow-reversal event (position exited + hypothetical opposite entry)."""
    sql = """
        INSERT INTO flow_reversals
            (symbol, detected_at, trade_id, from_side, to_side, spot, exit_occ, exit_price,
             same_leadership, opp_leadership, leadership_diff, opp_burst, opp_share,
             hypo_occ, hypo_strike, hypo_entry_price, flipped)
        VALUES (%(symbol)s, %(detected_at)s, %(trade_id)s, %(from_side)s, %(to_side)s,
                %(spot)s, %(exit_occ)s, %(exit_price)s, %(same_leadership)s,
                %(opp_leadership)s, %(leadership_diff)s, %(opp_burst)s, %(opp_share)s,
                %(hypo_occ)s, %(hypo_strike)s, %(hypo_entry_price)s, %(flipped)s)
        RETURNING id
    """
    conn = _get()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, r)
            rid = cur.fetchone()[0]
        conn.commit()
        return rid
    finally:
        _put(conn)


def save_signal_candidates(rows: list, ts, session_date) -> None:
    """Bulk-insert per-poll candidate evaluations (§73). `rows` are dicts from
    SignalDetector.last_candidates; ts/session_date stamp the whole batch."""
    if not rows:
        return
    from psycopg2.extras import execute_values
    values = [(
        ts, session_date, r['symbol'], r['candidate_side'], r['level_label'],
        r['strike'], r['spot'], r['dist_pct'], r['near_level'],
        r['contract_low_distance'], r['contract_near_low'], r['valid_volume_event'],
        r['already_alerted'], r['alert_fired'], r['signal_type'], r['blocked_reason'],
        r.get('hv_pctile'), r['atm_vol_1m'], r['win_vol'], r['active_bars'],
        r.get('gate_path'), r.get('gold_standard'), r.get('pending'),
        r.get('trigger_volume'), r.get('trigger_ratio'),
        r.get('premium_notional'), r.get('peak_1m'), r.get('vol_3m'), r.get('vol_5m'),
        r.get('event_share'), r.get('persistent_bg'), r.get('bar_status'),
        r.get('observed_vol'), r.get('completed_vol'), r.get('classification'),
    ) for r in rows]
    sql = """
        INSERT INTO signal_candidates
            (ts, session_date, symbol, candidate_side, level_label, strike, spot, dist_pct,
             near_level, contract_low_distance, contract_near_low, valid_volume_event,
             already_alerted, alert_fired, signal_type, blocked_reason, hv_pctile,
             atm_vol_1m, win_vol, active_bars,
             gate_path, gold_standard, pending, trigger_volume, trigger_ratio,
             premium_notional, peak_1m, vol_3m, vol_5m,
             event_share, persistent_bg, bar_status, observed_vol, completed_vol, classification)
        VALUES %s
    """
    conn = _get()
    try:
        with conn.cursor() as cur:
            execute_values(cur, sql, values)
        conn.commit()
    finally:
        _put(conn)


def save_research_finding(f: dict) -> Optional[int]:
    """Insert a Claude research finding/hypothesis into the journal (§30/§49)."""
    from psycopg2.extras import Json
    row = dict(f)
    for k in ('supporting_metrics_json', 'proposed_change_json', 'backtest_request_json'):
        if isinstance(row.get(k), (dict, list)):
            row[k] = Json(row[k])
    for k in ('session_date', 'category', 'observation', 'evidence_ids',
              'supporting_metrics_json', 'proposed_change_json', 'expected_benefit',
              'possible_cost', 'backtest_request_json', 'confidence'):
        row.setdefault(k, None)
    row.setdefault('status', 'PROPOSED')
    sql = """
        INSERT INTO research_findings
            (session_date, category, observation, evidence_ids, supporting_metrics_json,
             proposed_change_json, expected_benefit, possible_cost, backtest_request_json,
             confidence, status)
        VALUES (%(session_date)s, %(category)s, %(observation)s, %(evidence_ids)s,
                %(supporting_metrics_json)s, %(proposed_change_json)s, %(expected_benefit)s,
                %(possible_cost)s, %(backtest_request_json)s, %(confidence)s, %(status)s)
        RETURNING finding_id
    """
    conn = _get()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, row)
            fid = cur.fetchone()[0]
        conn.commit()
        return fid
    finally:
        _put(conn)


def get_research_findings(status: Optional[str] = None, limit: int = 50) -> list:
    """Read journal findings (most recent first), optionally filtered by status."""
    sql = ("SELECT finding_id, session_date, category, observation, confidence, status, created_at "
           "FROM research_findings")
    params: list = []
    if status:
        sql += " WHERE status = %s"
        params.append(status)
    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)
    conn = _get()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        _put(conn)


def get_oi_changes_today(symbol: str, snap_date: date) -> dict:
    """
    Return {(strike_float, option_type): {'oi_change': int, 'oi_change_pct': float}}
    for all contracts in today's option_chain_snapshots that have a non-NULL oi_change.
    Used by compute_secondary_watchlist's OI_BUILDUP tier after reconcile_oi_changes.
    Returns {} on any error (e.g. first session with no prior-day data).
    """
    sql = """
        SELECT DISTINCT ON (strike, option_type)
            strike, option_type, oi_change, oi_change_pct
        FROM option_chain_snapshots
        WHERE symbol = %s AND snap_date = %s
          AND oi_change IS NOT NULL
        ORDER BY strike, option_type, snap_time DESC
    """
    conn = _get()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (symbol, snap_date))
            return {
                (float(r[0]), r[1]): {
                    'oi_change':     int(r[2]),
                    'oi_change_pct': float(r[3]) if r[3] is not None else None,
                }
                for r in cur.fetchall()
            }
    except Exception:
        logger.warning("get_oi_changes_today failed for %s %s", symbol, snap_date, exc_info=True)
        return {}
    finally:
        _put(conn)


def save_secondary_oi_levels(
    symbol: str,
    level_date: date,
    computed_at: datetime,
    levels: list,
) -> None:
    """Upsert secondary OI watchlist rows (EXTENDED_RANK, OUTER_WALL, OI_BUILDUP)."""
    if not levels:
        return
    rows = [
        (
            symbol,
            level_date,
            lv['watchlist_tier'],
            float(lv['strike']),
            lv['option_type'],
            lv.get('open_interest'),
            lv.get('oi_change'),
            lv.get('oi_change_pct'),
            lv.get('distance_pct'),
            lv.get('band_rank'),
            lv.get('expiry'),
            computed_at,
        )
        for lv in levels
    ]
    sql = """
        INSERT INTO secondary_oi_levels
            (symbol, level_date, watchlist_tier, strike, option_type,
             open_interest, oi_change, oi_change_pct, distance_pct, band_rank,
             expiry, computed_at)
        VALUES %s
        ON CONFLICT (symbol, level_date, watchlist_tier, strike, option_type) DO UPDATE SET
            open_interest = EXCLUDED.open_interest,
            oi_change     = EXCLUDED.oi_change,
            oi_change_pct = EXCLUDED.oi_change_pct,
            distance_pct  = EXCLUDED.distance_pct,
            band_rank     = EXCLUDED.band_rank,
            expiry        = EXCLUDED.expiry,
            computed_at   = EXCLUDED.computed_at
    """
    conn = _get()
    try:
        with conn.cursor() as cur:
            execute_values(cur, sql, rows)
        conn.commit()
        logger.info("Saved %d secondary OI levels for %s", len(rows), symbol)
    finally:
        _put(conn)


def get_secondary_oi_levels(symbol: str, level_date: date) -> list:
    """Return all secondary OI watchlist rows for a symbol on the given date."""
    sql = """
        SELECT watchlist_tier, strike, option_type, open_interest,
               oi_change, oi_change_pct, distance_pct, band_rank, expiry
        FROM   secondary_oi_levels
        WHERE  symbol = %s AND level_date = %s
        ORDER  BY watchlist_tier, band_rank
    """
    conn = _get()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, (symbol, level_date))
            return cur.fetchall()
    finally:
        _put(conn)


def reconcile_prior_oi_events(symbol: str, today: date) -> int:
    """
    §37 Next-day intent reconciliation: update yesterday's oi_events with the
    confirmed overnight OI change now visible in today's option_chain_snapshots.

    Sets confirmed_oi_change, reconciled_intent (CONFIRMED_OPENING /
    CONFIRMED_CLOSING / NO_CHANGE), prediction_correct, and reconciled_at.
    Only processes rows where reconciled_at IS NULL.
    Returns the number of rows updated.
    """
    sql = """
        UPDATE oi_events e
        SET
            confirmed_oi_change     = s.oi_change,
            confirmed_oi_change_pct = s.oi_change_pct,
            reconciled_intent = CASE
                WHEN s.oi_change > 0  THEN 'CONFIRMED_OPENING'
                WHEN s.oi_change < 0  THEN 'CONFIRMED_CLOSING'
                ELSE 'NO_CHANGE'
            END,
            prediction_correct = CASE
                WHEN e.live_intent ILIKE 'OPENING_%%' AND s.oi_change > 0  THEN TRUE
                WHEN e.live_intent ILIKE 'CLOSING_%%' AND s.oi_change < 0  THEN TRUE
                WHEN e.live_intent = 'MIXED_OR_UNKNOWN'                     THEN NULL
                ELSE FALSE
            END,
            reconciled_at = NOW()
        FROM (
            SELECT DISTINCT ON (strike, option_type)
                strike, option_type, oi_change, oi_change_pct
            FROM option_chain_snapshots
            WHERE symbol = %s AND snap_date = %s AND oi_change IS NOT NULL
            ORDER BY strike, option_type, snap_time DESC
        ) s
        WHERE e.symbol      = %s
          AND e.session_date = (%s::date - INTERVAL '1 day')::date
          AND e.strike      = s.strike
          AND e.option_type = s.option_type
          AND e.reconciled_at IS NULL
    """
    conn = _get()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (symbol, today, symbol, today))
            count = cur.rowcount
        conn.commit()
        if count:
            logger.info("OI event reconciliation: %s %s — %d rows updated", symbol, today, count)
        return count
    finally:
        _put(conn)


def save_volume_leadership(
    symbol: str,
    bar_time,
    session_date: date,
    spot: float,
    scores: dict,
) -> None:
    """Upsert per-minute call/put leadership row for §41 (every poll, all symbols)."""
    sql = """
        INSERT INTO volume_leadership
            (symbol, bar_time, session_date, call_leadership, put_leadership,
             leadership_diff, dominant_side, call_vol_5m, put_vol_5m, spot)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (symbol, bar_time) DO UPDATE SET
            call_leadership = EXCLUDED.call_leadership,
            put_leadership  = EXCLUDED.put_leadership,
            leadership_diff = EXCLUDED.leadership_diff,
            dominant_side   = EXCLUDED.dominant_side,
            call_vol_5m     = EXCLUDED.call_vol_5m,
            put_vol_5m      = EXCLUDED.put_vol_5m,
            spot            = EXCLUDED.spot
    """
    conn = _get()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (
                symbol, bar_time, session_date,
                scores.get('call_leadership'), scores.get('put_leadership'),
                scores.get('leadership_diff'), scores.get('dominant_side'),
                scores.get('call_vol_5m'), scores.get('put_vol_5m'),
                round(float(spot), 4),
            ))
        conn.commit()
    finally:
        _put(conn)


def save_signal_volume_analytics(rows: list) -> None:
    """Bulk upsert §26-§29/§31 volume analytics per signal (one row per signal_id)."""
    if not rows:
        return
    _COLS = (
        'signal_id', 'session_date', 'symbol',
        'vol_2m', 'vol_3m', 'vol_5m', 'vol_10m', 'vol_15m', 'vol_30m',
        'ratio_2m', 'ratio_5m', 'ratio_10m', 'ratio_15m', 'ratio_30m',
        'volume_shape', 'shape_hhi', 'burst_ratio', 'staircase_score',
        'normalized_entropy',
        'atm_vol_share', 'itm_vol_share', 'otm_vol_share',
        'strike_volume_center', 'center_vs_spot',
        'vol_center_change', 'vol_migration_direction',
    )
    ph = ','.join(['%s'] * len(_COLS))
    values = [tuple(r.get(c) for c in _COLS) for r in rows]
    sql = f"""
        INSERT INTO signal_volume_analytics ({','.join(_COLS)})
        VALUES ({ph})
        ON CONFLICT (signal_id) DO UPDATE SET
            vol_2m=EXCLUDED.vol_2m, vol_3m=EXCLUDED.vol_3m, vol_5m=EXCLUDED.vol_5m,
            vol_10m=EXCLUDED.vol_10m, vol_15m=EXCLUDED.vol_15m, vol_30m=EXCLUDED.vol_30m,
            ratio_2m=EXCLUDED.ratio_2m, ratio_5m=EXCLUDED.ratio_5m,
            ratio_10m=EXCLUDED.ratio_10m, ratio_15m=EXCLUDED.ratio_15m,
            ratio_30m=EXCLUDED.ratio_30m, volume_shape=EXCLUDED.volume_shape,
            shape_hhi=EXCLUDED.shape_hhi, burst_ratio=EXCLUDED.burst_ratio,
            staircase_score=EXCLUDED.staircase_score,
            normalized_entropy=EXCLUDED.normalized_entropy,
            atm_vol_share=EXCLUDED.atm_vol_share, itm_vol_share=EXCLUDED.itm_vol_share,
            otm_vol_share=EXCLUDED.otm_vol_share,
            strike_volume_center=EXCLUDED.strike_volume_center,
            center_vs_spot=EXCLUDED.center_vs_spot,
            vol_center_change=EXCLUDED.vol_center_change,
            vol_migration_direction=EXCLUDED.vol_migration_direction
    """
    conn = _get()
    try:
        with conn.cursor() as cur:
            execute_values(cur, sql, values)
        conn.commit()
        logger.info("Saved %d signal_volume_analytics rows", len(rows))
    finally:
        _put(conn)


def save_volume_events(rows: list) -> None:
    """Bulk insert §32 volume event archive rows (idempotent — caller deletes first on re-run)."""
    if not rows:
        return
    _COLS = (
        'symbol', 'session_date', 'event_time', 'occ_symbol',
        'strike', 'option_type', 'expiry', 'event_type',
        'trigger_volume', 'trigger_ratio', 'mark_at_event', 'low_dist',
        'volume_shape', 'normalized_entropy',
        'led_to_signal', 'signal_id',
        'return_5m', 'return_15m', 'return_30m', 'mfe_pct', 'mae_pct',
    )
    ph = ','.join(['%s'] * len(_COLS))
    values = [tuple(r.get(c) for c in _COLS) for r in rows]
    sql = f"""
        INSERT INTO volume_events ({','.join(_COLS)})
        VALUES ({ph})
    """
    conn = _get()
    try:
        with conn.cursor() as cur:
            execute_values(cur, sql, values)
        conn.commit()
        logger.info("Saved %d volume_events rows", len(rows))
    finally:
        _put(conn)


def get_today_pc_ratio(symbol: str, snap_date: date) -> Optional[float]:
    """Return today's P/C ratio for a symbol, or None if not yet computed."""
    sql = """
        SELECT pc_ratio FROM morning_sentiment
        WHERE symbol = %s AND snap_date = %s
    """
    conn = _get()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (symbol, snap_date))
            row = cur.fetchone()
        return float(row[0]) if row else None
    finally:
        _put(conn)


def get_fired_directions_today(symbol: str, day: date) -> dict[str, list[str]]:
    """
    Confidences already fired for a symbol today, grouped by direction.

    Returns {signal_type: [confidence, ...]} from the signals table for `day`.
    Backs the detector's durable dedup so a restarted or second concurrent
    process sees what was already alerted and does not re-fire the same
    direction (the in-memory _fired_today alone is lost on restart and is
    per-process). Returns {} on any error so the caller falls back to in-memory.
    """
    sql = """
        SELECT signal_type, confidence
        FROM signals
        WHERE symbol = %s AND signal_time::date = %s
    """
    conn = _get()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (symbol, day))
            rows = cur.fetchall()
        out: dict[str, list[str]] = {}
        for signal_type, confidence in rows:
            out.setdefault(signal_type, []).append(confidence)
        return out
    finally:
        _put(conn)


def mark_signal_logged(signal_id: int) -> None:
    """Set sheets_logged=TRUE on a signal after it has been written to Google Sheets."""
    conn = _get()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE signals SET sheets_logged = TRUE WHERE id = %s",
                (signal_id,),
            )
        conn.commit()
    finally:
        _put(conn)


# ── Volume cluster positioning monitor ───────────────────────────────────────

def insert_cluster(cluster: dict) -> int:
    """Insert a new FORMING cluster. Returns the new row id."""
    sql = """
        INSERT INTO volume_clusters
            (symbol, detected_at, updated_at, pattern_type, option_type, strike, expiry,
             underlying_price, cluster_volume, bar_count, avg_vol_per_bar, status,
             nearest_sr_level, nearest_sr_strike, distance_from_price_pct)
        VALUES
            (%(symbol)s, %(detected_at)s, %(updated_at)s, %(pattern_type)s,
             %(option_type)s, %(strike)s, %(expiry)s, %(underlying_price)s,
             %(cluster_volume)s, %(bar_count)s, %(avg_vol_per_bar)s, %(status)s,
             %(nearest_sr_level)s, %(nearest_sr_strike)s, %(distance_from_price_pct)s)
        RETURNING id
    """
    conn = _get()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, cluster)
            row_id = cur.fetchone()[0]
        conn.commit()
        logger.info(
            "Cluster inserted: id=%d  %s %s %s@%.2f  status=%s",
            row_id, cluster['symbol'], cluster['pattern_type'],
            cluster['option_type'], cluster['strike'], cluster['status'],
        )
        return row_id
    finally:
        _put(conn)


def update_cluster(cluster_id: int, data: dict) -> None:
    """Partial update of an existing cluster row (status, volume, bar_count, etc.)."""
    allowed = {
        'status', 'cluster_volume', 'bar_count', 'avg_vol_per_bar',
        'underlying_price', 'updated_at',
    }
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return
    set_clause = ', '.join(f"{k} = %({k})s" for k in updates)
    sql = f"UPDATE volume_clusters SET {set_clause} WHERE id = %(id)s"
    updates['id'] = cluster_id
    conn = _get()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, updates)
        conn.commit()
    finally:
        _put(conn)


def fade_cluster(cluster_id: int, now: datetime) -> None:
    """Mark an active cluster as FADED once below-threshold bars exceed CLUSTER_FADE."""
    conn = _get()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE volume_clusters SET status = 'FADED', updated_at = %s WHERE id = %s",
                (now, cluster_id),
            )
        conn.commit()
        logger.info("Cluster %d marked FADED", cluster_id)
    finally:
        _put(conn)


def get_active_clusters(symbol: str) -> list:
    """Return FORMING + CONFIRMED clusters for a symbol."""
    sql = """
        SELECT id, pattern_type, option_type, strike, expiry, status, bar_count,
               cluster_volume, nearest_sr_level, nearest_sr_strike
        FROM   volume_clusters
        WHERE  symbol = %s AND status IN ('FORMING','CONFIRMED')
        ORDER  BY updated_at DESC
    """
    conn = _get()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, (symbol,))
            return cur.fetchall()
    finally:
        _put(conn)


def get_option_hist_range(symbol: str, strike, option_type: str, before_date):
    """
    A contract's FULL-history (low, high) from option_level_bars.

    Returns (min_low, max_high) over ALL stored prior sessions (every level_date <
    before_date) for (symbol, strike, option_type), or None if there are none.
    Backs the §13 historical-value gate's "at/near relative historical low"
    requirement: with Schwab serving no live option price-history, this is the
    deepest look-back available — the contract's relative low/high over everything
    we've stored. Matched by strike + type (not expiry), so a 0DTE strike inherits
    its same-strike history across expiries.
    """
    sql = """
        SELECT MIN(low), MAX(high)
        FROM   option_level_bars
        WHERE  symbol = %s AND strike = %s AND option_type = %s
          AND  level_date < %s AND low > 0
    """
    conn = _get()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (symbol, strike, option_type, before_date))
            row = cur.fetchone()
        if not row or row[0] is None or row[1] is None:
            return None
        return (float(row[0]), float(row[1]))
    except Exception as exc:
        logger.warning("get_option_hist_range(%s %s %s) failed: %s",
                       symbol, strike, option_type, exc)
        return None
    finally:
        _put(conn)


def get_signal_strength(signal_id: int) -> dict:
    """
    Return {'confidence': str, 'strong_cluster': bool} for one signal.

    Used by EOD logic to decide whether a losing, next-day-expiry position is
    strong enough to hold overnight. Returns {} on missing row or any error so
    the caller defaults to closing.
    """
    sql = "SELECT confidence, strong_cluster FROM signals WHERE id = %s"
    conn = _get()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (signal_id,))
            row = cur.fetchone()
        if not row:
            return {}
        return {'confidence': row[0], 'strong_cluster': bool(row[1])}
    except Exception as exc:
        logger.warning("get_signal_strength(%s) failed: %s", signal_id, exc)
        return {}
    finally:
        _put(conn)


def get_open_trades(symbol: str = None) -> list:
    """
    Return all trades that still have unfilled exits (for exit monitoring).
    Filters by symbol when provided.
    """
    sql = """
        SELECT id, signal_id, symbol, occ_symbol, signal_type,
               qty, limit_price, paper,
               exit1_underlying, exit2_underlying,
               exit1_qty, exit2_qty,
               exit1_filled, exit2_filled,
               stoploss_price, strike, option_type, expiry
        FROM   trades
        WHERE  status = 'placed'
          AND  (exit1_filled = FALSE OR exit2_filled = FALSE)
    """
    params = []
    if symbol:
        sql += " AND symbol = %s"
        params.append(symbol)
    conn = _get()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        _put(conn)


def count_open_trades() -> int:
    """
    Count currently-open positions (status='placed', not fully exited).

    Written synchronously by save_trade and cleared on close, so this reflects
    orders placed earlier in the same poll cycle without the lag of Alpaca's
    positions endpoint — used for the atomic MAX_OPEN_POSITIONS cap.
    """
    sql = """
        SELECT COUNT(*) FROM trades
        WHERE status = 'placed' AND (exit1_filled = FALSE OR exit2_filled = FALSE)
    """
    conn = _get()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            return int(cur.fetchone()[0])
    finally:
        _put(conn)


def mark_exit1_filled(trade_id: int, filled_at: datetime) -> None:
    """Record that the first half of the position was sold."""
    conn = _get()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE trades SET exit1_filled=TRUE, exit1_filled_at=%s WHERE id=%s",
                (filled_at, trade_id),
            )
        conn.commit()
    finally:
        _put(conn)


def mark_exit2_filled(trade_id: int, filled_at: datetime) -> None:
    """Record that the second half of the position was sold; mark trade closed."""
    conn = _get()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE trades
                   SET exit2_filled=TRUE, exit2_filled_at=%s, status='closed'
                   WHERE id=%s""",
                (filled_at, trade_id),
            )
        conn.commit()
    finally:
        _put(conn)


def mark_trade_eod_closed(trade_id: int, closed_at: datetime) -> None:
    """Mark a trade as EOD-liquidated."""
    conn = _get()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE trades SET status='eod_closed' WHERE id=%s",
                (trade_id,),
            )
        conn.commit()
    finally:
        _put(conn)


def update_stoploss(trade_id: int, new_price: float) -> None:
    """Move the stoploss to a new option mark level (e.g. breakeven after exit 1)."""
    conn = _get()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE trades SET stoploss_price=%s WHERE id=%s",
                (new_price, trade_id),
            )
        conn.commit()
        logger.info("Trade %d stoploss moved to %.4f", trade_id, new_price)
    finally:
        _put(conn)


def mark_trade_stopped(trade_id: int, stopped_at: datetime) -> None:
    """Mark a trade as stopped out; removes it from open-trade monitoring."""
    conn = _get()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE trades
                   SET status='stopped_out', exit2_filled=TRUE, exit2_filled_at=%s
                   WHERE id=%s""",
                (stopped_at, trade_id),
            )
        conn.commit()
        logger.info("Trade %d marked stopped_out", trade_id)
    finally:
        _put(conn)


def save_trade(trade: dict) -> int:
    """Insert an Alpaca order record tied to the originating signal. Returns trade id."""
    sql = """
        INSERT INTO trades
            (signal_id, symbol, occ_symbol, alpaca_order_id,
             qty, limit_price, buying_power_used, paper, status,
             signal_type, exit1_underlying, exit2_underlying,
             exit1_qty, exit2_qty,
             stoploss_price, strike, option_type, expiry)
        VALUES
            (%(signal_id)s, %(symbol)s, %(occ_symbol)s, %(alpaca_order_id)s,
             %(qty)s, %(limit_price)s, %(buying_power_used)s, %(paper)s, %(status)s,
             %(signal_type)s, %(exit1_underlying)s, %(exit2_underlying)s,
             %(exit1_qty)s, %(exit2_qty)s,
             %(stoploss_price)s, %(strike)s, %(option_type)s, %(expiry)s)
        RETURNING id
    """
    conn = _get()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, trade)
            trade_id = cur.fetchone()[0]
        conn.commit()
        logger.info(
            "Trade saved: id=%d  %s  qty=%d  limit=%.4f  paper=%s",
            trade_id, trade['occ_symbol'], trade['qty'],
            trade['limit_price'], trade['paper'],
        )
        return trade_id
    finally:
        _put(conn)


def get_last_signal_time(
    symbol: str, level_type: str, level_price: float
) -> Optional[datetime]:
    """Return the most recent signal timestamp for a given symbol and level, or None."""
    sql = """
        SELECT signal_time
        FROM   signals
        WHERE  symbol = %s
          AND  level_type = %s
          AND  ABS(level_price - %s) / NULLIF(CAST(%s AS NUMERIC), 0) < 0.005
        ORDER  BY signal_time DESC
        LIMIT  1
    """
    conn = _get()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (symbol, level_type, level_price, level_price))
            row = cur.fetchone()
        return row[0] if row else None
    finally:
        _put(conn)


# ── Phase 6: Statistical Research Toolkit (§74-§77) ─────────────────────────

def save_permutation_test(
    session_date,
    symbol: Optional[str],
    test_name: str,
    result: dict,
) -> None:
    """§74 Upsert a permutation test result (unique on session_date + test_name + symbol)."""
    sql = """
        INSERT INTO research_permutation_tests
            (session_date, symbol, test_name, metric,
             n_observed, n_control, n_permutations,
             observed_metric, null_mean, null_std,
             p_value, effect_size, percentile_rank,
             ci_lower, ci_upper, significant)
        VALUES
            (%s, %s, %s, %s,
             %s, %s, %s,
             %s, %s, %s,
             %s, %s, %s,
             %s, %s, %s)
        ON CONFLICT DO NOTHING
    """
    conn = _get()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (
                session_date, symbol, test_name, result.get('metric'),
                result.get('n_observed'), result.get('n_control'),
                result.get('n_permutations'),
                result.get('observed_metric'), result.get('null_mean'),
                result.get('null_std'), result.get('p_value'),
                result.get('effect_size'), result.get('percentile_rank'),
                result.get('ci_lower'), result.get('ci_upper'),
                result.get('significant'),
            ))
        conn.commit()
    finally:
        _put(conn)


def save_monte_carlo_result(
    session_date,
    symbol: Optional[str],
    result: dict,
) -> None:
    """§75 Insert a Monte Carlo simulation result."""
    sql = """
        INSERT INTO research_monte_carlo
            (session_date, symbol,
             n_trades, n_simulations, starting_capital,
             expected_return, median_return,
             probability_of_loss, probability_of_ruin, target_hit_probability,
             max_drawdown_p5, max_drawdown_p50, max_drawdown_p95,
             ci_lower_95, ci_upper_95)
        VALUES
            (%s, %s,
             %s, %s, %s,
             %s, %s,
             %s, %s, %s,
             %s, %s, %s,
             %s, %s)
    """
    conn = _get()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (
                session_date, symbol,
                result.get('n_trades'), result.get('n_simulations'),
                result.get('starting_capital'),
                result.get('expected_return'), result.get('median_return'),
                result.get('probability_of_loss'), result.get('probability_of_ruin'),
                result.get('target_hit_probability'),
                result.get('max_drawdown_p5'), result.get('max_drawdown_p50'),
                result.get('max_drawdown_p95'),
                result.get('ci_lower_95'), result.get('ci_upper_95'),
            ))
        conn.commit()
    finally:
        _put(conn)


def save_change_points(
    session_date,
    symbol: str,
    option_side: str,
    result: dict,
    pen: float,
) -> None:
    """§77 Insert a change-point detection result."""
    import json as _json
    sql = """
        INSERT INTO research_change_points
            (session_date, symbol, option_side,
             n_bars, n_breakpoints, breakpoint_indices,
             pre_regime_mean, post_regime_mean, regime_change_ratio,
             concentrated_event_detected, model_used, pen)
        VALUES
            (%s, %s, %s,
             %s, %s, %s::jsonb,
             %s, %s, %s,
             %s, %s, %s)
    """
    conn = _get()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (
                session_date, symbol, option_side,
                result.get('n_bars'), result.get('n_breakpoints'),
                _json.dumps(result.get('breakpoint_indices', [])),
                result.get('pre_regime_mean'), result.get('post_regime_mean'),
                result.get('regime_change_ratio'),
                result.get('concentrated_event_detected'),
                result.get('model_used'), pen,
            ))
        conn.commit()
    finally:
        _put(conn)
