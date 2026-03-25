"""
Central configuration for the NIFTY Futures algo trading system.
"""

from datetime import time
from zoneinfo import ZoneInfo

# ─── Timezone ─────────────────────────────────────────────────────────────────
IST = ZoneInfo("Asia/Kolkata")

# ─── Instrument ───────────────────────────────────────────────────────────────
SYMBOL      = "NIFTY"
EXCHANGE    = "NSE"
LOT_SIZE    = 25
TICK_SIZE   = 0.05

# ─── Data source ──────────────────────────────────────────────────────────────
# yfinance is used for both historical and live data
# pip install yfinance
# No credentials required.

# ─── Session ──────────────────────────────────────────────────────────────────
MARKET_OPEN   = time(9, 15)
MARKET_CLOSE  = time(15, 30)
ENTRY_START   = time(10, 35)  # first entry after 80-min OR window
ENTRY_CUTOFF  = time(15, 00)
SQUARE_OFF    = time(15, 20)

# ─── Strategy Parameters ──────────────────────────────────────────────────────
EMA_PERIOD         = 20
ATR_PERIOD         = 14
VOLUME_AVG_PERIOD  = 20
VOLUME_SPIKE_MULT  = 1.5

# ── ORB Strategy — tune these to improve results ──────────────────────────────

# OR_BARS: 16 bars = 80-min range (09:15–10:34)
# 10:15 slot has 9 trades at only 22% WR — push range end past that noise window
OR_BARS            = 14   # 16 × 5-min = 80-min opening range (09:15–10:34)

# ADX_THRESHOLD: keep at 30 — ranging market confirmed by SL domination
ADX_THRESHOLD      = 30
ADX_PERIOD         = 14

# ORB_ENTRY_CUTOFF: 12:xx and 13:xx slots are best — allow until 13:30
import datetime
ORB_ENTRY_CUTOFF   = datetime.time(13, 30)

# ENTRY_START now effectively 10:35 (after 80-min OR window)

# ATR_SL_MULT: wins avg 117 min — trades need room to breathe
# Keep at 1.5 — stop is appropriate for the move size
ATR_SL_MULT        = 1.5

# RR_RATIO: 2.0 is easier to hit than 2.5 — with 28.6% WR need ~34% at 2:1
# Lower slightly to get more targets hit
RR_RATIO           = 1.80

# ── Direction filter ──────────────────────────────────────────────────────────
# Data shows LONG -₹9,616 vs SHORT +₹1,627 over this period
# NIFTY has been in a downtrend — only trade shorts until trend reverses
# Options: "BOTH", "LONG_ONLY", "SHORT_ONLY"
TRADE_DIRECTION    = "BOTH"

# ─── Risk ─────────────────────────────────────────────────────────────────────
INITIAL_CAPITAL    = 500_000
MAX_LOTS_PER_TRADE = 1
MAX_DAILY_LOSS_PCT = 0.02
COMMISSION_PER_LOT = 40     # ₹ per lot round-trip approx

# ─── Paths ────────────────────────────────────────────────────────────────────
DATA_DIR         = "data"
OUTPUTS_DIR      = "outputs"
HISTORICAL_CSV   = "data/nifty_futures_5min.csv"
TRADES_CSV       = "outputs/trades.csv"
EQUITY_CURVE_PNG = "outputs/equity_curve.png"
PERFORMANCE_TXT  = "outputs/performance_report.txt"