"""
multi_engine.py — Multi-strategy live paper trading engine

Runs all 5 strategies simultaneously on the same live feed.
Each strategy has its own:
  - signal evaluation
  - open position tracker
  - P&L account
  - trade log

Shared:
  - Live WebSocket feed (single connection)
  - Bar buffer (single candle stream)
  - Lock (thread-safe state)

Architecture:
  LiveFeed → on_candle() → [S1, S2, S3, S4, S5] each evaluated independently
  Each strategy manages its own position (max 1 lot each, simultaneously)
  Dashboard polls /api/state which returns state for all 5

Intelligence Engine:
  - Macro/sentiment/regime analysis via IntelligenceEngine
  - Per-bar update; gates both trade entry and strategy-specific regime suitability
  - Regime multiplier < 0.4 suppresses a strategy for the current bar

1-min Chart:
  - Tick-based 1-min bar building in _on_tick (no dependency on candle cadence)
  - _get_chart_data() merges historical warmup bars + current incomplete bar
  - Chart starts from 09:30 IST, filtered on every poll
"""

import os
import csv
import time as _time
import threading
import logging
from datetime import datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np

from costs import round_trip_cost

logger = logging.getLogger("multi_engine")
IST    = ZoneInfo("Asia/Kolkata")

PAPER_TRADES_DIR = "outputs/paper_trades"
PAPER_TRADE_FIELDS = [
    "strategy", "entry_time", "exit_time", "direction", "entry", "exit",
    "lots", "lot_size", "pnl_points", "gross", "costs", "net", "reason",
]


def _append_paper_trade(row: dict):
    """Append one closed paper trade to outputs/paper_trades/YYYY-MM-DD.csv."""
    try:
        os.makedirs(PAPER_TRADES_DIR, exist_ok=True)
        day  = datetime.now(IST).strftime("%Y-%m-%d")
        path = os.path.join(PAPER_TRADES_DIR, f"{day}.csv")
        new  = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=PAPER_TRADE_FIELDS)
            if new:
                w.writeheader()
            w.writerow(row)
    except Exception as e:
        logger.error(f"Could not persist paper trade: {e}")

# ── Intelligence engine — lazy import ─────────────────────────────────────────
_intel_engine_cls = None

def _get_intel_cls():
    global _intel_engine_cls
    if _intel_engine_cls is None:
        import sys, os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from intelligence.engine import IntelligenceEngine
        _intel_engine_cls = IntelligenceEngine
    return _intel_engine_cls


# ── Per-strategy position tracker ─────────────────────────────────────────────

class StrategyPosition:
    """Tracks one open position for one strategy."""

    def __init__(self, direction: str, entry: float, sl: float,
                 target: float, lots: int, ts: datetime, name: str,
                 sl_dist: float = 0.0, atr: float = 0.0):
        self.direction    = direction
        self.entry_price  = entry
        self.sl           = sl
        self.target       = target
        self.lots         = lots
        self.entry_time   = ts
        self.strategy     = name
        self.sl_dist      = sl_dist        # original SL distance in points
        self.atr          = atr            # ATR at entry (for trailing)

        # T1 = midpoint between entry and target (always < T2, regardless of RR)
        self.t1 = round((entry + target) / 2, 2)

        # State tracking
        self.t1_hit          = False
        self.sl_at_be        = False
        self.trailing_active = False
        self.bars_held       = 0
        self.peak            = entry   # best price seen since entry (for trailing)

    def unrealised(self, price: float, lot_size: int) -> float:
        pts = (price - self.entry_price) if self.direction == "LONG" \
              else (self.entry_price - price)
        return round(pts * self.lots * lot_size, 0)

    def favour_pct(self, price: float) -> float:
        """How far price has moved toward T2 (0–100%)."""
        dist_total = abs(self.target - self.entry_price)
        if dist_total <= 0:
            return 0.0
        if self.direction == "LONG":
            moved = price - self.entry_price
        else:
            moved = self.entry_price - price
        return round(max(0.0, min(100.0, moved / dist_total * 100)), 1)


class StrategyState:
    """Complete runtime state for one strategy."""

    def __init__(self, name: str, label: str, lot_size: int,
                 commission: int, initial_capital: float,
                 max_hold_bars: int = 10,
                 atr_sl_mult: float = 1.0):
        self.name             = name
        self.label            = label
        self.lot_size         = lot_size
        self.commission       = commission
        self.capital          = initial_capital
        self.initial_capital  = initial_capital
        self.max_hold_bars    = max_hold_bars   # force-exit after N bars
        self.atr_sl_mult      = atr_sl_mult     # for manual trades

        self.open_pos: Optional[StrategyPosition] = None
        self.trades:   list[dict]  = []
        self.log:      list[str]   = []
        self.session_pnl: float    = 0.0
        self.status:  str          = "waiting"   # waiting / flat / long / short

    def enter(self, direction: str, entry: float, sl: float,
              target: float, ts: datetime,
              sl_dist: float = 0.0, atr: float = 0.0):
        self.open_pos = StrategyPosition(
            direction, entry, sl, target, 1, ts, self.name,
            sl_dist=sl_dist, atr=atr,
        )
        self.status = direction.lower()
        self._log(
            f"▶ {direction} @ {entry:.2f}  "
            f"SL {sl:.2f}  T1 {self.open_pos.t1:.2f}  TGT {target:.2f}"
        )

    def exit(self, price: float, reason: str, ts: datetime):
        if not self.open_pos:
            return
        p   = self.open_pos
        pts = (price - p.entry_price) if p.direction == "LONG" \
              else (p.entry_price - price)
        gross      = pts * p.lots * self.lot_size
        commission = round_trip_cost(p.direction, p.entry_price, price,
                                     p.lots, self.lot_size)
        net        = gross - commission

        self.session_pnl += net
        self.capital     += net
        self.open_pos     = None
        self.status       = "flat"

        emoji = "✅" if net > 0 else "❌"
        pnl_str = f"₹{net:+,.0f}"
        self.trades.append({
            "time":      p.entry_time.strftime("%H:%M"),
            "direction": p.direction,
            "entry":     p.entry_price,
            "exit":      round(price, 2),
            "pnl":       round(net, 0),
            "reason":    reason,
        })
        _append_paper_trade({
            "strategy":   self.name,
            "entry_time": p.entry_time.isoformat(),
            "exit_time":  ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
            "direction":  p.direction,
            "entry":      p.entry_price,
            "exit":       round(price, 2),
            "lots":       p.lots,
            "lot_size":   self.lot_size,
            "pnl_points": round(pts, 2),
            "gross":      round(gross, 2),
            "costs":      round(commission, 2),
            "net":        round(net, 2),
            "reason":     reason,
        })
        self._log(f"{emoji} EXIT {p.direction} @ {price:.2f}  {pnl_str}  "
                  f"(costs ₹{commission:,.0f})  [{reason}]")

    def check_exits(self, bar: pd.Series, current_price: float,
                    square_off: time) -> bool:
        """Check SL/T1/T2/time exits. Updates trailing SL in-place. Returns True if exited."""
        if not self.open_pos:
            return False
        p  = self.open_pos
        ts = bar.name
        h  = bar["high"]
        lo = bar["low"]
        p.bars_held += 1

        # ── Square-off time ───────────────────────────────────────────────────
        if ts.time() >= square_off:
            self.exit(current_price, "square-off", ts)
            return True

        # ── Max hold (time-exit for medium-frequency trading) ─────────────────
        if p.bars_held >= self.max_hold_bars:
            self.exit(current_price, f"time-exit ({p.bars_held}m)", ts)
            return True

        if p.direction == "LONG":
            # Update peak for trailing
            if h > p.peak:
                p.peak = h

            # T1 check (only once)
            if not p.t1_hit and h >= p.t1:
                p.t1_hit     = True
                p.sl         = p.entry_price   # SL → break-even
                p.sl_at_be   = True
                p.trailing_active = True
                self._log(
                    f"🎯 T1 HIT @ {p.t1:.2f}  ↑ SL moved to BE {p.entry_price:.2f}"
                )

            # Trailing stop: trail SL at (peak - original sl_dist)
            if p.trailing_active and p.sl_dist > 0:
                trail_level = round(p.peak - p.sl_dist, 2)
                if trail_level > p.sl:
                    self._log(
                        f"↕ TRAIL SL {p.sl:.2f} → {trail_level:.2f}  "
                        f"(peak={p.peak:.2f})"
                    )
                    p.sl = trail_level

            # Exit checks
            if lo <= p.sl:
                reason = "stop-loss" if not p.sl_at_be else (
                    "trailed-stop" if p.trailing_active else "be-stop"
                )
                self.exit(p.sl, reason, ts)
                return True
            if lo <= p.target:   # shouldn't happen but safety
                pass
            if h >= p.target:
                self.exit(p.target, "target (T2)", ts)
                return True

        else:  # SHORT
            if lo < p.peak:
                p.peak = lo

            if not p.t1_hit and lo <= p.t1:
                p.t1_hit     = True
                p.sl         = p.entry_price
                p.sl_at_be   = True
                p.trailing_active = True
                self._log(
                    f"🎯 T1 HIT @ {p.t1:.2f}  ↑ SL moved to BE {p.entry_price:.2f}"
                )

            if p.trailing_active and p.sl_dist > 0:
                trail_level = round(p.peak + p.sl_dist, 2)
                if trail_level < p.sl:
                    self._log(
                        f"↕ TRAIL SL {p.sl:.2f} → {trail_level:.2f}  "
                        f"(peak={p.peak:.2f})"
                    )
                    p.sl = trail_level

            if h >= p.sl:
                reason = "stop-loss" if not p.sl_at_be else (
                    "trailed-stop" if p.trailing_active else "be-stop"
                )
                self.exit(p.sl, reason, ts)
                return True
            if lo <= p.target:
                self.exit(p.target, "target (T2)", ts)
                return True

        return False

    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if t["pnl"] > 0)
        return round(wins / len(self.trades) * 100, 1)

    def _log(self, msg: str):
        now  = datetime.now(IST).strftime("%H:%M:%S")
        line = f"[{now}] [{self.label}] {msg}"
        self.log.append(line)
        logger.info(line)
        if len(self.log) > 200:
            self.log = self.log[-200:]

    def snapshot(self, current_price: float) -> dict:
        open_trade = None
        risk       = None
        if self.open_pos:
            p = self.open_pos
            fav = p.favour_pct(current_price)
            open_trade = {
                "direction":   p.direction,
                "entry_price": p.entry_price,
                "sl":          p.sl,
                "target":      p.target,
                "t1":          p.t1,
                "entry_time":  p.entry_time.strftime("%H:%M:%S"),
                "unrealised":  p.unrealised(current_price, self.lot_size),
                "bars_held":   p.bars_held,
            }
            risk = {
                "t1":              p.t1,
                "t1_hit":          p.t1_hit,
                "sl_at_be":        p.sl_at_be,
                "trailing_active": p.trailing_active,
                "favour_pct":      fav,
                "bars_held":       p.bars_held,
                "max_hold_bars":   self.max_hold_bars,
                "peak":            p.peak,
            }
        return {
            "name":        self.name,
            "label":       self.label,
            "status":      self.status,
            "session_pnl": round(self.session_pnl, 0),
            "capital":     round(self.capital, 0),
            "open_trade":  open_trade,
            "risk":        risk,
            "trades":      self.trades[-50:],
            "log":         self.log[-30:],
            "win_rate":    self.win_rate(),
            "n_trades":    len(self.trades),
        }


# ── Multi-strategy engine ──────────────────────────────────────────────────────

class MultiStrategyEngine:
    """
    Runs all 5 strategies on a single live feed simultaneously.
    Each strategy evaluates independently and manages its own position.
    """

    SQUARE_OFF = time(15, 20)
    BAR_MIN    = 5                      # strategy bar size — same as backtest/warmup CSV
    SPOT_KEY   = "NSE_INDEX|Nifty 50"   # live spot for S5 basis

    def __init__(self, token: str, instrument_key: str,
                 warmup_csv: Optional[str] = None,
                 spot_csv:   Optional[str] = None,
                 lot_size:   Optional[int] = None):

        self.token          = token
        self.instrument_key = instrument_key
        self.warmup_csv     = warmup_csv
        self.spot_csv       = spot_csv
        self.lot_size       = lot_size   # exchange lot size; overrides configs

        self._lock    = threading.Lock()
        self._running = False
        self.status   = "stopped"

        self.current_price:  float = 0.0
        self.bar_buffer:     list  = []
        self.spot_closes:    dict  = {}   # 5-min bar timestamp → spot close (S5)

        # 1-min chart data for dashboard candlestick chart
        self._chart_bars_1m: list = []   # {"ts","o","h","l","c","phase"}

        # ── Tick-based current 1-min bar accumulator ──────────────────────────
        # Updated every tick in _on_tick; flushed when the minute bucket rolls.
        # This gives the dashboard a live, sub-minute candle without waiting for
        # the next completed bar from the WebSocket feed.
        self._cur_1m_ts: Optional[pd.Timestamp] = None
        self._cur_1m_o:  float = 0.0
        self._cur_1m_h:  float = 0.0
        self._cur_1m_l:  float = float("inf")
        self._cur_1m_c:  float = 0.0

        # Session OHLCV (today's data)
        self._session_open:  float = 0.0
        self._session_high:  float = 0.0
        self._session_low:   float = float("inf")
        self._session_vwap:  float = 0.0
        self._session_pv:    float = 0.0  # price×volume sum for VWAP
        self._session_vol:   float = 0.0  # cumulative volume
        self._session_bars:  int   = 0

        # ORB range (populated from today's first 30 1-min bars via warmup)
        self._orb_high:  float = 0.0
        self._orb_low:   float = float("inf")
        self._orb_ready: bool  = False

        # Warmup status for dashboard
        self._warmup_summary: str = "Starting..."
        self._warmup_detail:  str = ""

        # ── Intelligence engine — macro/sentiment/regime ───────────────────────
        # Off by default: the gate was never backtested, and its network calls
        # (yfinance, NSE scraping — often blocked from cloud IPs) are slow.
        # When on, it refreshes in its own thread so the feed never waits on it.
        self._use_intel      = os.getenv("USE_INTEL_GATE", "0") == "1"
        self._intel_every    = int(os.getenv("INTEL_REFRESH_SEC", "300"))
        self._intel_engine   = None
        if self._use_intel:
            self._intel_engine = _get_intel_cls()(
                token          = token,
                instrument_key = instrument_key,
            )
        self._last_intel_out = None

        # ── Load strategies ───────────────────────────────────────────────────
        self._strategies = {}   # name → (strategy_obj, StrategyState, add_ind_fn)
        self._load_strategies()
        if self.lot_size:
            for _, (_, state, _) in self._strategies.items():
                state.lot_size = self.lot_size
            logger.info(f"Lot size {self.lot_size} (from exchange instrument master)")

    def _prime_strategies(self):
        """
        Run the full bar buffer through each strategy's on_bar() silently.
        This warms up internal state like OR tracker, VWAP accumulators, etc.
        Without this, S1's or_tracker never sees today's opening bars.
        """
        if len(self.bar_buffer) < 3:
            return

        logger.info("  Priming strategies with bar buffer...")

        # Build indicator df from full buffer
        try:
            from strategies.strategy_1.strategy import add_indicators as ai1
            df = pd.DataFrame(self.bar_buffer)
            if df.index.duplicated().any():
                df = df[~df.index.duplicated(keep="last")]
            df = ai1(df)
        except Exception as e:
            logger.warning(f"  Prime indicators failed: {e}")
            return

        context_flat = {
            "in_position": False, "position_dir": None,
            "session_bar": 0, "day_pnl": 0.0,
            "capital": 500_000, "new_day": False,
        }

        for s_key, (strat, state, add_ind) in self._strategies.items():
            try:
                # Apply strategy-specific indicators
                try:
                    df_s = add_ind(pd.DataFrame(self.bar_buffer).pipe(
                        lambda x: x[~x.index.duplicated(keep="last")]
                    ))
                except Exception:
                    df_s = df  # fallback to S1 indicators

                primed = 0
                for i in range(1, len(df_s)):
                    bar      = df_s.iloc[i]
                    prev_bar = df_s.iloc[i - 1]
                    try:
                        # Call on_bar in silent mode — discard the signal;
                        # this advances internal state (OR tracker, etc.)
                        strat.on_bar(bar, prev_bar, context_flat)
                        primed += 1
                    except Exception:
                        pass

                logger.info(f"  {state.label}: primed with {primed} bars")
            except Exception as e:
                logger.warning(f"  Prime {s_key} failed: {e}")

    def _load_strategies(self):
        """Load all 5 strategies with their configs."""
        from strategies.strategy_1.strategy import ORBStrategy, add_indicators as ai1
        from strategies.strategy_1.config import (
            LOT_SIZE as LS1, COMMISSION_PER_LOT as C1, INITIAL_CAPITAL as IC1,
            ATR_SL_MULT as SM1,
        )
        from strategies.strategy_1.config import MAX_HOLD_BARS as MH1
        self._strategies["S1"] = (
            ORBStrategy(),
            StrategyState("S1", "ORB", LS1, C1, IC1, max_hold_bars=MH1, atr_sl_mult=SM1),
            ai1,
        )

        from strategies.strategy_2.strategy import VWAPReversionStrategy, add_indicators as ai2
        from strategies.strategy_2.config import (
            LOT_SIZE as LS2, COMMISSION_PER_LOT as C2, INITIAL_CAPITAL as IC2,
            ATR_SL_MULT as SM2,
        )
        from strategies.strategy_2.config import MAX_HOLD_BARS as MH2
        self._strategies["S2"] = (
            VWAPReversionStrategy(use_rf=False),
            StrategyState("S2", "VWAP", LS2, C2, IC2, max_hold_bars=MH2, atr_sl_mult=SM2),
            ai2,
        )

        from strategies.strategy_3.strategy import CandleSRStrategy, add_indicators as ai3
        from strategies.strategy_3.config import (
            LOT_SIZE as LS3, COMMISSION_PER_LOT as C3, INITIAL_CAPITAL as IC3,
            ATR_SL_MULT as SM3,
        )
        from strategies.strategy_3.config import MAX_HOLD_BARS as MH3
        self._strategies["S3"] = (
            CandleSRStrategy(use_cnn=True),
            StrategyState("S3", "CANDLE", LS3, C3, IC3, max_hold_bars=MH3, atr_sl_mult=SM3),
            ai3,
        )

        from strategies.strategy_4.strategy import RLScalperStrategy, add_indicators as ai4
        from strategies.strategy_4.config import (
            LOT_SIZE as LS4, COMMISSION_PER_LOT as C4, INITIAL_CAPITAL as IC4,
            ATR_SL_MULT as SM4, MAX_HOLD_BARS as MH4,
        )
        self._strategies["S4"] = (
            RLScalperStrategy(load_existing=True),
            StrategyState("S4", "RL", LS4, C4, IC4, max_hold_bars=MH4, atr_sl_mult=SM4),
            ai4,
        )

        from strategies.strategy_5.strategy import BasisArbStrategy, add_indicators as ai5
        from strategies.strategy_5.config import (
            LOT_SIZE as LS5, COMMISSION_PER_LOT as C5, INITIAL_CAPITAL as IC5,
        )
        self._strategies["S5"] = (
            BasisArbStrategy(),
            StrategyState("S5", "BASIS", LS5, C5, IC5, max_hold_bars=20, atr_sl_mult=1.0),
            ai5,
        )

        logger.info(f"Loaded {len(self._strategies)} strategies")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        self._warm_up()

        # Prime BEFORE the feed starts so no live candle races the priming
        # loop on the same strategy objects.
        self._prime_strategies()

        if self._use_intel:
            threading.Thread(target=self._intel_loop, daemon=True).start()

        from live_feed import LiveFeed
        self.feed = LiveFeed(
            token          = self.token,
            instrument_key = self.instrument_key,
            on_candle      = self._on_candle,
            on_tick        = self._on_tick,
            interval_min   = self.BAR_MIN,
            spot_key       = self.SPOT_KEY,
            on_spot_tick   = self._on_spot_tick,
        )
        self.feed.start()
        self._running = True
        self.status   = "running"

        # Mark all strategies as flat (ready) after warmup
        for _, (_, state, _) in self._strategies.items():
            if state.status == "waiting":
                state.status = "flat"

        logger.info("Multi-strategy engine started")

    def _intel_loop(self):
        """Refresh the intelligence output off the feed thread."""
        while True:
            try:
                out = self._intel_engine.update(self.current_price)
                self._last_intel_out = out
                if out.regime and not out.regime.allow_trading:
                    logger.info(f"Intel gate: {out.regime.description}")
            except Exception as e:
                logger.warning(f"Intel update error: {e}")
            _time.sleep(self._intel_every)

    def stop(self):
        if hasattr(self, "feed") and self.feed:
            self.feed.stop()
        self._running = False
        self.status   = "stopped"
        self._print_summary()

    def _warm_up(self):
        """
        Two-phase warmup:
        Phase 1: Load last 300 bars from CSV for indicator history
        Phase 2: Fetch today's already-completed bars from Upstox API
                 so strategies have full context even if started mid-session.
                 Critical for ORB (needs opening range) and VWAP
                 (needs today's bars to compute session VWAP correctly).
        """
        logger.info("Warming up — Phase 1: recent sessions from Upstox API...")
        try:
            loaded = self._fetch_recent_history()
        except Exception as e:
            logger.warning(f"  Recent-history fetch failed: {e}")
            loaded = False

        if loaded:
            pass
        elif self.warmup_csv and os.path.exists(self.warmup_csv):
            logger.warning("  Falling back to CSV warmup (may be stale / mixed contracts)")
            df = pd.read_csv(self.warmup_csv, index_col="datetime")
            df.index = pd.to_datetime(df.index)
            if df.index.tzinfo is None:
                df.index = df.index.tz_localize(IST)
            else:
                df.index = df.index.tz_convert(IST)
            df.columns = [c.lower() for c in df.columns]

            warmup = df.tail(300)
            for ts, row in warmup.iterrows():
                self.bar_buffer.append(row)

            # S5 spot buffer
            if self.spot_csv and os.path.exists(self.spot_csv):
                spot = pd.read_csv(self.spot_csv, index_col="datetime")
                spot.index = pd.to_datetime(spot.index)
                if spot.index.tzinfo is None:
                    spot.index = spot.index.tz_localize(IST)
                else:
                    spot.index = spot.index.tz_convert(IST)
                spot.columns = [c.lower() for c in spot.columns]
                spot_aligned = spot["close"].reindex(
                    warmup.index, method="nearest",
                    tolerance=pd.Timedelta("5min")
                ).dropna()
                self.spot_closes.update(spot_aligned.to_dict())

            logger.info(f"  Phase 1: {len(self.bar_buffer)} historical bars loaded")
        else:
            logger.warning("  No warmup CSV — skipping Phase 1")

        # ── Phase 2: fetch today's completed bars from Upstox ─────────────────
        logger.info("Warming up — Phase 2: today's bars from Upstox API...")
        try:
            self._fetch_todays_bars()
        except Exception as e:
            logger.warning(f"  Phase 2 failed: {e} — continuing without today's bars")
        try:
            self._fetch_spot_today()
        except Exception as e:
            logger.warning(f"  Spot warmup failed: {e} — S5 waits for live spot ticks")

    def _fetch_history_5m(self, key: str, days: int) -> pd.DataFrame:
        """Completed 5-min session bars for `key` from previous trading days."""
        import requests
        from datetime import time as dtime
        from urllib.parse import quote

        today = datetime.now(IST).date()
        to_d  = today - timedelta(days=1)
        from_d = today - timedelta(days=days)
        url = (f"https://api.upstox.com/v3/historical-candle/"
               f"{quote(key, safe='')}/minutes/5/{to_d}/{from_d}")
        hdrs = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        resp = requests.get(url, headers=hdrs, timeout=20)
        if not resp.ok:
            raise RuntimeError(f"HTTP {resp.status_code} {resp.text[:120]}")

        rows = []
        for c in resp.json().get("data", {}).get("candles", []):
            ts = pd.Timestamp(c[0])
            ts = ts.tz_localize(IST) if ts.tzinfo is None else ts.tz_convert(IST)
            if dtime(9, 15) <= ts.time() <= dtime(15, 25) and ts.date() < today:
                rows.append({"datetime": ts, "open": float(c[1]), "high": float(c[2]),
                             "low": float(c[3]), "close": float(c[4]),
                             "volume": float(c[5])})
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows).set_index("datetime").sort_index()
        return df[~df.index.duplicated(keep="last")]

    def _fetch_recent_history(self) -> bool:
        """
        Phase 1 from the live front-month contract itself: clean (single
        contract), current, and with real volume — unlike the CSV.
        """
        days = int(os.getenv("WARMUP_DAYS", "10"))
        df = self._fetch_history_5m(self.instrument_key, days)
        if df.empty:
            logger.warning("  Upstox history returned no bars")
            return False

        warmup = df.tail(300)
        for _, row in warmup.iterrows():
            self.bar_buffer.append(row)
        logger.info(
            f"  Phase 1: {len(warmup)} bars from Upstox "
            f"[{warmup.index[0]:%Y-%m-%d %H:%M} → {warmup.index[-1]:%Y-%m-%d %H:%M}]"
        )

        try:
            spot = self._fetch_history_5m(self.SPOT_KEY, days)
            if not spot.empty:
                self.spot_closes.update(spot["close"].to_dict())
                logger.info(f"  Phase 1: {len(spot)} spot closes for S5")
        except Exception as e:
            logger.warning(f"  Spot history failed: {e}")
        return True

    def _fetch_spot_today(self):
        """Today's completed 5-min spot closes for S5 (from 1-min index candles)."""
        import requests
        from urllib.parse import quote

        hdrs = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        url  = (f"https://api.upstox.com/v3/historical-candle/intraday/"
                f"{quote(self.SPOT_KEY, safe='')}/minutes/1")
        resp = requests.get(url, headers=hdrs, timeout=15)
        if not resp.ok:
            logger.warning(f"  Spot intraday HTTP {resp.status_code}")
            return
        bars = []
        for c in resp.json().get("data", {}).get("candles", []):
            ts = pd.Timestamp(c[0])
            ts = ts.tz_localize(IST) if ts.tzinfo is None else ts.tz_convert(IST)
            bars.append({"ts": ts, "open": float(c[1]), "high": float(c[2]),
                         "low": float(c[3]), "close": float(c[4]), "volume": 0.0})
        for row in self._aggregate_to_5min(bars):
            self.spot_closes[row.name] = row["close"]
        logger.info(f"  Spot warmup: {len(self.spot_closes)} spot closes available")

    def _fetch_todays_bars(self):
        """
        Fetch today's intraday bars using the Upstox V3 Intraday API.

        Correct endpoint per docs:
          GET /v3/historical-candle/intraday/{instrument_key}/{unit}/{interval}
          No date params — always returns current trading day data.

        We fetch 1-min bars for maximum precision:
          - ORB range: 09:15–09:44 (first 30 bars)
          - VWAP: rolling typical price × volume
          - Bar buffer: 1-min bars fed directly to strategies
        Falls back to 5-min intraday if 1-min fails.
        """
        import requests
        from datetime import time as dtime
        from urllib.parse import quote

        hdrs    = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        encoded = quote(self.instrument_key, safe="")

        # ── Intraday 1-min endpoint (correct per Upstox V3 docs) ─────────────
        url_1m = f"https://api.upstox.com/v3/historical-candle/intraday/{encoded}/minutes/1"
        logger.info(f"  Fetching intraday 1-min: {url_1m[:80]}...")

        resp = requests.get(url_1m, headers=hdrs, timeout=15)

        if not resp.ok:
            logger.warning(f"  1-min intraday HTTP {resp.status_code} — trying 5-min")
            self._fetch_5min_today(hdrs, encoded)
            return

        data       = resp.json()
        candles_1m = data.get("data", {}).get("candles", [])
        if not candles_1m:
            logger.info("  No 1-min intraday bars yet — market not open or pre-market")
            self._warmup_summary = "Market not open yet — waiting for 09:15"
            return

        # Parse 1-min candles
        bars_1m = []
        for c in candles_1m:
            ts = pd.Timestamp(c[0])
            if ts.tzinfo is None: ts = ts.tz_localize(IST)
            else:                 ts = ts.tz_convert(IST)
            if dtime(9, 15) <= ts.time() <= dtime(15, 25):
                bars_1m.append({
                    "ts": ts, "open": float(c[1]), "high": float(c[2]),
                    "low": float(c[3]), "close": float(c[4]), "volume": float(c[5]),
                })

        if not bars_1m:
            logger.info("  No session 1-min bars yet")
            self._warmup_summary = "Waiting for session start (09:15)"
            return

        bars_1m.sort(key=lambda b: b["ts"])
        logger.info(
            f"  1-min bars: {len(bars_1m)} "
            f"[{bars_1m[0]['ts'].strftime('%H:%M')} → {bars_1m[-1]['ts'].strftime('%H:%M')}]"
        )

        # ── Populate chart data (phase="warmup") ─────────────────────────────
        for b in bars_1m:
            self._chart_bars_1m.append({
                "ts": b["ts"].isoformat(),
                "o":  b["open"], "h": b["high"],
                "l":  b["low"],  "c": b["close"],
                "phase": "warmup",   # grey colour — before bot started
            })

        # ── Compute ORB from first 30 bars (09:15–09:44) ──────────────────────
        orb_bars = [b for b in bars_1m if dtime(9, 15) <= b["ts"].time() < dtime(9, 45)]
        if orb_bars:
            self._orb_high  = max(b["high"] for b in orb_bars)
            self._orb_low   = min(b["low"]  for b in orb_bars)
            self._orb_ready = True
            logger.info(
                f"  ORB: H={self._orb_high:.2f}  L={self._orb_low:.2f}"
                f"  ({len(orb_bars)} 1-min bars)"
            )

        # ── Compute session OHLCV and VWAP from 1-min bars ───────────────────
        self._session_open = bars_1m[0]["open"]
        self._session_high = max(b["high"]  for b in bars_1m)
        self._session_low  = min(b["low"]   for b in bars_1m)
        pv  = sum((b["high"] + b["low"] + b["close"]) / 3 * b["volume"] for b in bars_1m)
        vol = sum(b["volume"] for b in bars_1m)
        self._session_pv   = pv
        self._session_vol  = vol
        self._session_vwap = pv / vol if vol > 0 else 0

        # ── Build 5-min candles from 1-min data for bar_buffer ───────────────
        # Keeps warmup bars at the same 5-min granularity as the historical CSV
        # (Phase 1). Live candles arrive at 1-min from the WebSocket feed.
        rows_5m = self._aggregate_to_5min(bars_1m)
        for row in rows_5m:
            self.bar_buffer.append(row)
        self._session_bars = len(rows_5m)

        summary = (
            f"Today: {len(bars_1m)} 1-min bars → {len(rows_5m)} 5-min warmup bars  |  "
            f"ORB {'ready' if self._orb_ready else 'building'}"
        )
        detail = (
            f"1-min: {bars_1m[0]['ts'].strftime('%H:%M')}–{bars_1m[-1]['ts'].strftime('%H:%M')}  "
            f"VWAP: {self._session_vwap:.2f}  "
            f"H/L: {self._session_high:.2f}/{self._session_low:.2f}"
        )
        self._warmup_summary = summary
        self._warmup_detail  = detail
        logger.info(f"  Phase 2 complete: {summary}")

    def _aggregate_to_5min(self, bars_1m: list) -> list:
        """
        Aggregate 1-min bars into 5-min candles for the bar buffer.
        The still-forming bucket is dropped — the live feed emits that candle
        when it closes, so it is never fed to strategies half-built.
        """
        now = pd.Timestamp.now(tz=IST)
        buckets: dict = {}
        for b in bars_1m:
            ts  = b["ts"]
            m5  = (ts.minute // 5) * 5
            key = ts.replace(minute=m5, second=0, microsecond=0)
            if key + pd.Timedelta(minutes=5) > now:
                continue
            if key not in buckets:
                buckets[key] = {
                    "open": b["open"], "high": b["high"],
                    "low":  b["low"],  "close": b["close"], "volume": b["volume"],
                }
            else:
                buckets[key]["high"]    = max(buckets[key]["high"],   b["high"])
                buckets[key]["low"]     = min(buckets[key]["low"],    b["low"])
                buckets[key]["close"]   = b["close"]
                buckets[key]["volume"] += b["volume"]

        rows = []
        for ts in sorted(buckets.keys()):
            b = buckets[ts]
            rows.append(pd.Series({
                "open": b["open"], "high": b["high"],
                "low":  b["low"],  "close": b["close"], "volume": b["volume"],
            }, name=ts))
        return rows

    def _bars_to_series(self, bars_1m: list) -> list:
        """Convert 1-min bar dicts (from Upstox API) to pd.Series list for bar_buffer."""
        rows = []
        for b in sorted(bars_1m, key=lambda x: x["ts"]):
            rows.append(pd.Series({
                "open":   b["open"],  "high":   b["high"],
                "low":    b["low"],   "close":  b["close"],
                "volume": b["volume"],
            }, name=b["ts"]))
        return rows

    def _fetch_5min_today(self, hdrs: dict, encoded: str):
        """Fallback: fetch 5-min intraday candles if 1-min fails."""
        import requests
        from datetime import time as dtime

        url = f"https://api.upstox.com/v3/historical-candle/intraday/{encoded}/minutes/5"
        logger.info(f"  5-min intraday fallback: {url[:80]}...")
        resp = requests.get(url, headers=hdrs, timeout=15)
        if not resp.ok:
            logger.warning(
                f"  5-min intraday also failed: {resp.status_code} {resp.text[:100]}"
            )
            self._warmup_summary = "Phase 2 failed — using CSV warmup only"
            return

        candles = resp.json().get("data", {}).get("candles", [])
        if not candles:
            logger.info("  5-min intraday: no bars yet")
            self._warmup_summary = "Market not open yet"
            return

        rows = []
        for c in candles:
            ts = pd.Timestamp(c[0])
            if ts.tzinfo is None: ts = ts.tz_localize(IST)
            else:                 ts = ts.tz_convert(IST)
            if dtime(9, 15) <= ts.time() <= dtime(15, 25):
                rows.append(pd.Series({
                    "open": float(c[1]), "high": float(c[2]),
                    "low":  float(c[3]), "close": float(c[4]), "volume": float(c[5]),
                }, name=ts))
        rows.sort(key=lambda r: r.name)
        # Drop the still-forming candle (the live feed emits it on close)
        now  = pd.Timestamp.now(tz=IST)
        rows = [r for r in rows if r.name + pd.Timedelta(minutes=5) <= now]

        # Compute ORB from 5-min bars (first 6 = 09:15–09:44)
        orb_bars = [r for r in rows if dtime(9, 15) <= r.name.time() < dtime(9, 45)]
        if orb_bars:
            self._orb_high  = max(r["high"] for r in orb_bars)
            self._orb_low   = min(r["low"]  for r in orb_bars)
            self._orb_ready = True

        # Session OHLCV
        if rows:
            self._session_open = rows[0]["open"]
            self._session_high = max(r["high"]  for r in rows)
            self._session_low  = min(r["low"]   for r in rows)
            pv  = sum((r["high"] + r["low"] + r["close"]) / 3 * r["volume"] for r in rows)
            vol = sum(r["volume"] for r in rows)
            self._session_pv   = pv
            self._session_vol  = vol
            self._session_vwap = pv / vol if vol > 0 else 0

        for row in rows:
            self.bar_buffer.append(row)
        self._session_bars = len(rows)
        self._warmup_summary = (
            f"5-min: {len(rows)} bars | ORB {'ready' if self._orb_ready else 'building'}"
        )
        logger.info(
            f"  5-min fallback: {len(rows)} bars  ORB={'ready' if self._orb_ready else 'N/A'}"
        )

    # ── Live feed handlers ────────────────────────────────────────────────────

    def _on_tick(self, price: float, ts: pd.Timestamp):
        """
        Tick handler — two responsibilities:
          1. Update current_price for P&L display.
          2. Maintain a rolling 1-min bar accumulator so the dashboard always
             shows the live, partially-formed current candle without waiting
             for the next completed bar from the WebSocket feed.
        """
        with self._lock:
            self.current_price = price

            # Snap tick timestamp to its 1-min bucket
            bucket = ts.replace(second=0, microsecond=0)

            if self._cur_1m_ts is None:
                # First tick ever — open a new bar
                self._cur_1m_ts = bucket
                self._cur_1m_o  = price
                self._cur_1m_h  = price
                self._cur_1m_l  = price
                self._cur_1m_c  = price

            elif bucket != self._cur_1m_ts:
                # Minute has rolled — flush completed bar to history
                self._chart_bars_1m.append({
                    "ts": self._cur_1m_ts.isoformat(),
                    "o":  self._cur_1m_o, "h": self._cur_1m_h,
                    "l":  self._cur_1m_l, "c": self._cur_1m_c,
                    "phase": "live",
                })
                if len(self._chart_bars_1m) > 500:
                    self._chart_bars_1m = self._chart_bars_1m[-400:]

                # Start fresh bar for the new minute
                self._cur_1m_ts = bucket
                self._cur_1m_o  = price
                self._cur_1m_h  = price
                self._cur_1m_l  = price
                self._cur_1m_c  = price

            # Update OHLC of in-progress bar
            self._cur_1m_h = max(self._cur_1m_h, price)
            self._cur_1m_l = min(self._cur_1m_l, price)
            self._cur_1m_c = price

    def _on_spot_tick(self, price: float, ts: pd.Timestamp):
        """Record the latest spot price for the 5-min bucket it falls in (S5)."""
        m      = (ts.minute // self.BAR_MIN) * self.BAR_MIN
        bucket = ts.replace(minute=m, second=0, microsecond=0)
        with self._lock:
            self.spot_closes[bucket] = price
            if len(self.spot_closes) > 2000:
                for k in sorted(self.spot_closes)[:-1500]:
                    del self.spot_closes[k]

    def _on_candle(self, candle: pd.Series):
        with self._lock:
            self.bar_buffer.append(candle)
            if len(self.bar_buffer) > 500:
                self.bar_buffer = self.bar_buffer[-400:]

            # Update session OHLCV tracking
            c  = candle["close"]
            h  = candle["high"]
            lo = candle["low"]
            v  = candle.get("volume", 0)
            if self._session_open == 0:
                self._session_open = candle["open"]
            self._session_high = max(self._session_high, h)
            if self._session_low == float("inf"):
                self._session_low = lo
            else:
                self._session_low = min(self._session_low, lo)
            self._session_pv  += ((h + lo + c) / 3) * v
            self._session_vol += v
            self._session_vwap = (
                self._session_pv / self._session_vol if self._session_vol > 0 else 0
            )
            self._session_bars += 1

            if len(self.bar_buffer) < 3:
                return

            # Build indicator df — deduplicate timestamps (warmup + live overlap)
            df_base = pd.DataFrame(self.bar_buffer)
            if df_base.index.duplicated().any():
                df_base = df_base[~df_base.index.duplicated(keep="last")]

            # ── Evaluate each strategy ────────────────────────────────────────
            for s_key, (strat, state, add_ind) in self._strategies.items():
                try:
                    self._evaluate_strategy(
                        s_key, strat, state, add_ind,
                        df_base, candle,
                        intel=self._last_intel_out,
                    )
                except Exception as e:
                    logger.error(f"{s_key} error: {e}", exc_info=True)

    def _evaluate_strategy(self, s_key: str, strat, state: StrategyState,
                           add_ind, df_base: pd.DataFrame,
                           candle: pd.Series, intel=None):
        """Evaluate one strategy on the current bar."""

        # S5 needs spot data aligned — fall back to futures price if no spot feed
        if s_key == "S5":
            df = self._build_s5_df(df_base)
        else:
            df = add_ind(df_base.copy())

        bar      = df.iloc[-1]
        prev_bar = df.iloc[-2]
        ts       = candle.name

        # ── Check exits first ─────────────────────────────────────────────────
        if state.open_pos:
            exited = state.check_exits(bar, self.current_price, self.SQUARE_OFF)
            if exited:
                strat.on_exit("exit", self.current_price, ts)

        # ── Generate new signal (only when flat) ─────────────────────────────
        if not state.open_pos:
            # Intelligence gate — regime / event-risk filter
            if intel is not None and intel.is_valid:
                if not intel.allow_trading():
                    return  # event risk / extreme macro conditions
                if intel.regime and intel.regime.mult(s_key) < 0.4:
                    return  # this strategy is unsuitable for current regime

            context = {
                "in_position":  False,
                "position_dir": None,
                "session_bar":  len(self.bar_buffer),
                "day_pnl":      state.session_pnl,
                "capital":      state.capital,
                "new_day":      False,
                "intel":        intel,   # IntelligenceOutput or None
            }
            sig = strat.on_bar(bar, prev_bar, context)

            if sig.direction in ("LONG", "SHORT") and sig.sl_dist > 0:
                close = float(bar["close"])
                tick  = 0.05
                # Extract ATR for trailing stop context
                atr = float(bar.get("atr", bar.get("ATR", sig.sl_dist / max(state.atr_sl_mult, 0.1))))

                if sig.direction == "LONG":
                    entry  = round(close + tick, 2)
                    sl     = round(entry - sig.sl_dist, 2)
                    target = round(entry + sig.sl_dist * sig.rr, 2)
                else:
                    entry  = round(close - tick, 2)
                    sl     = round(entry + sig.sl_dist, 2)
                    target = round(entry - sig.sl_dist * sig.rr, 2)

                state.enter(sig.direction, entry, sl, target, ts,
                            sl_dist=sig.sl_dist, atr=atr)
                strat.on_fill(sig.direction, entry, ts)
            else:
                # Log rejection reason at DEBUG level for diagnostics
                logger.debug(
                    f"  [{state.label}] NONE — {sig.reason or 'no signal'}"
                )

        elif state.open_pos:
            # Notify strategy it's in position (for strategies that track internally)
            context = {
                "in_position":  True,
                "position_dir": state.open_pos.direction,
                "session_bar":  len(self.bar_buffer),
                "day_pnl":      state.session_pnl,
                "capital":      state.capital,
                "new_day":      False,
            }
            strat.on_bar(bar, prev_bar, context)  # lets strategy update trackers

    def _build_s5_df(self, df_base: pd.DataFrame) -> pd.DataFrame:
        """Build S5 DataFrame with basis features from spot buffer."""
        from strategies.strategy_5.strategy import add_indicators as ai5
        from strategies.strategy_5.config import RISK_FREE_RATE, BASIS_ZSCORE_WINDOW
        from strategies.strategy_5.data_loader import _compute_days_to_expiry

        df = df_base.copy()
        df = df[~df.index.duplicated(keep="last")]

        # Spot close for the same 5-min bar. Bars with no spot stay NaN, so
        # their basis/z-score is NaN and S5 reports "warming up" instead of
        # trading on a made-up basis.
        spot = pd.Series(self.spot_closes, dtype=float).sort_index()
        df["spot_close"] = spot.reindex(df.index)

        df["days_to_expiry"] = _compute_days_to_expiry(df.index)
        df["basis"]          = df["close"] - df["spot_close"]
        df["fair_basis"]     = df["spot_close"] * RISK_FREE_RATE * (df["days_to_expiry"] / 365)
        df["basis_spread"]   = df["basis"] - df["fair_basis"]

        roll_mean = df["basis_spread"].rolling(BASIS_ZSCORE_WINDOW, min_periods=20).mean()
        roll_std  = df["basis_spread"].rolling(BASIS_ZSCORE_WINDOW, min_periods=20).std()
        df["basis_zscore"] = (
            (df["basis_spread"] - roll_mean) / roll_std.replace(0, np.nan)
        )

        return ai5(df)

    def manual_trade(self, s_key: str, direction: str) -> dict:
        """
        Force-enter or exit a position manually.
        direction: "LONG" | "SHORT" | "EXIT"
        Uses strategy's ATR_SL_MULT × current ATR for SL distance.
        Returns {ok, message}
        """
        with self._lock:
            if s_key not in self._strategies:
                return {"ok": False, "message": f"Unknown strategy {s_key}"}

            strat, state, add_ind = self._strategies[s_key]
            ts = datetime.now(IST)

            # EXIT branch
            if direction == "EXIT":
                if not state.open_pos:
                    return {"ok": False, "message": f"{s_key} has no open position"}
                state.exit(self.current_price, "manual-exit", ts)
                return {"ok": True, "message": f"{s_key} exited @ {self.current_price:.2f}"}

            # LONG / SHORT entry
            if state.open_pos:
                return {"ok": False, "message": f"{s_key} already in position ({state.status})"}

            if self.current_price <= 0:
                return {"ok": False, "message": "No live price yet"}

            # Compute ATR from bar buffer for SL sizing
            atr = 10.0   # fallback
            if len(self.bar_buffer) >= 15:
                try:
                    closes = np.array([float(b["close"]) for b in self.bar_buffer[-15:]])
                    highs  = np.array([float(b["high"])  for b in self.bar_buffer[-15:]])
                    lows   = np.array([float(b["low"])   for b in self.bar_buffer[-15:]])
                    tr     = np.maximum(highs[1:] - lows[1:],
                             np.maximum(np.abs(highs[1:] - closes[:-1]),
                                        np.abs(lows[1:]  - closes[:-1])))
                    atr = float(np.mean(tr[-14:]))
                except Exception:
                    pass

            sl_dist = round(state.atr_sl_mult * atr, 2)
            rr      = 1.3   # 1:1.3 RR for manual trades
            price   = self.current_price

            if direction == "LONG":
                entry  = round(price + 0.05, 2)
                sl     = round(entry - sl_dist, 2)
                target = round(entry + sl_dist * rr, 2)
            else:
                entry  = round(price - 0.05, 2)
                sl     = round(entry + sl_dist, 2)
                target = round(entry - sl_dist * rr, 2)

            state.enter(direction, entry, sl, target, ts,
                        sl_dist=sl_dist, atr=atr)
            state._log(f"👇 MANUAL {direction} @ {entry:.2f}")
            return {
                "ok": True,
                "message": f"{s_key} manual {direction} @ {entry:.2f}  SL {sl:.2f}  TGT {target:.2f}"
            }

    def _build_s5_df_placeholder(self):
        pass  # marker — real method above

    # ── State for dashboard ───────────────────────────────────────────────────

    def get_state(self) -> dict:
        with self._lock:
            price    = self.current_price
            strategies = {
                k: state.snapshot(price)
                for k, (_, state, _) in self._strategies.items()
            }
            total_pnl    = sum(s["session_pnl"] for s in strategies.values())
            total_cap    = sum(s["capital"]      for s in strategies.values())
            total_trades = sum(s["n_trades"]     for s in strategies.values())

            session_ohlcv = {
                "open":     self._session_open,
                "high":     max(self._session_high, price) if price else self._session_high,
                "low":      (
                    min(self._session_low, price)
                    if price and self._session_low < float("inf")
                    else self._session_low
                ),
                "vwap":     round(self._session_vwap, 2),
                "orb_high": self._orb_high if self._orb_ready else None,
                "orb_low":  (
                    self._orb_low
                    if self._orb_ready and self._orb_low < float("inf")
                    else None
                ),
                "n_bars":   self._session_bars,
            }

            # Intelligence summary for dashboard (safe — intel may be None)
            intel_summary = None
            if self._last_intel_out is not None:
                try:
                    intel_summary = {
                        "regime":       getattr(self._last_intel_out.regime, "description", None),
                        "allow_trading": self._last_intel_out.allow_trading(),
                        "is_valid":     self._last_intel_out.is_valid,
                    }
                except Exception:
                    intel_summary = None

            return {
                "status":        self.status,
                "current_price": round(price, 2),
                "total_pnl":     round(total_pnl, 0),
                "total_capital": round(total_cap, 0),
                "total_trades":  total_trades,
                "strategies":    strategies,
                "session_ohlcv": session_ohlcv,
                "warmup": {
                    "summary": self._warmup_summary,
                    "detail":  self._warmup_detail,
                },
                "chart_1m":      self._get_chart_data(),
                "intel":         intel_summary,
                "timestamp":     datetime.now(IST).strftime("%H:%M:%S"),
            }

    def _get_chart_data(self) -> list:
        """
        Combine historical 1-min bars (warmup + completed live bars) with the
        current tick-level incomplete 1-min bar.

        Filters to 09:30 IST onwards so the dashboard chart doesn't start with
        a big gap from pre-session warmup candles.
        """
        now_ist    = datetime.now(IST)
        start_time = now_ist.replace(hour=9, minute=30, second=0, microsecond=0)

        data = [
            b for b in self._chart_bars_1m
            if pd.Timestamp(b["ts"]) >= start_time
        ]

        # Append the current in-progress 1-min bar (built from ticks)
        cur_ts = self._cur_1m_ts
        if cur_ts is not None and cur_ts >= start_time:
            data.append({
                "ts": cur_ts.isoformat(),
                "o":  self._cur_1m_o, "h": self._cur_1m_h,
                "l":  self._cur_1m_l, "c": self._cur_1m_c,
                "phase": "live",
            })

        return data

    def _print_summary(self):
        logger.info("=" * 56)
        logger.info("SESSION SUMMARY")
        logger.info("=" * 56)
        for k, (_, state, _) in self._strategies.items():
            logger.info(
                f"  {state.label:<8} "
                f"trades={len(state.trades):>3}  "
                f"P&L=₹{state.session_pnl:>+8,.0f}  "
                f"WR={state.win_rate():.0f}%"
            )
        logger.info("=" * 56)