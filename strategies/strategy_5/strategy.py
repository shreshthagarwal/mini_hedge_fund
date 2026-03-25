"""
strategy_5/strategy.py — Futures vs Spot Basis Arbitrage

Signal logic:
  - Compute basis = futures_close − spot_close
  - Compute fair_basis = spot × risk_free_rate × (days_to_expiry / 365)
  - basis_spread = actual_basis − fair_basis
  - z_score = rolling z-score of basis_spread (20-day window)

  Entry SHORT (futures overpriced):
    z > +ZSCORE_ENTRY AND basis_spread > MIN_BASIS_SPREAD
    → short futures, expect basis to narrow toward fair value

  Entry LONG (futures underpriced):
    z < −ZSCORE_ENTRY AND basis_spread < −MIN_BASIS_SPREAD
    → long futures, expect basis to widen back toward fair value

  Exit:
    Target: z reverts to ZSCORE_TARGET (near zero)
    Stop:   z widens to ZSCORE_STOP (thesis wrong)
    Time:   square-off at 15:20

SL/Target in price terms (for engine compatibility):
  The engine takes sl_dist in points. We compute this from the
  z-score thresholds and current rolling std of basis_spread.
  sl_dist   = (ZSCORE_STOP   − |z|) × rolling_std
  tgt_dist  = (|z| − ZSCORE_TARGET) × rolling_std
  rr        = tgt_dist / sl_dist
"""

from __future__ import annotations

import logging
import numpy as np
import pandas as pd
from typing import Optional

from strategies.strategy_5.config import (
    ZSCORE_ENTRY, ZSCORE_TARGET, ZSCORE_STOP,
    MIN_BASIS_SPREAD, BASIS_ZSCORE_WINDOW,
    ENTRY_START, ENTRY_CUTOFF, SQUARE_OFF,
    RISK_FREE_RATE,
)
from src.engine import Signal

logger = logging.getLogger("strategy_5")


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    All basis indicators must already be computed by data_loader.
    This is a pass-through — the engine calls add_indicators_fn(df)
    and we just return df unchanged since data_loader did the work.
    Also add ATR for reference (not used in signal but useful in analytics).
    """
    df = df.copy()
    if "basis_zscore" not in df.columns:
        raise ValueError(
            "basis_zscore column missing. "
            "Load data with strategy_5.data_loader.load_aligned() first."
        )

    # ATR for analytics / position sizing reference
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"]  - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.ewm(span=14, adjust=False).mean()

    # Rolling std of basis_spread (used for SL/target distance calculation)
    df["basis_std"] = df["basis_spread"].rolling(
        BASIS_ZSCORE_WINDOW, min_periods=50
    ).std()

    return df


class BasisArbStrategy:
    """
    NIFTY Futures vs Spot Basis Arbitrage.
    Implements engine.StrategyProtocol.

    Trades the deviation of futures basis from its fair value.
    Completely uncorrelated to ORB (S1) and VWAP reversion (S2)
    because the signal source is a cross-instrument relationship,
    not a price pattern on a single instrument.
    """

    name = "S5_BASIS_ARB"

    def __init__(self):
        self.in_position    = False
        self.position_side: Optional[str] = None

        # Track the z-score at entry for dynamic exit management
        self._entry_zscore: float = 0.0

    def reset(self):
        self.in_position    = False
        self.position_side  = None
        self._entry_zscore  = 0.0

    def on_bar(self, bar: pd.Series, prev_bar: pd.Series, context: dict) -> Signal:
        ts = bar.name

        if context.get("in_position"):
            return Signal("NONE")

        if not (ENTRY_START <= ts.time() <= ENTRY_CUTOFF):
            return Signal("NONE", reason="outside window")

        z         = bar.get("basis_zscore",  np.nan)
        spread    = bar.get("basis_spread",  np.nan)
        basis_std = bar.get("basis_std",     np.nan)
        atr       = bar.get("atr",           np.nan)

        if any(pd.isna([z, spread, basis_std])) or basis_std == 0:
            return Signal("NONE", reason="warming up")

        # Require minimum abs spread to filter out noise around fair value
        if abs(spread) < MIN_BASIS_SPREAD:
            return Signal("NONE", reason=f"spread {spread:.1f} < {MIN_BASIS_SPREAD}")

        # ── SHORT signal: futures overpriced relative to spot ─────────────────
        if z > ZSCORE_ENTRY and spread > 0:
            # SL distance: basis_std × (ZSCORE_STOP − z)
            # If z=2.2 and stop=3.0: sl covers 0.8 std further divergence
            sl_z_dist  = ZSCORE_STOP - z
            tgt_z_dist = z - ZSCORE_TARGET

            if sl_z_dist <= 0 or tgt_z_dist <= 0:
                return Signal("NONE", reason="invalid z distances")

            sl_dist  = basis_std * sl_z_dist
            tgt_dist = basis_std * tgt_z_dist
            rr       = tgt_dist / sl_dist

            if rr < 0.8:   # minimum 0.8:1 — basis arb has high win rate
                return Signal("NONE", reason=f"RR {rr:.2f} < 0.8")

            return Signal(
                direction = "SHORT",
                sl_dist   = sl_dist,
                rr        = rr,
                reason    = (
                    f"BASIS SHORT  z={z:.2f}  spread={spread:.1f}  "
                    f"SL_z={ZSCORE_STOP:.1f}  TGT_z={ZSCORE_TARGET:.1f}"
                ),
                meta = {
                    "zscore":     z,
                    "spread":     spread,
                    "basis_std":  basis_std,
                    "entry_z":    z,
                },
            )

        # ── LONG signal: futures underpriced relative to spot ─────────────────
        if z < -ZSCORE_ENTRY and spread < 0:
            sl_z_dist  = ZSCORE_STOP - abs(z)
            tgt_z_dist = abs(z) - ZSCORE_TARGET

            if sl_z_dist <= 0 or tgt_z_dist <= 0:
                return Signal("NONE", reason="invalid z distances")

            sl_dist  = basis_std * sl_z_dist
            tgt_dist = basis_std * tgt_z_dist
            rr       = tgt_dist / sl_dist

            if rr < 0.8:
                return Signal("NONE", reason=f"RR {rr:.2f} < 0.8")

            return Signal(
                direction = "LONG",
                sl_dist   = sl_dist,
                rr        = rr,
                reason    = (
                    f"BASIS LONG   z={z:.2f}  spread={spread:.1f}  "
                    f"SL_z={ZSCORE_STOP:.1f}  TGT_z={ZSCORE_TARGET:.1f}"
                ),
                meta = {
                    "zscore":    z,
                    "spread":    spread,
                    "basis_std": basis_std,
                    "entry_z":   z,
                },
            )

        return Signal("NONE", reason=f"z={z:.2f} within bounds")

    def on_fill(self, direction: str, price: float, ts: pd.Timestamp):
        self.in_position   = True
        self.position_side = direction
        logger.debug(f"S5 fill {direction} @ {price:.2f}  [{ts.strftime('%H:%M')}]")

    def on_exit(self, reason: str, price: float, ts: pd.Timestamp):
        self.in_position   = False
        self.position_side = None
        self._entry_zscore = 0.0
        logger.debug(f"S5 exit [{reason}] @ {price:.2f}")