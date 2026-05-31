"""
Market-hours and timezone helpers.
All public functions use America/Chicago (CST/CDT) as the reference timezone.
NYSE open = 08:30 CST, close = 15:00 CST.
"""
from datetime import datetime, date, time

import pytz

import config

CST = pytz.timezone(config.SESSION_TZ)

_MARKET_OPEN  = time(config.MARKET_OPEN_HOUR,  config.MARKET_OPEN_MINUTE)
_MARKET_CLOSE = time(config.MARKET_CLOSE_HOUR, config.MARKET_CLOSE_MINUTE)
_SNAPSHOT     = time(config.SNAPSHOT_HOUR,      config.SNAPSHOT_MINUTE)


def now_cst() -> datetime:
    return datetime.now(CST)


def today_cst() -> date:
    return now_cst().date()


def is_weekday(dt: datetime = None) -> bool:
    if dt is None:
        dt = now_cst()
    return dt.weekday() < 5  # Mon=0 … Fri=4


def is_market_open(dt: datetime = None) -> bool:
    if dt is None:
        dt = now_cst()
    if not is_weekday(dt):
        return False
    t = dt.time().replace(second=0, microsecond=0)
    return _MARKET_OPEN <= t < _MARKET_CLOSE


def is_eod_window(dt: datetime = None, window_sec: int = 59) -> bool:
    """
    True if `dt` is within `window_sec` seconds of 14:55 CST (EOD liquidation trigger).
    Fires once per day from the 60-second loop, 5 minutes before market close.
    """
    if dt is None:
        dt = now_cst()
    if not is_weekday(dt):
        return False
    eod_dt = dt.replace(hour=14, minute=55, second=0, microsecond=0)
    return abs((dt - eod_dt).total_seconds()) <= window_sec


def is_snapshot_window(dt: datetime = None, window_sec: int = 59) -> bool:
    """
    True if `dt` is within `window_sec` seconds of today's 08:20 CST snapshot.
    Designed to fire exactly once per day when called from a 60-second loop.
    """
    if dt is None:
        dt = now_cst()
    if not is_weekday(dt):
        return False
    snap_dt = dt.replace(
        hour=_SNAPSHOT.hour,
        minute=_SNAPSHOT.minute,
        second=0,
        microsecond=0,
    )
    return abs((dt - snap_dt).total_seconds()) <= window_sec


