"""
Analytics Engine
Computes performance metrics and generates charts from BacktestResult.
"""

import os
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from typing import Optional

from config import INITIAL_CAPITAL, OUTPUTS_DIR, EQUITY_CURVE_PNG, PERFORMANCE_TXT
from utils import get_logger

logger = get_logger("analytics")


# ─── Metrics ──────────────────────────────────────────────────────────────────

def compute_metrics(trades_df: pd.DataFrame, equity_curve: pd.Series) -> dict:
    """Compute full performance metrics from trades and equity curve."""
    if trades_df.empty:
        return {"error": "No trades to analyse"}

    pnl = trades_df["pnl_net"]
    wins  = pnl[pnl > 0]
    losses = pnl[pnl < 0]

    total_trades   = len(trades_df)
    win_rate       = len(wins) / total_trades if total_trades else 0
    avg_win        = wins.mean()  if len(wins)   else 0
    avg_loss       = losses.mean() if len(losses) else 0
    expectancy     = (win_rate * avg_win) + ((1 - win_rate) * avg_loss)
    profit_factor  = abs(wins.sum() / losses.sum()) if losses.sum() != 0 else np.inf

    # Returns series from equity
    returns        = equity_curve.pct_change().dropna()
    trading_days   = max(1, (equity_curve.index[-1] - equity_curve.index[0]).days)
    annual_factor  = 252 * 75  # approx bars per year for 5-min IST session

    sharpe  = (returns.mean() / returns.std() * np.sqrt(annual_factor)
               if returns.std() != 0 else 0)
    neg_ret = returns[returns < 0]
    sortino = (returns.mean() / neg_ret.std() * np.sqrt(annual_factor)
               if len(neg_ret) > 0 and neg_ret.std() != 0 else 0)

    # Drawdown
    rolling_max  = equity_curve.cummax()
    drawdown     = (equity_curve - rolling_max) / rolling_max
    max_dd       = drawdown.min()
    max_dd_pct   = abs(max_dd) * 100

    cagr = ((equity_curve.iloc[-1] / INITIAL_CAPITAL) ** (365 / trading_days) - 1
            if trading_days > 0 else 0)
    calmar = (cagr / abs(max_dd) if max_dd != 0 else 0)

    # Consecutive losses
    win_flags = (pnl > 0).astype(int).tolist()
    max_consec_loss = _max_consecutive(win_flags, 0)

    avg_duration = trades_df["duration_min"].mean() if "duration_min" in trades_df else 0

    return {
        # Profit
        "total_pnl":           pnl.sum(),
        "total_trades":        total_trades,
        "win_rate":            win_rate,
        "avg_win":             avg_win,
        "avg_loss":            avg_loss,
        "expectancy":          expectancy,
        "profit_factor":       profit_factor,
        # Risk
        "max_drawdown_pct":    max_dd_pct,
        "sharpe_ratio":        sharpe,
        "sortino_ratio":       sortino,
        "calmar_ratio":        calmar,
        "cagr_pct":            cagr * 100,
        # Trade stats
        "avg_duration_min":    avg_duration,
        "max_consec_losses":   max_consec_loss,
        "long_trades":         len(trades_df[trades_df["direction"] == "LONG"]),
        "short_trades":        len(trades_df[trades_df["direction"] == "SHORT"]),
    }


def _max_consecutive(flags: list, value: int) -> int:
    max_run = current = 0
    for f in flags:
        if f == value:
            current += 1
            max_run = max(max_run, current)
        else:
            current = 0
    return max_run


# ─── Charts ───────────────────────────────────────────────────────────────────

def plot_analytics(
    trades_df: pd.DataFrame,
    equity_curve: pd.Series,
    save_path: str = EQUITY_CURVE_PNG,
):
    """Generate equity curve, drawdown, and PnL histogram."""
    os.makedirs(OUTPUTS_DIR, exist_ok=True)

    fig = plt.figure(figsize=(14, 10), facecolor="#0d1117")
    gs  = gridspec.GridSpec(3, 2, figure=fig, hspace=0.45, wspace=0.35)

    _style = {
        "axes.facecolor":   "#161b22",
        "axes.edgecolor":   "#30363d",
        "axes.labelcolor":  "#8b949e",
        "xtick.color":      "#8b949e",
        "ytick.color":      "#8b949e",
        "grid.color":       "#21262d",
        "text.color":       "#c9d1d9",
        "figure.facecolor": "#0d1117",
    }

    plt.rcParams.update(_style)

    # ── Equity curve ──────────────────────────────────────────────────────
    ax1 = fig.add_subplot(gs[0, :])
    ax1.plot(equity_curve.index, equity_curve.values, color="#58a6ff", linewidth=1.2)
    ax1.axhline(INITIAL_CAPITAL, color="#8b949e", linewidth=0.7, linestyle="--")
    ax1.fill_between(
        equity_curve.index, INITIAL_CAPITAL, equity_curve.values,
        where=equity_curve.values >= INITIAL_CAPITAL,
        alpha=0.15, color="#3fb950"
    )
    ax1.fill_between(
        equity_curve.index, INITIAL_CAPITAL, equity_curve.values,
        where=equity_curve.values < INITIAL_CAPITAL,
        alpha=0.15, color="#f85149"
    )
    ax1.set_title("Equity Curve", color="#e6edf3", fontsize=12)
    ax1.set_ylabel("Portfolio Value (₹)")
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"₹{x:,.0f}"))
    ax1.grid(True, alpha=0.4)

    # ── Drawdown ──────────────────────────────────────────────────────────
    ax2 = fig.add_subplot(gs[1, :])
    rolling_max = equity_curve.cummax()
    drawdown    = (equity_curve - rolling_max) / rolling_max * 100
    ax2.fill_between(drawdown.index, drawdown.values, 0, color="#f85149", alpha=0.6)
    ax2.plot(drawdown.index, drawdown.values, color="#f85149", linewidth=0.8)
    ax2.set_title("Drawdown %", color="#e6edf3", fontsize=12)
    ax2.set_ylabel("Drawdown (%)")
    ax2.grid(True, alpha=0.4)

    if trades_df.empty:
        plt.savefig(save_path, dpi=150, bbox_inches="tight")
        logger.info(f"Chart saved → {save_path}")
        plt.close()
        return

    # ── PnL histogram ─────────────────────────────────────────────────────
    ax3 = fig.add_subplot(gs[2, 0])
    wins_   = trades_df[trades_df["pnl_net"] > 0]["pnl_net"]
    losses_ = trades_df[trades_df["pnl_net"] < 0]["pnl_net"]
    ax3.hist(wins_,   bins=20, color="#3fb950", alpha=0.7, label="Wins")
    ax3.hist(losses_, bins=20, color="#f85149", alpha=0.7, label="Losses")
    ax3.axvline(0, color="#8b949e", linewidth=0.8)
    ax3.set_title("PnL Distribution", color="#e6edf3", fontsize=12)
    ax3.set_xlabel("Net PnL (₹)")
    ax3.legend(facecolor="#161b22", edgecolor="#30363d")
    ax3.grid(True, alpha=0.4)

    # ── Monthly PnL bars ──────────────────────────────────────────────────
    ax4 = fig.add_subplot(gs[2, 1])
    if "exit_time" in trades_df.columns and not trades_df["exit_time"].isna().all():
        trades_df = trades_df.copy()
        trades_df["month"] = pd.to_datetime(
            trades_df["exit_time"]
        ).dt.to_period("M")
        monthly = trades_df.groupby("month")["pnl_net"].sum()
        colors  = ["#3fb950" if v > 0 else "#f85149" for v in monthly.values]
        ax4.bar(monthly.index.astype(str), monthly.values, color=colors, alpha=0.8)
        ax4.set_title("Monthly PnL", color="#e6edf3", fontsize=12)
        ax4.set_xlabel("Month")
        ax4.set_ylabel("Net PnL (₹)")
        ax4.tick_params(axis="x", rotation=45)
        ax4.grid(True, alpha=0.3)

    plt.savefig(save_path, dpi=150, bbox_inches="tight")
    logger.info(f"Chart saved → {save_path}")
    plt.close()


# ─── Text report ──────────────────────────────────────────────────────────────

def print_report(metrics: dict) -> str:
    lines = [
        "=" * 50,
        "  NIFTY FUTURES BACKTEST REPORT",
        "=" * 50,
        "",
        "-- Profit Metrics " + "-" * 32,
        f"  Total Net PnL        ₹{metrics.get('total_pnl', 0):>12,.0f}",
        f"  Total Trades         {metrics.get('total_trades', 0):>12}",
        f"  Win Rate             {metrics.get('win_rate', 0) * 100:>11.1f}%",
        f"  Avg Win              ₹{metrics.get('avg_win', 0):>12,.0f}",
        f"  Avg Loss             ₹{metrics.get('avg_loss', 0):>12,.0f}",
        f"  Expectancy           ₹{metrics.get('expectancy', 0):>12,.0f}",
        f"  Profit Factor        {metrics.get('profit_factor', 0):>12.2f}",
        "",
        "-- Risk Metrics " + "-" * 34,
        f"  Max Drawdown         {metrics.get('max_drawdown_pct', 0):>11.2f}%",
        f"  Sharpe Ratio         {metrics.get('sharpe_ratio', 0):>12.2f}",
        f"  Sortino Ratio        {metrics.get('sortino_ratio', 0):>12.2f}",
        f"  Calmar Ratio         {metrics.get('calmar_ratio', 0):>12.2f}",
        f"  CAGR                 {metrics.get('cagr_pct', 0):>11.2f}%",
        "",
        "-- Trade Metrics " + "-" * 33,
        f"  Avg Duration         {metrics.get('avg_duration_min', 0):>9.1f} min",
        f"  Max Consec Losses    {metrics.get('max_consec_losses', 0):>12}",
        f"  Long Trades          {metrics.get('long_trades', 0):>12}",
        f"  Short Trades         {metrics.get('short_trades', 0):>12}",
        "",
        "=" * 50,
    ]
    report = "\n".join(lines)
    print(report)

    os.makedirs(OUTPUTS_DIR, exist_ok=True)
    with open(PERFORMANCE_TXT, "w", encoding="utf-8") as f:
        f.write(report)
    logger.info(f"Report saved → {PERFORMANCE_TXT}")
    return report