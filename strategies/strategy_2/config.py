"""
strategy_2/config.py — VWAP Mean Reversion + Random Forest

All parameters independent of other strategies.
Tune here without touching anything else.
"""

from datetime import time
from zoneinfo import ZoneInfo
import datetime, os

IST = ZoneInfo("Asia/Kolkata")

# ─── Instrument ───────────────────────────────────────────────────────────────
SYMBOL      = "NIFTY"
LOT_SIZE    = 75
TICK_SIZE   = 0.05

# ─── Session ──────────────────────────────────────────────────────────────────
MARKET_OPEN  = time(9,  15)
MARKET_CLOSE = time(15, 30)
SQUARE_OFF   = time(15, 20)
ENTRY_START  = time(9,  45)   # allow 2 bars warmup before first entry
ENTRY_CUTOFF = time(14, 45)   # no new entries after this

# ─── Indicators ───────────────────────────────────────────────────────────────
ATR_PERIOD        = 14
RSI_PERIOD        = 14
VOLUME_AVG_PERIOD = 20
VWAP_SLOPE_BARS   = 50   # bars to measure VWAP slope over (50 × 1-min = was 10 × 5-min)

# ─── VWAP Reversion Parameters ────────────────────────────────────────────────
# Minimum distance from VWAP to consider a stretch (in ATR units)
# 1.0 = price must be at least 1 ATR away from VWAP
VWAP_STRETCH_ATR   = 1.0

# RSI thresholds for oversold/overbought
RSI_OVERSOLD       = 35
RSI_OVERBOUGHT     = 65

# Volume must be above this multiple of the rolling average
VOLUME_CONFIRM_MULT = 1.0   # 1.0 = at least average volume

# SL: multiple of ATR beyond the signal bar extreme
ATR_SL_MULT        = 0.75

# Target: VWAP itself (dynamic — changes each bar)
# RR is not fixed — depends on distance to VWAP at entry time

# Minimum RR required to take a trade
# If VWAP is too close, skip the trade
MIN_RR             = 1.2

# ─── Random Forest Filter ─────────────────────────────────────────────────────
# Minimum RF predicted probability to take the trade
RF_MIN_PROB        = 0.55

# Retrain RF every N trading days on expanding window
RF_RETRAIN_DAYS    = 30

# Minimum training samples required before RF is used
RF_MIN_SAMPLES     = 100

# ─── Risk ─────────────────────────────────────────────────────────────────────
INITIAL_CAPITAL    = 500_000
MAX_LOTS_PER_TRADE = 1
MAX_DAILY_LOSS_PCT = 0.02
COMMISSION_PER_LOT = 40
MAX_TRADES_PER_DAY = 4   # VWAP reversion can fire many times — cap it

# ─── Paths ────────────────────────────────────────────────────────────────────
DATA_CSV    = "data/nifty_futures_5min.csv"
PROXY_CSV   = "data/nifty_proxy_5min_historical.csv"
OUTPUTS_DIR = "strategies/strategy_2/outputs"
MODEL_DIR   = "strategies/strategy_2/models"

os.makedirs(OUTPUTS_DIR, exist_ok=True)
os.makedirs(MODEL_DIR,   exist_ok=True)

TRADES_CSV       = f"{OUTPUTS_DIR}/trades.csv"
EQUITY_CURVE_PNG = f"{OUTPUTS_DIR}/equity_curve.png"
PERFORMANCE_TXT  = f"{OUTPUTS_DIR}/performance_report.txt"
RF_MODEL_PATH    = f"{MODEL_DIR}/rf_model.pkl"