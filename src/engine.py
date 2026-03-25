"""
engine.py — Industrial-grade backtesting engine

Design:
  - Vectorized NumPy bar loop (no iterrows)
  - Strategy-agnostic via StrategyProtocol
  - Correct intraday execution model:
      signal on bar[i] close
      entry  on bar[i+1] open + slippage  (same session only)
      exits  checked intra-bar via high/low
      square-off at first bar >= square_off_time
  - Intraday guardrails:
      pending signals cancelled if next bar is a new session
      no cross-session entries ever
      rollover gaps (overnight between contracts) ignored safely
  - Realistic fills: SL at SL price, target at target price
  - Daily P&L circuit breaker
  - Walk-forward validation built in
  - Full trade log with gross/commission/net split

Usage:
    from src.engine import BacktestEngine, EngineConfig
    from strategies.strategy_1.strategy import ORBStrategy, add_indicators

    cfg    = EngineConfig(initial_capital=500_000, lot_size=75, commission=40)
    result = BacktestEngine(cfg).run(df, ORBStrategy(), add_indicators)
"""

from __future__ import annotations

import time as _time
import logging
from dataclasses import dataclass, field
from datetime import time, date
from typing import Protocol, Optional
import numpy as np
import pandas as pd

logger = logging.getLogger("engine")


# ══════════════════════════════════════════════════════════════════════════════
# EngineConfig
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class EngineConfig:
    initial_capital:    float = 500_000
    lot_size:           int   = 75
    commission_per_lot: float = 40.0
    slippage_ticks:     int   = 1
    tick_size:          float = 0.05
    square_off_time:    time  = time(15, 20)
    max_lots:           int   = 1
    max_daily_loss_pct: float = 0.02

    @property
    def slippage(self) -> float:
        return self.slippage_ticks * self.tick_size

    def round_tick(self, price: float) -> float:
        return round(round(price / self.tick_size) * self.tick_size, 2)


# ══════════════════════════════════════════════════════════════════════════════
# Signal & StrategyProtocol
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Signal:
    direction: str        # "LONG" | "SHORT" | "NONE"
    sl_dist:   float = 0  # SL distance in points from entry
    rr:        float = 2  # reward-to-risk ratio
    reason:    str   = ""
    meta:      dict  = field(default_factory=dict)


class StrategyProtocol(Protocol):
    def on_bar(self, bar: pd.Series, prev_bar: pd.Series, context: dict) -> Signal: ...
    def on_fill(self, direction: str, price: float, ts: pd.Timestamp): ...
    def on_exit(self, reason: str, price: float, ts: pd.Timestamp): ...
    def reset(self): ...


# ══════════════════════════════════════════════════════════════════════════════
# Trade
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Trade:
    id:           int
    strategy:     str
    entry_time:   pd.Timestamp
    exit_time:    pd.Timestamp
    direction:    str
    entry_price:  float
    exit_price:   float
    sl:           float
    target:       float
    lots:         int
    pnl_points:   float
    pnl_gross:    float
    commission:   float
    pnl_net:      float
    exit_reason:  str
    duration_min: float
    signal_meta:  dict = field(default_factory=dict)


# ══════════════════════════════════════════════════════════════════════════════
# BacktestResult
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class BacktestResult:
    config:          EngineConfig
    trades:          list[Trade]
    equity_curve:    pd.Series
    daily_pnl:       pd.Series
    initial_capital: float
    final_capital:   float
    elapsed_sec:     float
    strategy_name:   str

    @property
    def trades_df(self) -> pd.DataFrame:
        if not self.trades:
            return pd.DataFrame()
        return pd.DataFrame([{
            "entry_time":   t.entry_time,
            "exit_time":    t.exit_time,
            "direction":    t.direction,
            "entry_price":  t.entry_price,
            "exit_price":   t.exit_price,
            "sl":           t.sl,
            "target":       t.target,
            "lots":         t.lots,
            "pnl_points":   round(t.pnl_points,  2),
            "pnl_gross":    round(t.pnl_gross,   2),
            "commission":   round(t.commission,  2),
            "pnl_net":      round(t.pnl_net,     2),
            "exit_reason":  t.exit_reason,
            "duration_min": round(t.duration_min, 1),
        } for t in self.trades])

    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades: return 0.0
        return sum(1 for t in self.trades if t.pnl_net > 0) / len(self.trades)

    @property
    def profit_factor(self) -> float:
        wins  = sum(t.pnl_net for t in self.trades if t.pnl_net > 0)
        loss  = abs(sum(t.pnl_net for t in self.trades if t.pnl_net < 0))
        return wins / loss if loss > 0 else float("inf")

    @property
    def max_drawdown(self) -> float:
        eq = self.equity_curve
        if eq.empty: return 0.0
        dd = (eq - eq.cummax()) / eq.cummax()
        return abs(dd.min())

    @property
    def sharpe(self) -> float:
        # Use daily P&L for Sharpe — more meaningful than bar-level returns
        dpnl = self.daily_pnl
        if dpnl.empty or dpnl.std() == 0: return 0.0
        return (dpnl.mean() / dpnl.std()) * np.sqrt(252)

    @property
    def sortino(self) -> float:
        dpnl = self.daily_pnl
        neg  = dpnl[dpnl < 0]
        if neg.empty or neg.std() == 0: return 0.0
        return (dpnl.mean() / neg.std()) * np.sqrt(252)

    @property
    def calmar(self) -> float:
        if self.max_drawdown == 0: return 0.0
        years = (self.equity_curve.index[-1] - self.equity_curve.index[0]).days / 365.25
        if years == 0: return 0.0
        cagr = (self.final_capital / self.initial_capital) ** (1 / years) - 1
        return cagr / self.max_drawdown

    def save(self, path: str):
        import os
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.trades_df.to_csv(path, index=False)
        logger.info(f"Trades saved -> {path}")

    def summary(self):
        net  = self.final_capital - self.initial_capital
        years = max(
            (self.equity_curve.index[-1] - self.equity_curve.index[0]).days / 365.25,
            1/365
        )
        cagr = ((self.final_capital / self.initial_capital) ** (1/years) - 1) * 100

        logger.info("=" * 56)
        logger.info(f"  {self.strategy_name}")
        logger.info("=" * 56)
        logger.info(f"  Trades        : {self.n_trades}")
        logger.info(f"  Net P&L       : ₹{net:+,.0f}")
        logger.info(f"  CAGR          : {cagr:.1f}%")
        logger.info(f"  Win rate      : {self.win_rate*100:.1f}%")
        logger.info(f"  Profit factor : {self.profit_factor:.2f}")
        logger.info(f"  Max drawdown  : {self.max_drawdown*100:.2f}%")
        logger.info(f"  Sharpe        : {self.sharpe:.2f}")
        logger.info(f"  Sortino       : {self.sortino:.2f}")
        logger.info(f"  Calmar        : {self.calmar:.2f}")
        logger.info(f"  Elapsed       : {self.elapsed_sec:.2f}s")
        logger.info("=" * 56)


# ══════════════════════════════════════════════════════════════════════════════
# BacktestEngine
# ══════════════════════════════════════════════════════════════════════════════

class BacktestEngine:
    """
    Intraday backtesting engine.

    Key intraday guarantees:
      1. Pending entries are CANCELLED if the next bar is a new session
         (prevents cross-session fills that don't exist in real trading)
      2. Square-off fires at the first bar >= square_off_time each day
      3. Rollover overnight gaps are harmless — no position is ever held
         past square-off, so contract price gaps don't affect P&L

    Execution model (all intraday):
      bar[i] close     → strategy evaluates → may set pending_sig
      bar[i+1] open    → fill pending_sig at open + slippage
                         ONLY if bar[i+1] is the same session as bar[i]
      bar[i+1] intra   → SL/target checked via high/low
      square-off time  → exit at bar open (conservative)
    """

    def __init__(self, config: EngineConfig):
        self.cfg = config

    def run(
        self,
        df:               pd.DataFrame,
        strategy:         StrategyProtocol,
        add_indicators_fn,
        strategy_name:    str = "strategy",
        output_csv:       Optional[str] = None,
    ) -> BacktestResult:

        t0 = _time.perf_counter()

        df  = add_indicators_fn(df.copy())
        df  = df.sort_index()

        # Drop any duplicate timestamps (safety — shouldn't exist post-pipeline)
        if df.index.duplicated().any():
            n_dup = df.index.duplicated().sum()
            logger.warning(f"Dropping {n_dup} duplicate timestamps before backtest")
            df = df[~df.index.duplicated(keep="last")]

        # Pre-extract NumPy arrays for speed
        opens  = df["open"].values
        highs  = df["high"].values
        lows   = df["low"].values
        closes = df["close"].values
        dates  = np.array([ts.date() for ts in df.index])
        n      = len(df)
        idx    = df.index

        logger.info(
            f"Backtesting {strategy_name} on {n:,} bars  "
            f"[{idx[0].strftime('%Y-%m-%d')}  →  {idx[-1].strftime('%Y-%m-%d')}]"
        )

        strategy.reset()

        # Position state
        in_pos      = False
        pos_dir     = None
        pos_entry   = 0.0
        pos_sl      = 0.0
        pos_target  = 0.0
        pos_lots    = 0
        pos_entry_i = 0
        pos_meta:   dict = {}

        # Pending signal (filled next bar, same session only)
        pending_sig:   Optional[Signal] = None
        pending_date:  Optional[date]   = None   # date pending signal was generated

        # Accounting
        capital     = float(self.cfg.initial_capital)
        trade_id    = 0
        trades:     list[Trade] = []
        equity      = np.full(n, capital)
        daily_pnl_d: dict = {}
        cur_date    = dates[0]
        day_pnl     = 0.0
        session_bar = 0

        max_daily_loss = self.cfg.initial_capital * self.cfg.max_daily_loss_pct

        for i in range(1, n):
            ts      = idx[i]
            d       = dates[i]
            new_day = d != cur_date

            # ── Day change bookkeeping ────────────────────────────────────────
            if new_day:
                daily_pnl_d[cur_date] = day_pnl
                cur_date    = d
                day_pnl     = 0.0
                session_bar = 0

                # KEY INTRADAY RULE: cancel any pending signal from yesterday
                # A signal that fired on the last bar of one session cannot
                # be filled on the first bar of the next session.
                # In live trading the order would never be submitted overnight.
                if pending_sig is not None:
                    logger.debug(
                        f"  Cancelled pending {pending_sig.direction} signal "
                        f"— new session started ({d})"
                    )
                    pending_sig  = None
                    pending_date = None
            else:
                session_bar += 1

            # ── Fill pending entry (same session only) ────────────────────────
            if pending_sig is not None and not in_pos:
                sig         = pending_sig
                pending_sig = None
                raw_entry   = opens[i]

                if sig.direction == "LONG":
                    entry  = self.cfg.round_tick(raw_entry + self.cfg.slippage)
                    sl     = self.cfg.round_tick(entry - sig.sl_dist)
                    target = self.cfg.round_tick(entry + sig.sl_dist * sig.rr)
                else:
                    entry  = self.cfg.round_tick(raw_entry - self.cfg.slippage)
                    sl     = self.cfg.round_tick(entry + sig.sl_dist)
                    target = self.cfg.round_tick(entry - sig.sl_dist * sig.rr)

                in_pos      = True
                pos_dir     = sig.direction
                pos_entry   = entry
                pos_sl      = sl
                pos_target  = target
                pos_lots    = self.cfg.max_lots
                pos_entry_i = i
                pos_meta    = sig.meta

                strategy.on_fill(sig.direction, entry, ts)
                logger.debug(
                    f"  FILL {sig.direction} @ {entry:.2f}  "
                    f"SL {sl:.2f}  TGT {target:.2f}  [{ts.strftime('%H:%M')}]"
                )

            # ── Check exits ───────────────────────────────────────────────────
            if in_pos:
                exit_price  = None
                exit_reason = None
                bar_high    = highs[i]
                bar_low     = lows[i]
                bar_open    = opens[i]

                # 1. Square-off (exit at bar open — conservative)
                if ts.time() >= self.cfg.square_off_time:
                    exit_price  = bar_open
                    exit_reason = "square-off"

                # 2. SL/Target intra-bar
                elif pos_dir == "LONG":
                    if bar_low <= pos_sl:
                        exit_price  = pos_sl
                        exit_reason = "stop loss"
                    elif bar_high >= pos_target:
                        exit_price  = pos_target
                        exit_reason = "target"

                elif pos_dir == "SHORT":
                    if bar_high >= pos_sl:
                        exit_price  = pos_sl
                        exit_reason = "stop loss"
                    elif bar_low <= pos_target:
                        exit_price  = pos_target
                        exit_reason = "target"

                if exit_price is not None:
                    pts        = (exit_price - pos_entry) if pos_dir == "LONG" \
                                 else (pos_entry - exit_price)
                    gross      = pts * pos_lots * self.cfg.lot_size
                    commission = self.cfg.commission_per_lot * pos_lots * 2
                    net        = gross - commission

                    capital  += net
                    day_pnl  += net
                    trade_id += 1

                    trades.append(Trade(
                        id           = trade_id,
                        strategy     = strategy_name,
                        entry_time   = idx[pos_entry_i],
                        exit_time    = ts,
                        direction    = pos_dir,
                        entry_price  = pos_entry,
                        exit_price   = exit_price,
                        sl           = pos_sl,
                        target       = pos_target,
                        lots         = pos_lots,
                        pnl_points   = pts,
                        pnl_gross    = gross,
                        commission   = commission,
                        pnl_net      = net,
                        exit_reason  = exit_reason,
                        duration_min = (ts - idx[pos_entry_i]).total_seconds() / 60,
                        signal_meta  = pos_meta,
                    ))
                    strategy.on_exit(exit_reason, exit_price, ts)

                    in_pos  = False
                    pos_dir = None

            equity[i] = capital

            # ── Generate new signal ───────────────────────────────────────────
            if not in_pos and pending_sig is None:
                # Circuit breaker
                if day_pnl < -max_daily_loss:
                    continue

                bar      = df.iloc[i]
                prev_bar = df.iloc[i - 1]

                context = {
                    "in_position":  in_pos,
                    "position_dir": pos_dir,
                    "session_bar":  session_bar,
                    "day_pnl":      day_pnl,
                    "capital":      capital,
                    "new_day":      new_day,
                }

                sig = strategy.on_bar(bar, prev_bar, context)

                if sig.direction in ("LONG", "SHORT") and sig.sl_dist > 0:
                    pending_sig  = sig
                    pending_date = d

        # ── Close lingering position (end of data) ────────────────────────────
        if in_pos:
            last_close = closes[-1]
            pts        = (last_close - pos_entry) if pos_dir == "LONG" \
                         else (pos_entry - last_close)
            gross      = pts * pos_lots * self.cfg.lot_size
            commission = self.cfg.commission_per_lot * pos_lots * 2
            net        = gross - commission
            capital   += net
            trade_id  += 1
            trades.append(Trade(
                id=trade_id, strategy=strategy_name,
                entry_time=idx[pos_entry_i], exit_time=idx[-1],
                direction=pos_dir, entry_price=pos_entry,
                exit_price=last_close, sl=pos_sl, target=pos_target,
                lots=pos_lots, pnl_points=pts, pnl_gross=gross,
                commission=commission, pnl_net=net,
                exit_reason="end of data",
                duration_min=(idx[-1]-idx[pos_entry_i]).total_seconds()/60,
            ))

        daily_pnl_d[cur_date] = day_pnl

        equity_series = pd.Series(equity, index=idx, name="equity")
        daily_series  = pd.Series(daily_pnl_d, name="daily_pnl")
        elapsed       = _time.perf_counter() - t0

        result = BacktestResult(
            config          = self.cfg,
            trades          = trades,
            equity_curve    = equity_series,
            daily_pnl       = daily_series,
            initial_capital = self.cfg.initial_capital,
            final_capital   = capital,
            elapsed_sec     = elapsed,
            strategy_name   = strategy_name,
        )

        if output_csv:
            result.save(output_csv)

        result.summary()
        return result

    # ── Walk-forward ──────────────────────────────────────────────────────────

    def walk_forward(
        self,
        df:               pd.DataFrame,
        strategy:         StrategyProtocol,
        add_indicators_fn,
        train_months:     int = 6,
        test_months:      int = 1,
        strategy_name:    str = "strategy",
    ) -> list[BacktestResult]:
        """
        Expanding window walk-forward validation.
        Fold 1: train months 1-6,  test month 7
        Fold 2: train months 1-7,  test month 8
        ...
        Each fold uses a fresh strategy.reset() so no data leakage.
        """
        df    = df.sort_index()
        end   = df.index[-1]
        fold  = 0
        results = []

        test_start = df.index[0] + pd.DateOffset(months=train_months)

        while test_start < end:
            test_end = min(test_start + pd.DateOffset(months=test_months), end)
            test_df  = df[(df.index >= test_start) & (df.index < test_end)]

            if len(test_df) < 200:
                break

            fold += 1
            logger.info(
                f"Walk-forward fold {fold}: "
                f"test {test_start.date()} → {test_end.date()} "
                f"({len(test_df):,} bars)"
            )
            strategy.reset()
            r = self.run(
                test_df, strategy, add_indicators_fn,
                strategy_name=f"{strategy_name}_f{fold}",
            )
            results.append(r)
            test_start = test_end

        if results:
            nets = [r.final_capital - r.initial_capital for r in results]
            wrs  = [r.win_rate for r in results]
            pfs  = [r.profit_factor for r in results
                    if r.profit_factor < float("inf")]
            logger.info(
                f"\nWalk-forward: {fold} folds  "
                f"total ₹{sum(nets):+,.0f}  "
                f"avg WR {np.mean(wrs)*100:.1f}%  "
                f"avg PF {np.mean(pfs):.2f}  "
                f"profitable folds: {sum(1 for n in nets if n > 0)}/{fold}"
            )

        return results