"""
paper_broker.py — simulated order execution engine

Tracks open positions, checks SL/target/time exits on every new bar,
calculates P&L, logs all trades to outputs/paper_trades.csv.

No real orders are placed. Fills happen at signal price instantly.
"""

import csv
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo
import pandas as pd

from config import (
    IST, LOT_SIZE, COMMISSION_PER_LOT,
    INITIAL_CAPITAL, SQUARE_OFF, OUTPUTS_DIR,
)
from utils import get_logger

logger = get_logger("paper_broker")

PAPER_TRADES_CSV = os.path.join(OUTPUTS_DIR, "paper_trades.csv")


# ─── Trade record ─────────────────────────────────────────────────────────────

@dataclass
class PaperTrade:
    id:           int
    direction:    str        # LONG / SHORT
    entry_time:   pd.Timestamp
    entry_price:  float
    sl:           float
    target:       float
    lots:         int
    exit_time:    Optional[pd.Timestamp] = None
    exit_price:   Optional[float]        = None
    exit_reason:  str                    = ""
    pnl_points:   float                  = 0.0
    pnl_net:      float                  = 0.0
    status:       str                    = "OPEN"   # OPEN / CLOSED


# ─── Broker ───────────────────────────────────────────────────────────────────

class PaperBroker:
    """
    Simulates a trading broker for paper trading.

    Usage:
        broker = PaperBroker()
        trade  = broker.enter("SHORT", price=23400, sl=23450, target=23300, lots=1, ts=ts)
        broker.on_bar(bar, ts)   # call on every new 5-min bar
    """

    def __init__(self, capital: float = INITIAL_CAPITAL):
        self.capital       = capital
        self.session_pnl   = 0.0
        self.open_trade:   Optional[PaperTrade] = None
        self.all_trades:   list[PaperTrade]     = []
        self._trade_id     = 0
        self._csv_ready    = False
        self._init_csv()

    # ── Public ────────────────────────────────────────────────────────────────

    def enter(
        self,
        direction: str,
        price: float,
        sl: float,
        target: float,
        lots: int,
        ts: pd.Timestamp,
    ) -> PaperTrade:
        """Open a new paper trade. Fills instantly at price."""
        if self.open_trade:
            logger.warning("Already in a position — ignoring new entry")
            return self.open_trade

        self._trade_id += 1
        trade = PaperTrade(
            id          = self._trade_id,
            direction   = direction,
            entry_time  = ts,
            entry_price = price,
            sl          = sl,
            target      = target,
            lots        = lots,
        )
        self.open_trade = trade

        arrow = "▲" if direction == "LONG" else "▼"
        logger.info(
            f"\n  {'─'*52}\n"
            f"  {arrow}  PAPER {direction}  @ {price:.2f}  "
            f"[{ts.strftime('%H:%M:%S IST')}]\n"
            f"     SL      {sl:.2f}\n"
            f"     Target  {target:.2f}\n"
            f"     Lots    {lots}  (notional ₹{price*lots*LOT_SIZE:,.0f})\n"
            f"  {'─'*52}"
        )
        return trade

    def on_bar(self, bar: pd.Series, ts: pd.Timestamp) -> Optional[PaperTrade]:
        """
        Call on every new 5-min bar.
        Checks SL, target, and 3:20 PM square-off.
        Returns closed trade if position was exited, else None.
        """
        if not self.open_trade:
            return None

        t     = self.open_trade
        close = bar["close"]
        high  = bar["high"]
        low   = bar["low"]

        exit_price  = None
        exit_reason = None

        # Square-off
        if ts.time() >= SQUARE_OFF:
            exit_price  = close
            exit_reason = "3:20 PM square-off"

        elif t.direction == "LONG":
            if low <= t.sl:
                exit_price  = t.sl
                exit_reason = "stop loss"
            elif high >= t.target:
                exit_price  = t.target
                exit_reason = "target hit"

        elif t.direction == "SHORT":
            if high >= t.sl:
                exit_price  = t.sl
                exit_reason = "stop loss"
            elif low <= t.target:
                exit_price  = t.target
                exit_reason = "target hit"

        if exit_price is not None:
            return self._close(exit_price, exit_reason, ts)

        return None

    def force_exit(self, price: float, ts: pd.Timestamp, reason: str = "manual exit"):
        """Force-close open position at given price."""
        if self.open_trade:
            self._close(price, reason, ts)

    def summary(self):
        """Print end-of-session summary."""
        trades  = self.all_trades
        n       = len(trades)
        wins    = [t for t in trades if t.pnl_net > 0]
        losses  = [t for t in trades if t.pnl_net < 0]

        logger.info("")
        logger.info("=" * 52)
        logger.info("  PAPER TRADING SESSION SUMMARY")
        logger.info("=" * 52)
        logger.info(f"  Session P&L     ₹{self.session_pnl:+,.0f}")
        logger.info(f"  Total capital   ₹{self.capital:,.0f}")
        logger.info(f"  Total trades    {n}")
        if n:
            logger.info(f"  Win rate        {len(wins)/n*100:.1f}%")
            logger.info(f"  Avg win         ₹{sum(t.pnl_net for t in wins)/max(len(wins),1):+,.0f}")
            logger.info(f"  Avg loss        ₹{sum(t.pnl_net for t in losses)/max(len(losses),1):+,.0f}")
        logger.info(f"  Saved           -> {PAPER_TRADES_CSV}")
        logger.info("=" * 52)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _close(self, exit_price: float, reason: str, ts: pd.Timestamp) -> PaperTrade:
        t = self.open_trade
        t.exit_time  = ts
        t.exit_price = exit_price
        t.exit_reason = reason
        t.status     = "CLOSED"

        if t.direction == "LONG":
            t.pnl_points = exit_price - t.entry_price
        else:
            t.pnl_points = t.entry_price - exit_price

        gross       = t.pnl_points * t.lots * LOT_SIZE
        t.pnl_net   = gross - (COMMISSION_PER_LOT * t.lots * 2)

        self.session_pnl += t.pnl_net
        self.capital     += t.pnl_net

        emoji = "✅" if t.pnl_net > 0 else "❌"
        logger.info(
            f"{emoji} EXIT #{t.id}  {t.direction}  "
            f"entry {t.entry_price:.2f} -> exit {exit_price:.2f}  "
            f"P&L ₹{t.pnl_net:+,.0f}  [{reason}]"
        )

        self.all_trades.append(t)
        self._append_csv(t)
        self.open_trade = None
        return t

    def _init_csv(self):
        os.makedirs(OUTPUTS_DIR, exist_ok=True)
        if not os.path.exists(PAPER_TRADES_CSV):
            with open(PAPER_TRADES_CSV, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=self._csv_fields())
                writer.writeheader()
        self._csv_ready = True

    def _append_csv(self, t: PaperTrade):
        with open(PAPER_TRADES_CSV, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self._csv_fields())
            writer.writerow({
                "date":         t.entry_time.strftime("%Y-%m-%d"),
                "entry_time":   t.entry_time.strftime("%H:%M:%S"),
                "exit_time":    t.exit_time.strftime("%H:%M:%S") if t.exit_time else "",
                "direction":    t.direction,
                "entry_price":  t.entry_price,
                "exit_price":   t.exit_price,
                "sl":           t.sl,
                "target":       t.target,
                "lots":         t.lots,
                "pnl_points":   round(t.pnl_points, 2),
                "pnl_net":      round(t.pnl_net, 2),
                "exit_reason":  t.exit_reason,
            })

    @staticmethod
    def _csv_fields():
        return [
            "date", "entry_time", "exit_time", "direction",
            "entry_price", "exit_price", "sl", "target", "lots",
            "pnl_points", "pnl_net", "exit_reason",
        ]