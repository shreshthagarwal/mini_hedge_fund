"""
intelligence/engine.py — Intelligence Engine

The orchestrator. Brings together all four layers:
  TechnicalCollector + MacroCollector + SentimentCollector
        ↓
    FeatureLayer (normalise + combine)
        ↓
  RegimeModule  →  RegimeOutput  (filter: should we trade?)
  ScoreModule   →  AlphaScore    (direction: long or short edge?)
        ↓
    IntelligenceOutput  (single object passed to all strategies)

Usage in multi_engine.py:
    intel_engine = IntelligenceEngine(token, instrument_key)
    intel_engine.start()   # fetches macro at startup, starts 5-min timer

    # Every 5-min candle:
    output = intel_engine.update(current_price)

    # In strategy evaluation:
    if not output.regime.allow_trading:
        return  # skip all trades
    if not output.alpha.agrees_with("LONG"):
        return  # intel disagrees, skip or reduce size
    size = output.alpha.size_for("LONG") * base_lots
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from intelligence.features import FeatureVector, FeatureLayer
from intelligence.regime import RegimeModule, RegimeOutput, RANGE_NEUTRAL
from intelligence.score import ScoreModule, AlphaScore
from intelligence.technical.collector import TechnicalCollector
from intelligence.macro.collector import MacroCollector
from intelligence.sentiment.collector import SentimentCollector

logger = logging.getLogger("intel.engine")
IST    = ZoneInfo("Asia/Kolkata")


@dataclass
class IntelligenceOutput:
    """Single output object passed to all strategies every bar."""
    features:  Optional[FeatureVector] = None
    regime:    Optional[RegimeOutput]  = None
    alpha:     Optional[AlphaScore]    = None
    timestamp: Optional[datetime]      = None
    is_valid:  bool                    = False

    def allow_trading(self) -> bool:
        if not self.is_valid or self.regime is None:
            return True   # no intel = don't block
        return self.regime.allow_trading

    def agrees_with(self, direction: str, strategy: str = "") -> bool:
        if not self.is_valid or self.alpha is None:
            return True   # no intel = don't block
        # Check regime multiplier for this strategy
        if self.regime and strategy:
            mult = self.regime.mult(strategy)
            if mult < 0.5:
                return False
        return self.alpha.agrees_with(direction)

    def size_mult(self, direction: str, strategy: str = "") -> float:
        if not self.is_valid:
            return 1.0
        base = self.alpha.size_for(direction) if self.alpha else 1.0
        regime_mult = self.regime.mult(strategy) if (self.regime and strategy) else 1.0
        return round(base * regime_mult, 2)

    def summary(self) -> str:
        if not self.is_valid:
            return "Intel: not ready"
        r = self.regime.regime if self.regime else "?"
        a = f"net={self.alpha.net_score:+.2f}" if self.alpha else ""
        f = f"bull={self.features.bull_score:+.2f}" if self.features else ""
        return f"Intel: {r} | {f} | {a}"


class IntelligenceEngine:
    """
    Main intelligence engine. Instantiated once in MultiStrategyEngine.

    Update schedule:
      Macro:     Once at startup + daily refresh at 09:00
      Technical: Once at startup + every 30 min
      Sentiment: Every 5-min candle (same as bar frequency)
    """

    def __init__(self, token: str = "", instrument_key: str = ""):
        self.token          = token
        self.instrument_key = instrument_key

        self.tech     = TechnicalCollector()
        self.macro    = MacroCollector()
        self.sentiment= SentimentCollector()
        self.features = FeatureLayer()
        self.regime   = RegimeModule()
        self.score    = ScoreModule()

        # Cached data
        self._tech_raw:  dict = {}
        self._tech_norm: dict = {}
        self._macro_raw: dict = {}
        self._macro_norm: dict = {}
        self._macro_bias: float = 0.0

        self._last_output: IntelligenceOutput = IntelligenceOutput()
        self._last_macro_fetch: Optional[datetime] = None
        self._last_tech_fetch:  Optional[datetime] = None

        self._lock = threading.Lock()

    def start(self):
        """Fetch initial macro and technical data at startup."""
        logger.info("Intelligence engine starting...")
        self._refresh_macro()
        self._refresh_technical()
        logger.info("Intelligence engine ready")

    def update(self, current_price: float) -> IntelligenceOutput:
        """
        Called every 5-min candle.
        Updates sentiment (fast) and periodically refreshes macro/technical.
        """
        with self._lock:
            now = datetime.now(IST)

            # Refresh macro every 30 minutes during market hours
            if (self._last_macro_fetch is None or
                    (now - self._last_macro_fetch).seconds > 1800):
                try:
                    self._refresh_macro()
                except Exception as e:
                    logger.debug(f"Macro refresh failed: {e}")

            # Refresh technical every 15 minutes
            if (self._last_tech_fetch is None or
                    (now - self._last_tech_fetch).seconds > 900):
                try:
                    self._refresh_technical()
                except Exception as e:
                    logger.debug(f"Technical refresh failed: {e}")

            # Fetch sentiment every bar (5-min)
            try:
                snap = self.sentiment.fetch(current_price)
            except Exception as e:
                logger.debug(f"Sentiment fetch failed: {e}")
                snap = None

            # Build feature vector
            fv = self.features.build(
                technical_norm = self._tech_norm,
                macro_norm     = self._macro_norm,
                macro_bias     = self._macro_bias,
                sentiment      = snap,
                current_price  = current_price,
            )

            # Classify regime
            regime_out = self.regime.classify(fv)

            # Compute alpha score
            alpha_out = self.score.score(fv, regime_out)

            output = IntelligenceOutput(
                features  = fv,
                regime    = regime_out,
                alpha     = alpha_out,
                timestamp = now,
                is_valid  = True,
            )
            self._last_output = output

            logger.debug(f"{output.summary()}")
            return output

    def _refresh_macro(self):
        self._macro_raw  = self.macro.fetch_daily()
        self._macro_norm = self.macro.normalise(self._macro_raw)
        self._macro_bias = self.macro.macro_bias(self._macro_norm)
        self.macro.update_history = lambda: None  # already done in fetch_daily
        self._last_macro_fetch = datetime.now(IST)
        logger.info(f"Macro refreshed: bias={self._macro_bias:+.3f}")

    def _refresh_technical(self):
        self._tech_raw  = self.tech.fetch_daily()
        if self.token and self.instrument_key:
            oi = self.tech.fetch_oi(self.token, self.instrument_key)
            if oi is not None:
                self._tech_raw["oi_change"] = oi
        self.tech.update_history(self._tech_raw)
        self._tech_norm = self.tech.normalise(self._tech_raw)
        self._last_tech_fetch = datetime.now(IST)
        logger.debug(f"Technical refreshed: {self._tech_norm}")

    @property
    def last(self) -> IntelligenceOutput:
        return self._last_output

    def dashboard_dict(self) -> dict:
        """Serialisable summary for dashboard."""
        out = self._last_output
        if not out.is_valid:
            return {"status": "not ready"}
        return {
            "regime":      out.regime.regime        if out.regime else "unknown",
            "allow":       out.regime.allow_trading if out.regime else True,
            "confidence":  out.regime.confidence    if out.regime else 0,
            "description": out.regime.description   if out.regime else "",
            "net_score":   out.alpha.net_score      if out.alpha  else 0,
            "long_score":  out.alpha.long_score     if out.alpha  else 0.5,
            "short_score": out.alpha.short_score    if out.alpha  else 0.5,
            "size_mult":   out.alpha.size_mult      if out.alpha  else 1.0,
            "bull_score":  out.features.bull_score  if out.features else 0,
            "macro_bias":  out.features.macro_bias  if out.features else 0,
            "uncertainty": out.features.uncertainty if out.features else 0.5,
            "fii_net":     self._macro_raw.get("fii_net", 0),
            "vix":         self._tech_raw.get("vix", 0),
            "pcr":         out.features.pcr_signal  if out.features else 0,
            "multipliers": out.regime.multipliers   if out.regime else {},
            "missing":     out.features.missing     if out.features else [],
        }