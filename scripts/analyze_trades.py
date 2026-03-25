"""
Analyze trades.csv to understand WHY the strategy is losing.
python scripts/analyze_trades.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pandas as pd
from zoneinfo import ZoneInfo

IST        = ZoneInfo("Asia/Kolkata")
TRADES_CSV = "outputs/trades.csv"

df = pd.read_csv(TRADES_CSV)
df["entry_time"] = pd.to_datetime(df["entry_time"])
df["exit_time"]  = pd.to_datetime(df["exit_time"])
df["entry_hour"] = df["entry_time"].dt.hour
df["entry_min"]  = df["entry_time"].dt.minute
df["entry_hhmm"] = df["entry_time"].dt.strftime("%H:%M")
df["win"]        = df["pnl_net"] > 0

print("=" * 55)
print("  TRADE ANALYSIS")
print("=" * 55)

# ── Exit reason breakdown ─────────────────────────────────────────────────────
print("\n── Exit reasons ─────────────────────────────────────────")
reason_stats = df.groupby("exit_reason").agg(
    count    = ("pnl_net", "count"),
    win_rate = ("win",     "mean"),
    avg_pnl  = ("pnl_net", "mean"),
    total    = ("pnl_net", "sum"),
).round(0)
print(reason_stats.to_string())

# ── PnL by entry hour ─────────────────────────────────────────────────────────
print("\n── PnL by entry hour ────────────────────────────────────")
hour_stats = df.groupby("entry_hour").agg(
    trades   = ("pnl_net", "count"),
    win_rate = ("win",     "mean"),
    avg_pnl  = ("pnl_net", "mean"),
    total    = ("pnl_net", "sum"),
).round(0)
print(hour_stats.to_string())

# ── PnL by direction ──────────────────────────────────────────────────────────
print("\n── Long vs Short ────────────────────────────────────────")
dir_stats = df.groupby("direction").agg(
    trades   = ("pnl_net", "count"),
    win_rate = ("win",     "mean"),
    avg_pnl  = ("pnl_net", "mean"),
    total    = ("pnl_net", "sum"),
).round(0)
print(dir_stats.to_string())

# ── Duration analysis ─────────────────────────────────────────────────────────
print("\n── Duration (minutes) ───────────────────────────────────")
print(f"  Wins  avg: {df[df.win]['duration_min'].mean():.1f} min")
print(f"  Losses avg: {df[~df.win]['duration_min'].mean():.1f} min")

# ── Most common entry times ───────────────────────────────────────────────────
print("\n── Entry time win rate (top 10 slots) ───────────────────")
time_stats = df.groupby("entry_hhmm").agg(
    trades   = ("pnl_net", "count"),
    win_rate = ("win",     "mean"),
    avg_pnl  = ("pnl_net", "mean"),
).round(2)
time_stats = time_stats[time_stats["trades"] >= 2].sort_values("avg_pnl", ascending=False)
print(time_stats.head(10).to_string())

print("\n── Worst entry time slots ───────────────────────────────")
print(time_stats.tail(10).to_string())

# ── Days of week ──────────────────────────────────────────────────────────────
print("\n── PnL by day of week ───────────────────────────────────")
df["weekday"] = df["entry_time"].dt.day_name()
day_order     = ["Monday","Tuesday","Wednesday","Thursday","Friday"]
day_stats     = df.groupby("weekday").agg(
    trades   = ("pnl_net", "count"),
    win_rate = ("win",     "mean"),
    avg_pnl  = ("pnl_net", "mean"),
    total    = ("pnl_net", "sum"),
).reindex(day_order).round(0)
print(day_stats.to_string())
print("=" * 55)

# add to scripts/analyze_trades.py temporarily
import pandas as pd
df = pd.read_csv("outputs/trades.csv")
df["entry_time"] = pd.to_datetime(df["entry_time"])
df["year"] = df["entry_time"].dt.year
yearly = df.groupby("year").agg(
    trades   = ("pnl_net", "count"),
    win_rate = ("pnl_net", lambda x: (x > 0).mean()),
    total    = ("pnl_net", "sum"),
).round(0)
print(yearly.to_string())