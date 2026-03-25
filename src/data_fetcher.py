"""
Data Fetcher — NIFTY Futures
─────────────────────────────
Historical : yfinance  → ^NSEI (NIFTY 50 spot, full IST sessions, 60-day max per call)
Live ticks : yfinance  → real-time quote polling every 30 seconds, builds 5-min candles

Install:
    pip install yfinance

No credentials required.
"""

import os
import time
import threading
from datetime import datetime, timedelta
from typing import Callable, Optional
from zoneinfo import ZoneInfo
import pandas as pd

from config import IST, MARKET_OPEN, MARKET_CLOSE, SQUARE_OFF, HISTORICAL_CSV
from utils import get_logger, session_bars

logger = get_logger("data_fetcher")

OHLCV_COLS  = ["open", "high", "low", "close", "volume"]
YF_SYMBOL   = "^NSEI"          # NIFTY 50 spot — best free proxy for futures
YF_INTERVAL = "5m"
YF_PERIOD   = "60d"            # yfinance max for 5-min data


# ═══════════════════════════════════════════════════════════════════════════════
# Historical feed  (yfinance)
# ═══════════════════════════════════════════════════════════════════════════════

class HistoricalFeed:
    """
    Loads NIFTY 5-min OHLCV via yfinance.
    Priority: local CSV cache → yfinance API → synthetic fallback.

    Re-run build_history.py every ~2 weeks to accumulate more history.
    The CSV merges old + new data so history grows over time.
    """

    def __init__(self, csv_path: str = HISTORICAL_CSV, force_refresh: bool = False):
        self.csv_path      = csv_path
        self.force_refresh = force_refresh

    def load(
        self,
        from_date: Optional[str] = None,
        to_date:   Optional[str] = None,
    ) -> pd.DataFrame:
        """Returns IST-indexed OHLCV DataFrame filtered to session hours."""
        if not self.force_refresh and os.path.exists(self.csv_path):
            logger.info(f"Loading cached data from {self.csv_path}")
            df = self._load_csv()
        else:
            logger.info("Fetching historical data via yfinance...")
            try:
                df = self._fetch_yf()
                self._save_csv(df)
            except Exception as e:
                logger.warning(f"yfinance failed ({e}). Falling back to synthetic data.")
                df = self._synthetic()

        logger.info(f"Raw bars before session filter: {len(df)}  |  {df.index[0]}  ->  {df.index[-1]}")
        df = session_bars(df)

        if from_date:
            df = df[df.index >= pd.Timestamp(from_date, tz=IST)]
        if to_date:
            df = df[df.index <= pd.Timestamp(to_date, tz=IST)]

        if df.empty:
            raise ValueError("No bars remain after filtering.")

        logger.info(f"Loaded {len(df):,} bars  {df.index[0]}  ->  {df.index[-1]}")
        return df

    # ── yfinance ──────────────────────────────────────────────────────────────

    def _fetch_yf(self) -> pd.DataFrame:
        import yfinance as yf

        logger.info(f"Requesting {YF_PERIOD} of {YF_SYMBOL} at {YF_INTERVAL}...")
        ticker = yf.Ticker(YF_SYMBOL)
        raw    = ticker.history(period=YF_PERIOD, interval=YF_INTERVAL)

        if raw is None or raw.empty:
            raise ValueError("yfinance returned empty DataFrame")

        raw.columns   = [c.lower() for c in raw.columns]
        df            = raw[OHLCV_COLS].copy()
        df.index      = pd.DatetimeIndex(df.index)

        if df.index.tzinfo is None:
            df.index = df.index.tz_localize(IST)
        else:
            df.index = df.index.tz_convert(IST)

        df.index.name = "datetime"
        logger.info(f"yfinance raw bars: {len(df):,}  |  {df.index[0]}  ->  {df.index[-1]}")
        return df

    # ── CSV helpers ───────────────────────────────────────────────────────────

    def _load_csv(self) -> pd.DataFrame:
        df            = pd.read_csv(self.csv_path, index_col="datetime")
        df.index      = pd.to_datetime(df.index, utc=True).tz_convert(IST)
        df.index.name = "datetime"
        df.columns    = [c.lower() for c in df.columns]
        return df[OHLCV_COLS]

    def _save_csv(self, df: pd.DataFrame):
        os.makedirs(os.path.dirname(self.csv_path) or ".", exist_ok=True)
        df.reset_index().to_csv(self.csv_path, index=False)
        logger.info(f"Cached -> {self.csv_path}")

    # ── Synthetic fallback ────────────────────────────────────────────────────

    def _synthetic(self) -> pd.DataFrame:
        import numpy as np
        logger.warning("SYNTHETIC DATA — install yfinance for real data: pip install yfinance")
        rng  = np.random.default_rng(42)
        bars = []
        d    = (datetime.now(IST) - timedelta(days=800)).date()
        end  = datetime.now(IST).date()
        while d <= end:
            if d.weekday() < 5:
                t       = pd.Timestamp(d, tz=IST).replace(hour=9,  minute=15)
                close_t = pd.Timestamp(d, tz=IST).replace(hour=15, minute=30)
                while t <= close_t:
                    bars.append(t)
                    t += pd.Timedelta(minutes=5)
            d += timedelta(days=1)
        n     = len(bars)
        ret   = rng.normal(0, 0.0008, n)
        price = 22000 * __import__("numpy").exp(__import__("numpy").cumsum(ret))
        noise = rng.uniform(0.0002, 0.0015, (n, 2))
        return pd.DataFrame({
            "open":   price * (1 + rng.uniform(-0.0005, 0.0005, n)),
            "high":   price * (1 + noise[:, 0]),
            "low":    price * (1 - noise[:, 1]),
            "close":  price,
            "volume": rng.integers(50_000, 300_000, n).astype(float),
        }, index=pd.DatetimeIndex(bars, name="datetime"))


# ═══════════════════════════════════════════════════════════════════════════════
# Live feed  (yfinance polling)
# ═══════════════════════════════════════════════════════════════════════════════

class LiveFeed:
    """
    Polls yfinance every 30 seconds for the latest price.
    Builds 5-min candles from ticks and fires on_candle() when each bar closes.

    Not true tick-level data — suitable for signal logging and strategy testing.
    For real execution, replace with a broker websocket (Zerodha/Fyers).

    Usage:
        feed = LiveFeed(on_candle=my_callback)
        feed.start()   # non-blocking, runs in background thread
        feed.stop()
    """

    POLL_SECONDS = 30

    def __init__(self, on_candle: Callable[[pd.Series], None]):
        self.on_candle   = on_candle
        self._running    = False
        self._thread: Optional[threading.Thread] = None

        # Current bar state
        self._bar_open:   Optional[float]        = None
        self._bar_high:   float                  = 0.0
        self._bar_low:    float                  = float("inf")
        self._bar_close:  float                  = 0.0
        self._bar_volume: float                  = 0.0
        self._bar_start:  Optional[pd.Timestamp] = None

    def start(self):
        self._running = True
        self._thread  = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info(f"Live feed started — polling {YF_SYMBOL} every {self.POLL_SECONDS}s")

    def stop(self):
        self._running = False
        logger.info("Live feed stopped.")

    # ── Internal ──────────────────────────────────────────────────────────────

    def _poll_loop(self):
        import yfinance as yf
        ticker = yf.Ticker(YF_SYMBOL)

        while self._running:
            try:
                now = datetime.now(IST)
                t   = now.time()

                # Only poll during market hours
                if not (MARKET_OPEN <= t <= MARKET_CLOSE):
                    time.sleep(self.POLL_SECONDS)
                    continue

                # Get latest 1-day 1-min data — last row = current price
                raw = ticker.history(period="1d", interval="1m")
                if raw is None or raw.empty:
                    time.sleep(self.POLL_SECONDS)
                    continue

                raw.columns = [c.lower() for c in raw.columns]
                last        = raw.iloc[-1]
                price       = float(last["close"])
                volume      = float(last.get("volume", 0))

                self._update_bar(price, volume, pd.Timestamp(now))

            except Exception as e:
                logger.warning(f"Live poll error: {e}")

            time.sleep(self.POLL_SECONDS)

    def _current_5min_bucket(self, ts: pd.Timestamp) -> pd.Timestamp:
        """Floor timestamp to the nearest 5-min boundary."""
        m = (ts.minute // 5) * 5
        return ts.replace(minute=m, second=0, microsecond=0)

    def _update_bar(self, price: float, volume: float, ts: pd.Timestamp):
        bucket = self._current_5min_bucket(ts)

        if self._bar_start is None:
            self._bar_start = bucket

        # New 5-min bar — emit the completed bar first
        if bucket != self._bar_start and self._bar_open is not None:
            candle = pd.Series({
                "open":   self._bar_open,
                "high":   self._bar_high,
                "low":    self._bar_low,
                "close":  self._bar_close,
                "volume": self._bar_volume,
            }, name=self._bar_start)
            self.on_candle(candle)
            # Reset for new bar
            self._bar_open   = None
            self._bar_high   = 0.0
            self._bar_low    = float("inf")
            self._bar_volume = 0.0
            self._bar_start  = bucket

        # Update current bar
        if self._bar_open is None:
            self._bar_open = price
        self._bar_high   = max(self._bar_high,  price)
        self._bar_low    = min(self._bar_low,   price)
        self._bar_close  = price
        self._bar_volume += volume

        logger.debug(
            f"[TICK] {ts.strftime('%H:%M:%S')}  price={price:.2f}  "
            f"bar O:{self._bar_open:.2f} H:{self._bar_high:.2f} "
            f"L:{self._bar_low:.2f} C:{self._bar_close:.2f}"
        )