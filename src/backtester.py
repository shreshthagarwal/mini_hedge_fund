"""
Backtesting Engine
Bar-by-bar simulation on historical 5-min NIFTY futures data.
"""

import os
from dataclasses import dataclass, field
from typing import Optional
import pandas as pd
import numpy as np

from config import (
    INITIAL_CAPITAL, LOT_SIZE, COMMISSION_PER_LOT,
    SQUARE_OFF, TRADES_CSV, OUTPUTS_DIR
)
from strategy import ORBStrategy, Signal, TradeSignal, add_indicators
from risk_manager import RiskManager
from utils import get_logger

logger = get_logger("backtester")


# ─── Trade record ─────────────────────────────────────────────────────────────

@dataclass
class Trade:
    entry_time:  pd.Timestamp
    direction:   str
    entry_price: float
    sl:          float
    target:      float
    lots:        int
    exit_time:   Optional[pd.Timestamp] = None
    exit_price:  Optional[float]        = None
    exit_reason: str                    = ""
    pnl_points:  float                  = 0.0
    pnl_net:     float                  = 0.0
    duration_min: float                 = 0.0


# ─── Engine ───────────────────────────────────────────────────────────────────

class Backtester:

    def __init__(self, capital: float = INITIAL_CAPITAL):
        self.capital  = capital
        self.strategy = ORBStrategy()
        self.risk     = RiskManager(capital)
        self.trades:  list[Trade] = []
        self.equity:  list[tuple[pd.Timestamp, float]] = []

        # State for open trade
        self._open_trade: Optional[Trade] = None

    # ── Main entry ────────────────────────────────────────────────────────────

    def run(self, df: pd.DataFrame) -> "BacktestResult":
        """
        df: OHLCV DataFrame with IST-aware DatetimeIndex.
            Must have been passed through add_indicators().
        """
        logger.info(f"Starting backtest on {len(df):,} bars")
        df = add_indicators(df)
        equity_value = self.capital

        bars = list(df.iterrows())

        for i, (ts, bar) in enumerate(bars):
            if i == 0:
                self.equity.append((ts, equity_value))
                continue

            prev_bar = bars[i - 1][1]

            # ── Check open trade SL / target / time exit ──────────────────
            if self._open_trade:
                closed, reason = self._check_exit(bar, ts)
                if closed:
                    pnl_points = (
                        (self._open_trade.exit_price - self._open_trade.entry_price)
                        if self._open_trade.direction == "LONG"
                        else (self._open_trade.entry_price - self._open_trade.exit_price)
                    )
                    net = self.risk.net_pnl(pnl_points, self._open_trade.lots)
                    self._open_trade.pnl_points  = pnl_points
                    self._open_trade.pnl_net     = net
                    self._open_trade.exit_reason = reason
                    self._open_trade.duration_min = (
                        (self._open_trade.exit_time - self._open_trade.entry_time)
                        .total_seconds() / 60
                    )
                    equity_value += net
                    self.risk.record_trade_pnl(pnl_points, self._open_trade.lots)
                    self.trades.append(self._open_trade)
                    self._open_trade = None
                    self.strategy.exit_position()

            # ── Evaluate strategy for new signal ──────────────────────────
            if not self._open_trade and self.risk.approve(ts):
                sig: TradeSignal = self.strategy.evaluate(bar, prev_bar)

                if sig.signal in (Signal.LONG, Signal.SHORT):
                    lots = self.risk.lots()
                    self._open_trade = Trade(
                        entry_time  = ts,
                        direction   = sig.signal.value,
                        entry_price = sig.entry,
                        sl          = sig.sl,
                        target      = sig.target,
                        lots        = lots,
                    )
                    self.strategy.enter(sig.signal.value)
                    logger.debug(
                        f"{ts}  {sig.signal.value}  @ {sig.entry:.2f} "
                        f"SL {sig.sl:.2f}  TGT {sig.target:.2f}  [{sig.reason}]"
                    )

            self.equity.append((ts, equity_value))

        # Close any lingering position
        if self._open_trade:
            last_ts, last_bar = bars[-1]
            self._open_trade.exit_time  = last_ts
            self._open_trade.exit_price = last_bar["close"]
            pnl_points = (
                (last_bar["close"] - self._open_trade.entry_price)
                if self._open_trade.direction == "LONG"
                else (self._open_trade.entry_price - last_bar["close"])
            )
            net = self.risk.net_pnl(pnl_points, self._open_trade.lots)
            self._open_trade.pnl_points  = pnl_points
            self._open_trade.pnl_net     = net
            self._open_trade.exit_reason = "end of data"
            self.trades.append(self._open_trade)

        result = BacktestResult(self.trades, self.equity, self.capital)
        logger.info(
            f"Backtest complete — {len(self.trades)} trades | "
            f"Final equity ₹{result.final_equity:,.0f}"
        )
        return result

    # ── Exit logic ────────────────────────────────────────────────────────────

    def _check_exit(self, bar: pd.Series, ts: pd.Timestamp):
        t = self._open_trade
        close  = bar["close"]
        high   = bar["high"]
        low    = bar["low"]

        # Time-based square-off
        if ts.time() >= SQUARE_OFF:
            t.exit_time  = ts
            t.exit_price = close
            return True, "3:20 PM square-off"

        if t.direction == "LONG":
            if low <= t.sl:
                t.exit_time  = ts
                t.exit_price = t.sl
                return True, "stop loss"
            if high >= t.target:
                t.exit_time  = ts
                t.exit_price = t.target
                return True, "target hit"

        elif t.direction == "SHORT":
            if high >= t.sl:
                t.exit_time  = ts
                t.exit_price = t.sl
                return True, "stop loss"
            if low <= t.target:
                t.exit_time  = ts
                t.exit_price = t.target
                return True, "target hit"

        return False, ""


# ─── Result container ─────────────────────────────────────────────────────────

class BacktestResult:

    def __init__(
        self,
        trades: list[Trade],
        equity: list[tuple],
        initial_capital: float,
    ):
        self.trades          = trades
        self.initial_capital = initial_capital

        # Equity curve as Series
        eq_index  = [e[0] for e in equity]
        eq_values = [e[1] for e in equity]
        self.equity_curve = pd.Series(eq_values, index=eq_index, name="equity")
        self.final_equity = eq_values[-1] if eq_values else initial_capital

        # Trades as DataFrame
        if trades:
            self.trades_df = pd.DataFrame([
                {
                    "entry_time":   t.entry_time,
                    "exit_time":    t.exit_time,
                    "direction":    t.direction,
                    "entry_price":  t.entry_price,
                    "exit_price":   t.exit_price,
                    "sl":           t.sl,
                    "target":       t.target,
                    "lots":         t.lots,
                    "pnl_points":   t.pnl_points,
                    "pnl_net":      t.pnl_net,
                    "exit_reason":  t.exit_reason,
                    "duration_min": t.duration_min,
                }
                for t in trades
            ])
        else:
            self.trades_df = pd.DataFrame()

    def save(self, path: str = TRADES_CSV):
        os.makedirs(OUTPUTS_DIR, exist_ok=True)
        if not self.trades_df.empty:
            self.trades_df.to_csv(path, index=False)
            logger.info(f"Trades saved → {path}")