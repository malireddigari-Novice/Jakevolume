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

Rate limiting
-------------
Google Sheets API allows ~300 writes/min per project.
This logger is serial and low-frequency; no additional batching needed.
"""
import logging
import random
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
    'Timestamp_CST', 'Symbol', 'Option_Type',
    'Price_To_Enter', 'Price_To_Exit', 'Spike_Volume',
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
        self._gc: Optional[gspread.Client] = None
        self._ss: Optional[gspread.Spreadsheet] = None
        self._ws_cache: dict[str, gspread.Worksheet] = {}

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self) -> None:
        creds = Credentials.from_service_account_file(
            config.GOOGLE_SERVICE_ACCOUNT_FILE,
            scopes=_SCOPES,
        )
        self._gc = gspread.authorize(creds)
        self._ss = self._gc.open_by_key(config.GOOGLE_SPREADSHEET_ID)

        for sheet_key in config.SHEET_NAMES:
            ws = self._ws(sheet_key)
            # Keep header row in sync with current column definitions
            headers = _HEADERS.get(config.SHEET_NAMES[sheet_key], [])
            if headers:
                ws.update([headers], 'A1')

        logger.info("Google Sheets connected: %s", self._ss.title)

    # ── Public logging methods ────────────────────────────────────────────────

    def log_daily_levels(
        self,
        symbol: str,
        levels: list[dict],
        underlying_price: float,
        computed_at: datetime,
    ) -> None:
        supports    = _ranked(levels, 'SUPPORT')
        resistances = _ranked(levels, 'RESISTANCE')

        row = [
            computed_at.strftime('%Y-%m-%d'),
            symbol,
            computed_at.strftime('%Y-%m-%d %H:%M:%S'),
            round(underlying_price, 4),
        ] + _level_cols(supports, 2) + _level_cols(resistances, 2)

        self._insert_row('daily_levels', row)
        logger.info("Sheets: logged daily levels for %s", symbol)

    def log_signal(self, signal: dict) -> None:
        row = [
            signal['signal_time'].strftime('%Y-%m-%d %H:%M:%S'),
            signal['symbol'],
            signal.get('option_type', ''),
            signal.get('price_to_enter', ''),
            signal.get('price_to_exit', ''),
            signal['spike_volume'],
        ]
        self._insert_row('signals', row)
        logger.info(
            "Sheets: logged signal %s %s  opt_%s  enter=%s  exit=%s  spk=%s",
            signal['symbol'], signal['signal_type'],
            signal.get('option_type', '?'),
            signal.get('price_to_enter', 'n/a'),
            signal.get('price_to_exit', 'n/a'),
            signal['spike_volume'],
        )

    def log_morning_sentiment(self, sentiment: dict, computed_at: datetime) -> None:
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
        self._insert_row('morning_sentiment', row)
        logger.info("Sheets: logged sentiment for %s => %s", sentiment['symbol'], sentiment['bias'])

    def log_oi_snapshot(
        self,
        symbol: str,
        expiry: date,
        top_calls: list[dict],
        top_puts: list[dict],
        underlying_price: float,
        snap_time: datetime,
    ) -> None:
        row = [
            snap_time.strftime('%Y-%m-%d'),
            snap_time.strftime('%H:%M:%S'),
            symbol,
            str(expiry),
        ] + _oi_cols(top_calls, 2) + _oi_cols(top_puts, 2) + [round(underlying_price, 4)]

        self._insert_row('oi_snapshot', row)
        logger.info("Sheets: logged OI snapshot for %s (expiry %s)", symbol, expiry)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _insert_row(self, sheet_key: str, row: list, max_retries: int = 4) -> None:
        """Insert at row 2 (newest-first) with exponential backoff on rate limits."""
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
        name = config.SHEET_NAMES[sheet_key]
        if name not in self._ws_cache:
            self._ws_cache[name] = self._get_or_create(name)
        return self._ws_cache[name]

    def _get_or_create(self, name: str) -> gspread.Worksheet:
        try:
            return self._ss.worksheet(name)
        except gspread.WorksheetNotFound:
            ws = self._ss.add_worksheet(title=name, rows=10000, cols=30)
            headers = _HEADERS.get(name, [])
            if headers:
                ws.append_row(headers, value_input_option='USER_ENTERED')
            logger.info("Sheets: created worksheet '%s'", name)
            return ws


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
