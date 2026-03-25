"""
intelligence/score.py — Alpha/Score Module

Generates directional alpha scores from the feature vector.
This is separate from regime — regime says WHETHER to trade,
score says WHICH DIRECTION has edge.

Outputs:
  long_score   0–1   confidence in going long
  short_score  0–1   confidence in going short
  net_score    -1→+1 net directional bias (+1 = strong long, -1 = strong short)
  size_mult    0.5–1.5 position size multiplier

Used by strategies to:
  1. Confirm direction alignment (don't fight the intel score)
  2. Scale position size when intel strongly agrees
  3. Skip trades when intel strongly disagrees
"""

from __future__ import annotations

import logging
import numpy as np
from dataclasses import dataclass
from typing import Optional

from intelligence.features import FeatureVector

logger = logging.getLogger("intel.score")


@dataclass
class AlphaScore:
    long_score:  float = 0.5    # 0–1
    short_score: float = 0.5    # 0–1
    net_score:   float = 0.0    # -1 to +1
    size_mult:   float = 1.0    # 0.5–1.5
    agrees_long:  bool = True   # intel agrees with a LONG trade
    agrees_short: bool = True   # intel agrees with a SHORT trade
    reason:      str   = ""

    def agrees_with(self, direction: str) -> bool:
        if direction == "LONG":
            return self.agrees_long
        if direction == "SHORT":
            return self.agrees_short
        return True

    def size_for(self, direction: str) -> float:
        """Size multiplier for a specific direction."""
        if direction == "LONG":
            return round(0.5 + self.long_score, 2)
        if direction == "SHORT":
            return round(0.5 + self.short_score, 2)
        return 1.0


class ScoreModule:
    """
    Computes directional alpha scores from feature vector.

    Scoring logic:
      - Macro bias, FII flow, S&P500 return → directional weight
      - Sentiment (PCR, max pain) → directional confirmation
      - VIX → uncertainty penalty
      - Combined into net score and per-direction confidence
    """

    def score(self, fv: FeatureVector, regime) -> AlphaScore:
        """
        Compute alpha score given feature vector and regime.
        regime: RegimeOutput from RegimeModule
        """
        b    = fv.bull_score
        u    = fv.uncertainty
        mb   = fv.macro_bias
        fi   = fv.fii_net_z
        sp   = fv.sp500_ret_z
        pcrs = fv.pcr_signal
        sent = fv.sentiment_overall
        vx   = fv.vix_z

        # ── Net directional score ─────────────────────────────────────────────
        # Weighted combination of all directional signals
        net = float(np.clip(
            b    * 0.30 +    # combined bull score (already weighted internally)
            mb   * 0.20 +    # macro bias
            fi   * 0.20 +    # FII flow
            pcrs * 0.15 +    # options PCR signal
            sent * 0.10 +    # overall sentiment
            sp   * 0.05,     # US market
            -1, 1,
        ))

        # VIX penalty: high VIX reduces conviction
        if vx > 1.0:
            net *= max(1 - (vx - 1) * 0.2, 0.3)

        # Uncertainty penalty
        net *= (1 - u * 0.5)

        # ── Per-direction confidence ──────────────────────────────────────────
        # long_score high when net is positive, short_score high when negative
        long_score  = float(np.clip(0.5 + net * 0.5, 0.1, 0.9))
        short_score = float(np.clip(0.5 - net * 0.5, 0.1, 0.9))

        # ── Regime alignment ──────────────────────────────────────────────────
        # If regime says bull trend, boost long score; if bear, boost short
        from intelligence.regime import BULL_TREND, BEAR_TREND, EVENT_RISK, HIGH_UNCERTAINTY, RANGE_NEUTRAL
        r = regime.regime if regime else RANGE_NEUTRAL

        if r == BULL_TREND:
            long_score  = min(long_score  * 1.2, 0.9)
        elif r == BEAR_TREND:
            short_score = min(short_score * 1.2, 0.9)
        elif r in (EVENT_RISK, HIGH_UNCERTAINTY):
            long_score  *= 0.5
            short_score *= 0.5

        # ── Agreement thresholds ──────────────────────────────────────────────
        # Strategy needs intel agreement to proceed at full size
        # "Agrees" = intel score for that direction > 0.45
        agrees_long  = long_score  > 0.45
        agrees_short = short_score > 0.45

        # ── Size multiplier ───────────────────────────────────────────────────
        # Max confidence in either direction → max size
        max_conf  = max(long_score, short_score)
        size_mult = round(0.5 + max_conf, 2)   # 0.6–1.4 range

        reason = (
            f"net={net:+.2f}  "
            f"long={long_score:.2f}  short={short_score:.2f}  "
            f"size={size_mult:.2f}x  "
            f"{'LONG✓' if agrees_long else 'LONG✗'}  "
            f"{'SHORT✓' if agrees_short else 'SHORT✗'}"
        )

        return AlphaScore(
            long_score   = long_score,
            short_score  = short_score,
            net_score    = net,
            size_mult    = size_mult,
            agrees_long  = agrees_long,
            agrees_short = agrees_short,
            reason       = reason,
        )