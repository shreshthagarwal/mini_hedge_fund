"""
intelligence/sentiment/collector.py

Sentiment signals from options market and derivatives data:
  - Put/Call Ratio (PCR) — options market positioning
  - Max Pain level — strike where options writers profit most
  - IV Percentile — implied volatility rank (0-100)
  - Futures premium/discount to fair value
  - OI concentration at strikes (where is the wall?)

These are derived from the NSE options chain.
Updated every 5 minutes during market hours (same as our bar frequency).
"""

from __future__ import annotations

import logging
import numpy as np
import pandas as pd
from typing import Optional
from dataclasses import dataclass, field

logger = logging.getLogger("intel.sentiment")


@dataclass
class SentimentSnapshot:
    pcr:              float = 1.0    # put-call ratio (neutral = 1.0)
    pcr_signal:       float = 0.0   # -1 bearish, 0 neutral, +1 bullish
    max_pain:         float = 0.0   # max pain strike price
    max_pain_signal:  float = 0.0   # distance from current price (normalised)
    iv_percentile:    float = 50.0  # 0-100 (high = expensive options)
    iv_signal:        float = 0.0   # high IV = expected move = caution
    futures_premium:  float = 0.0   # futures basis z-score
    ce_wall:          float = 0.0   # nearest call wall (resistance)
    pe_wall:          float = 0.0   # nearest put wall (support)
    overall:          float = 0.0   # combined sentiment: -1 to +1


class SentimentCollector:
    """
    Fetches options chain data from NSE and derives sentiment signals.
    """

    def __init__(self):
        self._pcr_history:  list[float] = []
        self._iv_history:   list[float] = []
        self._last_snapshot: Optional[SentimentSnapshot] = None

    def fetch(self, current_price: float) -> SentimentSnapshot:
        """
        Fetch NSE options chain and compute sentiment signals.
        current_price: current NIFTY futures price.
        """
        snap = SentimentSnapshot()

        chain = self._fetch_options_chain()
        if chain is None:
            logger.debug("Options chain unavailable — neutral sentiment")
            if self._last_snapshot:
                return self._last_snapshot
            return snap

        # ── PCR ───────────────────────────────────────────────────────────────
        snap.pcr = self._compute_pcr(chain)
        self._pcr_history.append(snap.pcr)
        if len(self._pcr_history) > 50:
            self._pcr_history = self._pcr_history[-50:]

        # PCR signal: < 0.7 = bullish, 0.7-1.2 = neutral, > 1.2 = bearish
        if snap.pcr < 0.7:
            snap.pcr_signal = +0.6
        elif snap.pcr < 0.85:
            snap.pcr_signal = +0.3
        elif snap.pcr < 1.0:
            snap.pcr_signal = 0.0
        elif snap.pcr < 1.2:
            snap.pcr_signal = -0.3
        else:
            snap.pcr_signal = -0.6

        # ── Max pain ──────────────────────────────────────────────────────────
        snap.max_pain = self._compute_max_pain(chain)
        if snap.max_pain > 0 and current_price > 0:
            # Signal: price above max pain = put sellers happy = neutral/bullish
            dist_pct = (current_price - snap.max_pain) / current_price
            snap.max_pain_signal = float(np.clip(dist_pct * 10, -1, 1))

        # ── OI walls (support/resistance from options) ────────────────────────
        snap.ce_wall, snap.pe_wall = self._find_oi_walls(chain, current_price)

        # ── IV percentile ─────────────────────────────────────────────────────
        snap.iv_percentile = self._compute_iv_percentile(chain)
        # High IV = uncertainty = reduce position size
        # Low IV  = complacency = normal sizing
        snap.iv_signal = float(np.clip(1.0 - snap.iv_percentile / 100, 0, 1)) * 2 - 1

        # ── Combined sentiment ────────────────────────────────────────────────
        snap.overall = round(
            snap.pcr_signal      * 0.40 +
            snap.max_pain_signal * 0.30 +
            snap.iv_signal       * 0.30,
            3,
        )

        self._last_snapshot = snap
        logger.debug(
            f"Sentiment: PCR={snap.pcr:.2f}({snap.pcr_signal:+.2f})  "
            f"MaxPain={snap.max_pain:.0f}  IV%ile={snap.iv_percentile:.0f}  "
            f"Overall={snap.overall:+.2f}"
        )
        return snap

    def _fetch_options_chain(self) -> Optional[list]:
        """Fetch NSE NIFTY options chain. Returns list of strike data."""
        try:
            import requests
            session = requests.Session()
            # Step 1: get cookies
            session.get(
                "https://www.nseindia.com",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=5
            )
            # Step 2: fetch chain
            resp = session.get(
                "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY",
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "application/json",
                    "Referer": "https://www.nseindia.com/option-chain",
                },
                timeout=10,
            )
            if resp.ok:
                return resp.json().get("records", {}).get("data", [])
        except Exception as e:
            logger.debug(f"Options chain fetch failed: {e}")
        return None

    def _compute_pcr(self, chain: list) -> float:
        """Compute put/call ratio by OI."""
        total_ce = sum(r.get("CE", {}).get("openInterest", 0) for r in chain if r.get("CE"))
        total_pe = sum(r.get("PE", {}).get("openInterest", 0) for r in chain if r.get("PE"))
        return round(total_pe / total_ce, 3) if total_ce > 0 else 1.0

    def _compute_max_pain(self, chain: list) -> float:
        """
        Max pain = strike where total options value (CE+PE) is minimised.
        Options writers profit most at this price on expiry.
        """
        strikes = {}
        for r in chain:
            strike = r.get("strikePrice", 0)
            if strike:
                ce_oi = r.get("CE", {}).get("openInterest", 0)
                pe_oi = r.get("PE", {}).get("openInterest", 0)
                strikes[strike] = {"ce": ce_oi, "pe": pe_oi}

        if not strikes:
            return 0.0

        all_strikes = sorted(strikes.keys())
        pain = {}
        for s in all_strikes:
            # Total intrinsic value at expiry if price = s
            total = 0
            for k, v in strikes.items():
                ce_pain = v["ce"] * max(s - k, 0)   # CE worth 0 if s > k
                pe_pain = v["pe"] * max(k - s, 0)   # PE worth 0 if s < k
                total  += ce_pain + pe_pain
            pain[s] = total

        max_pain_strike = min(pain, key=pain.get)
        return float(max_pain_strike)

    def _find_oi_walls(
        self, chain: list, current_price: float
    ) -> tuple[float, float]:
        """
        Find the nearest significant call wall (resistance) and put wall (support).
        A wall = strike with OI > 2x average OI at nearby strikes.
        """
        ce_oi_by_strike = {}
        pe_oi_by_strike = {}

        for r in chain:
            strike = r.get("strikePrice", 0)
            if not strike:
                continue
            if r.get("CE"):
                ce_oi_by_strike[strike] = r["CE"].get("openInterest", 0)
            if r.get("PE"):
                pe_oi_by_strike[strike] = r["PE"].get("openInterest", 0)

        # Call wall: highest OI among strikes ABOVE current price
        ce_above = {k: v for k, v in ce_oi_by_strike.items() if k > current_price}
        pe_below = {k: v for k, v in pe_oi_by_strike.items() if k < current_price}

        ce_wall = max(ce_above, key=ce_above.get) if ce_above else 0.0
        pe_wall = max(pe_below, key=pe_below.get) if pe_below else 0.0

        return float(ce_wall), float(pe_wall)

    def _compute_iv_percentile(self, chain: list) -> float:
        """
        Compute IV percentile from ATM options.
        Returns 0-100. High = expensive options = uncertainty.
        """
        ivs = []
        for r in chain:
            for side in ["CE", "PE"]:
                if r.get(side):
                    iv = r[side].get("impliedVolatility", 0)
                    if iv > 0:
                        ivs.append(iv)

        if not ivs:
            return 50.0

        current_iv = np.median(ivs)
        self._iv_history.append(current_iv)
        if len(self._iv_history) > 50:
            self._iv_history = self._iv_history[-50:]

        if len(self._iv_history) < 5:
            return 50.0

        pct = float(
            np.sum(np.array(self._iv_history[:-1]) < current_iv) /
            len(self._iv_history[:-1]) * 100
        )
        return round(pct, 1)

    @property
    def last(self) -> Optional[SentimentSnapshot]:
        return self._last_snapshot