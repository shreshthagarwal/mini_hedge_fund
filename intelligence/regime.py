"""
intelligence/regime.py — Regime Module

Uses the feature vector to classify the current market environment.
This is a FILTER — it decides whether trading should occur at all
and which strategy types are appropriate.

Regime classification:
  BULL_TREND      Strong upward momentum, macro supportive
  BEAR_TREND      Strong downward momentum, macro negative
  RANGE_BULLISH   Ranging but macro/sentiment leaning bullish
  RANGE_BEARISH   Ranging but macro/sentiment leaning bearish
  RANGE_NEUTRAL   Ranging with no directional bias
  HIGH_UNCERTAINTY VIX elevated, conflicting signals — reduce all
  EVENT_RISK      Extreme readings — pause trading

For each regime, outputs:
  - allow_trading: bool
  - strategy_multipliers: dict  (S1–S5)
  - max_lots: int
  - description: str
"""

from __future__ import annotations

import logging
import numpy as np
from dataclasses import dataclass, field
from typing import Optional

from intelligence.features import FeatureVector

logger = logging.getLogger("intel.regime")

# Regime names
BULL_TREND       = "BULL_TREND"
BEAR_TREND       = "BEAR_TREND"
RANGE_BULLISH    = "RANGE_BULLISH"
RANGE_BEARISH    = "RANGE_BEARISH"
RANGE_NEUTRAL    = "RANGE_NEUTRAL"
HIGH_UNCERTAINTY = "HIGH_UNCERTAINTY"
EVENT_RISK       = "EVENT_RISK"


@dataclass
class RegimeOutput:
    regime:       str  = RANGE_NEUTRAL
    allow_trading: bool = True
    multipliers:  dict = field(default_factory=lambda: {
        "S1": 1.0, "S2": 1.0, "S3": 1.0, "S4": 1.0, "S5": 1.0
    })
    max_lots:     int  = 1
    confidence:   float = 0.5   # 0–1 how confident we are in this regime
    description:  str  = ""

    def mult(self, strategy: str) -> float:
        return self.multipliers.get(strategy, 1.0)


class RegimeModule:
    """
    Classifies market regime from the feature vector.

    Rules-based classifier using thresholds on key features:
      bull_score, uncertainty, macro_bias, vix_z, fii_net_z

    Strategy multipliers by regime:
      S1 ORB works best in trending markets
      S2 VWAP works best in ranging markets
      S3 Candle works in all but high uncertainty
      S4 RL adapts but needs clear signals
      S5 Basis arb is regime-agnostic
    """

    def classify(self, fv: FeatureVector) -> RegimeOutput:
        """Classify regime from feature vector."""

        b  = fv.bull_score
        u  = fv.uncertainty
        vx = fv.vix_z
        mb = fv.macro_bias
        fi = fv.fii_net_z
        sent = fv.sentiment_overall

        # ── Event risk: extreme VIX or extreme uncertainty ────────────────────
        if vx > 2.5 or u > 0.9:
            return RegimeOutput(
                regime        = EVENT_RISK,
                allow_trading = False,
                multipliers   = {k: 0.0 for k in "S1 S2 S3 S4 S5".split()},
                max_lots      = 0,
                confidence    = 0.9,
                description   = f"Event risk: VIX_z={vx:.1f} uncertainty={u:.2f} — pausing all trading",
            )

        # ── High uncertainty: conflicting signals ─────────────────────────────
        if u > 0.7 or (abs(b) < 0.15 and vx > 1.5):
            return RegimeOutput(
                regime        = HIGH_UNCERTAINTY,
                allow_trading = True,
                multipliers   = {"S1": 0.5, "S2": 0.6, "S3": 0.5, "S4": 0.5, "S5": 0.9},
                max_lots      = 1,
                confidence    = 0.6,
                description   = f"High uncertainty: bull={b:+.2f} uncertainty={u:.2f} — reduced sizing",
            )

        # ── Strong bull trend ─────────────────────────────────────────────────
        if b > 0.4 and mb > 0.2 and fi > 0.5:
            conf = min(0.6 + abs(b) * 0.4, 1.0)
            return RegimeOutput(
                regime        = BULL_TREND,
                allow_trading = True,
                multipliers   = {"S1": 1.4, "S2": 0.7, "S3": 1.1, "S4": 1.2, "S5": 1.0},
                max_lots      = 1,
                confidence    = conf,
                description   = f"Bull trend: bull={b:+.2f} macro={mb:+.2f} FII={fi:+.2f}",
            )

        # ── Strong bear trend ─────────────────────────────────────────────────
        if b < -0.4 and mb < -0.2:
            conf = min(0.6 + abs(b) * 0.4, 1.0)
            return RegimeOutput(
                regime        = BEAR_TREND,
                allow_trading = True,
                multipliers   = {"S1": 1.3, "S2": 0.7, "S3": 1.1, "S4": 1.2, "S5": 1.0},
                max_lots      = 1,
                confidence    = conf,
                description   = f"Bear trend: bull={b:+.2f} macro={mb:+.2f}",
            )

        # ── Range with bias ───────────────────────────────────────────────────
        if b > 0.15 or sent > 0.2:
            return RegimeOutput(
                regime        = RANGE_BULLISH,
                allow_trading = True,
                multipliers   = {"S1": 1.0, "S2": 1.3, "S3": 1.0, "S4": 1.0, "S5": 1.0},
                max_lots      = 1,
                confidence    = 0.55,
                description   = f"Range bullish: bull={b:+.2f} sentiment={sent:+.2f}",
            )

        if b < -0.15 or sent < -0.2:
            return RegimeOutput(
                regime        = RANGE_BEARISH,
                allow_trading = True,
                multipliers   = {"S1": 1.0, "S2": 1.3, "S3": 1.0, "S4": 1.0, "S5": 1.0},
                max_lots      = 1,
                confidence    = 0.55,
                description   = f"Range bearish: bull={b:+.2f} sentiment={sent:+.2f}",
            )

        # ── Neutral range ─────────────────────────────────────────────────────
        return RegimeOutput(
            regime        = RANGE_NEUTRAL,
            allow_trading = True,
            multipliers   = {"S1": 0.9, "S2": 1.2, "S3": 0.9, "S4": 0.9, "S5": 1.0},
            max_lots      = 1,
            confidence    = 0.5,
            description   = f"Neutral range: bull={b:+.2f} uncertainty={u:.2f}",
        )