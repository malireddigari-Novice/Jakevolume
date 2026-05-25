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
        )
        for c in contracts
    ]
    sql = """
        INSERT INTO option_chain_snapshots
            (symbol, snap_date, snap_time, expiry_date, strike, option_type,
             open_interest, volume, bid, ask, mark, underlying_price)
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

def save_bars(symbol: str, bars: list) -> None:
    """Bulk-insert 1-min OHLCV bars; silently skips bars already stored for the same timestamp."""
    if not bars:
        return
    rows = [
        (symbol, b['bar_time'], b['open'], b['high'], b['low'], b['close'], b['volume'])
        for b in bars
    ]
    sql = """
        INSERT INTO price_bars (symbol, bar_time, open, high, low, close, volume)
        VALUES %s
        ON CONFLICT (symbol, bar_time) DO NOTHING
    """
    conn = _get()
    try:
        with conn.cursor() as cur:
            execute_values(cur, sql, rows)
        conn.commit()
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
             atm_vol_1m, atm_spike_ratio, atm_vol_3m,
             itm_vol_1m, itm_spike_ratio, itm_vol_3m,
             spread_pct, low_dist, room_score, room_pct,
             pc_ratio, pc_conviction,
             opt_vol_delta, avg_volume_20, spike_volume, consecutive_spikes)
        VALUES
            (%(symbol)s, %(signal_time)s, %(signal_type)s, %(bias)s,
             %(level_type)s, %(level_price)s, %(trigger_price)s,
             %(option_type)s, %(opt_mark)s, %(opt_bid)s, %(opt_ask)s,
             %(price_to_enter)s, %(price_to_exit)s,
             %(prox_score)s, %(cluster_strength)s, %(strong_cluster)s, %(flow_shape)s,
             %(atm_vol_1m)s, %(atm_spike_ratio)s, %(atm_vol_3m)s,
             %(itm_vol_1m)s, %(itm_spike_ratio)s, %(itm_vol_3m)s,
             %(spread_pct)s, %(low_dist)s, %(room_score)s, %(room_pct)s,
             %(pc_ratio)s, %(pc_conviction)s,
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
               exit1_filled, exit2_filled
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


def save_trade(trade: dict) -> int:
    """Insert an Alpaca order record tied to the originating signal. Returns trade id."""
    sql = """
        INSERT INTO trades
            (signal_id, symbol, occ_symbol, alpaca_order_id,
             qty, limit_price, buying_power_used, paper, status,
             signal_type, exit1_underlying, exit2_underlying,
             exit1_qty, exit2_qty)
        VALUES
            (%(signal_id)s, %(symbol)s, %(occ_symbol)s, %(alpaca_order_id)s,
             %(qty)s, %(limit_price)s, %(buying_power_used)s, %(paper)s, %(status)s,
             %(signal_type)s, %(exit1_underlying)s, %(exit2_underlying)s,
             %(exit1_qty)s, %(exit2_qty)s)
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
