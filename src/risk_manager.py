"""
Risk Manager
Enforces per-trade and daily risk limits.
"""

from dataclasses import dataclass, field
from typing import Optional
import pandas as pd

from config import (
    INITIAL_CAPITAL, MAX_LOTS_PER_TRADE,
    MAX_DAILY_LOSS_PCT, LOT_SIZE, COMMISSION_PER_LOT
)
from utils import get_logger

logger = get_logger("risk_manager")


class RiskManager:
    """
    Responsibilities:
    - Approve or reject incoming trade signals
    - Track daily P&L and halt trading if daily loss limit breached
    - Calculate lot size (fixed for now; extendable to Kelly / volatility sizing)
    """

    def __init__(self, capital: float = INITIAL_CAPITAL):
        self.capital       = capital
        self.daily_pnl     = 0.0
        self.session_date: Optional[pd.Timestamp] = None
        self._halted       = False

    # ── Public ────────────────────────────────────────────────────────────────

    def approve(self, ts: pd.Timestamp) -> bool:
        """Return True if we are allowed to take a new trade right now."""
        self._check_new_session(ts)
        if self._halted:
            logger.warning("Trading halted — daily loss limit reached.")
            return False
        return True

    def lots(self) -> int:
        """How many lots to trade. Fixed at max lots for now."""
        return MAX_LOTS_PER_TRADE

    def record_trade_pnl(self, pnl_points: float, lots: int):
        """Call after each trade closes. pnl_points = points gained/lost per unit."""
        gross  = pnl_points * lots * LOT_SIZE
        net    = gross - (COMMISSION_PER_LOT * lots * 2)   # entry + exit
        self.daily_pnl += net
        self.capital   += net

        loss_limit = INITIAL_CAPITAL * MAX_DAILY_LOSS_PCT
        if self.daily_pnl < -loss_limit:
            logger.warning(
                f"Daily loss ₹{abs(self.daily_pnl):,.0f} exceeds limit ₹{loss_limit:,.0f}. "
                "Halting trading for the day."
            )
            self._halted = True

        return net

    def net_pnl(self, pnl_points: float, lots: int) -> float:
        gross = pnl_points * lots * LOT_SIZE
        return gross - (COMMISSION_PER_LOT * lots * 2)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _check_new_session(self, ts: pd.Timestamp):
        day = ts.date()
        if self.session_date is None or day != self.session_date:
            self.session_date = day
            self.daily_pnl    = 0.0
            self._halted      = False