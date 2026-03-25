"""
Run this to diagnose why 0 trades are being generated.
python scripts/debug_strategy.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pandas as pd
from data_fetcher import HistoricalFeed
from strategy import ORBStrategy, add_indicators, OR_BARS, ADX_THRESHOLD
from utils import session_bars

# ── 1. Load data ──────────────────────────────────────────────────────────────
feed = HistoricalFeed()
df   = feed.load()

print("=" * 55)
print("  DATA CHECK")
print("=" * 55)
print(f"Total bars       : {len(df)}")
print(f"First bar        : {df.index[0]}")
print(f"Last bar         : {df.index[-1]}")
print(f"Unique dates     : {df.index.normalize().nunique()}")
print()

# ── 2. Check session time distribution ───────────────────────────────────────
times = df.index.time
print("Bar time distribution (sample):")
from collections import Counter
time_counts = Counter(str(t)[:5] for t in times)
for t, c in sorted(time_counts.items())[:10]:
    print(f"  {t}  →  {c} bars")
print(f"  ...")
print()

# ── 3. Check per-day bar counts ───────────────────────────────────────────────
daily = df.groupby(df.index.date).size()
print(f"Avg bars/day     : {daily.mean():.1f}")
print(f"Min bars/day     : {daily.min()}")
print(f"Max bars/day     : {daily.max()}")
print()

# ── 4. Check if opening range bars exist (9:15–9:44) ─────────────────────────
or_bars = df[df.index.time <= pd.Timestamp("09:44").time()]
print(f"Bars in 9:15-9:44 (OR window): {len(or_bars)}")
print()

# ── 5. Check indicator values ─────────────────────────────────────────────────
df = add_indicators(df)
print("Indicator sanity check (last 5 bars):")
print(df[["close","atr","adx"]].tail(5).to_string())
print()

adx_ok = (df["adx"] > ADX_THRESHOLD).sum()
print(f"Bars where ADX > {ADX_THRESHOLD}: {adx_ok} / {len(df)}")
print()

# ── 6. Walk through a few bars and show why signal = NONE ────────────────────
print("=" * 55)
print("  SIGNAL TRACE (first 20 bars that have indicators ready)")
print("=" * 55)
strat = ORBStrategy()
bars  = list(df.iterrows())
shown = 0
for i, (ts, bar) in enumerate(bars):
    if i == 0:
        continue
    prev = bars[i-1][1]
    sig  = strat.evaluate(bar, prev)
    if shown < 20:
        print(f"  {ts.strftime('%m-%d %H:%M')}  "
              f"close={bar['close']:.0f}  "
              f"adx={bar['adx']:.1f}  "
              f"sig={sig.signal.value:<5}  [{sig.reason}]")
        shown += 1