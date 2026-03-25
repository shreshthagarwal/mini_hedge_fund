"""
analyze_csv.py — deep analysis of NIFTY futures CSV

Shows:
  - Overview and data quality
  - Overlap detection (duplicate timestamps from multiple contracts)
  - Per-month coverage: bars, days, % complete
  - Missing trading days
  - Price sanity

Run:
    python scripts/analyze_csv.py
    python scripts/analyze_csv.py --csv data/nifty_proxy_5min_historical.csv
"""

import sys, os, argparse
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pandas as pd
import numpy as np
from datetime import date, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# Approximate NSE holidays — extend as needed
NSE_HOLIDAYS = {
    date(2024,1,26), date(2024,3,25), date(2024,3,29),
    date(2024,4,14), date(2024,4,17), date(2024,5,23),
    date(2024,6,17), date(2024,7,17), date(2024,8,15),
    date(2024,10,2), date(2024,10,24), date(2024,11,1),
    date(2024,11,15), date(2024,12,25),
    date(2025,2,26), date(2025,3,14), date(2025,3,31),
    date(2025,4,10), date(2025,4,14), date(2025,4,18),
    date(2025,5,1),  date(2025,8,15), date(2025,8,27),
    date(2025,10,2), date(2025,10,20), date(2025,10,23),
    date(2025,11,5), date(2025,12,25),
    date(2026,1,26), date(2026,3,2),  date(2026,3,20),
}

def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in NSE_HOLIDAYS

def sep(char="─", n=64): print(char * n)

def load(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path, index_col="datetime")
    df.index = pd.to_datetime(df.index)
    if df.index.tzinfo is None:
        df.index = df.index.tz_localize(IST)
    else:
        df.index = df.index.tz_convert(IST)
    df.index.name = "datetime"
    df.columns = [c.lower() for c in df.columns]
    return df


def run(csv_path: str):
    print(f"\nAnalyzing: {csv_path}")
    sep("═")

    if not os.path.exists(csv_path):
        print(f"ERROR: File not found — {csv_path}")
        sys.exit(1)

    df = load(csv_path)

    # ── 1. Overview ───────────────────────────────────────────────────────────
    print("OVERVIEW")
    sep()
    print(f"  Total bars       : {len(df):,}")
    print(f"  Date range       : {df.index[0].strftime('%Y-%m-%d %H:%M')}  →  {df.index[-1].strftime('%Y-%m-%d %H:%M')}")
    print(f"  Unique days      : {df.index.normalize().nunique()}")
    print(f"  File size        : {os.path.getsize(csv_path)/1024:.1f} KB")
    print()

    # ── 2. Duplicate / overlap detection ──────────────────────────────────────
    print("DUPLICATE TIMESTAMPS (contract overlap indicator)")
    sep()
    dupes = df.index.duplicated(keep=False)
    n_dupes = dupes.sum()
    print(f"  Duplicate timestamps : {n_dupes:,}")
    if n_dupes > 0:
        dup_df = df[dupes].sort_index()
        print(f"  First duplicate      : {dup_df.index[0]}")
        print(f"  Last duplicate       : {dup_df.index[-1]}")
        # Show a sample
        sample_ts = dup_df.index[0]
        sample    = dup_df[dup_df.index == sample_ts]
        print(f"  Sample at {sample_ts}:")
        for _, row in sample.iterrows():
            print(f"    O:{row['open']:.2f}  H:{row['high']:.2f}  "
                  f"L:{row['low']:.2f}  C:{row['close']:.2f}")
        print()
        print("  ACTION NEEDED: run build_history_upstox.py with")
        print("  front-month-only stitching to remove overlaps.")
    print()

    # ── 3. Daily bar counts ───────────────────────────────────────────────────
    df_dedup = df[~df.index.duplicated(keep='first')]
    daily    = df_dedup.groupby(df_dedup.index.date).size()

    print("DAILY BAR COUNTS (after dedup)")
    sep()
    print(f"  Expected / day   : 75  (09:15–15:25 every 5 min)")
    print(f"  Mean             : {daily.mean():.1f}")
    print(f"  Median           : {daily.median():.0f}")
    print(f"  Min              : {daily.min()}  on {daily.idxmin()}")
    print(f"  Max              : {daily.max()}  on {daily.idxmax()}")
    print(f"  Days with 75 bars: {(daily == 75).sum()}")
    print(f"  Days with < 60   : {(daily < 60).sum()}")
    print(f"  Days with < 30   : {(daily < 30).sum()}")
    print()

    # ── 4. Per-month breakdown ─────────────────────────────────────────────────
    print("PER-MONTH COVERAGE")
    sep()
    print(f"  {'Month':<10} {'Days':>5} {'Exp Days':>9} {'Bars':>7} "
          f"{'Exp Bars':>9} {'Coverage':>9}  Status")
    sep("-", 70)

    start_d = df_dedup.index[0].date()
    end_d   = df_dedup.index[-1].date()
    today   = date.today()

    y = start_d.year
    m = start_d.month
    total_missing_days = 0

    while date(y, m, 1) <= today:
        # Month boundaries
        if m == 12:
            month_end = date(y+1, 1, 1) - timedelta(days=1)
        else:
            month_end = date(y, m+1, 1) - timedelta(days=1)

        month_start = date(y, m, 1)
        actual_end  = min(month_end, today, end_d)

        if month_start > end_d:
            break

        # Expected trading days
        exp_days = sum(
            1 for i in range((actual_end - month_start).days + 1)
            if is_trading_day(month_start + timedelta(days=i))
        )
        exp_bars = exp_days * 75

        # Actual bars in this month
        mask = (
            (df_dedup.index.year  == y) &
            (df_dedup.index.month == m)
        )
        actual_bars = mask.sum()
        actual_days = df_dedup[mask].index.normalize().nunique()

        pct = actual_bars / exp_bars * 100 if exp_bars > 0 else 0
        missing_days = max(exp_days - actual_days, 0)
        total_missing_days += missing_days

        if pct >= 90:
            status = "OK"
        elif pct >= 50:
            status = "PARTIAL"
        elif pct > 0:
            status = "SPARSE"
        elif month_start > end_d:
            status = "—"
        else:
            status = "MISSING"

        # Highlight current month
        is_current = (y == today.year and m == today.month)
        tag = " ◄" if is_current else ""

        print(f"  {y}-{m:02d}      {actual_days:>5} {exp_days:>9} "
              f"{actual_bars:>7,} {exp_bars:>9,} {pct:>8.1f}%  {status}{tag}")

        m += 1
        if m > 12:
            m = 1
            y += 1

    print()

    # ── 5. Missing trading days list ──────────────────────────────────────────
    print("MISSING TRADING DAYS")
    sep()
    existing_dates = set(df_dedup.index.date)
    d = start_d
    missing = []
    while d <= min(end_d, today):
        if is_trading_day(d) and d not in existing_dates:
            missing.append(d)
        d += timedelta(days=1)

    if not missing:
        print("  None — full coverage within date range")
    else:
        print(f"  Total missing: {len(missing)} trading days")
        # Group consecutive gaps
        groups, run_s, run_e = [], missing[0], missing[0]
        for d in missing[1:]:
            if (d - run_e).days <= 4:
                run_e = d
            else:
                groups.append((run_s, run_e))
                run_s = run_e = d
        groups.append((run_s, run_e))

        for gs, ge in groups[:20]:
            span = (ge - gs).days + 1
            tag  = f"  ({span}d gap)" if gs != ge else ""
            print(f"    {gs}  →  {ge}{tag}")
        if len(groups) > 20:
            print(f"    ... and {len(groups)-20} more gaps")
    print()

    # ── 6. Intra-day gaps ─────────────────────────────────────────────────────
    print("INTRA-DAY GAPS (bars > 5 min apart within same session)")
    sep()
    diff      = df_dedup.index.to_series().diff().dt.total_seconds() / 60
    same_day  = pd.Series(df_dedup.index.date) == pd.Series(df_dedup.index.date).shift(1)
    same_day.index = df_dedup.index
    intra     = diff[(diff > 6) & same_day]

    if intra.empty:
        print("  None detected")
    else:
        print(f"  Found: {len(intra)} intra-day gaps")
        for ts, mins in intra.nlargest(10).items():
            print(f"    {ts.strftime('%Y-%m-%d %H:%M')}  gap = {mins:.0f} min")
    print()

    # ── 7. Price sanity ───────────────────────────────────────────────────────
    print("PRICE SANITY")
    sep()
    zeros  = (df_dedup["close"] == 0).sum()
    negs   = (df_dedup["close"] < 0).sum()
    spikes = (df_dedup["close"].pct_change().abs() > 0.05).sum()
    print(f"  Zero closes      : {zeros}")
    print(f"  Negative closes  : {negs}")
    print(f"  Spikes > 5%/bar  : {spikes}")
    print(f"  Price range      : {df_dedup['close'].min():.0f}  →  {df_dedup['close'].max():.0f}")
    print(f"  Mean close       : {df_dedup['close'].mean():.0f}")
    print()

    # ── 8. Summary ────────────────────────────────────────────────────────────
    print("SUMMARY")
    sep("═")
    total_exp = sum(
        1 for i in range((min(end_d, today) - start_d).days + 1)
        if is_trading_day(start_d + timedelta(days=i))
    )
    coverage = (total_exp - len(missing)) / total_exp * 100 if total_exp > 0 else 0

    print(f"  Total bars (raw)     : {len(df):,}")
    print(f"  Duplicate bars       : {n_dupes:,}")
    print(f"  Bars after dedup     : {len(df_dedup):,}")
    print(f"  Trading day coverage : {coverage:.1f}%  ({total_exp - len(missing)}/{total_exp} days)")
    print(f"  Missing days         : {len(missing)}")
    print(f"  Intra-day gaps       : {len(intra)}")
    print(f"  Price anomalies      : {zeros + negs + spikes}")

    if n_dupes > 0:
        print()
        print("  ⚠  OVERLAP DETECTED — fix with front-month stitching")
    elif coverage >= 90 and daily.mean() >= 70:
        print()
        print("  ✓  Quality: GOOD — suitable for backtesting")
    elif coverage >= 70:
        print()
        print("  △  Quality: FAIR — usable but has gaps")
    else:
        print()
        print("  ✗  Quality: POOR — significant data missing")
    sep("═")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analyze NIFTY futures CSV")
    parser.add_argument(
        "--csv",
        default="data/nifty_futures_5min.csv",
        help="Path to CSV (default: data/nifty_futures_5min.csv)",
    )
    args = parser.parse_args()
    run(args.csv)