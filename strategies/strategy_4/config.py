"""
strategy_4/config.py — RL Momentum Scalper (PPO)
"""

from datetime import time
from zoneinfo import ZoneInfo
import os

IST = ZoneInfo("Asia/Kolkata")

SYMBOL    = "NIFTY"
LOT_SIZE  = 75
TICK_SIZE = 0.05

MARKET_OPEN  = time(9,  15)
MARKET_CLOSE = time(15, 30)
SQUARE_OFF   = time(15, 20)
ENTRY_START  = time(9,  30)
ENTRY_CUTOFF = time(14, 45)

# ─── State space ──────────────────────────────────────────────────────────────
LOOKBACK_BARS     = 20    # bars of price history in state vector
STATE_DIM         = (
    LOOKBACK_BARS     # normalized returns
    + LOOKBACK_BARS   # volume ratios
    + 7               # atr, adx, rsi, vwap_dist, time_norm, in_pos, bars_held
)  # = 47

# ─── Action space ─────────────────────────────────────────────────────────────
# 0=HOLD, 1=LONG, 2=SHORT, 3=EXIT
N_ACTIONS = 4

# ─── Reward ───────────────────────────────────────────────────────────────────
REWARD_SCALE      = 0.001   # divide P&L by this for normalized reward
TIME_PENALTY      = 0.001   # per-bar penalty for holding (forces decisiveness)
SL_PENALTY        = 2.0     # extra penalty on stop loss hit
TARGET_BONUS      = 0.5     # extra bonus on target hit

# ─── Risk / exits ─────────────────────────────────────────────────────────────
ATR_SL_MULT       = 1.2
ATR_TARGET_MULT   = 1.5
MAX_HOLD_BARS     = 8      # force exit after 8 minutes (was 120 — 2 hours)

# ─── PPO hyperparameters ──────────────────────────────────────────────────────
GAMMA             = 0.99    # discount factor
LAMBDA            = 0.95    # GAE lambda
CLIP_EPS          = 0.2     # PPO clip epsilon
LR                = 3e-4    # learning rate
EPOCHS_PER_UPDATE = 10      # PPO update epochs per rollout
BATCH_SIZE        = 64
ROLLOUT_STEPS     = 512     # steps per rollout before update
HIDDEN_DIM        = 128     # network hidden layer size

# ─── Training loop ────────────────────────────────────────────────────────────
TRAIN_EVERY_BARS  = 1000    # retrain every N bars during backtest
MIN_TRAIN_BARS    = 1500    # minimum bars before first training
N_TRAIN_EPOCHS    = 50      # full passes through data per training cycle

# ─── Risk ─────────────────────────────────────────────────────────────────────
INITIAL_CAPITAL     = 500_000
MAX_LOTS_PER_TRADE  = 1
MAX_DAILY_LOSS_PCT  = 0.02
COMMISSION_PER_LOT  = 40

# ─── Paths ────────────────────────────────────────────────────────────────────
DATA_CSV     = "data/nifty_futures_5min.csv"
PROXY_CSV    = "data/nifty_proxy_5min_historical.csv"
OUTPUTS_DIR  = "strategies/strategy_4/outputs"
MODEL_DIR    = "strategies/strategy_4/models"
MODEL_PATH   = f"{MODEL_DIR}/ppo_policy.pkl"

os.makedirs(OUTPUTS_DIR, exist_ok=True)
os.makedirs(MODEL_DIR,   exist_ok=True)

TRADES_CSV       = f"{OUTPUTS_DIR}/trades.csv"
EQUITY_CURVE_PNG = f"{OUTPUTS_DIR}/equity_curve.png"
PERFORMANCE_TXT  = f"{OUTPUTS_DIR}/performance_report.txt"