"""
strategy_2/strategy.py — VWAP Mean Reversion + Random Forest

Signal logic:
  1. Price stretched > VWAP_STRETCH_ATR × ATR from VWAP
  2. RSI reversing from extreme (crosses back through threshold)
  3. Volume >= VOLUME_CONFIRM_MULT × rolling average
  4. Distance to VWAP gives RR >= MIN_RR after SL
  5. RF model probability >= RF_MIN_PROB  (skipped until enough data)

Entry:  next bar open + slippage  (engine handles this)
Target: VWAP at signal time       (dynamic — not fixed R:R)
Stop:   ATR_SL_MULT × ATR beyond signal bar extreme

RF features:
  adx, vwap_slope, stretch_atr, rsi, time_norm,
  prior_5bar_ret, vol_ratio, dist_prev_close
"""

from __future__ import annotations

import os
import pickle
import logging
import numpy as np
import pandas as pd
from typing import Optional

from strategies.strategy_2.config import (
    ATR_PERIOD, RSI_PERIOD, VOLUME_AVG_PERIOD, VWAP_SLOPE_BARS,
    VWAP_STRETCH_ATR, RSI_OVERSOLD, RSI_OVERBOUGHT,
    VOLUME_CONFIRM_MULT, ATR_SL_MULT, MIN_RR,
    RF_MIN_PROB, RF_RETRAIN_DAYS, RF_MIN_SAMPLES,
    ENTRY_START, ENTRY_CUTOFF, SQUARE_OFF,
    MAX_TRADES_PER_DAY, RF_MODEL_PATH,
)
from src.engine import Signal

logger = logging.getLogger("strategy_2")


# ── Indicators ────────────────────────────────────────────────────────────────

def compute_atr(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"]  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=ATR_PERIOD, adjust=False).mean()


def compute_rsi(series: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    delta  = series.diff()
    gain   = delta.clip(lower=0).ewm(span=period, adjust=False).mean()
    loss   = (-delta.clip(upper=0)).ewm(span=period, adjust=False).mean()
    rs     = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def compute_vwap(df: pd.DataFrame) -> pd.Series:
    typical = (df["high"] + df["low"] + df["close"]) / 3
    pv      = typical * df["volume"]
    vwap    = pd.Series(np.nan, index=df.index)
    for day in np.unique(df.index.date):
        mask       = df.index.date == day
        cum_vol    = df["volume"][mask].cumsum().replace(0, np.nan)
        vwap[mask] = pv[mask].cumsum() / cum_vol
    return vwap


def compute_adx(df: pd.DataFrame) -> pd.Series:
    high, low, close   = df["high"], df["low"], df["close"]
    prev_high = high.shift(1)
    prev_low  = low.shift(1)
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    dm_plus  = (high - prev_high).clip(lower=0)
    dm_minus = (prev_low - low).clip(lower=0)
    both     = (dm_plus > 0) & (dm_minus > 0)
    dm_plus[both  & (dm_minus >= dm_plus)]  = 0
    dm_minus[both & (dm_plus  > dm_minus)]  = 0
    atr_s    = tr.ewm(span=14, adjust=False).mean()
    safe_atr = atr_s.replace(0, np.nan)
    di_plus  = 100 * dm_plus.ewm(span=14, adjust=False).mean() / safe_atr
    di_minus = 100 * dm_minus.ewm(span=14, adjust=False).mean() / safe_atr
    dx = 100 * (di_plus - di_minus).abs() / (di_plus + di_minus).replace(0, np.nan)
    return dx.ewm(span=14, adjust=False).mean()


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["atr"]      = compute_atr(df)
    df["rsi"]      = compute_rsi(df["close"])
    df["vwap"]     = compute_vwap(df)
    df["adx"]      = compute_adx(df)
    df["vol_avg"]  = df["volume"].rolling(VOLUME_AVG_PERIOD).mean()
    df["rsi_prev"] = df["rsi"].shift(1)

    # VWAP slope: change in VWAP over last N bars (normalised by ATR)
    df["vwap_slope"] = (df["vwap"] - df["vwap"].shift(VWAP_SLOPE_BARS)) / df["atr"]

    # Distance from VWAP in ATR units (signed: positive = above, negative = below)
    df["vwap_dist_atr"] = (df["close"] - df["vwap"]) / df["atr"]

    # Prior 5-bar return
    df["ret5"] = df["close"].pct_change(5)

    # Volume ratio vs rolling average
    df["vol_ratio"] = df["volume"] / df["vol_avg"].replace(0, np.nan)

    # Previous day close (for gap context)
    df["prev_day_close"] = df.groupby(df.index.date)["close"].transform("first").shift(1)

    return df


# ── RF Feature Builder ────────────────────────────────────────────────────────

def build_features(bar: pd.Series) -> list[float]:
    """Extract RF feature vector from a single bar."""
    ts        = bar.name
    time_norm = (ts.hour * 60 + ts.minute - 9 * 60 - 15) / (6 * 60 + 15)

    prev_close = bar.get("prev_day_close", bar["close"])
    dist_prev  = (bar["close"] - prev_close) / prev_close if prev_close else 0

    return [
        float(bar.get("adx",           0)),
        float(bar.get("vwap_slope",     0)),
        float(abs(bar.get("vwap_dist_atr", 0))),
        float(bar.get("rsi",           50)),
        float(time_norm),
        float(bar.get("ret5",           0)),
        float(bar.get("vol_ratio",      1)),
        float(dist_prev),
    ]

FEATURE_NAMES = [
    "adx", "vwap_slope", "stretch_atr", "rsi",
    "time_norm", "ret5_pct", "vol_ratio", "dist_prev_close",
]


# ── Random Forest Manager ─────────────────────────────────────────────────────

class RFManager:
    """
    Manages training and inference of the RF quality filter.

    In backtesting:  trains on expanding window, retrains every RF_RETRAIN_DAYS
    In live trading: loads saved model from RF_MODEL_PATH

    Labels: 1 = trade reached VWAP before SL, 0 = SL hit first
    These labels are computed retrospectively during backtest.
    """

    def __init__(self):
        self.model        = None
        self.trained      = False
        self._X_train:    list = []
        self._y_train:    list = []
        self._last_retrain_day: Optional[pd.Timestamp] = None

    def record(self, features: list[float], outcome: int):
        """Record a completed trade's features and outcome (1=win, 0=loss)."""
        self._X_train.append(features)
        self._y_train.append(outcome)

    def maybe_retrain(self, current_date):
        """Retrain if enough samples and enough time since last retrain."""
        if len(self._X_train) < RF_MIN_SAMPLES:
            return
        if self._last_retrain_day is not None:
            days_since = (current_date - self._last_retrain_day).days
            if days_since < RF_RETRAIN_DAYS:
                return
        self._retrain()
        self._last_retrain_day = current_date

    def _retrain(self):
        try:
            from sklearn.ensemble import RandomForestClassifier
            X = np.array(self._X_train)
            y = np.array(self._y_train)
            self.model   = RandomForestClassifier(
                n_estimators = 100,
                max_depth    = 6,
                min_samples_leaf = 10,
                class_weight = "balanced",
                random_state = 42,
                n_jobs       = -1,
            )
            self.model.fit(X, y)
            self.trained = True
            wins = y.sum()
            logger.info(
                f"RF retrained: {len(y)} samples  "
                f"{wins/len(y)*100:.1f}% win rate"
            )
        except ImportError:
            logger.warning("scikit-learn not installed — RF filter disabled")

    def predict_proba(self, features: list[float]) -> float:
        """Return probability of win. Returns 1.0 if model not trained yet."""
        if not self.trained or self.model is None:
            return 1.0   # no filter until model is ready
        X   = np.array(features).reshape(1, -1)
        prob = self.model.predict_proba(X)[0]
        # prob[1] = probability of class 1 (win)
        classes = list(self.model.classes_)
        if 1 in classes:
            return float(prob[classes.index(1)])
        return 0.5

    def save(self, path: str = RF_MODEL_PATH):
        if self.model:
            with open(path, "wb") as f:
                pickle.dump(self.model, f)

    def load(self, path: str = RF_MODEL_PATH):
        if os.path.exists(path):
            with open(path, "rb") as f:
                self.model   = pickle.load(f)
            self.trained = True
            logger.info(f"RF model loaded from {path}")


# ── VWAP Reversion Strategy ───────────────────────────────────────────────────

class VWAPReversionStrategy:
    """
    VWAP Mean Reversion + Random Forest quality filter.
    Implements engine.StrategyProtocol.

    Signal conditions (all must be true):
      1. |vwap_dist_atr| >= VWAP_STRETCH_ATR
      2. RSI reversing through threshold (was extreme, now crossing back)
      3. volume >= VOLUME_CONFIRM_MULT × vol_avg
      4. RR (dist_to_vwap / sl_dist) >= MIN_RR
      5. RF predicted probability >= RF_MIN_PROB

    Direction:
      LONG  when price is BELOW VWAP (expecting upward reversion)
      SHORT when price is ABOVE VWAP (expecting downward reversion)
    """

    name = "S2_VWAP_RF"

    def __init__(self, use_rf: bool = True):
        self.use_rf       = use_rf
        self.rf           = RFManager()
        self.in_position  = False
        self.position_side: Optional[str] = None

        # Per-trade tracking for RF labeling
        self._pending_features: Optional[list] = None
        self._pending_sl:       float          = 0.0
        self._pending_vwap:     float          = 0.0
        self._pending_dir:      str            = ""

        # Daily trade counter
        self._day_trades: int              = 0
        self._current_day: Optional[object] = None

    def reset(self):
        self.rf              = RFManager()
        self.in_position     = False
        self.position_side   = None
        self._pending_features = None
        self._day_trades     = 0
        self._current_day    = None

    def on_bar(self, bar: pd.Series, prev_bar: pd.Series, context: dict) -> Signal:
        ts  = bar.name
        day = ts.date()

        # Reset daily counter
        if day != self._current_day:
            self._current_day = day
            self._day_trades  = 0
            self.rf.maybe_retrain(day)

        if context.get("in_position"):
            return Signal("NONE")

        if self._day_trades >= MAX_TRADES_PER_DAY:
            return Signal("NONE", reason="daily trade limit")

        if not (ENTRY_START <= ts.time() <= ENTRY_CUTOFF):
            return Signal("NONE", reason="outside window")

        close    = bar["close"]
        atr      = bar.get("atr", np.nan)
        rsi      = bar.get("rsi", np.nan)
        rsi_prev = bar.get("rsi_prev", np.nan)
        vwap     = bar.get("vwap", np.nan)
        vol      = bar.get("volume", 0)
        vol_avg  = bar.get("vol_avg", np.nan)
        dist_atr = bar.get("vwap_dist_atr", 0)

        if any(pd.isna([atr, rsi, vwap, vol_avg])) or atr == 0:
            return Signal("NONE", reason="warming up")

        if vwap == 0:
            return Signal("NONE", reason="no VWAP")

        # ── Condition 1: price stretched from VWAP ────────────────────────────
        if abs(dist_atr) < VWAP_STRETCH_ATR:
            return Signal("NONE", reason=f"stretch {abs(dist_atr):.2f} < {VWAP_STRETCH_ATR}")

        # ── Condition 2: RSI reversing from extreme ────────────────────────────
        long_setup  = (dist_atr < 0  # price below VWAP
                       and rsi_prev <= RSI_OVERSOLD
                       and rsi      >  RSI_OVERSOLD)

        short_setup = (dist_atr > 0  # price above VWAP
                       and rsi_prev >= RSI_OVERBOUGHT
                       and rsi      <  RSI_OVERBOUGHT)

        if not long_setup and not short_setup:
            return Signal("NONE", reason="no RSI reversal")

        # ── Condition 3: volume confirmation ──────────────────────────────────
        if vol < VOLUME_CONFIRM_MULT * vol_avg:
            return Signal("NONE", reason="low volume")

        # ── Condition 4: RR check ─────────────────────────────────────────────
        direction = "LONG" if long_setup else "SHORT"

        if direction == "LONG":
            sl_dist   = ATR_SL_MULT * atr          # stop below signal bar low
            dist_vwap = vwap - close                # distance to target (VWAP)
        else:
            sl_dist   = ATR_SL_MULT * atr
            dist_vwap = close - vwap

        if sl_dist <= 0 or dist_vwap <= 0:
            return Signal("NONE", reason="invalid distances")

        rr = dist_vwap / sl_dist
        if rr < MIN_RR:
            return Signal("NONE", reason=f"RR {rr:.2f} < {MIN_RR}")

        # ── Condition 5: RF filter ────────────────────────────────────────────
        features = build_features(bar)
        if self.use_rf:
            prob = self.rf.predict_proba(features)
            if prob < RF_MIN_PROB:
                return Signal("NONE", reason=f"RF prob {prob:.2f} < {RF_MIN_PROB}")
        else:
            prob = 1.0

        # Store for labeling after trade closes
        self._pending_features = features
        self._pending_sl       = sl_dist
        self._pending_vwap     = vwap
        self._pending_dir      = direction

        return Signal(
            direction = direction,
            sl_dist   = sl_dist,
            rr        = rr,
            reason    = (
                f"VWAP {direction}  dist={dist_atr:.2f}ATR  "
                f"RSI={rsi:.0f}  RR={rr:.2f}  RF={prob:.2f}"
            ),
            meta = {
                "vwap":      vwap,
                "dist_atr":  dist_atr,
                "rsi":       rsi,
                "rr":        rr,
                "rf_prob":   prob,
                "features":  features,
            },
        )

    def on_fill(self, direction: str, price: float, ts: pd.Timestamp):
        self.in_position   = True
        self.position_side = direction
        self._day_trades  += 1
        logger.debug(f"S2 filled {direction} @ {price:.2f}  [{ts}]")

    def on_exit(self, reason: str, price: float, ts: pd.Timestamp):
        self.in_position   = False
        self.position_side = None

        # Label the trade for RF training
        if self._pending_features is not None and self.use_rf:
            outcome = 1 if reason == "target" else 0
            self.rf.record(self._pending_features, outcome)
            self._pending_features = None

        logger.debug(f"S2 exit [{reason}] @ {price:.2f}")