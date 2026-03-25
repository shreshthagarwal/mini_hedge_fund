"""
paper_engine.py — live paper trading engine

Wires together:
  LiveFeed (Upstox WebSocket) → candle builder → ORB strategy → paper broker
  Exposes state dict for dashboard consumption.

Usage:
    engine = PaperEngine(token, instrument_key, warmup_csv)
    engine.start()       # non-blocking
    state = engine.get_state()   # called by dashboard
    engine.stop()
"""

import os
import threading
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo
import pandas as pd

from config import (
    IST, INITIAL_CAPITAL, SQUARE_OFF, MARKET_OPEN, MARKET_CLOSE, OR_BARS,
)
from data_fetcher import HistoricalFeed
from strategy import ORBStrategy, add_indicators, Signal
from paper_broker import PaperBroker
from risk_manager import RiskManager
from live_feed import LiveFeed
from utils import get_logger

logger = get_logger("paper_engine")
WARMUP_BARS = 120


class PaperEngine:
    """
    Central engine for live paper trading.
    Thread-safe state accessible by the Flask dashboard.
    """

    def __init__(
        self,
        token:          str,
        instrument_key: str,
        warmup_csv:     Optional[str] = None,
    ):
        self.token          = token
        self.instrument_key = instrument_key
        self.warmup_csv     = warmup_csv

        self.strategy  = ORBStrategy()
        self.broker    = PaperBroker(INITIAL_CAPITAL)
        self.risk      = RiskManager(INITIAL_CAPITAL)
        self.feed:     Optional[LiveFeed] = None

        self.bar_buffer: list[pd.Series] = []
        self._lock       = threading.Lock()
        self._running    = False

        # Dashboard state
        self.status       = "stopped"
        self.current_price: float = 0.0
        self.session_log:  list[str] = []

    # ── Public ────────────────────────────────────────────────────────────────

    def start(self):
        self._warm_up()
        self.feed = LiveFeed(
            token          = self.token,
            instrument_key = self.instrument_key,
            on_candle      = self._on_candle,
            on_tick        = self._on_tick,
        )
        self.feed.start()
        self._running = True
        self.status   = "running"
        logger.info("Paper engine started")

    def stop(self):
        if self.feed:
            self.feed.stop()
        self._running = False
        self.status   = "stopped"
        self.broker.summary()

    def get_state(self) -> dict:
        """Thread-safe snapshot for dashboard."""
        with self._lock:
            open_trade = None
            if self.broker.open_trade:
                t = self.broker.open_trade
                open_trade = {
                    "direction":   t.direction,
                    "entry_price": t.entry_price,
                    "sl":          t.sl,
                    "target":      t.target,
                    "entry_time":  t.entry_time.strftime("%H:%M:%S"),
                    "unrealised":  self._unrealised_pnl(),
                }

            trades = [
                {
                    "time":      t.entry_time.strftime("%H:%M"),
                    "direction": t.direction,
                    "entry":     t.entry_price,
                    "exit":      t.exit_price,
                    "pnl":       round(t.pnl_net, 0),
                    "reason":    t.exit_reason,
                }
                for t in self.broker.all_trades
            ]

            return {
                "status":        self.status,
                "current_price": round(self.current_price, 2),
                "session_pnl":   round(self.broker.session_pnl, 0),
                "capital":       round(self.broker.capital, 0),
                "open_trade":    open_trade,
                "trades":        trades,
                "log":           self.session_log[-30:],   # last 30 log lines
                "win_rate":      self._win_rate(),
            }

    # ── Warm-up ───────────────────────────────────────────────────────────────

    def _warm_up(self):
        logger.info(f"Warming up {WARMUP_BARS} bars...")
        try:
            feed = HistoricalFeed(csv_path=self.warmup_csv) if self.warmup_csv \
                   else HistoricalFeed()
            df   = feed.load()
            df   = add_indicators(df)
            for ts, row in df.tail(WARMUP_BARS).iterrows():
                self.bar_buffer.append(row)
            self._log(f"Warm-up done — {len(self.bar_buffer)} bars loaded")
        except Exception as e:
            self._log(f"Warm-up failed: {e}")

    # ── Tick / candle handlers ────────────────────────────────────────────────

    def _on_tick(self, price: float, ts: pd.Timestamp):
        with self._lock:
            self.current_price = price

    def _on_candle(self, candle: pd.Series):
        with self._lock:
            ts = candle.name

            # Check broker exits on new bar
            if self.broker.open_trade:
                closed = self.broker.on_bar(candle, ts)
                if closed:
                    self.strategy.exit_position()
                    self.risk.record_trade_pnl(closed.pnl_points, closed.lots)
                    emoji = "✅" if closed.pnl_net > 0 else "❌"
                    self._log(
                        f"{emoji} EXIT {closed.direction} "
                        f"@ {closed.exit_price:.0f}  "
                        f"P&L ₹{closed.pnl_net:+,.0f}  [{closed.exit_reason}]"
                    )

            # Add bar to buffer, recompute indicators
            self.bar_buffer.append(candle)
            if len(self.bar_buffer) > 500:
                self.bar_buffer = self.bar_buffer[-300:]

            if len(self.bar_buffer) < 2:
                return

            df       = pd.DataFrame(self.bar_buffer)
            df       = add_indicators(df)
            bar      = df.iloc[-1]
            prev_bar = df.iloc[-2]

            # Evaluate strategy
            if not self.broker.open_trade and self.risk.approve(ts):
                sig = self.strategy.evaluate(bar, prev_bar)
                if sig.signal in (Signal.LONG, Signal.SHORT):
                    lots = self.risk.lots()
                    self.broker.enter(
                        direction=sig.signal.value,
                        price=sig.entry, sl=sig.sl,
                        target=sig.target, lots=lots, ts=ts,
                    )
                    self.strategy.enter(sig.signal.value)
                    arrow = "▲" if sig.signal == Signal.LONG else "▼"
                    self._log(
                        f"{arrow} {sig.signal.value} @ {sig.entry:.0f}  "
                        f"SL {sig.sl:.0f}  TGT {sig.target:.0f}"
                    )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _unrealised_pnl(self) -> float:
        if not self.broker.open_trade or not self.current_price:
            return 0.0
        t = self.broker.open_trade
        if t.direction == "LONG":
            pts = self.current_price - t.entry_price
        else:
            pts = t.entry_price - self.current_price
        from config import LOT_SIZE, COMMISSION_PER_LOT
        return round(pts * t.lots * LOT_SIZE - COMMISSION_PER_LOT * t.lots * 2, 0)

    def _win_rate(self) -> float:
        trades = self.broker.all_trades
        if not trades:
            return 0.0
        wins = sum(1 for t in trades if t.pnl_net > 0)
        return round(wins / len(trades) * 100, 1)

    def _log(self, msg: str):
        now  = datetime.now(IST).strftime("%H:%M:%S")
        line = f"[{now}] {msg}"
        self.session_log.append(line)
        logger.info(msg)