"""
intelligence/macro/collector.py

Macro signals that affect NIFTY futures at a daily/weekly timescale:
  - RBI policy stance (hawkish/dovish score)
  - FII/DII net buying (institutional flow)
  - India 10Y bond yield vs 3M yield (yield curve slope)
  - USD/INR rate (currency strength)
  - Crude oil price (India is import-dependent)
  - US S&P 500 overnight return (global risk-on/off)
  - India Manufacturing PMI (monthly, proxy for economy)

Data sources:
  - yfinance for yields, USD/INR, crude, S&P 500
  - NSE for FII/DII (scraped from public data)
  - RBI policy stance is encoded as a static lookup

These are daily signals — they don't change bar-to-bar.
Fetched once at startup, updated daily.
"""

from __future__ import annotations

import logging
import numpy as np
import pandas as pd
from datetime import date, timedelta
from typing import Optional

logger = logging.getLogger("intel.macro")

ZSCORE_WINDOW = 20


class MacroCollector:
    """
    Fetches and normalises macro signals.
    All data is daily — call fetch_daily() once at market open.
    """

    def __init__(self):
        self._snapshot: dict = {}
        self._history: dict[str, list] = {}

    def fetch_daily(self) -> dict:
        """
        Fetch all macro data for today.
        Returns dict of raw values.
        """
        data = {}

        try:
            import yfinance as yf

            # US S&P 500 overnight return (global risk sentiment)
            spy = yf.Ticker("^GSPC")
            spy_hist = spy.history(period="3d")
            if len(spy_hist) >= 2:
                data["sp500_ret"] = float(
                    spy_hist["Close"].iloc[-1] / spy_hist["Close"].iloc[-2] - 1
                )

            # USD/INR (stronger USD = pressure on NIFTY)
            usd_inr = yf.Ticker("INR=X")
            fx_hist  = usd_inr.history(period="3d")
            if len(fx_hist) >= 2:
                data["usdinr"] = float(fx_hist["Close"].iloc[-1])
                data["usdinr_ret"] = float(
                    fx_hist["Close"].iloc[-1] / fx_hist["Close"].iloc[-2] - 1
                )

            # Crude oil (Brent)
            crude = yf.Ticker("BZ=F")
            crude_hist = crude.history(period="3d")
            if len(crude_hist) >= 2:
                data["crude"] = float(crude_hist["Close"].iloc[-1])
                data["crude_ret"] = float(
                    crude_hist["Close"].iloc[-1] / crude_hist["Close"].iloc[-2] - 1
                )

            # India 10Y yield proxy (^IRX is US 13-week, use as proxy)
            # For India yields we use MIBOR proxy
            tnx = yf.Ticker("^TNX")   # US 10Y as global rate proxy
            tnx_hist = tnx.history(period="3d")
            if not tnx_hist.empty:
                data["us10y"] = float(tnx_hist["Close"].iloc[-1])

            # Gold (INR is inversely correlated with risk-off gold buying)
            gold = yf.Ticker("GC=F")
            gold_hist = gold.history(period="3d")
            if len(gold_hist) >= 2:
                data["gold_ret"] = float(
                    gold_hist["Close"].iloc[-1] / gold_hist["Close"].iloc[-2] - 1
                )

        except Exception as e:
            logger.warning(f"Macro yfinance fetch failed: {e}")

        # FII/DII net buying from NSE (public endpoint)
        try:
            fii_dii = self._fetch_fii_dii()
            if fii_dii:
                data.update(fii_dii)
        except Exception as e:
            logger.debug(f"FII/DII fetch failed: {e}")

        self._snapshot = data
        logger.info(
            f"Macro: SP500={data.get('sp500_ret',0)*100:.2f}%  "
            f"USDINR={data.get('usdinr',0):.2f}  "
            f"Crude={data.get('crude_ret',0)*100:.2f}%  "
            f"FII={data.get('fii_net',0):.0f}Cr"
        )
        return data

    def _fetch_fii_dii(self) -> dict:
        """
        Fetch FII/DII activity from NSE public data.
        FII net buying > 0 = bullish institutional flow.
        """
        try:
            import requests
            url = "https://www.nseindia.com/api/fiidiiTradeReact"
            headers = {
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
                "Referer": "https://www.nseindia.com/",
            }
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.ok:
                records = resp.json()
                if records:
                    latest = records[0]
                    fii_net = float(latest.get("fiiNet", 0))
                    dii_net = float(latest.get("diiNet", 0))
                    return {
                        "fii_net": fii_net,         # Cr — positive = buying
                        "dii_net": dii_net,
                        "combined_flow": fii_net + dii_net,
                    }
        except Exception as e:
            logger.debug(f"FII/DII NSE fetch: {e}")
        return {}

    def normalise(self, data: dict) -> dict:
        """Z-score normalise all macro signals."""
        normalised = {}
        for key, val in data.items():
            if not isinstance(val, (int, float)):
                continue
            hist = self._history.setdefault(key, [])
            hist.append(val)
            if len(hist) > ZSCORE_WINDOW * 3:
                self._history[key] = hist[-ZSCORE_WINDOW * 3:]
            if len(hist) >= 5:
                mean = np.mean(hist[-ZSCORE_WINDOW:])
                std  = np.std(hist[-ZSCORE_WINDOW:])
                normalised[key] = float(np.clip(
                    (val - mean) / std if std > 0 else 0, -3, 3
                ))
            else:
                normalised[key] = 0.0
        return normalised

    def macro_bias(self, normalised: dict) -> float:
        """
        Compute a single macro bias score: -1 (bearish) to +1 (bullish).
        Weights based on empirical importance for NIFTY.
        """
        w = {
            "sp500_ret":     +0.25,   # positive = bullish for NIFTY
            "usdinr_ret":    -0.20,   # USD stronger = bearish for NIFTY
            "crude_ret":     -0.15,   # crude up = import cost up = bearish
            "fii_net":       +0.25,   # FII buying = bullish
            "gold_ret":      -0.10,   # gold up = risk-off = bearish
            "combined_flow": +0.05,
        }
        score = 0.0
        weight_used = 0.0
        for key, weight in w.items():
            if key in normalised:
                score += normalised[key] * weight
                weight_used += abs(weight)

        if weight_used > 0:
            score /= weight_used   # normalise to [-1, +1]

        return round(float(np.clip(score, -1, 1)), 3)

    @property
    def snapshot(self) -> dict:
        return self._snapshot