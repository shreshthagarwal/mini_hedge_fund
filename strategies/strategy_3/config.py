"""
strategy_3/config.py — Candlestick Pattern + CNN Support/Resistance

All parameters independent of other strategies.
"""

from datetime import time
from zoneinfo import ZoneInfo
import os

IST = ZoneInfo("Asia/Kolkata")

# ─── Instrument ───────────────────────────────────────────────────────────────
SYMBOL    = "NIFTY"
LOT_SIZE  = 75
TICK_SIZE = 0.05

# ─── Session ──────────────────────────────────────────────────────────────────
MARKET_OPEN  = time(9,  15)
MARKET_CLOSE = time(15, 30)
SQUARE_OFF   = time(15, 20)
ENTRY_START  = time(9,  45)   # allow 30 min warmup
ENTRY_CUTOFF = time(14, 30)

# ─── Candlestick Pattern Parameters ───────────────────────────────────────────
# Doji: body < this fraction of total range
DOJI_BODY_RATIO       = 0.1

# Hammer/shooting star: body in top/bottom 33% of range
HAMMER_BODY_RATIO     = 0.33

# Engulfing: current body must exceed previous by this factor
ENGULF_FACTOR         = 1.0

# Minimum candle size (ATR fraction) — filter out tiny candles
MIN_CANDLE_ATR_RATIO  = 0.3

# ─── CNN S/R Parameters ───────────────────────────────────────────────────────
# Bars to look back for S/R chart image
SR_LOOKBACK_BARS      = 300  # ~1 trading session worth of 1-min bars (was 60 × 5-min)

# Image dimensions for CNN input
IMG_WIDTH             = 64
IMG_HEIGHT            = 48

# S/R level tolerance (fraction of ATR) — how close price must be to S/R level
SR_TOLERANCE_ATR      = 0.5

# CNN training
CNN_TRAIN_EVERY_BARS  = 2500   # retrain every N bars (2500 × 1-min ≈ was 500 × 5-min)
CNN_MIN_SAMPLES       = 1000   # minimum training samples before CNN used (was 200 × 5-min)
CNN_LOOKBACK_TRAIN    = 25000  # bars of history for training (was 5000 × 5-min)

# ─── Checklist Thresholds ─────────────────────────────────────────────────────
# Volume: must be above average × this factor
VOLUME_MIN_RATIO      = 1.1

# RRR: minimum reward-to-risk ratio
MIN_RR                = 1.5

# SL: ATR multiple beyond pattern extreme
ATR_SL_MULT           = 1.0

# Maximum bars to hold a position (10 min for medium-frequency)
MAX_HOLD_BARS         = 10

# Dow trend filter: use EMA to determine primary trend
DOW_EMA_PERIOD        = 50   # ~50 bars = ~4 hours intraday trend

# ─── Risk ─────────────────────────────────────────────────────────────────────
INITIAL_CAPITAL       = 500_000
MAX_LOTS_PER_TRADE    = 1
MAX_DAILY_LOSS_PCT    = 0.02
COMMISSION_PER_LOT    = 40

# ─── Paths ────────────────────────────────────────────────────────────────────
DATA_CSV     = "data/nifty_futures_5min.csv"
PROXY_CSV    = "data/nifty_proxy_5min_historical.csv"
OUTPUTS_DIR  = "strategies/strategy_3/outputs"
MODEL_DIR    = "strategies/strategy_3/models"
CNN_MODEL_PATH = f"{MODEL_DIR}/sr_cnn.pkl"

os.makedirs(OUTPUTS_DIR, exist_ok=True)
os.makedirs(MODEL_DIR,   exist_ok=True)

TRADES_CSV       = f"{OUTPUTS_DIR}/trades.csv"
EQUITY_CURVE_PNG = f"{OUTPUTS_DIR}/equity_curve.png"
PERFORMANCE_TXT  = f"{OUTPUTS_DIR}/performance_report.txt"