"""
strategy_3/patterns.py — Rule-based candlestick pattern detection

Detects single and multi-bar patterns on NIFTY 5-min data.
All patterns return a dict with:
  name      : pattern name
  direction : "LONG" or "SHORT"
  strength  : 0.0–1.0 (higher = cleaner pattern)
  or None if no pattern detected.

Patterns implemented (checklist item 1):
  Single bar:  Hammer, Shooting Star, Bullish Marubozu, Bearish Marubozu, Doji
  Two bar:     Bullish Engulfing, Bearish Engulfing
  Three bar:   Morning Star, Evening Star, Three White Soldiers, Three Black Crows
"""

from __future__ import annotations
import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import Optional

from strategies.strategy_3.config import (
    DOJI_BODY_RATIO, HAMMER_BODY_RATIO,
    ENGULF_FACTOR, MIN_CANDLE_ATR_RATIO,
)


@dataclass
class Pattern:
    name:      str
    direction: str    # "LONG" | "SHORT"
    strength:  float  # 0.0 – 1.0


def _body(bar: pd.Series) -> float:
    return abs(bar["close"] - bar["open"])

def _range(bar: pd.Series) -> float:
    return bar["high"] - bar["low"]

def _upper_shadow(bar: pd.Series) -> float:
    return bar["high"] - max(bar["open"], bar["close"])

def _lower_shadow(bar: pd.Series) -> float:
    return min(bar["open"], bar["close"]) - bar["low"]

def _is_bull(bar: pd.Series) -> bool:
    return bar["close"] > bar["open"]

def _is_bear(bar: pd.Series) -> bool:
    return bar["close"] < bar["open"]


def detect(bars: pd.DataFrame, atr: float) -> Optional[Pattern]:
    """
    Detect candlestick pattern on the last 1-3 bars.
    bars: DataFrame with last N rows, most recent last.
    atr:  current ATR value for size filtering.
    Returns Pattern or None.
    """
    if len(bars) < 1:
        return None

    b0 = bars.iloc[-1]   # current bar
    b1 = bars.iloc[-2] if len(bars) >= 2 else None
    b2 = bars.iloc[-3] if len(bars) >= 3 else None

    r0 = _range(b0)
    if r0 == 0:
        return None

    # Filter tiny candles
    if atr > 0 and r0 < MIN_CANDLE_ATR_RATIO * atr:
        return None

    body0  = _body(b0)
    body_r = body0 / r0   # body as fraction of range
    us0    = _upper_shadow(b0)
    ls0    = _lower_shadow(b0)

    # ── Single bar patterns ───────────────────────────────────────────────────

    # Doji — open ≈ close, signals indecision / reversal
    if body_r < DOJI_BODY_RATIO:
        # Direction determined by prior trend (caller handles)
        return Pattern("Doji", "NONE", 0.6)

    # Hammer — small body in upper 33%, long lower shadow (bullish reversal)
    if (ls0 > 2 * body0
            and us0 < body0
            and min(b0["open"], b0["close"]) > b0["low"] + 0.6 * r0):
        strength = min(ls0 / r0 * 1.5, 1.0)
        return Pattern("Hammer", "LONG", strength)

    # Shooting Star — small body in lower 33%, long upper shadow (bearish reversal)
    if (us0 > 2 * body0
            and ls0 < body0
            and max(b0["open"], b0["close"]) < b0["high"] - 0.6 * r0):
        strength = min(us0 / r0 * 1.5, 1.0)
        return Pattern("ShootingStar", "SHORT", strength)

    # Bullish Marubozu — full body, almost no shadows
    if (_is_bull(b0) and body_r > 0.85
            and us0 < 0.05 * r0 and ls0 < 0.05 * r0):
        return Pattern("BullishMarubozu", "LONG", 0.9)

    # Bearish Marubozu
    if (_is_bear(b0) and body_r > 0.85
            and us0 < 0.05 * r0 and ls0 < 0.05 * r0):
        return Pattern("BearishMarubozu", "SHORT", 0.9)

    # Inverted Hammer (at bottom) — long upper shadow, small body at low
    if (us0 > 2 * body0 and ls0 < body0
            and min(b0["open"], b0["close"]) < b0["low"] + 0.4 * r0):
        return Pattern("InvertedHammer", "LONG", 0.65)

    # ── Two bar patterns ──────────────────────────────────────────────────────

    if b1 is not None:
        body1 = _body(b1)
        r1    = _range(b1)

        # Bullish Engulfing
        if (_is_bear(b1) and _is_bull(b0)
                and b0["open"] <= b1["close"]
                and b0["close"] >= b1["open"] + ENGULF_FACTOR * body1):
            strength = min(body0 / max(body1, 1) * 0.6, 1.0)
            return Pattern("BullishEngulfing", "LONG", strength)

        # Bearish Engulfing
        if (_is_bull(b1) and _is_bear(b0)
                and b0["open"] >= b1["close"]
                and b0["close"] <= b1["open"] - ENGULF_FACTOR * body1):
            strength = min(body0 / max(body1, 1) * 0.6, 1.0)
            return Pattern("BearishEngulfing", "SHORT", strength)

        # Piercing Line (bullish)
        if (_is_bear(b1) and _is_bull(b0)
                and b0["open"] < b1["close"]
                and b0["close"] > (b1["open"] + b1["close"]) / 2):
            return Pattern("PiercingLine", "LONG", 0.7)

        # Dark Cloud Cover (bearish)
        if (_is_bull(b1) and _is_bear(b0)
                and b0["open"] > b1["close"]
                and b0["close"] < (b1["open"] + b1["close"]) / 2):
            return Pattern("DarkCloud", "SHORT", 0.7)

        # Tweezer Bottom (bullish reversal)
        if (_is_bear(b1) and _is_bull(b0)
                and abs(b0["low"] - b1["low"]) < 0.002 * b0["low"]):
            return Pattern("TweezerBottom", "LONG", 0.75)

        # Tweezer Top (bearish reversal)
        if (_is_bull(b1) and _is_bear(b0)
                and abs(b0["high"] - b1["high"]) < 0.002 * b0["high"]):
            return Pattern("TweezerTop", "SHORT", 0.75)

    # ── Three bar patterns ────────────────────────────────────────────────────

    if b2 is not None and b1 is not None:
        body2 = _body(b2)

        # Morning Star (bullish): bear, small, bull
        if (_is_bear(b2) and body2 > 0.5 * _range(b2)
                and _body(b1) < 0.3 * _range(b1)   # small middle bar
                and _is_bull(b0) and body0 > 0.5 * r0
                and b0["close"] > (b2["open"] + b2["close"]) / 2):
            return Pattern("MorningStar", "LONG", 0.9)

        # Evening Star (bearish): bull, small, bear
        if (_is_bull(b2) and body2 > 0.5 * _range(b2)
                and _body(b1) < 0.3 * _range(b1)
                and _is_bear(b0) and body0 > 0.5 * r0
                and b0["close"] < (b2["open"] + b2["close"]) / 2):
            return Pattern("EveningStar", "SHORT", 0.9)

        # Three White Soldiers (bullish): three consecutive bull bars
        if (_is_bull(b2) and _is_bull(b1) and _is_bull(b0)
                and b0["close"] > b1["close"] > b2["close"]
                and _body(b0) > 0.5 * r0
                and _body(b1) > 0.5 * _range(b1)):
            return Pattern("ThreeWhiteSoldiers", "LONG", 0.85)

        # Three Black Crows (bearish)
        if (_is_bear(b2) and _is_bear(b1) and _is_bear(b0)
                and b0["close"] < b1["close"] < b2["close"]
                and _body(b0) > 0.5 * r0
                and _body(b1) > 0.5 * _range(b1)):
            return Pattern("ThreeBlackCrows", "SHORT", 0.85)

    return None