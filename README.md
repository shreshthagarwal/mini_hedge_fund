# NIFTY Futures Algorithmic Trading System

A production-grade, multi-strategy intraday algorithmic trading system for NIFTY 50 Futures on NSE. Built in Python, running on Upstox API, with five independent trading strategies, a shared industrial-grade backtesting engine, a macro/sentiment/regime intelligence layer, and a live paper trading dashboard.

---

## Table of Contents

1. [Project Purpose](#1-project-purpose)
2. [Architecture Overview](#2-architecture-overview)
3. [Folder Structure](#3-folder-structure)
4. [Data Layer](#4-data-layer)
5. [Strategies](#5-strategies)
6. [Intelligence Layer](#6-intelligence-layer)
7. [Backtesting Engine](#7-backtesting-engine)
8. [Paper Trading and Dashboard](#8-paper-trading-and-dashboard)
9. [Daily Workflow](#9-daily-workflow)
10. [Key Design Decisions](#10-key-design-decisions)
11. [Backtest Results](#11-backtest-results)
12. [Environment and Dependencies](#12-environment-and-dependencies)
13. [Configuration Reference](#13-configuration-reference)
14. [Known Limitations and Roadmap](#14-known-limitations-and-roadmap)

---

## 1. Project Purpose

This system trades NIFTY 50 Futures (NSE_FO) intraday using five complementary strategies designed to be profitable in different market regimes:

- **All positions are intraday** — every trade opens and closes within the same session. No overnight risk.
- **All five strategies run simultaneously** in paper trading, each with its own capital allocation (Rs 5L each, Rs 25L total).
- **The intelligence layer** provides macro, sentiment, and regime context that individual strategies cannot compute from price data alone.
- **The backtesting engine** is look-ahead-free: signal fires on bar close, entry fills at next bar open + slippage.

The goal is a diversified intraday portfolio where strategies are uncorrelated. ORB profits on trending days, VWAP reversion profits on ranging days, basis arb profits regardless of direction.

---

## 2. Architecture Overview

```
INTELLIGENCE LAYER
  MacroCollector       TechnicalCollector     SentimentCollector
  (S&P500, FII,        (India VIX,            (PCR, Max Pain,
   USD/INR, Crude)      BankNifty, OI)         IV%, OI Walls)
        |                     |                      |
        +---------------------+----------------------+
                              |
                        FeatureLayer
                    (z-score normalise)
                              |
               +--------------+--------------+
           RegimeModule               ScoreModule
          (filter: should           (direction:
           we trade at all?)         long/short edge?)
               +--------------+--------------+
                          IntelligenceOutput
                    (passed to all 5 strategies)

STRATEGY LAYER
  S1: ORB+ADX   S2: VWAP+RF   S3: Candle+CNN   S4: RL   S5: Basis
  (Breakout)    (Mean Rev)    (Pattern+S/R)    (PPO)    (Arb)
        |             |              |            |         |
        +-------------+--------------+------------+---------+
                                    |
                       BacktestEngine / MultiStrategyEngine

DATA LAYER
  nifty_futures_5min.csv              nifty_proxy_5min_historical.csv
  (Upstox real futures,               (yfinance ^NSEI spot,
   Sep 2024 to present)                2015 to present)
  build_history_upstox.py             build_history.py
```

### Execution model (intraday, no look-ahead)

```
bar[i]   close  ->  strategy.on_bar()  ->  pending signal stored
bar[i+1] open   ->  fill at open + 1 tick slippage
bar[i+1] intra  ->  SL/target checked via high/low (pessimistic: SL wins if both touched)
15:20           ->  forced square-off at bar open
```

Pending signals are cancelled if the next bar is a new session (prevents cross-session fills).

---

## 3. Folder Structure

```
algo_trading_project/
|
+-- main.py                          # Entry point: backtest | paper | fetch | report
+-- requirements.txt
+-- .upstox_token                    # Daily OAuth token (run upstox_auth.py each morning)
|
+-- data/                            # CSV data files (not committed to git)
|   +-- nifty_futures_5min.csv       # Real NIFTY futures from Upstox (Sep 2024 to present)
|   +-- nifty_proxy_5min_historical.csv  # yfinance ^NSEI proxy (2015 to present)
|
+-- scripts/                         # Utility scripts
|   +-- upstox_auth.py               # OAuth2 flow -> saves .upstox_token (run daily)
|   +-- build_history_upstox.py      # Fetch/update real futures CSV from Upstox API
|   +-- build_history.py             # Fetch/update spot proxy CSV from yfinance
|   +-- analyze_csv.py               # Deep CSV analysis: gaps, coverage, duplicates
|   +-- analyze_trades.py            # Analyse trades.csv output from backtests
|   +-- probe_upstox_coverage.py     # Debug: which months Upstox has data for
|   +-- test_expired_candle.py       # Debug: test expired candle API for specific dates
|
+-- src/                             # Shared engine and infrastructure
|   +-- engine.py                    # Industrial-grade backtesting engine (the core)
|   +-- multi_engine.py              # Live paper trading engine: runs all 5 strategies
|   +-- multi_dashboard.py           # Flask dashboard: Kinetic Observatory UI
|   +-- data_fetcher.py              # HistoricalFeed: loads CSV with fallback
|   +-- analytics.py                 # Metrics, reports, equity curve plots
|   +-- live_feed.py                 # Upstox MarketDataStreamerV3 WebSocket
|   +-- utils.py                     # IST helpers, nse_expiry(), session_bars()
|   +-- risk_manager.py              # Daily loss limits, lot approval
|   +-- paper_broker.py              # Single-strategy paper broker (legacy)
|   +-- paper_engine.py              # Single-strategy paper engine (legacy)
|   +-- backtester.py                # Original backtester (superseded by engine.py)
|   +-- config.py                    # Global config (superseded by per-strategy configs)
|
+-- strategies/                      # Five independent strategies
|   +-- __init__.py
|   |
|   +-- strategy_1/                  # S1: ORB + ADX (Opening Range Breakout)
|   |   +-- config.py                # All S1 parameters (independent of other strategies)
|   |   +-- strategy.py              # ORBStrategy implementing StrategyProtocol
|   |   +-- backtester.py            # S1-specific backtest runner (wraps engine.py)
|   |
|   +-- strategy_2/                  # S2: VWAP Mean Reversion + Random Forest
|   |   +-- config.py
|   |   +-- strategy.py              # VWAPReversionStrategy + RFManager
|   |   +-- models/                  # Saved RF model (rf_model.pkl)
|   |
|   +-- strategy_3/                  # S3: Candlestick Pattern + CNN Support/Resistance
|   |   +-- config.py
|   |   +-- strategy.py              # CandleSRStrategy (6-item checklist evaluator)
|   |   +-- patterns.py              # 15 candlestick patterns (pure geometry)
|   |   +-- cnn_sr.py                # CNN S/R detector: chart -> image -> MLP -> S/R levels
|   |   +-- models/                  # Saved CNN model (sr_cnn.pkl)
|   |
|   +-- strategy_4/                  # S4: RL Momentum Scalper (PPO)
|   |   +-- config.py
|   |   +-- strategy.py              # RLScalperStrategy + training loop
|   |   +-- environment.py           # Gym-style trading environment
|   |   +-- ppo.py                   # PPO-lite (sklearn MLP, no PyTorch needed)
|   |   +-- models/                  # Saved PPO policy (ppo_policy.pkl)
|   |
|   +-- strategy_5/                  # S5: Futures vs Spot Basis Arbitrage
|       +-- config.py
|       +-- strategy.py              # BasisArbStrategy (z-score of basis spread)
|       +-- data_loader.py           # Loads + aligns futures + spot CSVs, computes basis
|
+-- intelligence/                    # Intelligence layer (macro + sentiment + regime)
|   +-- __init__.py
|   +-- engine.py                    # IntelligenceEngine: orchestrates all sub-modules
|   +-- features.py                  # FeatureLayer: normalises all signals to z-scores
|   +-- regime.py                    # RegimeModule: classifies market environment
|   +-- score.py                     # ScoreModule: directional alpha score (-1 to +1)
|   |
|   +-- technical/
|   |   +-- collector.py             # India VIX, BankNifty divergence, OI change, PCR
|   |
|   +-- macro/
|   |   +-- collector.py             # S&P500, USD/INR, Crude, FII/DII, US 10Y yield
|   |
|   +-- sentiment/
|       +-- collector.py             # NSE options: PCR, Max Pain, IV%, OI walls
|
+-- outputs/                         # Generated outputs (not committed)
    +-- trades.csv
    +-- equity_curve.png
    +-- performance_report.txt
```

Each strategy also auto-creates its own `outputs/` directory:
```
strategies/strategy_1/outputs/trades.csv
strategies/strategy_2/outputs/trades.csv
...
```

---

## 4. Data Layer

### 4.1 Real Futures Data (Primary)

**File:** `data/nifty_futures_5min.csv`  
**Source:** Upstox API  
**Coverage:** Sep 2024 to present (updates daily)  
**Columns:** `datetime, open, high, low, close, volume`  
**Timezone:** IST (Asia/Kolkata), tz-aware DatetimeIndex  

**How it is built:** `scripts/build_history_upstox.py` uses three Upstox endpoint groups:

Step 1 — discover available expiry dates:
```
GET /v2/expired-instruments/expiries?instrument_key=NSE_INDEX|Nifty 50
```
Returns exact dates Upstox has data for. Never guess expiry dates.

Step 2 — get the expired instrument key per expiry:
```
GET /v2/expired-instruments/future/contract?instrument_key=...&expiry_date=YYYY-MM-DD
```
Returns the `expired_instrument_key` (e.g. `NSE_FO|35382|31-10-2024`). The `|` must be URL-encoded as `%7C`.

Step 3 — fetch candle data for expired contracts:
```
GET /v2/expired-instruments/historical-candle/{encoded_key}/5minute/{to}/{from}
```

Step 4 — fetch today's intraday bars for the live front-month contract:
```
GET /v3/historical-candle/intraday/{encoded_key}/minutes/1
```
No date parameters. Always returns current trading day. Used for both 1-min (ORB/VWAP warmup) and 5-min (strategy bar buffer).

Step 5 — fetch recent bars for the live contract (within 30-day window):
```
GET /v3/historical-candle/{encoded_key}/minutes/5/{to}/{from}
```

**NSE expiry rule change (Sep 2025):** NIFTY monthly expiry moved from last Thursday to last Tuesday. `utils.nse_expiry(year, month)` handles this automatically — returns `last_thursday()` pre-Sep 2025, `last_tuesday()` from Sep 2025 onwards.

**Upstox known gap:** Dec 2024 has a confirmed database hole in the expired instruments API. Key exists, returns 0 candles. Not fixable.

### 4.2 Spot Proxy Data (Fallback + S5)

**File:** `data/nifty_proxy_5min_historical.csv`  
**Source:** yfinance `^NSEI`  
**Coverage:** 2015 to present  
**Used by:** S5 Basis Arbitrage (needs spot price), and as fallback for backtesting longer periods  

### 4.3 Data quality check

```powershell
python scripts/analyze_csv.py
```

Reports: total bars, date range, duplicate timestamps (contract overlap indicator), per-month coverage table, missing trading days list, intra-day gaps, price sanity (zeros, negatives, spikes).

Expected output on healthy data:
```
Trading day coverage: 98.7% (378/383 days)
Duplicate timestamps: 0
Intra-day gaps:       None detected
Price anomalies:      0
Quality: GOOD — suitable for backtesting
```

---

## 5. Strategies

All strategies implement the `StrategyProtocol` defined in `src/engine.py`:

```python
class StrategyProtocol(Protocol):
    def on_bar(self, bar: pd.Series, prev_bar: pd.Series, context: dict) -> Signal: ...
    def on_fill(self, direction: str, price: float, ts: pd.Timestamp): ...
    def on_exit(self, reason: str, price: float, ts: pd.Timestamp): ...
    def reset(self): ...
```

`Signal` dataclass fields: `direction` (LONG/SHORT/NONE), `sl_dist` (SL distance in points from entry), `rr` (reward-to-risk ratio), `reason` (human-readable string), `meta` (dict for debugging).

`context` dict keys passed by the engine:
```python
{
    "in_position":  bool,    # True if strategy has an open position
    "position_dir": str,     # "LONG", "SHORT", or None
    "session_bar":  int,     # bar number within session (0 = first bar of day)
    "day_pnl":      float,   # today's realised P&L so far
    "capital":      float,   # current capital
    "new_day":      bool,    # True on the first bar of a new trading day
    "intel":        IntelligenceOutput or None,  # intelligence layer output
}
```

Each strategy has its own `config.py` — no global config dependency.

---

### S1: ORB + ADX (Opening Range Breakout)

**Directory:** `strategies/strategy_1/`  
**Type:** Rule-based, no ML  
**Best regime:** BULL_TREND / BEAR_TREND  

**Signal logic:**
1. Build opening range from first OR_BARS bars (default 6, covering 09:15–09:44)
2. After OR closes, watch for close breaking above OR_HIGH (long) or below OR_LOW (short)
3. Require previous bar did not break (first-touch rule)
4. ADX filter: `adx > ADX_THRESHOLD` (default 35) — confirms trend strength
5. Dead zone: skip entries between 10:35–10:54 (post-OR noise from backtest analysis)
6. Skip slots: specific time slots that historically underperform (configurable list)
7. Cutoff: no new entries after `ORB_ENTRY_CUTOFF` (default 13:30)

**Entry/exit:**
- Entry: next bar open + 1 tick slippage (no look-ahead)
- SL: `entry - ATR_SL_MULT * ATR` for longs (default 1.85x ATR)
- Target: `entry + RR_RATIO * SL_distance` (default 1.93:1)
- Square-off: 15:20

**Tunable parameters:**
```python
OR_BARS            = 6      # 30-min opening range
ADX_THRESHOLD      = 35
ATR_SL_MULT        = 1.85
RR_RATIO           = 1.93
ORB_ENTRY_CUTOFF   = time(13, 30)
TRADE_DIRECTION    = "BOTH"   # or "LONG_ONLY" or "SHORT_ONLY"
```

**Backtest (Sep 2024–Mar 2026, futures data):**  
Net P&L: +Rs 1,50,322 | CAGR: 19.6% | Sharpe: 1.91 | Win rate: 49% | Max DD: 10.74%

---

### S2: VWAP Mean Reversion + Random Forest

**Directory:** `strategies/strategy_2/`  
**Type:** Rule-based signal + ML quality filter  
**Best regime:** RANGE_NEUTRAL / RANGE_BULLISH / RANGE_BEARISH  

**Signal logic (all conditions must be true simultaneously):**
1. `|vwap_dist_atr| >= VWAP_STRETCH_ATR` (price stretched >= 1.5x ATR from VWAP)
2. RSI reversing from extreme: was oversold (<30) and now crossing above, or overbought (>70) and crossing below
3. Volume >= 1.2x rolling 20-bar average
4. ADX < 28 (regime filter: reversion fails in strong trends)
5. RR check: distance to VWAP / SL distance >= MIN_RR (1.5)
6. RF filter (if enabled): predicted probability >= RF_MIN_PROB (0.52)

**Target:** VWAP itself (variable distance, not fixed R:R). This is the key difference from S1.

**RF filter:** `RFManager` in `strategy.py` records each completed trade's 8 features and outcome (win/loss). Retrains every 20 trading days on expanding window using `sklearn.ensemble.RandomForestClassifier`. Requires 60 minimum samples before activation. Features: ADX, VWAP slope, stretch magnitude, RSI, time of day, 5-bar return, volume ratio, gap from previous close.

**Backtest (no RF, Sep 2024–Mar 2026):**  
Net P&L: +Rs 36,296 | CAGR: 4.9% | Sharpe: 0.86 | Win rate: 32.2% | Profit Factor: 1.13

---

### S3: Candlestick Pattern + CNN Support/Resistance

**Directory:** `strategies/strategy_3/`  
**Type:** Rule-based patterns + ML S/R detection  
**Best regime:** All except EVENT_RISK  

**Six-item checklist (from user specification):**

Item 1 — Recognisable pattern (`patterns.py`): 15 patterns implemented as pure OHLCV geometry:
- Single bar: Hammer, Shooting Star, Doji, Bullish Marubozu, Bearish Marubozu, Inverted Hammer
- Two bar: Bullish Engulfing, Bearish Engulfing, Piercing Line, Dark Cloud Cover, Tweezer Bottom, Tweezer Top
- Three bar: Morning Star, Evening Star, Three White Soldiers, Three Black Crows

Item 2 — S/R confirms trade (`cnn_sr.py`): CNN predicts S/R price levels. SL of pattern must be within `SR_TOLERANCE_ATR * ATR` of a predicted S/R level.

Item 3 — Volume confirmation: volume >= 1.1x rolling 20-bar average.

Item 4 — Dow Theory (primary trend): LONG signals only taken when close > EMA50, SHORT signals only when close < EMA50.

Item 5 — Indicators (optional, does not block): RSI direction alignment noted for informational purposes. Per original checklist spec, trade proceeds even if indicators do not confirm.

Item 6 — RRR: minimum 1.5:1 enforced as hard filter.

**CNN S/R detector (`cnn_sr.py`):**
- Auto-loads saved model from `models/sr_cnn.pkl` on startup. Only retrains if no saved model exists.
- Training loop: every 500 bars, generates (chart_image, S/R_label) pairs via sliding window. Labels are local highs/lows in the next 20 bars after each window (look-ahead for training only, never inference).
- Model: `MultiOutputClassifier(MLPClassifier)`, 20 binary outputs (one per price bin)
- Chart rendering: OHLCV bars drawn as grayscale image (64x48 pixels). Body = 1.0, shadow = 0.5, volume = 0.3 intensity in bottom 20%.
- Fallback: if model not trained, uses pivot-point S/R (local highs/lows in lookback window)

**Backtest (pivot fallback, Sep 2024–Mar 2026):**  
Net P&L: +Rs 1,26,838 | CAGR: 15.8% | Sharpe: 1.14 | Win rate: 34.3%

---

### S4: RL Momentum Scalper (PPO)

**Directory:** `strategies/strategy_4/`  
**Type:** Reinforcement Learning (PPO-lite, no PyTorch needed)  
**Best regime:** BULL_TREND / BEAR_TREND (high ADX)  

**Environment (`environment.py`):**
- State vector (47 dimensions): 20 normalised returns + 20 volume ratios + ATR/ADX/RSI/VWAP_distance/time_norm/in_position/bars_held
- Action space: 0=HOLD, 1=LONG, 2=SHORT, 3=EXIT
- Reward: `net_pnl / (capital * 0.001)` on exit, `-0.001` per bar held (time penalty forces decisiveness), `-2.0` extra on stop loss, `+0.5` bonus on target
- Forced exits: square-off at 15:20, max hold 24 bars, SL/target

**PPO implementation (`ppo.py`):**
- Policy network: `sklearn.neural_network.MLPClassifier` with `warm_start=True` (incremental)
- Value network: `sklearn.neural_network.MLPRegressor`
- Training: PPO-lite — computes GAE advantages, uses importance-weighted sample retraining via sklearn's `sample_weight` parameter. Approximates PPO clipped objective without full gradient implementation.
- Retrains every 1000 bars on the last 1500 bars of history
- Saves model to `models/ppo_policy.pkl` after each update. Auto-loads on startup.

**Training requirement:** S4 needs >= 1500 bars in the bar buffer before the first training cycle. During paper trading this means approximately 20 trading hours of combined historical + live bars before S4 starts generating signals (shows WAITING status until trained).

---

### S5: Futures vs Spot Basis Arbitrage

**Directory:** `strategies/strategy_5/`  
**Type:** Statistical arbitrage (cross-instrument)  
**Regime sensitivity:** Regime-agnostic (multiplier = 1.0 in all regimes)  

**Concept:** NIFTY futures trade at a premium to spot (cost of carry). When actual basis deviates significantly from the theoretical fair basis, it reverts. The deviation is measured as a rolling z-score.

**Signal computation (`data_loader.py`):**
```
basis        = futures_close - spot_close
fair_basis   = spot * RISK_FREE_RATE * (days_to_expiry / 365)
basis_spread = actual_basis - fair_basis
z_score      = (basis_spread - rolling_mean(1500 bars)) / rolling_std(1500 bars)

SHORT futures: z_score > +2.0  (futures overpriced vs spot)
LONG  futures: z_score < -2.0  (futures underpriced vs spot)
Exit target:   z_score reverts to +/-0.3
Stop loss:     z_score widens to +/-3.0
```

**SL/target in price terms:** `sl_dist = basis_std * (ZSCORE_STOP - |z|)`. Automatically wider when basis is more volatile.

**Data requirement:** Requires both `nifty_futures_5min.csv` and `nifty_proxy_5min_historical.csv`. The `data_loader.load_aligned()` function performs an inner join on timestamp and computes all basis features including `days_to_expiry` using `utils.nse_expiry()` (correctly handles the Sep 2025 rule change).

**Backtest (Sep 2024–Mar 2026):**  
Net P&L: +Rs 4,65,946 | CAGR: 53.2% | Sharpe: 2.55 | Win rate: 35% | Profit Factor: 1.98 | Max DD: 5.22%

---

## 6. Intelligence Layer

**Directory:** `intelligence/`

### Purpose

Provides macro, sentiment, and regime context that price-based strategies cannot compute. Acts as a shared filter and size scaler. Does not replace strategy signals or recompute technical indicators (each strategy handles its own).

Does: filter bad-environment trades, scale position size in favourable conditions, suppress strategies in wrong regimes, provide directional bias from macro and sentiment data.

Does not: compute RSI, ATR, VWAP, EMA, or other bar-level technical indicators.

### Data flow

```
At startup (and refreshed periodically):
  MacroCollector     -> S&P500, USD/INR, Crude, FII/DII, US10Y  (yfinance + NSE)
  TechnicalCollector -> India VIX, BankNifty, OI change          (yfinance + Upstox)

Every 5-min candle:
  SentimentCollector -> PCR, Max Pain, IV percentile, OI walls   (NSE options chain)

All raw values -> FeatureLayer -> z-score normalised FeatureVector (18 features)

FeatureVector -> RegimeModule  -> RegimeOutput (regime + per-strategy multipliers)
FeatureVector -> ScoreModule   -> AlphaScore   (long/short confidence, size mult)

RegimeOutput + AlphaScore -> IntelligenceOutput
  -> passed to all 5 strategies via context["intel"]
```

### Regime classification and strategy multipliers

```
Regime          S1 ORB   S2 VWAP   S3 Candle   S4 RL   S5 Basis
BULL_TREND       1.4x     0.7x      1.1x        1.2x    1.0x
BEAR_TREND       1.3x     0.7x      1.1x        1.2x    1.0x
RANGE_BULLISH    1.0x     1.3x      1.0x        1.0x    1.0x
RANGE_BEARISH    1.0x     1.3x      1.0x        1.0x    1.0x
RANGE_NEUTRAL    0.9x     1.2x      0.9x        0.9x    1.0x
HIGH_UNCERTAINTY 0.5x     0.6x      0.5x        0.5x    0.9x
EVENT_RISK       0.0x     0.0x      0.0x        0.0x    0.0x
```

If `strategy_multiplier < 0.4`, signal is suppressed for that strategy.  
If `regime == EVENT_RISK`, all trading is paused (triggered by VIX z-score > 2.5 or uncertainty > 0.9).

### Macro signals (daily refresh)

| Signal | Source | Direction | Rationale |
|--------|---------|-----------|-----------|
| S&P 500 overnight return | yfinance `^GSPC` | Positive = bullish | Global risk-on flows into EM |
| USD/INR change | yfinance `INR=X` | Stronger USD = bearish | FII sell INR-denominated assets |
| Brent crude return | yfinance `BZ=F` | Higher crude = bearish | India imports 80%+ of crude |
| FII net flow | NSE public API | FII buying = bullish | Institutional flow dominates NIFTY |
| US 10Y yield | yfinance `^TNX` | Higher yield = bearish | Risk-free rate competition |

### Technical signals (15-min refresh)

| Signal | Source | Interpretation |
|--------|---------|---------------|
| India VIX | yfinance `^INDIAVIX` | VIX > 2 sigma = EVENT_RISK |
| BankNifty vs NIFTY divergence | yfinance `^NSEBANK` | Sector rotation signal |
| Futures OI change | Upstox market quote API | Large OI build = conviction |

### Sentiment signals (every 5-min candle)

| Signal | Interpretation |
|--------|---------------|
| Put/Call Ratio (PCR) | PCR < 0.7 = bullish, PCR > 1.2 = bearish |
| Max Pain | Price gravitates to max pain on expiry; distance from current price = directional signal |
| IV Percentile | High IV = expensive options = uncertainty = reduce size |
| OI walls | Nearest call wall (resistance) and put wall (support) from strike-level OI |

### How strategies use IntelligenceOutput

```python
# In multi_engine._evaluate_strategy():
if intel is not None and intel.is_valid:
    if not intel.allow_trading():
        return   # EVENT_RISK or extreme conditions

    if intel.regime and intel.regime.mult(strategy_key) < 0.4:
        return   # wrong regime for this strategy

# In strategy itself (via context["intel"]):
intel = context.get("intel")
if intel and not intel.agrees_with("LONG", "S1"):
    # macro/sentiment disagrees with long direction
    # strategy may choose to skip or reduce size
    pass
```

---

## 7. Backtesting Engine

**File:** `src/engine.py`

### Usage

```python
from src.engine import BacktestEngine, EngineConfig
from strategies.strategy_1.strategy import ORBStrategy, add_indicators

cfg    = EngineConfig(initial_capital=500_000, lot_size=75, commission=40)
result = BacktestEngine(cfg).run(df, ORBStrategy(), add_indicators, strategy_name="S1_ORB")
```

### Execution model details

The engine pre-extracts NumPy arrays (`opens`, `highs`, `lows`, `closes`) for the entire dataset before the bar loop. The loop indexes directly into these arrays — no pandas `iterrows()`, no Python object creation per bar. This is approximately 100x faster than row-by-row DataFrame iteration.

```python
# Correct intraday execution (no look-ahead):
bar[i] fires strategy.on_bar() -> pending_sig stored
bar[i+1]: fill at opens[i+1] + slippage   # pending_sig consumed
           SL: lows[i+1] <= pos_sl         # pessimistic: SL wins
           TGT: highs[i+1] >= pos_target
           SO: ts.time() >= square_off_time -> exit at opens[i+1]
```

**Cross-session guard:**
```python
if new_day:
    if pending_sig is not None:
        pending_sig = None   # cancel — never fill overnight
```

### Walk-forward validation

```python
results = BacktestEngine(cfg).walk_forward(
    df, strategy, add_indicators,
    train_months=6,   # minimum history before testing starts
    test_months=1,    # one month of out-of-sample per fold
)
# Fold 1: test month 7
# Fold 2: test month 8
# ...
# Each fold calls strategy.reset() — no data leakage
```

### BacktestResult metrics

```python
result.n_trades         # int
result.win_rate         # float (e.g. 0.49 = 49%)
result.profit_factor    # gross_wins / gross_losses
result.max_drawdown     # float (fraction, e.g. 0.10 = 10%)
result.sharpe           # annualised, computed from daily P&L
result.sortino          # downside-deviation version of Sharpe
result.calmar           # CAGR / max_drawdown
result.trades_df        # pd.DataFrame with all trade details
result.equity_curve     # pd.Series indexed by timestamp
result.daily_pnl        # pd.Series of daily P&L values
```

Sharpe is computed from `daily_pnl` series (not bar-level returns). For intraday strategies, 90% of 5-min bars have zero P&L. Daily P&L Sharpe is the correct metric and matches institutional reporting standards.

---

## 8. Paper Trading and Dashboard

### Starting paper trading

```powershell
python scripts/upstox_auth.py   # get fresh token first
python main.py paper
# Open: http://localhost:5000
```

### Two-phase warmup

When started at any time during the session (e.g. 13:30), the engine performs:

**Phase 1 — Historical context (CSV):**  
Loads last 300 5-min bars from `nifty_futures_5min.csv`. Provides indicator warmup history (EMA50, ATR, ADX, etc.).

**Phase 2 — Today's bars (Upstox intraday API):**  
Calls `GET /v3/historical-candle/intraday/{key}/minutes/1` (no date params).  
From the returned 1-min bars:
- Computes **ORB range** (09:15–09:44) — S1 knows today's opening range immediately
- Computes **session VWAP** — S2 has correct VWAP from market open
- Computes session Open/High/Low — shown in dashboard hero card
- Aggregates 1-min to 5-min bars — appended to bar buffer for strategy evaluation

**Strategy priming:**  
After warmup, runs the full bar buffer (historical + today) through each strategy's `on_bar()` silently. This advances internal state like ORB tracker, VWAP accumulators, and pattern history without generating live signals.

### Live data (WebSocket)

`src/live_feed.py` connects to `upstox_client.MarketDataStreamerV3`. Subscribes to front-month instrument key inside `on_open` callback (required pattern). Extracts LTP from full feed message path: `feeds -> {key} -> fullFeed -> marketFF -> ltpc -> ltp`. Builds 5-min candles by bucketing ticks to floor-5-min timestamps. Simultaneously builds 1-min candles from ticks for the dashboard candlestick chart.

### Multi-strategy execution

Each strategy has its own `StrategyState` tracking position, P&L, trades, and logs independently. One shared WebSocket connection, one shared bar buffer.

Per candle:
1. Deduplicate timestamps in bar buffer (warmup + live can overlap)
2. Update intelligence engine (macro/sentiment/regime)
3. For each S1–S5:
   - Check exits (SL/target/square-off) on current bar
   - If regime blocks this strategy, skip
   - Evaluate `strat.on_bar()`, get Signal
   - If direction LONG/SHORT and sl_dist > 0, enter position
   - Call `strat.on_fill()` to update strategy internal state

S5 builds a basis DataFrame separately: aligns spot buffer (from yfinance proxy warmup) with futures bars and computes basis_spread and z-score.

### Dashboard (Kinetic Observatory)

Single-page HTML app served by Flask at `localhost:5000`. Polls `/api/state` every 3 seconds.

**Top navigation bar:** live price, session P&L, bot status dot (pulsing green = active), timestamp.

**Hero section (8/12 columns):** large price display, total session P&L across all strategies, today's Open/High/Low, ORB High/Low, VWAP, bar count. Ambient cyan glow background.

**Portfolio overview (4/12 columns):** strategies ready count, capital deployed, combined P&L, win rate, warmup status, warmup detail.

**Intelligence panel:** current regime (colour-coded: cyan=trend, gold=range, grey=uncertain, red=blocked), confidence score, gate status (PASS green / BLOCKED red), best strategies for current conditions, alpha score reason string.

**1-min candlestick chart (Canvas 2D):**  
- Grey candles = before bot started (Phase 2 warmup bars loaded from API)  
- Cyan/red candles = after bot started (live bars from WebSocket)  
- Volume bars below chart (same colour coding)  
- Hover tooltip showing OHLC and phase  
- Chart resizes on window resize  

**Strategy cards (5 columns):** one per strategy. Status badge (FLAT / LONG / SHORT / WAITING), session P&L, sparkline (last 40 P&L data points), trade count, win rate. If in position: entry/SL/target/unrealised P&L.

**Trade History:** filterable by strategy (S1–S5 or All). Shows time/direction/entry/exit/P&L/reason. Sorted newest first.

**Activity Feed:** combined log across all strategies, filterable by strategy. Entry arrows in blue, wins in green, losses in red.

---

## 9. Daily Workflow

```powershell
# Morning setup (before 09:15)
python scripts/upstox_auth.py
python scripts/build_history_upstox.py

# Optional: verify data quality
python scripts/analyze_csv.py

# Optional: run backtest before trading
python main.py backtest
# -> prompts: strategy (1-5), data source, run type (full or walk-forward)

# Paper trading
python main.py paper
# -> starts warmup, opens dashboard at http://localhost:5000
# -> Ctrl+C to stop

# After market close
python main.py report
# -> re-runs analytics on saved trades from today's session
```

---

## 10. Key Design Decisions

### Intraday only, no overnight positions
Every strategy square-offs by 15:20. Eliminates overnight gap risk, margin top-up risk, and contract rollover P&L contamination. The backtesting engine enforces this through `EngineConfig.square_off_time`.

### One config per strategy, no global config dependency
Changing S1's `ATR_SL_MULT` cannot affect S2. The engine receives config values explicitly through `EngineConfig`. Strategies import only from their own `config.py`.

### Entry on next bar open, not signal bar close
Signal on bar[i] close -> fill at bar[i+1] open + slippage. This is the correct model for end-of-bar strategies. Earlier versions used bar close as entry, which is look-ahead bias.

### Lot size 75 (from Nov 2024)
SEBI changed NIFTY lot size from 25 to 75 in November 2024. All configs use `LOT_SIZE = 75`.

### NSE expiry: Thursday pre-Sep 2025, Tuesday from Sep 2025
From Sep 1, 2025, NSE moved NIFTY monthly expiry from last Thursday to last Tuesday. `utils.nse_expiry(year, month)` encodes this rule. The `Get Expiries` API always returns actual dates so expired contract fetching is not affected.

### Futures data via expired instruments API, not historical candle API
Upstox does not serve historical futures data for expired contracts via the standard historical candle endpoint. The three-step process (Get Expiries -> Get Expired Contract Key -> Get Expired Candle Data) is required. Failing to do this was the original source of "no data returned" errors in early development.

### Daily P&L Sharpe, not bar-level
For intraday strategies, 90% of 5-min bars have zero P&L. Bar-level Sharpe gives a misleadingly inflated denominator. Daily P&L Sharpe matches institutional reporting.

### Intelligence layer does not recompute technical indicators
Each strategy already computes ATR, ADX, RSI, VWAP etc. via its own `add_indicators()` function. The intelligence layer provides only cross-asset signals (macro flows, implied volatility, options positioning) that strategies cannot derive from their own price data.

---

## 11. Backtest Results

All backtests use `nifty_futures_5min.csv` (Sep 2024 to Mar 2026, approximately 18 months of real futures data):

| Strategy | Net P&L | CAGR | Sharpe | Win Rate | PF | Max DD | Avg Duration |
|----------|---------|------|--------|----------|-----|--------|-------------|
| S1 ORB + ADX | +Rs 1,50,322 | 19.6% | 1.91 | 49.0% | 1.43 | 10.74% | 138 min |
| S2 VWAP (no RF) | +Rs 36,296 | 4.9% | 0.86 | 32.2% | 1.13 | 6.38% | 25 min |
| S3 Candle (pivot) | +Rs 1,26,838 | 15.8% | 1.14 | 34.3% | 1.12 | 8.79% | 12 min |
| S4 RL Scalper | Needs training | — | — | — | — | — | — |
| S5 Basis Arb | +Rs 4,65,946 | 53.2% | 2.55 | 35.0% | 1.98 | 5.22% | 14 min |

PF = Profit Factor. Max DD = maximum drawdown.

Results are per-strategy on Rs 5,00,000 capital (1 lot). Combined portfolio P&L is not simply additive since strategies run in parallel with separate capital pools and are designed to be uncorrelated across market regimes.

---

## 12. Environment and Dependencies

**Python:** 3.12 (Anaconda, environment name: `stock-predictor`)  
**OS tested:** Windows 11, PowerShell

### Installation

```powershell
conda activate stock-predictor
pip install pandas>=2.0 numpy>=1.24 matplotlib>=3.7 yfinance>=0.2
pip install flask requests scikit-learn upstox-python-sdk
```

### Dependency map

| Package | Used by |
|---------|---------|
| `pandas` | All — DataFrames, time series, CSV I/O |
| `numpy` | Engine loop, indicators, z-scores, PPO |
| `matplotlib` | analytics.py — equity curve plots |
| `yfinance` | build_history.py (proxy), macro collector (VIX, S&P500, FII proxy) |
| `flask` | multi_dashboard.py — web server |
| `requests` | All Upstox REST API calls, NSE scraping |
| `scikit-learn` | S2 (RandomForestClassifier), S3 (MLPClassifier for CNN), S4 (PPO networks) |
| `upstox-python-sdk` | live_feed.py — MarketDataStreamerV3 WebSocket |
| `zoneinfo` | All — IST timezone handling (Python stdlib >= 3.9) |
| `pickle` | Saving/loading ML models (RF, CNN, PPO) |

### Environment variables

```powershell
$env:UPSTOX_API_KEY    = "your_api_key_here"
$env:UPSTOX_API_SECRET = "your_api_secret_here"
```

After `python scripts/upstox_auth.py`, the daily access token is saved to `.upstox_token` in the project root. This file is read by all scripts automatically. It expires at the end of each trading day.

---

## 13. Configuration Reference

### EngineConfig (src/engine.py)

```python
@dataclass
class EngineConfig:
    initial_capital:    float = 500_000      # Rs
    lot_size:           int   = 75           # NIFTY lot size (updated Nov 2024)
    commission_per_lot: float = 40.0         # Rs per lot round-trip
    slippage_ticks:     int   = 1            # 1 tick = 0.05 points
    tick_size:          float = 0.05
    square_off_time:    time  = time(15, 20) # force exit at this time
    max_lots:           int   = 1
    max_daily_loss_pct: float = 0.02         # 2% of initial_capital
```

### Strategy 1 parameters (strategies/strategy_1/config.py)

```python
OR_BARS             = 6           # opening range bars
ADX_PERIOD          = 14
ADX_THRESHOLD       = 35          # minimum trend strength
ATR_PERIOD          = 14
ATR_SL_MULT         = 1.85        # SL width
RR_RATIO            = 1.93        # target R:R
ORB_ENTRY_CUTOFF    = time(13,30) # no entries after this
ORB_DEAD_ZONE_START = time(10,35) # skip this window
ORB_DEAD_ZONE_END   = time(10,54)
ORB_SKIP_SLOTS      = [(11,40),(11,45),(11,50),(12,50),(12,55),(13,25)]
TRADE_DIRECTION     = "BOTH"
LOT_SIZE            = 75
COMMISSION_PER_LOT  = 40
INITIAL_CAPITAL     = 500_000
```

### Upstox API endpoints reference

| Endpoint | Purpose |
|----------|---------|
| `GET /v2/expired-instruments/expiries?instrument_key=NSE_INDEX\|Nifty 50` | All available expiry dates (up to 6 months back) |
| `GET /v2/expired-instruments/future/contract?instrument_key=...&expiry_date=YYYY-MM-DD` | Get expired_instrument_key for a specific expiry |
| `GET /v2/expired-instruments/historical-candle/{encoded_key}/5minute/{to}/{from}` | Candles for expired contracts |
| `GET /v3/historical-candle/{encoded_key}/minutes/5/{to}/{from}` | Historical candles for live contracts (30-day window) |
| `GET /v3/historical-candle/intraday/{encoded_key}/minutes/1` | Today's 1-min bars (no date params, always current day) |
| `GET /v3/historical-candle/intraday/{encoded_key}/minutes/5` | Today's 5-min bars (no date params) |
| `https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz` | Live instruments file (all NSE instruments) |
| `GET /v2/market-quote/quotes?instrument_key={key}` | Live market quote including OI |
| `MarketDataStreamerV3` (WebSocket) | Real-time tick feed |

**URL encoding:** All instrument keys containing `|` must be URL-encoded as `%7C` in path parameters. Use `urllib.parse.quote(key, safe="")`. Failure to encode causes HTTP 400.

---

## 14. Known Limitations and Roadmap

### Current limitations

**Dec 2024 data gap:** Confirmed Upstox database hole for the Dec 2024 expiry. Key exists, returns empty candles. Workaround: use yfinance proxy for that period if needed.

**S4 RL training latency:** S4 requires approximately 1500 bars in the buffer before its first PPO training cycle. In paper trading, this means ~20 trading hours of accumulated live data before S4 generates signals (status shows WAITING until then).

**S5 spot data in live mode:** The spot buffer is populated from the yfinance proxy CSV (end-of-day data) during warmup. For live basis arb, the spot price should be the real-time NSE index value. Workaround: use Upstox index quote API (`NSE_INDEX|Nifty 50`) for live spot price.

**Intelligence layer external APIs:** NSE options chain (for PCR/max pain), FII/DII data, and VIX data can be rate-limited or temporarily unavailable. All collectors have graceful fallback to neutral values (0.0 z-score, 1.0 PCR) when data is unavailable.

**No real order execution:** The system is paper trading only. Upstox order placement APIs are not implemented.

### Roadmap

- Pre-train S4 on full 18-month historical dataset before paper trading (removes the 20-hour warmup requirement)
- Replace yfinance spot proxy in live S5 with real-time Upstox index quote
- Walk-forward parameter optimisation: systematic grid search of S1/S2 parameters per fold
- PostgreSQL/TimescaleDB migration for data at scale
- Live order execution via Upstox Orders API
- S2 RF model quality improvement (more training samples needed; currently limited by 18-month dataset)
- Risk dashboard: real-time VAR, correlation matrix across running strategies, drawdown alerts

---

## Quick Reference Card

```
Setup (once):
  conda activate stock-predictor
  pip install pandas numpy matplotlib yfinance flask requests scikit-learn upstox-python-sdk

Daily routine:
  python scripts/upstox_auth.py
  python scripts/build_history_upstox.py

Backtest:
  python main.py backtest   (prompts strategy, data, run type)

Paper trade all 5 strategies:
  python main.py paper      (dashboard at http://localhost:5000)

Data analysis:
  python scripts/analyze_csv.py

Re-run report on saved trades:
  python main.py report
```

---

*NIFTY 50 Futures, NSE, India. Intraday only. Paper trading system — not financial advice.*