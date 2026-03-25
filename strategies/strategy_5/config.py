"""
strategy_5/config.py — Futures vs Spot Basis Arbitrage

All parameters independent of other strategies.
"""

from datetime import time
from zoneinfo import ZoneInfo
import os

IST = ZoneInfo("Asia/Kolkata")

# ─── Instrument ───────────────────────────────────────────────────────────────
SYMBOL   = "NIFTY"
LOT_SIZE = 75
TICK_SIZE = 0.05

# ─── Session ──────────────────────────────────────────────────────────────────
MARKET_OPEN  = time(9,  15)
MARKET_CLOSE = time(15, 30)
SQUARE_OFF   = time(15, 20)
ENTRY_START  = time(9,  30)   # skip first bar — let basis stabilise
ENTRY_CUTOFF = time(14, 30)   # no new entries in last hour

# ─── Basis calculation ────────────────────────────────────────────────────────
# Risk-free rate (approximate RBI repo rate)
RISK_FREE_RATE     = 0.065   # 6.5% annualised

# Rolling window for z-score normalisation (in bars)
# 20 days × 375 bars/day = 7500 bars (1-min; was 1500 bars at 5-min)
BASIS_ZSCORE_WINDOW = 7500

# Minimum bars of history before trading starts
MIN_WARMUP_BARS     = 1000   # was 200 × 5-min; now 1000 × 1-min = same ~3.3 hours

# ─── Signal thresholds ────────────────────────────────────────────────────────
# Entry: z-score beyond this → basis is stretched
ZSCORE_ENTRY        = 2.0

# Exit target: z-score reverts to this level
ZSCORE_TARGET       = 0.3

# Stop: z-score widens beyond this → thesis wrong, cut loss
ZSCORE_STOP         = 3.0

# Minimum basis spread in points (filter out noise)
MIN_BASIS_SPREAD    = 5.0

# ─── Risk ─────────────────────────────────────────────────────────────────────
INITIAL_CAPITAL     = 500_000
MAX_LOTS_PER_TRADE  = 1
MAX_DAILY_LOSS_PCT  = 0.02
COMMISSION_PER_LOT  = 40

# ─── Data paths ───────────────────────────────────────────────────────────────
FUTURES_CSV = "data/nifty_futures_5min.csv"
SPOT_CSV    = "data/nifty_proxy_5min_historical.csv"
OUTPUTS_DIR = "strategies/strategy_5/outputs"

os.makedirs(OUTPUTS_DIR, exist_ok=True)

TRADES_CSV       = f"{OUTPUTS_DIR}/trades.csv"
EQUITY_CURVE_PNG = f"{OUTPUTS_DIR}/equity_curve.png"
PERFORMANCE_TXT  = f"{OUTPUTS_DIR}/performance_report.txt"