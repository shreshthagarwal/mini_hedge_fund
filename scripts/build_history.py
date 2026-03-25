"""
build_history.py — fetch and merge NIFTY 5-min data via yfinance

Safe to run any time. Never overwrites — always merges new data
with the existing CSV so no history is ever lost.

Run:
    python scripts/build_history.py
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import yfinance as yf
import pandas as pd
from zoneinfo import ZoneInfo
from utils import get_logger, session_bars

logger     = get_logger("build_history")
IST        = ZoneInfo("Asia/Kolkata")
OUTPUT_CSV = "data/nifty_proxy_5min_historical.csv"
OHLCV      = ["open", "high", "low", "close", "volume"]


def fetch_new() -> pd.DataFrame:
    logger.info("Fetching ^NSEI 5-min from yfinance (last 60 days)...")
    raw = yf.Ticker("^NSEI").history(period="60d", interval="5m")
    if raw is None or raw.empty:
        raise ValueError("yfinance returned no data. Check internet connection.")
    raw.columns   = [c.lower() for c in raw.columns]
    df            = raw[OHLCV].copy()
    df.index      = pd.DatetimeIndex(df.index)
    if df.index.tzinfo is None:
        df.index  = df.index.tz_localize(IST)
    else:
        df.index  = df.index.tz_convert(IST)
    df.index.name = "datetime"
    df            = session_bars(df)
    logger.info(f"Fetched  : {len(df):,} bars  |  {df.index[0]}  ->  {df.index[-1]}")
    return df


def load_existing() -> pd.DataFrame | None:
    if not os.path.exists(OUTPUT_CSV):
        logger.info("No existing CSV found — will create fresh.")
        return None
    df            = pd.read_csv(OUTPUT_CSV, index_col="datetime")
    df.index      = pd.to_datetime(df.index)
    if df.index.tzinfo is None:
        df.index  = df.index.tz_localize(IST)
    else:
        df.index  = df.index.tz_convert(IST)
    df.index.name = "datetime"
    df.columns    = [c.lower() for c in df.columns]
    df            = df[OHLCV]
    logger.info(f"Existing : {len(df):,} bars  |  {df.index[0]}  ->  {df.index[-1]}")
    return df


if __name__ == "__main__":
    new      = fetch_new()
    existing = load_existing()

    if existing is not None:
        combined = pd.concat([existing, new]).sort_index()
        combined = combined[~combined.index.duplicated(keep="last")]
    else:
        combined = new.sort_index()

    os.makedirs("data", exist_ok=True)
    combined.reset_index().to_csv(OUTPUT_CSV, index=False)

    days     = combined.index.normalize().nunique()
    added    = len(combined) - (len(existing) if existing is not None else 0)

    logger.info("=" * 50)
    logger.info(f"Saved         -> {OUTPUT_CSV}")
    logger.info(f"Total bars    : {len(combined):,}")
    logger.info(f"New bars added: {max(added, 0):,}")
    logger.info(f"Unique days   : {days}")
    logger.info(f"Bars/day avg  : {len(combined)/days:.1f}")
    logger.info(f"Date range    : {combined.index[0]}  ->  {combined.index[-1]}")
    logger.info("=" * 50)
    logger.info("Run: python main.py backtest")