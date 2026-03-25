"""
strategy_5/data_loader.py — load and align futures + spot data

Produces a single DataFrame with columns:
  futures_open, futures_high, futures_low, futures_close, futures_volume
  spot_close
  basis, fair_basis, basis_spread, basis_zscore
  days_to_expiry

Used by both backtest and live trading.
"""

from __future__ import annotations

import logging
import numpy as np
import pandas as pd
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from strategies.strategy_5.config import (
    FUTURES_CSV, SPOT_CSV,
    RISK_FREE_RATE, BASIS_ZSCORE_WINDOW, IST,
)
from utils import nse_expiry

logger = logging.getLogger("strategy_5.data")


def load_aligned(
    futures_csv: str = FUTURES_CSV,
    spot_csv:    str = SPOT_CSV,
) -> pd.DataFrame:
    """
    Load futures and spot CSVs, align on timestamp, compute basis features.
    Returns aligned DataFrame ready for strategy evaluation.
    """

    # ── Load futures ──────────────────────────────────────────────────────────
    fut = pd.read_csv(futures_csv, index_col="datetime")
    fut.index = pd.to_datetime(fut.index)
    if fut.index.tzinfo is None:
        fut.index = fut.index.tz_localize(IST)
    else:
        fut.index = fut.index.tz_convert(IST)
    fut.columns = [f"futures_{c.lower()}" for c in fut.columns]
    logger.info(f"Futures: {len(fut):,} bars  "
                f"{fut.index[0].strftime('%Y-%m-%d')} → {fut.index[-1].strftime('%Y-%m-%d')}")

    # ── Load spot ─────────────────────────────────────────────────────────────
    spot = pd.read_csv(spot_csv, index_col="datetime")
    spot.index = pd.to_datetime(spot.index)
    if spot.index.tzinfo is None:
        spot.index = spot.index.tz_localize(IST)
    else:
        spot.index = spot.index.tz_convert(IST)
    spot.columns = [c.lower() for c in spot.columns]
    logger.info(f"Spot:    {len(spot):,} bars  "
                f"{spot.index[0].strftime('%Y-%m-%d')} → {spot.index[-1].strftime('%Y-%m-%d')}")

    # ── Align on common timestamps (inner join) ───────────────────────────────
    df = fut.join(spot[["close"]].rename(columns={"close": "spot_close"}),
                  how="inner")
    logger.info(f"Aligned: {len(df):,} bars  "
                f"{df.index[0].strftime('%Y-%m-%d')} → {df.index[-1].strftime('%Y-%m-%d')}")

    if len(df) == 0:
        raise ValueError(
            "No overlapping timestamps between futures and spot CSVs.\n"
            "Check that both cover the same date range."
        )

    # ── Days to expiry ────────────────────────────────────────────────────────
    df["days_to_expiry"] = _compute_days_to_expiry(df.index)

    # ── Basis features ────────────────────────────────────────────────────────
    df["basis"]       = df["futures_close"] - df["spot_close"]
    df["fair_basis"]  = (df["spot_close"]
                         * RISK_FREE_RATE
                         * (df["days_to_expiry"] / 365))
    df["basis_spread"] = df["basis"] - df["fair_basis"]

    # Z-score of basis_spread over rolling window
    roll_mean = df["basis_spread"].rolling(BASIS_ZSCORE_WINDOW, min_periods=50).mean()
    roll_std  = df["basis_spread"].rolling(BASIS_ZSCORE_WINDOW, min_periods=50).std()
    df["basis_zscore"] = (df["basis_spread"] - roll_mean) / roll_std.replace(0, np.nan)

    # Rename futures columns to standard OHLCV for engine compatibility
    df = df.rename(columns={
        "futures_open":   "open",
        "futures_high":   "high",
        "futures_low":    "low",
        "futures_close":  "close",
        "futures_volume": "volume",
    })

    logger.info(
        f"Basis stats: mean={df['basis'].mean():.1f}  "
        f"std={df['basis'].std():.1f}  "
        f"z range=[{df['basis_zscore'].min():.1f}, {df['basis_zscore'].max():.1f}]"
    )

    return df


def _compute_days_to_expiry(index: pd.DatetimeIndex) -> pd.Series:
    """
    For each bar, compute calendar days remaining until the
    front-month NIFTY futures expiry.
    Uses nse_expiry() which returns last Thursday (pre-Sep 2025)
    or last Tuesday (Sep 2025+).
    """
    dte = pd.Series(np.nan, index=index)

    for ts in index:
        d = ts.date()
        y, m = d.year, d.month

        # Find current front-month expiry
        exp = nse_expiry(y, m)
        if d > exp:
            # Past this month's expiry — use next month's
            m += 1
            if m > 12:
                m, y = 1, y + 1
            exp = nse_expiry(y, m)

        dte[ts] = max((exp - d).days, 1)   # minimum 1 day to avoid div/0

    return dte