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
    sql = """
        INSERT INTO signals
            (symbol, signal_time, signal_type, bias, level_type, level_price,
             trigger_price, avg_volume_20, spike_volume, consecutive_spikes,
             option_type, opt_mark, opt_bid, opt_ask, opt_vol_delta,
             price_to_enter, price_to_exit)
        VALUES
            (%(symbol)s, %(signal_time)s, %(signal_type)s, %(bias)s,
             %(level_type)s, %(level_price)s, %(trigger_price)s,
             %(avg_volume_20)s, %(spike_volume)s, %(consecutive_spikes)s,
             %(option_type)s, %(opt_mark)s, %(opt_bid)s, %(opt_ask)s, %(opt_vol_delta)s,
             %(price_to_enter)s, %(price_to_exit)s)
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


def mark_signal_logged(signal_id: int) -> None:
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


def get_last_signal_time(
    symbol: str, level_type: str, level_price: float
) -> Optional[datetime]:
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
