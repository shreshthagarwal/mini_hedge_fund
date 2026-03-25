"""
Live Ticker
───────────
Connects TvDatafeedLive → strategy engine → signal logger.
No paper trading, no order management.
Logs every signal with entry, SL, target to console + CSV.

Run:
    python main.py live
"""

import os
import csv
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo
import pandas as pd

from config import (
    IST, SYMBOL, SQUARE_OFF, MARKET_CLOSE, OUTPUTS_DIR,
    INITIAL_CAPITAL,
)
from data_fetcher import HistoricalFeed, LiveFeed
from strategy import ORBStrategy, Signal, add_indicators
from utils import get_logger

logger = get_logger("live_ticker")

SIGNALS_CSV = os.path.join(OUTPUTS_DIR, "live_signals.csv")


class LiveTicker:
    """
    Warm-up from historical data → subscribe to live feed →
    evaluate strategy on each new 5-min bar → log signals.
    """

    WARMUP_BARS = 100   # bars needed to seed VWAP, EMA, ATR

    def __init__(self):
        self.strategy    = VWAPEMAStrategy()
        self.bar_buffer: list[pd.Series] = []
        self.live_feed:  Optional[LiveFeed] = None
        self._open_signal: Optional[dict] = None

        os.makedirs(OUTPUTS_DIR, exist_ok=True)
        self._init_signals_csv()

    # ── Public ────────────────────────────────────────────────────────────────

    def run(self):
        """Warm up then start live feed. Blocks until market close or Ctrl+C."""
        self._warm_up()

        self.live_feed = LiveFeed(on_candle=self._on_candle)
        self.live_feed.start()

        logger.info("Press Ctrl+C to stop.")
        try:
            import time
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            self.live_feed.stop()
            logger.info("Session ended.")

    # ── Warm-up ───────────────────────────────────────────────────────────────

    def _warm_up(self):
        logger.info(f"Loading {self.WARMUP_BARS} warm-up bars…")
        feed = HistoricalFeed()
        df   = feed.load()
        df   = add_indicators(df)
        for ts, row in df.tail(self.WARMUP_BARS).iterrows():
            self.bar_buffer.append(row)
        logger.info("Warm-up done. Indicators seeded.")

    # ── Candle handler ────────────────────────────────────────────────────────

    def _on_candle(self, candle: pd.Series):
        """Called on every new closed 5-min bar from TvDatafeedLive."""
        self.bar_buffer.append(candle)
        if len(self.bar_buffer) > 500:
            self.bar_buffer = self.bar_buffer[-300:]

        if len(self.bar_buffer) < 2:
            return

        # Recompute indicators over rolling buffer
        df       = pd.DataFrame(self.bar_buffer)
        df       = add_indicators(df)
        bar      = df.iloc[-1]
        prev_bar = df.iloc[-2]
        ts       = candle.name

        # ── Check exit on open signal ─────────────────────────────────────
        if self._open_signal:
            self._check_signal_exit(bar, ts)

        # ── Evaluate for new entry ────────────────────────────────────────
        if not self._open_signal:
            sig = self.strategy.evaluate(bar, prev_bar)

            if sig.signal in (Signal.LONG, Signal.SHORT):
                self._open_signal = {
                    "entry_time":  ts,
                    "direction":   sig.signal.value,
                    "entry_price": sig.entry,
                    "sl":          sig.sl,
                    "target":      sig.target,
                    "atr":         sig.atr,
                    "reason":      sig.reason,
                }
                self.strategy.enter(sig.signal.value)
                self._log_entry(self._open_signal)

        # ── Auto square-off at 3:20 ───────────────────────────────────────
        if ts.time() >= SQUARE_OFF and self._open_signal:
            self._close_signal(bar["close"], ts, "3:20 PM square-off")

    # ── Signal tracking ───────────────────────────────────────────────────────

    def _check_signal_exit(self, bar: pd.Series, ts: pd.Timestamp):
        s    = self._open_signal
        side = s["direction"]

        if ts.time() >= SQUARE_OFF:
            self._close_signal(bar["close"], ts, "3:20 PM square-off")
        elif side == "LONG":
            if bar["low"]  <= s["sl"]:      self._close_signal(s["sl"],     ts, "stop loss")
            elif bar["high"] >= s["target"]: self._close_signal(s["target"], ts, "target hit")
            elif bar["close"] < bar["vwap"]: self._close_signal(bar["close"], ts, "VWAP cross")
        elif side == "SHORT":
            if bar["high"] >= s["sl"]:      self._close_signal(s["sl"],     ts, "stop loss")
            elif bar["low"]  <= s["target"]: self._close_signal(s["target"], ts, "target hit")
            elif bar["close"] > bar["vwap"]: self._close_signal(bar["close"], ts, "VWAP cross")

    def _close_signal(self, exit_price: float, ts: pd.Timestamp, reason: str):
        s = self._open_signal
        if s is None:
            return

        side      = s["direction"]
        pnl_pts   = (exit_price - s["entry_price"]) if side == "LONG" else (s["entry_price"] - exit_price)
        outcome   = "WIN" if pnl_pts > 0 else "LOSS"
        emoji     = "✅" if pnl_pts > 0 else "❌"
        duration  = int((ts - s["entry_time"]).total_seconds() / 60)

        logger.info(
            f"{emoji} EXIT  {side}  {exit_price:.2f}  "
            f"P&L {pnl_pts:+.2f} pts  [{reason}]  {duration}min"
        )
        self._write_signal_csv({**s, "exit_time": ts, "exit_price": exit_price,
                                 "exit_reason": reason, "pnl_points": pnl_pts,
                                 "outcome": outcome, "duration_min": duration})
        self._open_signal = None
        self.strategy.exit_position()

    # ── Logging ───────────────────────────────────────────────────────────────

    def _log_entry(self, s: dict):
        arrow = "▲" if s["direction"] == "LONG" else "▼"
        logger.info(
            f"\n  {'─'*48}\n"
            f"  {arrow}  {s['direction']}  {SYMBOL} FUT  "
            f"@ {s['entry_price']:.2f}\n"
            f"     SL     {s['sl']:.2f}\n"
            f"     Target {s['target']:.2f}\n"
            f"     ATR    {s['atr']:.2f}   [{s['reason']}]\n"
            f"     Time   {s['entry_time'].strftime('%H:%M:%S IST')}\n"
            f"  {'─'*48}"
        )

    def _init_signals_csv(self):
        if not os.path.exists(SIGNALS_CSV):
            with open(SIGNALS_CSV, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=[
                    "entry_time", "exit_time", "direction",
                    "entry_price", "exit_price", "sl", "target",
                    "atr", "exit_reason", "pnl_points", "outcome", "duration_min",
                ])
                writer.writeheader()

    def _write_signal_csv(self, row: dict):
        with open(SIGNALS_CSV, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "entry_time", "exit_time", "direction",
                "entry_price", "exit_price", "sl", "target",
                "atr", "exit_reason", "pnl_points", "outcome", "duration_min",
            ])
            writer.writerow({k: row.get(k, "") for k in writer.fieldnames})