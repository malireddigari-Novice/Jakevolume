"""
Pure Databento market data client.

Morning setup  — prev_close only via XNAS.ITCH Historical (T+1 daily bar).
                 All other morning data comes from the Live feed below.

Live feed      — one XNAS.ITCH session + one OPRA.PILLAR session, each a daemon thread.
                 XNAS.ITCH   : ohlcv-1m  -> 1-min equity bars
                 OPRA.PILLAR : definition -> contract metadata + expiry catalogue
                               statistics -> real-time OI per contract (stat_type=9)
                               ohlcv-1m  -> intraday option close/volume at S/R strikes

get_option_chain()   reads Live _contract_buf + _oi_buf  (falls back to Historical on cold start)
get_nearest_expiry() reads Live _contract_buf            (falls back to Historical on cold start)
get_option_quotes_for_levels() reads Live _opt_buf
get_bars() / get_quote()       read Live _bar_buf

Requires a Databento live-data license on DATABENTO_API_KEY.
"""
import logging
import os
import threading
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone, date
from typing import Optional

import databento as db
import pandas as pd
from dotenv import load_dotenv

import config
from data.market_utils import CST, today_cst

load_dotenv()
logger = logging.getLogger(__name__)

UTC           = timezone.utc
_OI_STAT_TYPE = 9
_UNDEF_PRICE  = (1 << 63) - 1   # INT64_MAX — Databento sentinel "no price"
_BAR_MAXLEN   = 60               # ring-buffer depth: last 60 1-min bars per symbol


def _ts(dt: datetime) -> str:
    return dt.strftime('%Y-%m-%dT%H:%M:%S')


def _parse_db_date(end_str: str) -> date:
    """Databento end is exclusive midnight UTC; last available = end - 1 day."""
    clean = end_str.split('.')[0].replace('Z', '+00:00')
    return (datetime.fromisoformat(clean) - timedelta(days=1)).date()


def _fp(price_int: int) -> Optional[float]:
    """Databento fixed-point int64 -> USD float. Returns None for undefined prices."""
    if price_int >= _UNDEF_PRICE:
        return None
    return price_int * 1e-9


def _parse_osi(raw: str) -> Optional[tuple]:
    """
    Decode OSI raw_symbol -> (underlying, strike_usd, option_type, expiry_date).
    Format: '{underlying:6}{YYMMDD}{C|P}{strike_thousandths:08d}'
    'AAPL  260518C00300000'  ->  ('AAPL', 300.0, 'CALL', date(2026,5,18))
    """
    s = raw.strip()
    if len(s) < 21:
        return None
    underlying = s[:6].strip()
    opt_type   = 'CALL' if s[12] == 'C' else 'PUT'
    try:
        strike = int(s[13:21]) / 1000.0
        expiry = datetime.strptime(s[6:12], '%y%m%d').date()
    except (ValueError, IndexError):
        return None
    return (underlying, strike, opt_type, expiry)


class WebullClient:

    def __init__(self) -> None:
        key = os.environ.get('DATABENTO_API_KEY', '')
        if not key:
            raise RuntimeError("DATABENTO_API_KEY not set in .env")
        self._db_key        = key
        self._db            = db.Historical(key)   # Historical: prev_close + cold-start fallback
        self._last_db_date: dict[str, date] = {}

        # ── Live sessions (set by start_live_feed) ────────────────────────────
        self._live_eq:  Optional[db.Live] = None
        self._live_opt: Optional[db.Live] = None
        self._lock = threading.Lock()

        # ── Equity bar ring buffer ─────────────────────────────────────────────
        # symbol -> deque of bar dicts {bar_time, open, high, low, close, volume}
        self._bar_buf: dict[str, deque] = defaultdict(lambda: deque(maxlen=_BAR_MAXLEN))

        # ── Option chain buffers (from OPRA.PILLAR definition + statistics) ───
        # raw_symbol -> {eq_symbol, expiry, strike, opt_type}
        self._contract_buf: dict[str, dict] = {}
        # raw_symbol -> OI count (from statistics stat_type=9)
        self._oi_buf: dict[str, int] = {}

        # ── Option intraday bar buffer (from OPRA.PILLAR ohlcv-1m) ────────────
        # (eq_sym, strike, opt_type) -> {mark, bid, ask, volume}
        self._opt_buf: dict[tuple, dict] = {}

        # ── Expiry cache ──────────────────────────────────────────────────────
        self._expiry_cache: dict[str, date] = {}

    # ── Login / startup ───────────────────────────────────────────────────────

    def login(self, interactive: bool = True) -> None:
        logger.info(
            "Databento client ready  "
            "[Historical: prev_close only]  "
            "[Live: chain defs / OI / equity bars / option bars]"
        )

    def start_live_feed(self) -> None:
        """
        Open two Databento Live sessions (daemon threads).
        XNAS.ITCH   — ohlcv-1m equity bars
        OPRA.PILLAR — definition + statistics + ohlcv-1m (option chain, OI, bars)
        Requires a live-data license on the API key.
        """
        logger.info("Starting Databento Live feeds...")
        self._start_equity_feed()
        self._start_options_feed()
        logger.info("Live feeds started — XNAS.ITCH ohlcv-1m | OPRA.PILLAR def+stats+ohlcv-1m")

    # ── Live session builders ─────────────────────────────────────────────────

    def _start_equity_feed(self) -> None:
        self._live_eq = db.Live(key=self._db_key)
        self._live_eq.subscribe(
            dataset="XNAS.ITCH",
            schema="ohlcv-1m",
            symbols=config.SYMBOLS,
            stype_in="raw_symbol",
        )
        threading.Thread(target=self._equity_consumer,
                         name="db-live-equity", daemon=True).start()

    def _start_options_feed(self) -> None:
        opt_syms = [f"{s}.OPT" for s in config.SYMBOLS]
        self._live_opt = db.Live(key=self._db_key)
        # Three schemas on one OPRA.PILLAR session
        self._live_opt.subscribe(
            dataset="OPRA.PILLAR", schema="definition",
            symbols=opt_syms, stype_in="parent",
        )
        self._live_opt.subscribe(
            dataset="OPRA.PILLAR", schema="statistics",
            symbols=opt_syms, stype_in="parent",
        )
        self._live_opt.subscribe(
            dataset="OPRA.PILLAR", schema="ohlcv-1m",
            symbols=opt_syms, stype_in="parent",
        )
        threading.Thread(target=self._options_consumer,
                         name="db-live-options", daemon=True).start()

    # ── Live consumers ────────────────────────────────────────────────────────

    def _equity_consumer(self) -> None:
        """Drain XNAS.ITCH ohlcv-1m stream into _bar_buf."""
        try:
            for record in self._live_eq:
                if not hasattr(record, 'close'):
                    continue
                iid      = record.instrument_id
                sym_info = self._live_eq.symbology_map.get(iid)
                symbol   = str(sym_info.raw_symbol).strip() if sym_info else ''
                if symbol not in config.SYMBOLS:
                    continue
                ts  = datetime.fromtimestamp(record.ts_event * 1e-9, tz=UTC).astimezone(CST)
                bar = {
                    'bar_time': ts,
                    'open':     _fp(record.open)  or 0.0,
                    'high':     _fp(record.high)  or 0.0,
                    'low':      _fp(record.low)   or 0.0,
                    'close':    _fp(record.close) or 0.0,
                    'volume':   int(record.volume),
                }
                with self._lock:
                    self._bar_buf[symbol].append(bar)
        except Exception as exc:
            logger.error("Equity Live consumer error: %s", exc, exc_info=True)

    def _options_consumer(self) -> None:
        """
        Drain OPRA.PILLAR stream.
        Routes each record to the correct buffer by its schema signature:
          definition  -> _contract_buf
          statistics  -> _oi_buf  (stat_type == 9 only)
          ohlcv-1m    -> _opt_buf
        """
        try:
            for record in self._live_opt:
                iid      = record.instrument_id
                sym_info = self._live_opt.symbology_map.get(iid)
                raw_sym  = str(sym_info.raw_symbol).strip() if sym_info else ''

                with self._lock:
                    # ── Contract definition ──
                    if _is_definition(record):
                        self._process_definition(record, raw_sym)
                    # ── OI statistics ──
                    elif _is_statistic(record):
                        if getattr(record, 'stat_type', None) == _OI_STAT_TYPE and raw_sym:
                            oi = int(getattr(record, 'quantity', 0) or 0)
                            self._oi_buf[raw_sym] = oi
                    # ── Intraday option bar ──
                    elif _is_ohlcv(record) and raw_sym:
                        parsed = _parse_osi(raw_sym)
                        if parsed:
                            eq_sym, strike, opt_type, expiry = parsed
                            if eq_sym in config.SYMBOLS:
                                self._opt_buf[(eq_sym, strike, opt_type, expiry)] = {
                                    'mark':   _fp(record.close) or 0.0,
                                    'bid':    None,
                                    'ask':    None,
                                    'volume': int(record.volume),
                                }
        except Exception as exc:
            logger.error("Options Live consumer error: %s", exc, exc_info=True)

    def _process_definition(self, record, raw_sym: str) -> None:
        """Store one contract definition into _contract_buf (called under _lock)."""
        if not raw_sym:
            return
        parsed = _parse_osi(raw_sym)
        if parsed is None:
            return
        eq_sym, strike, opt_type, osi_expiry = parsed
        if eq_sym not in config.SYMBOLS:
            return
        try:
            # Prefer expiration from the record (authoritative); fall back to OSI date.
            expiry = datetime.fromtimestamp(
                record.expiration * 1e-9, tz=UTC
            ).date()
        except Exception:
            expiry = osi_expiry
        self._contract_buf[raw_sym] = {
            'raw_symbol':  raw_sym,
            'eq_symbol':   eq_sym,
            'expiry':      expiry,
            'strike':      strike,
            'option_type': opt_type,
        }

    # ── Historical helpers (prev_close + cold-start fallback) ─────────────────

    def _db_last_date(self, dataset: str) -> date:
        if dataset not in self._last_db_date:
            info = self._db.metadata.get_dataset_range(dataset=dataset)
            self._last_db_date[dataset] = _parse_db_date(info['end'])
            logger.info("Databento %s last available date: %s",
                        dataset, self._last_db_date[dataset])
        return self._last_db_date[dataset]

    def _db_day_range(self, d: date):
        start = datetime(d.year, d.month, d.day, tzinfo=UTC)
        return start, start + timedelta(days=1)

    def _hist_nearest_expiry(self, symbol: str) -> date:
        """Historical fallback for get_nearest_expiry (used on cold start)."""
        prev_day   = self._db_last_date("OPRA.PILLAR")
        start, end = self._db_day_range(prev_day)
        df = self._db.timeseries.get_range(
            dataset="OPRA.PILLAR",
            symbols=[f"{symbol}.OPT"],
            schema="definition",
            start=_ts(start), end=_ts(end),
            stype_in="parent",
        ).to_df()
        if df.empty or 'expiration' not in df.columns:
            raise ValueError(f"No OPRA definitions for {symbol} (Historical fallback)")
        today   = today_cst()
        expiries = sorted(pd.to_datetime(df['expiration']).dt.date.unique())
        for exp in expiries:
            if exp >= today:
                label = "0DTE" if exp == today else "next"
                logger.info("%s: expiry = %s (%s) [Historical fallback]", symbol, exp, label)
                return exp
        raise ValueError(f"No upcoming expiry for {symbol}: {expiries[:5]}")

    def _hist_option_chain(self, symbol: str, expiry: date) -> dict:
        """Historical fallback for get_option_chain (used on cold start)."""
        prev_day   = self._db_last_date("OPRA.PILLAR")
        start, end = self._db_day_range(prev_day)

        def_df = self._db.timeseries.get_range(
            dataset="OPRA.PILLAR",
            symbols=[f"{symbol}.OPT"],
            schema="definition",
            start=_ts(start), end=_ts(end),
            stype_in="parent",
        ).to_df()
        if def_df.empty:
            raise ValueError(f"No OPRA definitions for {symbol} (Historical fallback)")

        def_df['expiry_date'] = pd.to_datetime(def_df['expiration']).dt.date
        def_df = def_df[def_df['expiry_date'] == expiry].drop_duplicates('raw_symbol')

        oi_map: dict[str, int] = {}
        try:
            stat_df = self._db.timeseries.get_range(
                dataset="OPRA.PILLAR",
                symbols=[f"{symbol}.OPT"],
                schema="statistics",
                start=_ts(start), end=_ts(end),
                stype_in="parent",
            ).to_df()
            if not stat_df.empty and 'stat_type' in stat_df.columns:
                oi_rows = stat_df[stat_df['stat_type'] == _OI_STAT_TYPE]
                for raw_sym, grp in oi_rows.groupby('symbol'):
                    oi_map[raw_sym.strip()] = int(grp['quantity'].iloc[0])
        except Exception as e:
            logger.warning("%s: Historical OI fetch failed: %s", symbol, e)

        calls, puts = [], []
        for _, row in def_df.iterrows():
            raw_sym    = str(row.get('raw_symbol', '')).strip()
            inst_class = str(row.get('instrument_class', '')).strip()
            strike     = float(row.get('strike_price', 0))
            opt_type   = 'CALL' if inst_class == 'C' else 'PUT'
            contract   = {
                'option_type':   opt_type,
                'strike':        strike,
                'open_interest': oi_map.get(raw_sym, 0),
                'volume': 0, 'bid': None, 'ask': None, 'mark': None,
            }
            (calls if opt_type == 'CALL' else puts).append(contract)

        logger.info("%s: chain expiry=%s calls=%d puts=%d [Historical fallback]",
                    symbol, expiry, len(calls), len(puts))
        return {
            'symbol': symbol, 'expiry': expiry,
            'calls': calls, 'puts': puts, 'all': calls + puts,
            'fetched_at': datetime.now(CST),
        }

    # ═════════════════════════════════════════════════════════════════════════
    # PUBLIC API
    # ═════════════════════════════════════════════════════════════════════════

    def get_prev_close(self, symbol: str) -> float:
        """Previous trading day's close from XNAS.ITCH Historical ohlcv-1d."""
        prev_day   = self._db_last_date("XNAS.ITCH")
        start, end = self._db_day_range(prev_day)
        df = self._db.timeseries.get_range(
            dataset="XNAS.ITCH",
            symbols=[symbol],
            schema="ohlcv-1d",
            start=_ts(start), end=_ts(end),
        ).to_df()
        if df.empty:
            raise ValueError(f"No daily bar for {symbol} in XNAS.ITCH")
        df.index = pd.to_datetime(df.index, utc=True)
        close = float(df.iloc[-1]['close'])
        logger.info("%s: prev_close = %.4f (%s)", symbol, close, prev_day)
        return close

    def get_nearest_expiry(self, symbol: str, force: bool = False) -> date:
        """
        0DTE expiry (today) if available, else next upcoming expiry.
        Reads from the Live _contract_buf; falls back to Historical on cold start.
        """
        cached = self._expiry_cache.get(symbol)
        if not force and cached and cached >= today_cst():
            return cached

        today = today_cst()

        # ── Live buffer ──────────────────────────────────────────────────────
        with self._lock:
            live_expiries = sorted({
                c['expiry'] for c in self._contract_buf.values()
                if c['eq_symbol'] == symbol and c['expiry'] >= today
            })

        if live_expiries:
            exp   = live_expiries[0]
            label = "0DTE" if exp == today else "next"
            logger.info("%s: expiry = %s (%s) [Live]", symbol, exp, label)
            self._expiry_cache[symbol] = exp
            return exp

        # ── Historical fallback ──────────────────────────────────────────────
        logger.info("%s: Live chain buffer empty, using Historical for expiry", symbol)
        exp = self._hist_nearest_expiry(symbol)
        self._expiry_cache[symbol] = exp
        return exp

    def get_option_chain(self, symbol: str, expiry: Optional[date] = None) -> dict:
        """
        Build option chain (definitions + OI) from the Live feed.
        Falls back to OPRA.PILLAR Historical if the Live buffer is empty
        (e.g. app just started before the session snapshot arrived).
        """
        if expiry is None:
            expiry = self.get_nearest_expiry(symbol)

        # ── Live buffer ──────────────────────────────────────────────────────
        with self._lock:
            snapshot = [
                {**c, 'open_interest': self._oi_buf.get(c['raw_symbol'], 0)}
                for c in self._contract_buf.values()
                if c['eq_symbol'] == symbol and c['expiry'] == expiry
            ]

        if snapshot:
            calls = [_fmt_contract(c) for c in snapshot if c['option_type'] == 'CALL']
            puts  = [_fmt_contract(c) for c in snapshot if c['option_type'] == 'PUT']
            logger.info("%s: chain expiry=%s calls=%d puts=%d  [Live]",
                        symbol, expiry, len(calls), len(puts))
            return {
                'symbol': symbol, 'expiry': expiry,
                'calls': calls, 'puts': puts, 'all': calls + puts,
                'fetched_at': datetime.now(CST),
            }

        # ── Historical fallback ──────────────────────────────────────────────
        logger.info("%s: Live chain buffer empty, using Historical for option chain", symbol)
        return self._hist_option_chain(symbol, expiry)

    def get_bars(self, symbol: str, count: int = None) -> list[dict]:
        """Latest 1-min OHLCV bars from the Live equity buffer, oldest-first."""
        count = count or config.BARS_TO_FETCH
        with self._lock:
            bars = list(self._bar_buf[symbol])
        bars.sort(key=lambda b: b['bar_time'])
        return bars[-count:]

    def get_quote(self, symbol: str) -> dict:
        """Current price from the latest Live equity bar; returns 0 pre-market."""
        bars = self.get_bars(symbol, count=1)
        if bars:
            b = bars[-1]
            return {
                'symbol': symbol, 'price': b['close'], 'volume': b['volume'],
                'open': b['open'], 'high': b['high'], 'low': b['low'],
                'fetched_at': b['bar_time'],
            }
        return {
            'symbol': symbol, 'price': 0.0, 'volume': 0,
            'open': 0.0, 'high': 0.0, 'low': 0.0,
            'fetched_at': datetime.now(CST),
        }

    def get_option_quotes_for_levels(
        self,
        symbol: str,
        expiry: date,
        levels: list,
    ) -> dict:
        """
        Live mark/volume for S/R strikes from the OPRA.PILLAR ohlcv-1m buffer.
        Returns {(strike, option_type): {bid, ask, mark, volume}}.
        """
        result: dict = {}
        with self._lock:
            for level in levels:
                strike      = float(level['strike'])
                option_type = str(level.get('option_type', ''))
                entry = self._opt_buf.get((symbol, strike, option_type, expiry))
                if entry:
                    result[(strike, option_type)] = dict(entry)
        return result

    def get_expiry_pair(self, symbol: str) -> tuple:
        """
        Return (today_expiry_or_None, next_expiry_or_None) from the Live buffer.
        Used by the positioning monitor to distinguish 0DTE vs next-expiry volume.
        """
        today = today_cst()
        with self._lock:
            upcoming = sorted({
                c['expiry'] for c in self._contract_buf.values()
                if c['eq_symbol'] == symbol and c['expiry'] >= today
            })
        today_exp = upcoming[0] if upcoming and upcoming[0] == today else None
        if today_exp:
            next_exp = upcoming[1] if len(upcoming) > 1 else None
        else:
            next_exp = upcoming[0] if upcoming else None
        return (today_exp, next_exp)

    def get_atm_option_quotes_all_expiries(
        self,
        symbol: str,
        underlying_price: float,
    ) -> dict:
        """
        Return all tracked option bars near ATM across all expiries.
        Result keyed by (strike, option_type, expiry) -> {mark, bid, ask, volume}.
        Used by PositioningMonitor to detect volume clustering on any expiry.
        """
        lo = underlying_price * (1 - config.ATM_RANGE_PCT)
        hi = underlying_price * (1 + config.ATM_RANGE_PCT)
        result: dict = {}
        with self._lock:
            for (eq_sym, strike, opt_type, expiry), bar in self._opt_buf.items():
                if eq_sym == symbol and lo <= strike <= hi:
                    result[(strike, opt_type, expiry)] = dict(bar)
        return result


# ── Module-level helpers ──────────────────────────────────────────────────────

def _is_definition(record) -> bool:
    return (hasattr(record, 'strike_price')
            and hasattr(record, 'instrument_class')
            and hasattr(record, 'expiration'))


def _is_statistic(record) -> bool:
    return hasattr(record, 'stat_type') and hasattr(record, 'quantity')


def _is_ohlcv(record) -> bool:
    return (hasattr(record, 'open') and hasattr(record, 'close')
            and hasattr(record, 'high') and hasattr(record, 'volume')
            and not hasattr(record, 'stat_type')
            and not hasattr(record, 'strike_price'))


def _fmt_contract(c: dict) -> dict:
    return {
        'option_type':   c['option_type'],
        'strike':        c['strike'],
        'open_interest': c.get('open_interest', 0),
        'volume':        0,
        'bid':           None,
        'ask':           None,
        'mark':          None,
    }
