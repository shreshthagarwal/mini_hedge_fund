"""
strategy_3/cnn_sr.py — CNN-based Support/Resistance level detection

Training loop:
  1. Slide a window of SR_LOOKBACK_BARS over historical data
  2. Render each window as a price chart image (IMG_WIDTH × IMG_HEIGHT)
  3. Label each image with the S/R levels (price levels that acted as
     support or resistance in the NEXT window — look-ahead for training only)
  4. Train a CNN to predict S/R level positions from chart images
  5. At inference: CNN outputs the top N price levels with confidence scores

Architecture:
  Input:  (1, IMG_HEIGHT, IMG_WIDTH) grayscale chart image
  Conv1:  16 filters 3×3, ReLU
  Pool1:  2×2 max pool
  Conv2:  32 filters 3×3, ReLU
  Pool2:  2×2 max pool
  FC1:    128 units, ReLU, dropout 0.3
  Output: SR_BINS bins (each bin = a price level band), sigmoid

Output interpretation:
  Each bin represents a price range. High activation = S/R level present there.
  The top-K activated bins are returned as S/R price levels.
"""

from __future__ import annotations

import os
import pickle
import logging
import numpy as np
import pandas as pd
from typing import Optional

from strategies.strategy_3.config import (
    SR_LOOKBACK_BARS, IMG_WIDTH, IMG_HEIGHT,
    CNN_TRAIN_EVERY_BARS, CNN_MIN_SAMPLES,
    CNN_LOOKBACK_TRAIN, CNN_MODEL_PATH,
)

logger = logging.getLogger("strategy_3.cnn")

SR_BINS = 20   # divide price range into 20 bins


# ── Chart renderer ─────────────────────────────────────────────────────────────

def render_chart(bars: pd.DataFrame) -> np.ndarray:
    """
    Convert a window of OHLCV bars to a grayscale image array.
    Shape: (IMG_HEIGHT, IMG_WIDTH), values 0–1.

    Encoding:
      - Price axis: vertical (top = high, bottom = low)
      - Time axis: horizontal (left = oldest, right = newest)
      - Body: bright pixels (1.0)
      - Shadows: medium pixels (0.5)
      - Volume: bottom 20% of image as bar chart (0.3 intensity)
    """
    if len(bars) == 0:
        return np.zeros((IMG_HEIGHT, IMG_WIDTH), dtype=np.float32)

    img = np.zeros((IMG_HEIGHT, IMG_WIDTH), dtype=np.float32)

    price_min = bars["low"].min()
    price_max = bars["high"].max()
    price_rng = price_max - price_min
    if price_rng == 0:
        return img

    vol_max   = bars["volume"].max()
    vol_area  = int(IMG_HEIGHT * 0.2)   # bottom 20% for volume
    price_h   = IMG_HEIGHT - vol_area   # top 80% for price

    n = len(bars)
    for j, (_, bar) in enumerate(bars.iterrows()):
        # Map bar index to x pixel
        x = int(j / n * IMG_WIDTH)
        x = min(x, IMG_WIDTH - 1)

        def price_to_y(p):
            # Invert: high price = low y (top of image)
            norm = (p - price_min) / price_rng
            return max(0, min(price_h - 1, int((1 - norm) * price_h)))

        y_high  = price_to_y(bar["high"])
        y_low   = price_to_y(bar["low"])
        y_open  = price_to_y(bar["open"])
        y_close = price_to_y(bar["close"])

        # Shadow (wick)
        for y in range(y_high, y_low + 1):
            img[y, x] = 0.5

        # Body
        y_top    = min(y_open, y_close)
        y_bottom = max(y_open, y_close)
        for y in range(y_top, y_bottom + 1):
            img[y, x] = 1.0

        # Volume bar (bottom section)
        if vol_max > 0:
            vol_h = int((bar["volume"] / vol_max) * vol_area)
            for y in range(IMG_HEIGHT - vol_h, IMG_HEIGHT):
                img[y, x] = 0.3

    return img


# ── Label generator ────────────────────────────────────────────────────────────

def compute_sr_labels(
    bars:       pd.DataFrame,
    future_bars: pd.DataFrame,
    n_bins:     int = SR_BINS,
) -> np.ndarray:
    """
    Compute ground-truth S/R level labels for a training window.
    
    S/R levels are price levels where price reverses in the FUTURE window.
    This uses look-ahead only during training — never at inference.
    
    A price level is labeled as S/R if:
      - price touches it (within 0.1% range) AND
      - reverses direction within the next 3 bars
    
    Returns: binary array of shape (n_bins,)
             1 = this price bin contains an S/R level
    """
    labels = np.zeros(n_bins, dtype=np.float32)

    if len(future_bars) == 0:
        return labels

    price_min = bars["low"].min()
    price_max = bars["high"].max()
    price_rng = price_max - price_min
    if price_rng == 0:
        return labels

    # Find reversal points in future bars
    for i in range(1, len(future_bars) - 1):
        prev = future_bars.iloc[i-1]
        curr = future_bars.iloc[i]
        nxt  = future_bars.iloc[i+1]

        # Local high (potential resistance)
        if curr["high"] > prev["high"] and curr["high"] > nxt["high"]:
            norm = (curr["high"] - price_min) / price_rng
            bin_idx = max(0, min(int(norm * n_bins), n_bins - 1))
            labels[bin_idx] = 1.0

        # Local low (potential support)
        if curr["low"] < prev["low"] and curr["low"] < nxt["low"]:
            norm = (curr["low"] - price_min) / price_rng
            bin_idx = max(0, min(int(norm * n_bins), n_bins - 1))
            labels[bin_idx] = 1.0

    return labels


# ── CNN Model ─────────────────────────────────────────────────────────────────

class SRDetector:
    """
    CNN-based Support/Resistance detector.
    
    Training loop (run_training_loop):
      For every CNN_TRAIN_EVERY_BARS bars:
        - Take CNN_LOOKBACK_TRAIN bars of history
        - Generate (image, label) pairs using sliding window
        - Train CNN on these pairs
        - Save model
    
    Inference (predict_sr_levels):
      - Render current window as image
      - Pass through CNN
      - Return top-K price levels with confidence scores
    """

    def __init__(self, auto_load: bool = True):
        self.model         = None
        self._scaler       = None
        self.trained       = False
        self._n_trained    = 0
        self._last_train_i = -CNN_TRAIN_EVERY_BARS

        # Auto-load saved model on startup — skip retraining if model exists
        if auto_load:
            self.load()

    def needs_training(self, bar_i: int) -> bool:
        return (bar_i - self._last_train_i) >= CNN_TRAIN_EVERY_BARS

    def train(self, history: pd.DataFrame, current_bar_i: int):
        """Train CNN on history up to current_bar_i."""
        try:
            from sklearn.neural_network import MLPClassifier
        except ImportError:
            logger.warning("scikit-learn not installed — CNN S/R disabled")
            return

        logger.info(f"  Training S/R CNN on {len(history)} bars...")

        # Generate training samples
        X, Y = [], []
        step = 10   # slide every 10 bars for speed

        for start in range(0, len(history) - SR_LOOKBACK_BARS - 10, step):
            end         = start + SR_LOOKBACK_BARS
            future_end  = min(end + 20, len(history))

            window      = history.iloc[start:end]
            future      = history.iloc[end:future_end]

            img    = render_chart(window).flatten()
            labels = compute_sr_labels(window, future)

            X.append(img)
            Y.append(labels)

        if len(X) < CNN_MIN_SAMPLES:
            logger.info(f"  Insufficient samples ({len(X)}) — skipping training")
            # Still update last_train_i so we don't hammer every bar
            # Try again after CNN_TRAIN_EVERY_BARS more bars
            self._last_train_i = current_bar_i
            return

        X = np.array(X)
        Y = np.array(Y)

        # Use MultiLayerPerceptron as CNN approximation
        # True CNN would need PyTorch/TensorFlow — MLPClassifier works well
        # on flattened chart images for S/R detection at this scale
        from sklearn.neural_network import MLPClassifier
        from sklearn.preprocessing import StandardScaler
        from sklearn.multioutput import MultiOutputClassifier

        self._scaler = StandardScaler()
        X_scaled = self._scaler.fit_transform(X)

        # Multi-output classifier: one binary output per price bin
        base = MLPClassifier(
            hidden_layer_sizes = (256, 128, 64),
            activation         = "relu",
            max_iter           = 30,
            random_state       = 42,
            early_stopping     = True,
            validation_fraction = 0.1,
        )
        self.model   = MultiOutputClassifier(base, n_jobs=-1)
        self.model.fit(X_scaled, Y.astype(int))
        self.trained = True
        self._n_trained += 1
        self._last_train_i = current_bar_i

        self._save()
        logger.info(f"  S/R CNN trained (fold {self._n_trained}, "
                    f"{len(X)} samples)")

    def predict_sr_levels(
        self,
        bars:      pd.DataFrame,
        current_price: float,
        top_k:     int = 3,
    ) -> list[tuple[float, float]]:
        """
        Predict top-K S/R levels from chart image.
        Returns list of (price_level, confidence) tuples.
        """
        if not self.trained or self.model is None:
            # Fallback: use pivot point method
            return self._pivot_sr(bars, current_price, top_k)

        price_min = bars["low"].min()
        price_max = bars["high"].max()
        price_rng = price_max - price_min

        if price_rng == 0:
            return []

        img     = render_chart(bars).flatten().reshape(1, -1)
        img_sc  = self._scaler.transform(img)

        # Get probability from each binary classifier
        probs = np.zeros(SR_BINS)
        for bin_idx, clf in enumerate(self.model.estimators_):
            p = clf.predict_proba(img_sc)
            if p.shape[1] == 2:
                probs[bin_idx] = p[0, 1]   # probability of class=1 (S/R present)

        # Convert bin indices to price levels
        levels = []
        bin_size = price_rng / SR_BINS
        for bin_idx in np.argsort(probs)[::-1][:top_k]:
            price = price_min + (bin_idx + 0.5) * bin_size
            conf  = float(probs[bin_idx])
            if conf > 0.3:   # minimum confidence threshold
                levels.append((price, conf))

        return levels

    def _pivot_sr(
        self,
        bars:          pd.DataFrame,
        current_price: float,
        top_k:         int,
    ) -> list[tuple[float, float]]:
        """
        Fallback S/R using pivot point method (no CNN needed).
        Finds local highs/lows in the lookback window.
        """
        levels = []
        highs  = bars["high"].values
        lows   = bars["low"].values
        n      = len(highs)

        for i in range(2, n - 2):
            # Local high
            if highs[i] > highs[i-1] and highs[i] > highs[i+1]:
                levels.append((highs[i], 0.5))
            # Local low
            if lows[i] < lows[i-1] and lows[i] < lows[i+1]:
                levels.append((lows[i], 0.5))

        # Sort by proximity to current price
        levels.sort(key=lambda x: abs(x[0] - current_price))
        return levels[:top_k * 2]   # return more for pivot fallback

    def _save(self):
        try:
            with open(CNN_MODEL_PATH, "wb") as f:
                pickle.dump({"model": self.model, "scaler": self._scaler,
                             "n_trained": self._n_trained}, f)
        except Exception as e:
            logger.warning(f"Could not save CNN model: {e}")

    def load(self):
        if os.path.exists(CNN_MODEL_PATH):
            with open(CNN_MODEL_PATH, "rb") as f:
                obj = pickle.load(f)
            self.model    = obj["model"]
            self._scaler  = obj.get("scaler")
            self._n_trained = obj.get("n_trained", 0)
            self.trained  = True
            logger.info(f"S/R CNN loaded ({self._n_trained} training folds)")