"""
intelligence/features.py — Feature Layer

Normalises and combines all raw signals from technical, macro, and sentiment
into a unified feature vector. This is the single source of truth that
feeds both the RegimeModule and the ScoreModule.

Feature vector (18 features, all z-scored or [-1,+1]):
  Technical (5):
    vix_z           India VIX z-score
    banknifty_div   BankNifty vs Nifty divergence
    oi_change_z     Futures OI change z-score
    pcr_z           Put-call ratio z-score (from options)
    iv_pct          IV percentile (0-1 normalised)

  Macro (6):
    sp500_ret_z     S&P500 overnight return z-score
    usdinr_ret_z    USD/INR change z-score
    crude_ret_z     Crude oil change z-score
    fii_net_z       FII net flow z-score
    macro_bias      Combined macro directional bias (-1 to +1)
    us10y_z         US 10Y yield z-score

  Sentiment (5):
    pcr_signal      PCR directional signal (-1 to +1)
    max_pain_dist   Distance of price from max pain (normalised)
    iv_signal       IV-based caution signal (-1 to +1)
    sentiment_overall Combined sentiment (-1 to +1)
    oi_wall_dist    Distance to nearest OI wall (normalised)

  Composite (2):
    bull_score      Weighted bull signal (+: bullish, -: bearish)
    uncertainty     How uncertain the market is (0=certain, 1=uncertain)
"""

from __future__ import annotations

import logging
import numpy as np
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("intel.features")

FEATURE_NAMES = [
    # Technical
    "vix_z", "banknifty_div", "oi_change_z", "pcr_z", "iv_pct",
    # Macro
    "sp500_ret_z", "usdinr_ret_z", "crude_ret_z", "fii_net_z",
    "macro_bias", "us10y_z",
    # Sentiment
    "pcr_signal", "max_pain_dist", "iv_signal", "sentiment_overall",
    "oi_wall_dist",
    # Composite
    "bull_score", "uncertainty",
]


@dataclass
class FeatureVector:
    """Normalised feature vector passed to regime and score modules."""

    # Technical
    vix_z:          float = 0.0
    banknifty_div:  float = 0.0
    oi_change_z:    float = 0.0
    pcr_z:          float = 0.0
    iv_pct:         float = 0.5

    # Macro
    sp500_ret_z:    float = 0.0
    usdinr_ret_z:   float = 0.0
    crude_ret_z:    float = 0.0
    fii_net_z:      float = 0.0
    macro_bias:     float = 0.0
    us10y_z:        float = 0.0

    # Sentiment
    pcr_signal:     float = 0.0
    max_pain_dist:  float = 0.0
    iv_signal:      float = 0.0
    sentiment_overall: float = 0.0
    oi_wall_dist:   float = 0.0

    # Composite
    bull_score:     float = 0.0
    uncertainty:    float = 0.5

    # Metadata
    is_complete:    bool  = False   # False if data is missing/stale
    missing:        list  = field(default_factory=list)

    def to_array(self) -> np.ndarray:
        return np.array([
            self.vix_z, self.banknifty_div, self.oi_change_z,
            self.pcr_z, self.iv_pct,
            self.sp500_ret_z, self.usdinr_ret_z, self.crude_ret_z,
            self.fii_net_z, self.macro_bias, self.us10y_z,
            self.pcr_signal, self.max_pain_dist, self.iv_signal,
            self.sentiment_overall, self.oi_wall_dist,
            self.bull_score, self.uncertainty,
        ], dtype=np.float32)


class FeatureLayer:
    """
    Assembles FeatureVector from raw technical, macro, and sentiment data.
    Called once per update cycle (daily for macro, every 5-min for sentiment).
    """

    def build(
        self,
        technical_norm: dict,
        macro_norm: dict,
        macro_bias: float,
        sentiment,          # SentimentSnapshot
        current_price: float,
    ) -> FeatureVector:
        """
        Build and return a FeatureVector from all available data.
        Missing data is filled with 0.0 (neutral) and flagged.
        """
        fv = FeatureVector()
        missing = []

        # ── Technical features ────────────────────────────────────────────────
        fv.vix_z        = float(technical_norm.get("vix", 0.0))
        fv.banknifty_div = float(technical_norm.get("bnk_nifty_div", 0.0))
        fv.oi_change_z  = float(technical_norm.get("oi_change", 0.0))
        # PCR z-score from technical collector (options)
        fv.pcr_z        = float(technical_norm.get("pcr", 0.0))

        if not any(k in technical_norm for k in ["vix", "banknifty_div"]):
            missing.append("technical")

        # ── Macro features ────────────────────────────────────────────────────
        fv.sp500_ret_z  = float(macro_norm.get("sp500_ret", 0.0))
        fv.usdinr_ret_z = float(macro_norm.get("usdinr_ret", 0.0))
        fv.crude_ret_z  = float(macro_norm.get("crude_ret", 0.0))
        fv.fii_net_z    = float(macro_norm.get("fii_net", 0.0))
        fv.macro_bias   = float(macro_bias)
        fv.us10y_z      = float(macro_norm.get("us10y", 0.0))

        if not any(k in macro_norm for k in ["sp500_ret", "fii_net"]):
            missing.append("macro")

        # ── Sentiment features ────────────────────────────────────────────────
        if sentiment is not None:
            fv.pcr_signal      = float(sentiment.pcr_signal)
            fv.iv_pct          = float(sentiment.iv_percentile / 100)
            fv.iv_signal       = float(sentiment.iv_signal)
            fv.sentiment_overall = float(sentiment.overall)
            fv.max_pain_dist   = float(sentiment.max_pain_signal)

            # OI wall distance (normalised by current price)
            if sentiment.ce_wall > 0 and current_price > 0:
                dist = (sentiment.ce_wall - current_price) / current_price
                fv.oi_wall_dist = float(np.clip(dist * 20, -1, 1))
        else:
            missing.append("sentiment")

        # ── Composite features ────────────────────────────────────────────────
        # Bull score: weighted sum of directional signals
        fv.bull_score = float(np.clip(
            fv.sp500_ret_z  * 0.15 +
            fv.fii_net_z    * 0.25 +
            fv.macro_bias   * 0.20 +
            fv.pcr_signal   * 0.20 +
            -fv.usdinr_ret_z * 0.10 +
            -fv.crude_ret_z  * 0.10,
            -1, 1,
        ))

        # Uncertainty: how conflicted are the signals?
        signals = [
            fv.sp500_ret_z, fv.fii_net_z, fv.macro_bias,
            fv.pcr_signal, fv.sentiment_overall,
        ]
        non_zero = [abs(s) for s in signals if abs(s) > 0.05]
        if non_zero:
            # High agreement = low uncertainty, high disagreement = high uncertainty
            mean_direction = np.mean([np.sign(s) for s in signals if abs(s) > 0.05])
            fv.uncertainty = float(1 - abs(mean_direction))
        else:
            fv.uncertainty = 0.5

        # VIX adds to uncertainty
        fv.uncertainty = float(np.clip(fv.uncertainty + fv.vix_z * 0.15, 0, 1))

        fv.is_complete = len(missing) == 0
        fv.missing     = missing

        logger.debug(
            f"Features: bull={fv.bull_score:+.2f}  "
            f"uncertainty={fv.uncertainty:.2f}  "
            f"macro={fv.macro_bias:+.2f}  "
            f"sentiment={fv.sentiment_overall:+.2f}  "
            f"missing={missing or 'none'}"
        )
        return fv