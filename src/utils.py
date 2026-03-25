"""
Shared utilities: logging, time helpers, NSE calendar.
"""

import logging
import sys
from datetime import date, timedelta
from typing import Optional
import pandas as pd
from zoneinfo import ZoneInfo

from config import IST, MARKET_OPEN, MARKET_CLOSE


# ─── Logger ───────────────────────────────────────────────────────────────────

def get_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        fmt = logging.Formatter(
            "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        handler.setFormatter(fmt)
        logger.addHandler(handler)
    logger.setLevel(level)
    return logger


# ─── Time helpers ─────────────────────────────────────────────────────────────

def to_ist(dt) -> pd.Timestamp:
    """Ensure a timestamp is IST-aware."""
    ts = pd.Timestamp(dt)
    if ts.tzinfo is None:
        return ts.tz_localize(IST)
    return ts.tz_convert(IST)


def is_market_hours(ts: pd.Timestamp) -> bool:
    t = ts.time()
    return MARKET_OPEN <= t <= MARKET_CLOSE


def last_thursday(year: int, month: int) -> date:
    """Return the last Thursday of a given month."""
    if month == 12:
        last_day = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = date(year, month + 1, 1) - timedelta(days=1)
    offset = (last_day.weekday() - 3) % 7  # 3 = Thursday
    return last_day - timedelta(days=offset)


def last_tuesday(year: int, month: int) -> date:
    """Return the last Tuesday of a given month."""
    if month == 12:
        last_day = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = date(year, month + 1, 1) - timedelta(days=1)
    offset = (last_day.weekday() - 1) % 7  # 1 = Tuesday
    return last_day - timedelta(days=offset)


# NSE circular: effective 01-Sep-2025, expiry shifts from Thursday to Tuesday
EXPIRY_CHANGE_DATE = date(2025, 9, 1)

def nse_expiry(year: int, month: int) -> date:
    """
    Return the correct NSE monthly expiry date for a given month.
    Pre Sep-2025  : last Thursday of the month
    From Sep-2025 : last Tuesday of the month
    """
    if date(year, month, 1) >= EXPIRY_CHANGE_DATE:
        return last_tuesday(year, month)
    return last_thursday(year, month)


def expiry_calendar(from_year: int, to_year: int) -> list[date]:
    """All monthly NIFTY expiry dates for a range of years."""
    expiries = []
    for year in range(from_year, to_year + 1):
        for month in range(1, 13):
            expiries.append(nse_expiry(year, month))
    return sorted(expiries)


def roll_date(expiry: date, days_before: int = 2) -> date:
    """Roll to next contract N trading days before expiry."""
    d = expiry - timedelta(days=days_before)
    while d.weekday() >= 5:  # skip weekends
        d -= timedelta(days=1)
    return d


# ─── DataFrame helpers ────────────────────────────────────────────────────────

def round_to_tick(price: float, tick: float = 0.05) -> float:
    return round(round(price / tick) * tick, 2)


def session_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Filter to regular market session bars only."""
    return df[df.index.map(lambda ts: is_market_hours(ts))]