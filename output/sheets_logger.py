"""
Google Sheets logging layer.

Sheet layout
------------
Daily_Levels  — one row per symbol per day: computed S/R levels + underlying price.
Signals       — one row per fired signal.
OI_Snapshot   — one row per symbol per day: top-3 call / put OI strikes near ATM.

Auth
----
Uses a Google Cloud service-account JSON key file.
The file path is read from config.GOOGLE_SERVICE_ACCOUNT_FILE.
Share the target spreadsheet with the service-account email (editor role).

Concurrency
-----------
All public log_* methods are non-blocking: they enqueue a (sheet_key, row) task
onto an internal queue.Queue and return immediately.  A single background daemon
thread serialises the actual API calls, so the main polling loop is never stalled
by Sheets I/O or a rate-limit back-off sleep.

Rate limiting
-------------
Google Sheets API allows ~300 writes/min per project.
_insert_row() retries on HTTP 429 with exponential back-off (up to 4 attempts).
connect() also retries on transient auth / network failures with back-off.
"""
import logging
import queue
import random
import threading
import time
from datetime import date, datetime
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

import config
from data.market_utils import CST

logger = logging.getLogger(__name__)

_SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive',
]

# ── Column headers ────────────────────────────────────────────────────────────

_HDR_DAILY_LEVELS = [
    'Date', 'Symbol', 'Computed_At_CST', 'Underlying_Price',
    'S1_Strike', 'S1_OI', 'S2_Strike', 'S2_OI',
    'R1_Strike', 'R1_OI', 'R2_Strike', 'R2_OI',
]

_HDR_SIGNALS = [
    'Datetime_CST', 'Contract', 'Option_Price_To_Enter', 'Option_Price_To_Exit',
]

_HDR_MORNING_SENTIMENT = [
    'Date', 'Computed_At_CST', 'Symbol',
    'Prev_Close', 'PM_Price', 'PM_Change_Pct',
    'Call_OI', 'Put_OI', 'PC_Ratio',
    'Drift_Score', 'PC_Score', 'Total_Score', 'Bias',
]

_HDR_OI_SNAPSHOT = [
    'Date', 'Time_CST', 'Symbol', 'Expiry',
    'Call_1_Strike', 'Call_1_OI',
    'Call_2_Strike', 'Call_2_OI',
    'Put_1_Strike',  'Put_1_OI',
    'Put_2_Strike',  'Put_2_OI',
    'Underlying_Price',
]

_HEADERS = {
    config.SHEET_NAMES['daily_levels']:      _HDR_DAILY_LEVELS,
    config.SHEET_NAMES['signals']:           _HDR_SIGNALS,
    config.SHEET_NAMES['oi_snapshot']:       _HDR_OI_SNAPSHOT,
    config.SHEET_NAMES['morning_sentiment']: _HDR_MORNING_SENTIMENT,
}


class SheetsLogger:

    def __init__(self) -> None:
        """Initialise the logger and start the background Sheets write worker."""
        self._gc: Optional[gspread.Client] = None
        self._ss: Optional[gspread.Spreadsheet] = None
        self._ws_cache: dict[str, gspread.Worksheet] = {}

        # Non-blocking write queue: public log_* methods enqueue tasks here;
        # _drain_queue() serialises all actual Sheets API calls in a daemon thread.
        self._write_queue: queue.Queue = queue.Queue()
        self._worker = threading.Thread(
            target=self._drain_queue,
            name="sheets-writer",
            daemon=True,
        )
        self._worker.start()

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self, max_retries: int = 4) -> None:
        """
        Authenticate with Google and open the configured spreadsheet.

        Retries with exponential back-off so a transient network error at
        startup does not crash the process before the main loop begins.
        """
        for attempt in range(max_retries):
            try:
                creds = Credentials.from_service_account_file(
                    config.GOOGLE_SERVICE_ACCOUNT_FILE,
                    scopes=_SCOPES,
                )
                self._gc = gspread.authorize(creds)
                self._ss = self._gc.open_by_key(config.GOOGLE_SPREADSHEET_ID)

                for sheet_key in config.SHEET_NAMES:
                    ws = self._ws(sheet_key)
                    headers = _HEADERS.get(config.SHEET_NAMES[sheet_key], [])
                    if headers:
                        ws.update([headers], 'A1')

                logger.info("Google Sheets connected: %s", self._ss.title)
                return

            except Exception as exc:
                if attempt < max_retries - 1:
                    wait = (2 ** attempt) + random.random()
                    logger.warning(
                        "Sheets connect failed (attempt %d/%d), retrying in %.1fs: %s",
                        attempt + 1, max_retries, wait, exc,
                    )
                    time.sleep(wait)
                else:
                    logger.error(
                        "Sheets connect failed after %d attempts: %s", max_retries, exc
                    )
                    raise

    # ── Public logging methods (all non-blocking) ─────────────────────────────

    def log_daily_levels(
        self,
        symbol: str,
        levels: list[dict],
        underlying_price: float,
        computed_at: datetime,
    ) -> None:
        """Enqueue a daily S/R level row; returns immediately without blocking."""
        supports    = _ranked(levels, 'SUPPORT')
        resistances = _ranked(levels, 'RESISTANCE')

        row = [
            computed_at.strftime('%Y-%m-%d'),
            symbol,
            computed_at.strftime('%Y-%m-%d %H:%M:%S'),
            round(underlying_price, 4),
        ] + _level_cols(supports, 2) + _level_cols(resistances, 2)

        self._enqueue('daily_levels', row)
        logger.info("Sheets: queued daily levels for %s", symbol)

    def log_signal(self, signal: dict) -> None:
        """Enqueue a fired signal row; returns immediately without blocking."""
        opt_char = 'C' if signal.get('option_type', '') == 'CALL' else 'P'
        expiry   = signal.get('expiry')
        expiry_s = expiry.strftime('%m/%d') if expiry else ''
        strike   = signal.get('level_price', '')
        contract = f"{signal['symbol']} {strike}{opt_char} {expiry_s}".strip()

        row = [
            signal['signal_time'].strftime('%Y-%m-%d %H:%M:%S'),
            contract,
            signal.get('price_to_enter', ''),
            signal.get('price_to_exit', ''),
        ]
        self._enqueue('signals', row)
        logger.info(
            "Sheets: queued signal  %s  enter=%s  exit=%s",
            contract,
            signal.get('price_to_enter', 'n/a'),
            signal.get('price_to_exit', 'n/a'),
        )

    def log_morning_sentiment(self, sentiment: dict, computed_at: datetime) -> None:
        """Enqueue a morning sentiment row; returns immediately without blocking."""
        row = [
            computed_at.strftime('%Y-%m-%d'),
            computed_at.strftime('%Y-%m-%d %H:%M:%S'),
            sentiment['symbol'],
            sentiment['prev_close'],
            sentiment['pm_price'],
            sentiment['pm_change_pct'],
            sentiment['call_oi'],
            sentiment['put_oi'],
            sentiment['pc_ratio'],
            sentiment['drift_score'],
            sentiment['pc_score'],
            sentiment['total_score'],
            sentiment['bias'],
        ]
        self._enqueue('morning_sentiment', row)
        logger.info(
            "Sheets: queued sentiment for %s => %s", sentiment['symbol'], sentiment['bias']
        )

    def log_oi_snapshot(
        self,
        symbol: str,
        expiry: date,
        top_calls: list[dict],
        top_puts: list[dict],
        underlying_price: float,
        snap_time: datetime,
    ) -> None:
        """Enqueue a top-OI snapshot row; returns immediately without blocking."""
        row = [
            snap_time.strftime('%Y-%m-%d'),
            snap_time.strftime('%H:%M:%S'),
            symbol,
            str(expiry),
        ] + _oi_cols(top_calls, 2) + _oi_cols(top_puts, 2) + [round(underlying_price, 4)]

        self._enqueue('oi_snapshot', row)
        logger.info("Sheets: queued OI snapshot for %s (expiry %s)", symbol, expiry)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _enqueue(self, sheet_key: str, row: list) -> None:
        """Add a (sheet_key, row) task to the write queue without blocking the caller."""
        self._write_queue.put((sheet_key, row))

    def _drain_queue(self) -> None:
        """
        Background daemon: dequeue and write rows to Sheets one at a time.

        Failures are logged but never stop the worker — a bad row is dropped
        so subsequent writes are not blocked.  The thread lives for the process
        lifetime (daemon=True).
        """
        while True:
            sheet_key, row = self._write_queue.get()
            try:
                self._insert_row(sheet_key, row)
            except Exception:
                logger.exception(
                    "Sheets background write failed for sheet '%s'", sheet_key
                )
            finally:
                self._write_queue.task_done()

    def _insert_row(self, sheet_key: str, row: list, max_retries: int = 4) -> None:
        """Insert at row 2 (newest-first) with exponential back-off on HTTP 429."""
        ws = self._ws(sheet_key)
        for attempt in range(max_retries):
            try:
                ws.insert_row(row, index=2, value_input_option='USER_ENTERED')
                return
            except gspread.exceptions.APIError as exc:
                status = getattr(exc.response, 'status_code', None)
                if status == 429 and attempt < max_retries - 1:
                    wait = (2 ** attempt) + random.random()
                    logger.warning(
                        "Sheets rate limit (attempt %d/%d), retrying in %.1fs",
                        attempt + 1, max_retries, wait,
                    )
                    time.sleep(wait)
                else:
                    raise

    def _ws(self, sheet_key: str) -> gspread.Worksheet:
        """Return a cached Worksheet handle, creating the sheet on first access."""
        name = config.SHEET_NAMES[sheet_key]
        if name not in self._ws_cache:
            self._ws_cache[name] = self._get_or_create(name)
        return self._ws_cache[name]

    def _get_or_create(self, name: str) -> gspread.Worksheet:
        """
        Return the named worksheet, creating it if it does not exist.

        Wraps both the lookup and the creation in try-except so API errors
        surface with a clear log message rather than an unhandled exception.
        """
        try:
            return self._ss.worksheet(name)
        except gspread.WorksheetNotFound:
            try:
                ws = self._ss.add_worksheet(title=name, rows=10000, cols=30)
                headers = _HEADERS.get(name, [])
                if headers:
                    ws.append_row(headers, value_input_option='USER_ENTERED')
                logger.info("Sheets: created worksheet '%s'", name)
                return ws
            except Exception as exc:
                logger.error("Sheets: failed to create worksheet '%s': %s", name, exc)
                raise
        except Exception as exc:
            logger.error("Sheets: failed to open worksheet '%s': %s", name, exc)
            raise


# ── Module-level helpers ──────────────────────────────────────────────────────

def _ranked(levels: list[dict], level_type: str) -> list[dict]:
    return sorted(
        [lv for lv in levels if lv['level_type'] == level_type],
        key=lambda x: x['rank'],
    )


def _level_cols(levels: list[dict], n: int) -> list:
    """Flatten up to n levels into [strike, oi, strike, oi, …] columns."""
    out = []
    for i in range(n):
        if i < len(levels):
            out += [levels[i]['strike'], levels[i]['open_interest']]
        else:
            out += ['', '']
    return out


def _oi_cols(contracts: list[dict], n: int) -> list:
    """Flatten up to n contracts into [strike, oi, strike, oi, …] columns."""
    out = []
    for i in range(n):
        if i < len(contracts):
            out += [contracts[i]['strike'], contracts[i]['open_interest']]
        else:
            out += ['', '']
    return out
