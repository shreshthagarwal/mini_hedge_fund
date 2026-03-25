"""
strategy_1/config.py — ORB + ADX strategy configuration

All tunable parameters live here. No dependency on any shared config.
Change parameters here to tune this strategy independently.
"""

from datetime import time
from zoneinfo import ZoneInfo

# ─── Timezone ─────────────────────────────────────────────────────────────────
IST = ZoneInfo("Asia/Kolkata")

# ─── Instrument ───────────────────────────────────────────────────────────────
SYMBOL      = "NIFTY"
LOT_SIZE    = 75       # Updated Nov 2024
TICK_SIZE   = 0.05

# ─── Session ──────────────────────────────────────────────────────────────────
MARKET_OPEN   = time(9,  15)
MARKET_CLOSE  = time(15, 30)
SQUARE_OFF    = time(15, 20)

# ─── Indicators ───────────────────────────────────────────────────────────────
ATR_PERIOD         = 14
ADX_PERIOD         = 14
EMA_PERIOD         = 20
VOLUME_AVG_PERIOD  = 20
VOLUME_SPIKE_MULT  = 1.5

# ─── ORB Parameters ───────────────────────────────────────────────────────────
# OR_BARS: number of 1-min bars that define the opening range
# 30 bars = 09:15–09:44 (30-min range) — same real time as 6×5-min bars
# Try: 30 (30-min), 45 (45-min), 60 (60-min), 80 (80-min)
OR_BARS            = 30

# ADX_THRESHOLD: minimum trend strength required to take a trade
# Higher = fewer but cleaner trades. Try 25, 30, 35
ADX_THRESHOLD      = 30

# ATR_SL_MULT: stop loss = entry ± (ATR_SL_MULT × ATR)
# Higher = wider stop, fewer stop-outs but larger losses
ATR_SL_MULT        = 1.8

# RR_RATIO: target = RR_RATIO × SL distance
# 2.0 means target is twice the stop distance
RR_RATIO           = 1.9

# ORB_ENTRY_CUTOFF: no new entries after this time
import datetime
ORB_ENTRY_CUTOFF    = datetime.time(13, 30)

# Dead zone: skip entries in this window (consistently poor results)
ORB_DEAD_ZONE_START = datetime.time(10, 35)
ORB_DEAD_ZONE_END   = datetime.time(10, 54)

# Specific time slots to skip based on backtest analysis
ORB_SKIP_SLOTS = [(11,40),(11,45),(11,50),(12,50),(12,55),(13,25)]

# Direction filter: "BOTH", "LONG_ONLY", "SHORT_ONLY"
TRADE_DIRECTION    = "BOTH"

# ─── Risk ─────────────────────────────────────────────────────────────────────
INITIAL_CAPITAL    = 500_000
MAX_LOTS_PER_TRADE = 1
MAX_DAILY_LOSS_PCT = 0.02
COMMISSION_PER_LOT = 40       # ₹ per lot round-trip

# ─── Paths ────────────────────────────────────────────────────────────────────
DATA_CSV     = "data/nifty_futures_5min.csv"      # primary: real futures
PROXY_CSV    = "data/nifty_proxy_5min_historical.csv"  # fallback: spot proxy
OUTPUTS_DIR  = "strategies/strategy_1/outputs"

import os
os.makedirs(OUTPUTS_DIR, exist_ok=True)

TRADES_CSV       = f"{OUTPUTS_DIR}/trades.csv"
EQUITY_CURVE_PNG = f"{OUTPUTS_DIR}/equity_curve.png"
PERFORMANCE_TXT  = f"{OUTPUTS_DIR}/performance_report.txt"