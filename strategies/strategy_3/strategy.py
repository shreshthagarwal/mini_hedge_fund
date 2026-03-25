"""
strategy_3/strategy.py — Candlestick Pattern + CNN S/R + Checklist

Checklist (from user spec):
  1. Recognisable candlestick pattern       [patterns.py — 15 patterns]
  2. S/R confirms trade (SL near S/R)       [cnn_sr.py  — CNN + pivot fallback]
  3. Volume confirms (above average)        [VOLUME_MIN_RATIO filter]
  4. Dow Theory (primary trend)             [EMA50 trend direction]
  5. Indicators confirm (RSI)              [scale size if confirms, else proceed]
  6. RRR satisfactory                       [MIN_RR = 1.5 hard filter]
"""

from __future__ import annotations

import logging
import numpy as np
import pandas as pd
from typing import Optional

from strategies.strategy_3.config import (
    DOW_EMA_PERIOD,
    VOLUME_MIN_RATIO,
    MIN_RR,
    ATR_SL_MULT,
    SR_LOOKBACK_BARS,
    SR_TOLERANCE_ATR,
    CNN_LOOKBACK_TRAIN,
    ENTRY_START, ENTRY_CUTOFF, SQUARE_OFF,
)
from strategies.strategy_3.patterns import detect, Pattern
from strategies.strategy_3.cnn_sr import SRDetector
from src.engine import Signal

logger = logging.getLogger("strategy_3")

ATR_PERIOD        = 14
VOLUME_AVG_PERIOD = 20
RSI_PERIOD        = 14


# ── Indicators ────────────────────────────────────────────────────────────────

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # ATR
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"]  - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.ewm(span=ATR_PERIOD, adjust=False).mean()

    # Volume average
    df["vol_avg"] = df["volume"].rolling(VOLUME_AVG_PERIOD).mean()

    # EMA50 — Dow Theory primary trend (item 4)
    df["ema50"] = df["close"].ewm(span=DOW_EMA_PERIOD, adjust=False).mean()

    # RSI — indicator confirmation (item 5)
    delta = df["close"].diff()
    gain  = delta.clip(lower=0).ewm(span=RSI_PERIOD, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(span=RSI_PERIOD, adjust=False).mean()
    rs    = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    return df


# ── Strategy ──────────────────────────────────────────────────────────────────

class CandleSRStrategy:
    """
    Candlestick + CNN S/R strategy implementing the 6-item checklist.
    Implements engine.StrategyProtocol.
    """

    name = "S3_CANDLE_SR"

    def __init__(self, use_cnn: bool = True):
        self.use_cnn     = use_cnn
        self.sr_detector = SRDetector()
        self.in_position = False
        self.position_side: Optional[str] = None

        self._bar_count   = 0
        self._bar_buffer: list[pd.Series] = []

    def reset(self):
        self.sr_detector  = SRDetector()
        self.in_position  = False
        self.position_side = None
        self._bar_count   = 0
        self._bar_buffer  = []

    def on_bar(self, bar: pd.Series, prev_bar: pd.Series, context: dict) -> Signal:
        ts = bar.name
        self._bar_count += 1
        self._bar_buffer.append(bar)

        # Keep buffer bounded
        if len(self._bar_buffer) > max(SR_LOOKBACK_BARS + 10, CNN_LOOKBACK_TRAIN):
            self._bar_buffer = self._bar_buffer[-CNN_LOOKBACK_TRAIN:]

        # ── CNN training loop (item 2 prep) ──────────────────────────────────
        if (self.use_cnn
                and self._bar_count > SR_LOOKBACK_BARS + 50
                and self.sr_detector.needs_training(self._bar_count)):
            history = pd.DataFrame(self._bar_buffer)
            self.sr_detector.train(history, self._bar_count)

        if context.get("in_position"):
            return Signal("NONE")

        if not (ENTRY_START <= ts.time() <= ENTRY_CUTOFF):
            return Signal("NONE", reason="outside window")

        atr     = bar.get("atr",     np.nan)
        vol     = bar.get("volume",  0)
        vol_avg = bar.get("vol_avg", np.nan)
        ema50   = bar.get("ema50",   np.nan)
        rsi     = bar.get("rsi",     np.nan)
        close   = bar["close"]

        if any(pd.isna([atr, vol_avg, ema50])) or atr == 0:
            return Signal("NONE", reason="warming up")

        if len(self._bar_buffer) < 10:
            return Signal("NONE", reason="insufficient history")

        # ── Item 1: Candlestick pattern ───────────────────────────────────────
        recent = pd.DataFrame(self._bar_buffer[-3:])
        pattern = detect(recent, atr)

        if pattern is None:
            return Signal("NONE", reason="no pattern")

        # Resolve Doji direction from trend
        direction = pattern.direction
        if direction == "NONE":
            direction = "LONG" if close > ema50 else "SHORT"

        # ── Item 4: Dow Theory — primary trend filter ─────────────────────────
        # For LONG: price must be above EMA50 (uptrend)
        # For SHORT: price must be below EMA50 (downtrend)
        # OPTIONAL: can be disabled — if disabled, trade both directions
        in_uptrend   = close > ema50
        in_downtrend = close < ema50

        if direction == "LONG" and not in_uptrend:
            return Signal("NONE",
                          reason=f"DOW: {pattern.name} LONG but downtrend")
        if direction == "SHORT" and not in_downtrend:
            return Signal("NONE",
                          reason=f"DOW: {pattern.name} SHORT but uptrend")

        # ── Item 3: Volume confirmation ───────────────────────────────────────
        vol_ratio = vol / vol_avg if vol_avg > 0 else 0
        if vol_ratio < VOLUME_MIN_RATIO:
            return Signal("NONE",
                          reason=f"volume {vol_ratio:.2f} < {VOLUME_MIN_RATIO}")

        # ── Item 2: S/R confirmation (CNN) ────────────────────────────────────
        sr_window = pd.DataFrame(self._bar_buffer[-SR_LOOKBACK_BARS:])
        sr_levels = self.sr_detector.predict_sr_levels(
            sr_window, close, top_k=5
        )

        # Find the nearest S/R level to the pattern's extreme
        if direction == "LONG":
            pattern_extreme = bar["low"]    # SL should be near support
        else:
            pattern_extreme = bar["high"]   # SL should be near resistance

        nearest_sr    = None
        nearest_conf  = 0.0
        sr_tolerance  = SR_TOLERANCE_ATR * atr

        for price_level, conf in sr_levels:
            if abs(price_level - pattern_extreme) <= sr_tolerance:
                if conf > nearest_conf:
                    nearest_sr   = price_level
                    nearest_conf = conf

        # S/R confirmation strength — used to scale position (item 5 style)
        sr_confirmed = nearest_sr is not None

        # ── Item 6: RRR calculation ───────────────────────────────────────────
        sl_dist = ATR_SL_MULT * atr

        # If S/R confirmed, SL is at the S/R level (tighter/more precise)
        if sr_confirmed:
            sr_sl_dist = abs(close - nearest_sr) + (0.1 * atr)  # small buffer
            sl_dist    = min(sl_dist, sr_sl_dist)   # use tighter of the two

        # Target: next S/R level in the trade direction
        tgt_dist = sl_dist * MIN_RR   # minimum RR enforced

        # Try to find a better target at an S/R level beyond entry
        for price_level, conf in sorted(sr_levels, key=lambda x: x[0]):
            if direction == "LONG" and price_level > close + tgt_dist:
                tgt_dist = price_level - close
                break
            if direction == "SHORT" and price_level < close - tgt_dist:
                tgt_dist = close - price_level
                break

        rr = tgt_dist / sl_dist if sl_dist > 0 else 0

        if rr < MIN_RR:
            return Signal("NONE", reason=f"RRR {rr:.2f} < {MIN_RR}")

        # ── Item 5: RSI confirmation (optional — scales confidence) ───────────
        rsi_confirms = False
        if not pd.isna(rsi):
            if direction == "LONG"  and rsi < 60:   rsi_confirms = True
            if direction == "SHORT" and rsi > 40:   rsi_confirms = True
        # RSI doesn't block the trade — it's informational per the checklist

        return Signal(
            direction = direction,
            sl_dist   = sl_dist,
            rr        = rr,
            reason    = (
                f"{pattern.name} {direction}  "
                f"SR={'YES' if sr_confirmed else 'NO(pivot)'}  "
                f"vol={vol_ratio:.1f}x  "
                f"RSI={'OK' if rsi_confirms else 'weak'}  "
                f"RR={rr:.2f}"
            ),
            meta = {
                "pattern":       pattern.name,
                "pattern_str":   pattern.strength,
                "sr_confirmed":  sr_confirmed,
                "sr_level":      nearest_sr,
                "sr_conf":       nearest_conf,
                "vol_ratio":     vol_ratio,
                "rsi":           rsi,
                "rr":            rr,
            },
        )

    def on_fill(self, direction: str, price: float, ts: pd.Timestamp):
        self.in_position   = True
        self.position_side = direction

    def on_exit(self, reason: str, price: float, ts: pd.Timestamp):
        self.in_position   = False
        self.position_side = None