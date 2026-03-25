"""
intelligence/technical/collector.py

Collects cross-market technical data that individual strategies cannot see:
  - NIFTY Bank (proxy for financial sector weight)
  - India VIX (implied volatility / fear gauge)
  - NIFTY vs Bank Nifty divergence (sector rotation signal)
  - Advance/Decline ratio from NSE (breadth)
  - Put/Call Ratio from NSE options data
  - Open Interest change (futures positioning)

Data sources:
  - yfinance for VIX, BankNifty (free, no auth needed)
  - Upstox market quote API for live OI on futures
  - NSE website for A/D ratio (scraped)

All data is normalised to z-scores over a 20-day rolling window
before being passed to the feature layer.
"""

from __future__ import annotations

import logging
import numpy as np
import pandas as pd
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger("intel.technical")
IST    = ZoneInfo("Asia/Kolkata")

# Rolling window for z-score normalisation
ZSCORE_WINDOW = 20


class TechnicalCollector:
    """
    Fetches and normalises cross-market technical signals.
    Called once at startup (for history) and then updated each bar.
    """

    def __init__(self):
        self._history: dict[str, list] = {
            "vix":          [],
            "banknifty":    [],
            "pcr":          [],   # put-call ratio
            "oi_change":    [],   # futures OI change %
            "nifty_close":  [],
        }
        self._last_date: Optional[date] = None

    # ── Fetch daily snapshot (run once at market open) ────────────────────────

    def fetch_daily(self) -> dict:
        """
        Fetch today's cross-market technical data.
        Returns raw values dict.
        """
        data = {}

        # India VIX and Bank Nifty via yfinance
        try:
            import yfinance as yf
            vix = yf.Ticker("^INDIAVIX")
            hist_vix = vix.history(period="1d")
            if not hist_vix.empty:
                data["vix"] = float(hist_vix["Close"].iloc[-1])

            bnk = yf.Ticker("^NSEBANK")
            hist_bnk = bnk.history(period="2d")
            if len(hist_bnk) >= 2:
                data["banknifty_ret"] = float(
                    hist_bnk["Close"].iloc[-1] / hist_bnk["Close"].iloc[-2] - 1
                )
                data["banknifty_close"] = float(hist_bnk["Close"].iloc[-1])

            nifty = yf.Ticker("^NSEI")
            hist_n = nifty.history(period="2d")
            if len(hist_n) >= 2:
                data["nifty_ret"] = float(
                    hist_n["Close"].iloc[-1] / hist_n["Close"].iloc[-2] - 1
                )
                data["nifty_close"] = float(hist_n["Close"].iloc[-1])

        except Exception as e:
            logger.debug(f"yfinance fetch failed: {e}")

        # Bank-Nifty vs Nifty divergence (sector rotation)
        if "banknifty_ret" in data and "nifty_ret" in data:
            data["bnk_nifty_div"] = data["banknifty_ret"] - data["nifty_ret"]

        logger.debug(f"Technical daily: {data}")
        return data

    def fetch_pcr(self) -> Optional[float]:
        """
        Fetch Put/Call Ratio from NSE.
        PCR > 1.2 = bearish (more puts bought)
        PCR < 0.7 = bullish (more calls bought)
        PCR 0.8-1.0 = neutral
        """
        try:
            import requests
            url = "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY"
            headers = {
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
                "Referer": "https://www.nseindia.com/",
            }
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.ok:
                data = resp.json()
                total_ce_oi = sum(
                    r.get("CE", {}).get("openInterest", 0)
                    for r in data.get("records", {}).get("data", [])
                    if r.get("CE")
                )
                total_pe_oi = sum(
                    r.get("PE", {}).get("openInterest", 0)
                    for r in data.get("records", {}).get("data", [])
                    if r.get("PE")
                )
                if total_ce_oi > 0:
                    pcr = total_pe_oi / total_ce_oi
                    logger.info(f"PCR: {pcr:.3f}")
                    return round(pcr, 3)
        except Exception as e:
            logger.debug(f"PCR fetch failed: {e}")
        return None

    def fetch_oi(self, token: str, instrument_key: str) -> Optional[float]:
        """
        Fetch current Open Interest from Upstox market quote API.
        Returns OI change % vs previous close.
        """
        try:
            import requests
            from urllib.parse import quote
            url = f"https://api.upstox.com/v2/market-quote/quotes?instrument_key={quote(instrument_key)}"
            hdrs = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            resp = requests.get(url, headers=hdrs, timeout=10)
            if resp.ok:
                d = resp.json().get("data", {})
                key = list(d.keys())[0] if d else None
                if key:
                    oi     = d[key].get("oi", 0)
                    oi_day = d[key].get("oi_day_high", oi)
                    if oi_day > 0:
                        return round((oi / oi_day - 1) * 100, 2)
        except Exception as e:
            logger.debug(f"OI fetch failed: {e}")
        return None

    # ── Normalise to z-scores ─────────────────────────────────────────────────

    def update_history(self, data: dict):
        """Add today's values to rolling history for z-score computation."""
        for key in ["vix", "banknifty", "pcr", "oi_change", "nifty_close"]:
            val = data.get(key)
            if val is not None:
                self._history[key].append(val)
                if len(self._history[key]) > ZSCORE_WINDOW * 2:
                    self._history[key] = self._history[key][-ZSCORE_WINDOW * 2:]

    def normalise(self, data: dict) -> dict:
        """
        Convert raw values to z-scores.
        Z-score = (value - rolling_mean) / rolling_std
        Clipped to [-3, 3].
        """
        normalised = {}
        for key, val in data.items():
            hist = self._history.get(key.split("_")[0], [])
            if len(hist) >= 5:
                mean = np.mean(hist[-ZSCORE_WINDOW:])
                std  = np.std(hist[-ZSCORE_WINDOW:])
                if std > 0:
                    normalised[key] = float(np.clip((val - mean) / std, -3, 3))
                else:
                    normalised[key] = 0.0
            else:
                normalised[key] = 0.0   # not enough history yet
        return normalised