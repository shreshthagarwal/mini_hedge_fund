# Project Status & Full Code Audit

*Last updated: 2026-10-21. Replaces `docs/AUDIT_2026-10-21.md`.*

This is the single source of truth for **where the project stands** and **what
is wrong with the code**. Read it at the start of every work session; tick
items off in the tracker (§7) as they are fixed.

> This repo is **public**. Never put server IPs, URLs with secrets, passwords,
> tokens or API keys in this file or anywhere else in git.

---

## 1. Snapshot

| | |
|---|---|
| What it is | Intraday NIFTY-futures paper-trading system: 5 strategies, shared backtester, live engine, web dashboard |
| Mode | **Paper only.** No order placement code exists |
| Running on | AWS Lightsail ($12 plan: 2 vCPU / 2 GB + 2 GB swap), Ubuntu 24.04, Python 3.12, Sydney region |
| Public access | HTTPS dashboard on a static `sslip.io` hostname, password-protected (Caddy basic auth) |
| Daily login | Browser OAuth on the `/auth` page (phone → OTP → PIN), any time after 03:30 IST |
| Schedule | 09:00 IST bot starts if a fresh token exists · 15:40 IST bot stops |
| Trade records | `outputs/paper_trades/YYYY-MM-DD.csv` on the server (not in git, not backed up) |
| Strategy edge | **None proven.** Backtests are invalid (§5.1, §5.3); after realistic costs S2/S3/S4 lose money and S1 is about break-even |

**Bottom line:** the infrastructure works end to end (data → live feed →
5 strategies → paper P&L with real costs → dashboard on any device). The
*research* side is not trustworthy yet. Before any strategy is trusted, the data has to be
rebuilt, the backtester made pessimistic and cost-aware, and backtest and live made to
share one exit model.

---

## 2. Progress log

| Date | What happened |
|---|---|
| 2026-03 | Initial commits: 5 strategies, backtester, intelligence layer, dashboard |
| 2026-10-20 | Analysis found the futures CSV interleaves two contracts (§5.1.1) |
| 2026-10-21 | Deep audit, then deploy-to-paper prep (commit `82fab6c`): `.env` config, realistic costs (`src/costs.py`), lot size from the exchange (65), paper trades persisted to CSV, warmup from recent Upstox history instead of the stale CSV, priming before the feed starts, intelligence gate off by default and threaded, clean SIGTERM |
| 2026-10-21 | Deployed to Lightsail; first paper session 13:35–15:40 (§3) |
| 2026-10-21 | Public HTTPS dashboard via Caddy + `sslip.io`, password login; dashboard CSRF fix; always-on `hedgebot-token` service with `/auth` page |
| 2026-10-21 | Tried Upstox **Access Token Request** (approve on WhatsApp → token to a notifier webhook). Approval works, but **Upstox's firewall never delivers the webhook** to unreviewed domains (confirmed by Upstox staff in their community forum). Replaced with browser OAuth on `/auth`; the approval flow and webhook were removed entirely (commit `9e5d2f4`) |
| 2026-10-21 | This full audit. Removed a hardcoded (expired) Upstox token from `scripts/get_options_data.py`; it is still in git history (§5.12.1) |

### Facts learned the hard way
- **Upstox tokens expire at 03:30 IST the next day, whenever they were issued.** Logging in the evening does not help.
- **Upstox webhooks (notifier/postback) are firewalled** unless Upstox whitelists the domain.
- **NIFTY lot size is 65** (instrument master, Oct 2026); every strategy config still says 75.
- **Upstox 5-min candles are labelled by bar start** (09:15 = first bar). Futures history also contains 15:30 and 15:35 candles after the close.
- **nseindia.com usually blocks cloud/AWS IPs**, so the NSE scrapes in the intelligence layer will mostly fail on the server.
- The **spot proxy CSV has `volume = 0` on every row** (215,809 bars, 2015 to 2026-09-11).

---

## 3. First paper session (2026-10-21, 13:35–15:40, costs included)

| Strategy | Trades | Net ₹ | Notes |
|---|---|---|---|
| S1 ORB | 0 | 0 | Opening range not complete (2.5 h, see S1-1) |
| S2 VWAP | 2 | −3,799 | Both stopped out at about 16 pts |
| S3 Candle | 1 | −2,815 | Stopped out |
| S4 RL | 2 | −4,081 | Includes a 0-pt "trailed-stop" that still cost ₹878 |
| S5 Basis | 1 | +1,149 | Only winner |
| **Total** | **6** | **−9,546** | Costs were about ₹878/trade, **₹5,270 of the loss** |

Two hours prove nothing about edge, but they match the audit: these strategies
need well over 13 pts/trade just to cover costs.

---

## 4. Architecture & operations

```
Laptop (dev) ──git push──▶ GitHub (public: code only)
                                │ git pull (manual)
                                ▼
Lightsail VM  ~/mini_hedge_fund
  venv/                      Python deps (requirements.txt pinned)
  .env, .upstox_token        secrets — scp'd / written on the VM, gitignored
  data/*.csv, models/*.pkl   scp'd, gitignored
  systemd: hedgebot          python main.py paper  (bot + Flask dashboard :5173, localhost)
  systemd: hedgebot-token    scripts/token_service.py (/auth page + OAuth :5174, localhost)
  systemd: caddy             HTTPS :443 → basic auth → /auth* to :5174, rest to :5173;
                             502 (bot stopped) → redirect to /auth
  cron /etc/cron.d/hedgebot  09:00 start-if-fresh · 15:40 stop
```

- **Deploy an update:** push from the laptop, then on the VM run `git pull` (plus `venv/bin/pip install -r requirements.txt` if needed), then `sudo systemctl restart hedgebot` (and `hedgebot-token` if `scripts/token_service.py` or `src/upstox_token.py` changed). If `deploy/*` changed, re-copy the files into `/etc/...` as the file headers describe.
- **Server-only config (never in git):** `/etc/caddy/hedgebot.env` (`SITE_ADDRESS`, `DASH_USER`, `DASH_PASSWORD_HASH`) loaded by `/etc/systemd/system/caddy.service.d/hedgebot.conf`; `.env` holds `UPSTOX_WEB_REDIRECT_URI`.
- **Upstox app settings:** Redirect URL = `https://<site>/auth/callback`. The notifier webhook is unused and can be cleared. Because of this redirect, the old laptop script `scripts/upstox_auth.py` **no longer works**.
- **Logs:** `journalctl -u hedgebot -f`, `-u hedgebot-token`, `-u caddy` (access log on).
- **Lightsail firewall:** 22 (SSH) and 443 (HTTPS) only.

---

## 5. Findings by layer

Severity: **P0** = invalidates results or a security problem · **P1** = wrong
behaviour, fix before trusting paper or backtests · **P2** = quality, robustness, cleanup.
IDs are stable; refer to them in commits ("fix E-2").

### 5.1 Data layer

| ID | Sev | Where | Problem | Fix |
|---|---|---|---|---|
| D-1 | P0 | `scripts/build_history_upstox.py:355`, `:421-422` | Each expired contract is fetched for the **60 days before expiry**, so consecutive contracts overlap by about a month. The merge is `sort_index()` (unstable) then `duplicated(keep="first")`, so for every overlapping timestamp an **arbitrary contract survives**. Result: 30–46 % of bars in most months come from the next-month contract, and the basis flips about 100 pts bar to bar. **All backtests are invalid.** | Per contract, keep only its front-month window (previous expiry or roll date → this expiry or roll date, via `utils.roll_date`). Build with an explicit `contract` column, then validate: futures-vs-spot 5-min return correlation > 0.9 and no same-session \|Δbasis\| > 40 pts |
| D-2 | P0 | `main.py:257-260` → `src/data_fetcher.py:45` → `src/config.py:74` | **`python main.py fetch` overwrites the real futures CSV** with 60 days of yfinance *spot* data (`HistoricalFeed(force_refresh=True)` defaults to `HISTORICAL_CSV = data/nifty_futures_5min.csv`). One command destroys 2 years of data | Remove `cmd_fetch` or point it at `build_history_upstox.py`; never let a fetch write a different source's data to the futures path |
| D-3 | P1 | `src/data_fetcher.py:62-65`, `122-147` | If the CSV is missing and yfinance fails, it **silently backtests on synthetic random-walk data** (warning only) | Raise instead |
| D-4 | P1 | `scripts/build_history_upstox.py:384` | Live front-month fetched from `expiry − 40 days`, which overlaps the previous contract's last weeks (same interleaving as D-1) | Start at the previous contract's roll date |
| D-5 | P1 | `src/utils.py:41-43`, `:103-105` | `session_bars` keeps bars ≤ **15:30**, so post-close 15:30 candles stay in backtests while live warmup keeps ≤ 15:25. Indicators differ slightly | Session = 09:15 ≤ t ≤ 15:25 everywhere |
| D-6 | P1 | `data/nifty_proxy_5min_historical.csv` | Volume is 0 on all rows, so VWAP is NaN (S2 never trades), the S3 volume filter always fails, S1 `vol_spike` is always False, and S4 volume features are 0. Backtests on the proxy are meaningless for 4 of 5 strategies | Mark the proxy as price-only; refuse to backtest volume-based strategies on it |
| D-7 | P1 | `scripts/clean_nifty_data.py:19-32`, `35-55` | Cleaner (a) merges duplicate timestamps from **two contracts** into Frankenstein bars (high = max of both, etc.); (b) builds a tz-naive business-day index (holidays included) and reindexes tz-aware data, so the output is mostly NaN or flat synthetic days. Output is unused | Delete; superseded by the D-1 fix |
| D-8 | P2 | `scripts/build_history_upstox.py:265-274` | `coverage_per_expiry` is dead code (returns `{}`) | Delete |
| D-9 | P2 | `scripts/build_history_upstox.py:229-240` | OI column fetched then dropped | Keep `oi`; useful for features |
| D-10 | P2 | `scripts/build_history_upstox.py:54`, `15` | Messages point to `upstox_auth.py`, which no longer works with the server redirect | Read the token from the server (`scp`) or add a "copy token" note |
| D-11 | P2 | `scripts/build_history.py` | Docstring and README claim "2015 to present" at 5-min, but yfinance only serves 60 days of 5-min; the old part of the CSV came from somewhere else and can't be refreshed or reproduced | Document the provenance, or rebuild spot from Upstox `NSE_INDEX\|Nifty 50` history |

### 5.2 Shared utilities & config

| ID | Sev | Where | Problem | Fix |
|---|---|---|---|---|
| U-1 | P1 | `src/config.py` (whole file) | Legacy global config still imported by `utils`, `data_fetcher` and `analytics`: `LOT_SIZE = 25`, `OR_BARS = 14` (comment says 16), `RR 1.8`, etc. Contradicts the per-strategy configs and sets the `HISTORICAL_CSV` that causes D-2 | Shrink to true globals (IST, session times, paths); delete strategy params |
| U-2 | P1 | all `strategies/*/config.py` | `LOT_SIZE = 75`; the exchange says **65**. Live overrides it; **backtests don't** | One `LOT_SIZE` source (instrument master, or a dated lot-size table for history) |
| U-3 | P1 | all `strategies/*/config.py` | `COMMISSION_PER_LOT = 40` (engine charges ×2 = ₹80/round trip) vs about ₹950 real | Backtests use `src/costs.round_trip_cost` (see E-1) |
| U-4 | P2 | `src/utils.py:89-94` | `roll_date` says "N trading days" but subtracts calendar days and ignores holidays | Use a trading calendar |
| U-5 | P2 | `src/utils.py:69-77` | `nse_expiry` ignores holidays (a holiday on the last Tuesday moves expiry earlier). Live S5 uses it instead of the real expiry from the instrument master | Live: use instrument-master expiry. History: holiday list |
| U-6 | P2 | `src/utils.py:17-28` + `main.py:23` | `get_logger` adds its own stdout handler and the root `basicConfig` adds another, so **every `live_feed`/`utils` log line appears twice** in journald | `logger.propagate = False`, or drop the custom handler |
| U-7 | P2 | `scripts/analyze_csv.py:27-39` | Hard-coded holiday list stops at Mar-2026 | Shared holiday calendar |

### 5.3 Backtest engine (`src/engine.py`)

| ID | Sev | Where | Problem | Fix |
|---|---|---|---|---|
| E-1 | P0 | `engine.py:409`, `:455`, `:519` | Costs = `commission_per_lot × lots × 2` = ₹80 per round trip, about **12× too low**. Re-costed saved trade logs: S1 ≈ ₹0, S2 −₹1.2 L, S3 −₹9.4 L, S4 −₹11.9 L | Call `costs.round_trip_cost(direction, entry, exit, lots, lot_size)` |
| E-2 | P0 | `risk_management.py:281-308` | Risk-managed path checks **T2 before SL**, so a bar touching both counts as a **win** (README claims the opposite). This is the active path (`use_risk_mgmt=True`) | Check SL first (or simulate intrabar using bar direction) |
| E-3 | P1 | `engine.py:436-448`, `risk_management.py:294-308` | Stops fill **at the SL price even when the bar opens beyond it** (gaps), which is optimistic | LONG fill = `min(open, sl)`, SHORT = `max(open, sl)` |
| E-4 | P1 | `risk_management.py:318`, engine `max_lots=1` | With 1 lot, T1 "partial" = `max(1, round(0.5)) = 1` → **exits the whole position at T1** (0.8–1.0 R). T2, trailing and signal `rr` never matter; the backtest really tests "target = 1R" | Use 2+ lots to make partials real, or model a single target honestly |
| E-5 | P1 | `engine.py:490` | `strategy.on_bar` is called **only when flat with no pending signal**. S3/S4 build their own bar buffers inside `on_bar`, so their history has gaps. Live calls `on_bar` every bar, so backtest ≠ live | Call `on_bar` every bar with `in_position` in the context |
| E-6 | P1 | `engine.py:559-616` | `walk_forward` is just monthly chunks with a reset: no training window, parameters tuned on full data, S3 loads a model trained on everything. Fold equity curves each restart at initial capital and are concatenated, so drawdown and Sharpe are wrong | Real walk-forward: fit/tune on train, test out-of-sample, continuous equity |
| E-7 | P1 | `engine.py:318-342` | **No forced exit on a day change.** If a session lacks a ≥ 15:20 bar (missing data, half-day), the position is carried overnight and closed next day across the gap | On `new_day` with a position open, close at the previous bar's close |
| E-8 | P1 | `engine.py:39-44` | If `risk_management` fails to import (e.g. `src` not on `sys.path`), the engine **silently** switches to the simple SL/target model | Import explicitly; fail loudly |
| E-9 | P2 | `engine.py:492-493` | Daily loss breaker counts only realized P&L and only blocks new entries | Include open MTM; optionally flatten |
| E-10 | P2 | `risk_management.py:401-409` | Time-stop exits at the bar **open** after that bar's high/low were used to rule out the SL (small look-ahead) | Evaluate the time-stop at the start of the bar, before the range checks |
| E-11 | P2 | `engine.py:266-273` | Indicators computed **before** sort and dedupe | Sort and dedupe first |
| E-12 | P2 | `engine.py:487` | Equity curve is realized-only (no mark-to-market), so drawdown is understated | Mark open positions to close |
| E-13 | P2 | `engine.py:58` | Slippage = 1 tick (0.05). Market orders realistically cost 0.5–1 pt | Configurable, default about 0.5 pt per side |
| E-14 | P2 | `engine.py:495-496`, `:280` | `df.iloc[i]` per bar + Python date list are slow | Pre-build rows or use itertuples |
| E-15 | P2 | `risk_management.py:330-331` | After T1 it sets `trailing_active=True` even when `use_trailing=False` (S2, S5), so exits are mislabelled `stop-loss(trailing)` | Only set it if trailing is enabled |
| E-16 | P2 | `risk_management.py:247` | Every BE/trail/T1 event logs at INFO, flooding backtests and journald | DEBUG |
| E-17 | P2 | `risk_management.py:81` | `max_loss_per_trade_r` defined, never used | Use or delete |

### 5.4 Analytics & CLI (`src/analytics.py`, `main.py`)

| ID | Sev | Where | Problem | Fix |
|---|---|---|---|---|
| A-1 | P1 | `analytics.py:40-48` | Sharpe/Sortino from **bar-level** returns of a realized-only equity curve × √(252·75): mostly zero returns, so the number is meaningless and disagrees with `BacktestResult.sharpe` (daily). `cmd_report` feeds **per-trade** equity into the same formula | Daily P&L based Sharpe everywhere |
| A-2 | P2 | `analytics.py:15`, `105`, `229-231` | Charts and reports always written to global `outputs/`, so every strategy overwrites the last; per-strategy `EQUITY_CURVE_PNG`/`PERFORMANCE_TXT` are unused | Pass per-strategy paths |
| A-3 | P2 | `analytics.py:56` | CAGR uses global `INITIAL_CAPITAL` | Use the result's capital |
| A-4 | P2 | `main.py:121` | Always logs "BACKTEST strategy_1" | Use the chosen label |
| A-5 | P2 | `main.py:264-293` | `report` only supports S1 though it prompts 1–5 | All strategies |
| A-6 | P2 | `main.py:99-100` | "Load existing PPO model?" has no effect: `engine.run` calls `strategy.reset()`, which builds a fresh agent | Respect the flag in `reset` |
| A-7 | P2 | `main.py:302` | Default command (no args) is `backtest`, which is interactive and hangs under non-interactive runners | Print usage instead |

### 5.5 Strategy S1 — ORB + ADX

| ID | Sev | Where | Problem | Fix |
|---|---|---|---|---|
| S1-1 | P0 | `strategy_1/config.py:35` | `OR_BARS = 30` "1-min bars", but data is 5-min, so the **opening range is 09:15–11:45 (2.5 h)**. With the dead zone, skip slots and the 13:30 cutoff, entries only happen 11:45–13:30, which is why there are 51 trades in 18 months | `OR_BARS = 6` (30 min), or define the OR by time, not bar count |
| S1-2 | P1 | `strategy_1/config.py:57-61` | `ORB_DEAD_ZONE` and `ORB_SKIP_SLOTS` were mined from backtest results (`scripts/analyze_trades.py`). That's overfitting to time slots | Remove; re-evaluate out-of-sample |
| S1-3 | P1 | `strategy_1/config.py:49-50` | `MAX_HOLD_BARS = 10` "minutes" = 50 min live; backtest uses `RISK_CONFIGS["S1"]` (24 bars) | One exit model (L-1) |
| S1-4 | P2 | `strategy_1/strategy.py:80-81` | `vol_avg`/`vol_spike` computed, never used | Use or delete |
| S1-5 | P2 | `strategy_1/strategy.py:168-187` | Signals need a close-cross only; there's no retest or ATR buffer, so breakouts by 0.05 pt count | Minimum breakout distance |
| S1-6 | P2 | `strategy_1/strategy.py:30-37` + S2/S3/S4 copies | ATR, ADX, RSI and VWAP copy-pasted across 4 strategies (and ATR is EWM, not Wilder) | Shared `indicators.py` |

### 5.6 Strategy S2 — VWAP reversion + RF

| ID | Sev | Where | Problem | Fix |
|---|---|---|---|---|
| S2-1 | P0 | `strategy_2/strategy.py:395` | RF label `1 if reason == "target"`, but exit reasons are `target-T1`, `target-T2`, `stop-loss…` (live: `"exit"`). **Every label is 0**, so after 100 samples the RF trains on one class, `predict_proba` → 0.5 < `RF_MIN_PROB` 0.55, and **S2 blocks every trade for the rest of the backtest** | Label from P&L (`net > 0`) or `reason.startswith("target")` |
| S2-2 | P1 | `strategy_2/config.py:45` | `ATR_SL_MULT = 0.5`, about 8–10 pt stops, **below the round-trip cost** (~13–15 pts) | SL ≥ costs + noise (≥ 1 ATR or ≥ 20 pts) |
| S2-3 | P1 | `strategy_2/strategy.py:118` | `prev_day_close` = groupby-first shifted one **row**, i.e. today's first close for almost every bar, not yesterday's close | Daily resample: last close of the previous session |
| S2-4 | P1 | `strategy_2/config.py:30`, `48` | `VWAP_SLOPE_BARS = 50` "1-min" = 250 min; `MAX_HOLD_BARS = 8` "min" = 40 min | Re-derive for 5-min |
| S2-5 | P2 | `strategy_2/strategy.py:346-348`, `365-368` | Target fixed at entry from VWAP distance; VWAP moves but the target doesn't | Optionally trail the target to the live VWAP |
| S2-6 | P2 | `strategy_2/strategy.py:287` | RF retrain keyed on calendar days | Fine; document |

### 5.7 Strategy S3 — Candlestick + "CNN" S/R

| ID | Sev | Where | Problem | Fix |
|---|---|---|---|---|
| S3-1 | P0 | `strategy_3/cnn_sr.py:181-190`, `strategy.py:91` | **Model leakage:** `SRDetector()` auto-loads `sr_cnn.pkl` (trained on the full dataset) on construct and on every `reset()`, so backtests use a model that has seen the future | No auto-load in backtests; train only on data before the test window |
| S3-2 | P0 | `strategy_3/strategy.py:193-195` | S/R-tightened SL `min(ATR, \|close−sr\| + 0.1·ATR)` can be **1–2 pts**, far below costs. With pivot fallback (`cnn_sr.py:303-328` returns every local high/low) a level is almost always "confirmed" | SL floor (≥ costs + noise, e.g. ≥ 1 ATR or 20 pts) |
| S3-3 | P1 | `strategy_3/patterns.py:97-116` | **InvertedHammer can never fire**: its condition (body in the bottom 40 %) is a subset of ShootingStar (checked first), so a bottom inverted hammer is traded as a **SHORT** | Use trend context to separate them, or drop one |
| S3-4 | P1 | `strategy_3/patterns.py:150-158` | Tweezer tolerance `0.002 × price` ≈ **50 pts** at 25k, wider than a typical 5-min bar, so Tweezer fires on nearly every bear→bull pair | Tolerance in ATR terms (e.g. 0.1 ATR) |
| S3-5 | P1 | `strategy_3/patterns.py:125-136` | `ENGULF_FACTOR = 1.0` requires close ≥ prior open **+ one more prior body**, stricter than a real engulfing | `close >= b1.open` (factor as a multiplier on body0/body1) |
| S3-6 | P1 | `strategy_3/strategy.py:148-156` | Reversal patterns (hammer, shooting star…) only taken when the EMA50 trend **already** agrees, so they're used as continuation. Conceptually contradictory | Decide: reversal at S/R against the trend, or continuation patterns only |
| S3-7 | P1 | `strategy_3/config.py:37-41` | `SR_LOOKBACK_BARS = 300` "1-min" = 4 days of 5-min; CNN counts 5× too large | Re-derive for 5-min |
| S3-8 | P2 | `strategy_3/cnn_sr.py:1-24` | Docstring describes a conv net; it's an sklearn MLP on flattened pixels (200 MB pickle, about 20 MLPs) | Rename, shrink, or replace with a simple pivot clustering S/R |
| S3-9 | P2 | `strategy_3/cnn_sr.py:142-157` | Labels: future pivots outside the window range clip into the edge bins, which are over-labelled | Drop out-of-range pivots |
| S3-10 | P2 | `strategy_3/cnn_sr.py:76-105` | Pixel-loop renderer with `iterrows`: very slow training | Vectorise |
| S3-11 | P2 | `strategy_3/config.py` | `HAMMER_BODY_RATIO` unused (hammer uses a hard-coded 0.6) | Use or delete |

### 5.8 Strategy S4 — RL scalper (PPO-lite)

| ID | Sev | Where | Problem | Fix |
|---|---|---|---|---|
| S4-1 | P0 | `strategy_4/ppo.py:205` | `sample_weights = abs(ratio × advantage)`. The **sign of the advantage is discarded**, so bad actions are reinforced as strongly as good ones. The policy imitates its own past (random) actions; **it cannot learn from reward** | Proper PPO (e.g. PyTorch or stable-baselines3), or weight only positive-advantage samples |
| S4-2 | P1 | `strategy_4/strategy.py:160-170` | Agent's EXIT action ignored at execution (`pass`), but trained where EXIT works, so the policy is used out of context | Honour EXIT (signal an exit to the engine) |
| S4-3 | P1 | `strategy_4/environment.py` vs `risk_management.py` vs live | Three exit models (training env: SL/target + 8-bar hold; backtest: `RISK_CONFIGS["S4"]`; live: `StrategyState`) | One exit model (L-1) |
| S4-4 | P1 | `strategy_4/ppo.py:145`, `strategy.py:268` | `n_epochs` (= `N_TRAIN_EPOCHS` 50) accepted and ignored | Loop epochs |
| S4-5 | P1 | `strategy_4/strategy.py:239`, `252` | Rollout chunks of 512 not marked `done`, so GAE bleeds across unrelated chunks; `values_padded` last next-value = 0 | Mark chunk ends; bootstrap the last value |
| S4-6 | P1 | live | Live training can trigger after 1,500 `on_bar` calls, inside the feed lock, and overwrites `ppo_policy.pkl` | Disable training live |
| S4-7 | P2 | `strategy_4/ppo.py:112`, `118` | Unseeded `np.random`, so backtests aren't reproducible | Seed |
| S4-8 | P2 | `strategy_4/strategy.py:216-218` | Inference state uses `bars_held = 0` always | Pass the real bars held |
| S4-9 | P2 | — | `ppo_policy.pkl` trained on contaminated data (D-1) | Retrain after the data fix |

### 5.9 Strategy S5 — "Basis arbitrage"

| ID | Sev | Where | Problem | Fix |
|---|---|---|---|---|
| S5-1 | P0 | `strategy_5/strategy.py` | **Not arbitrage**: trades only the futures leg, so P&L is unhedged NIFTY direction triggered by a basis signal. Backtest profit is the D-1 interleaving artifact (about ₹1.5 k on cleaned data) | Hedge with spot (e.g. NIFTYBEES) or options, or drop S5 |
| S5-2 | P1 | `strategy_5/strategy.py` | SL/target distances come from `basis_std` (a few pts) applied to **futures price**, which is noise-sized. Exits never re-check the z-score; `ZSCORE_TARGET`/`ZSCORE_STOP` only size distances | Exit on z-score reversion or blow-out |
| S5-3 | P1 | `src/multi_engine.py:1031`, `strategy_5/config.py:28` | Live buffer ≤ 500 bars, so the live z-score uses ≤ 400 bars (`min_periods=20`) vs 7,500 in the backtest. Different statistic | Persist a long basis history, or use the same short window in both |
| S5-4 | P2 | `strategy_5/config.py:25`, `data_loader.py:78-80` | Fair basis = spot × 6.5 % × DTE/365: stale rate, no dividend yield | Current repo/T-bill rate minus dividend yield, or just z-score the raw basis |
| S5-5 | P2 | `strategy_5/data_loader.py:106-130` | Per-bar Python loop for DTE; generic expiry (U-5) | Vectorise; use real expiries |

### 5.10 Live paper engine & feed (`src/multi_engine.py`, `src/live_feed.py`)

| ID | Sev | Where | Problem | Fix |
|---|---|---|---|---|
| L-1 | P0 | `multi_engine.py:208-300` (`StrategyState.check_exits`) | Live exits differ from the backtest: T1 = midpoint, BE only after T1, trail by the full `sl_dist`, `MAX_HOLD_BARS` in 5-min bars, entry at the signal bar close (backtest: next bar open). **Paper results cannot validate backtests** | Live uses `PositionRiskManager` and the same entry convention |
| L-2 | P1 | `multi_engine.py:236-256` | Peak, T1 and trail updated from the bar's high **before** the SL check on the same bar, which assumes the high came first (optimistic) | Check the SL against the previous stop first, then update |
| L-3 | P1 | `multi_engine.py:1104-1107`, `1141-1144` | `context["session_bar"] = len(bar_buffer)` (300+), not the bar within the session; `new_day` always False | Track the session bar index; set `new_day` |
| L-4 | P1 | `multi_engine.py` | No daily-loss circuit breaker live (backtest has one) | Add |
| L-5 | P1 | `live_feed.py` | No staleness watchdog: if the WebSocket stalls, candles stop, exits and square-off never run, and nobody is told | Watchdog: no tick for N s during market → reconnect, then alert |
| L-6 | P1 | `multi_engine.py:1090` | `strat.on_exit("exit", …)` passes a generic reason instead of the real one | Pass the actual exit reason |
| L-7 | P2 | `live_feed.py:177-204` | First live candle is partial (joins mid-bucket); ticks outside 09:15–15:30 not filtered | Drop the first partial bucket; filter session |
| L-8 | P2 | `live_feed.py:173-175` | Candle closes only when a tick from the next bucket arrives, so a bar can close late in quiet markets | Timer-based close |
| L-9 | P2 | `multi_engine.py:440-490` | Priming uses a hard-coded `capital 500_000` context and swallows all exceptions silently (`except: pass`) | Log failures |
| L-10 | P2 | `multi_engine.py:1239-1240` | `_build_s5_df_placeholder` dead stub | Delete |
| L-11 | P2 | `multi_engine.py` | Session OHLC, VWAP and ORB displays are never reset (fine only because of the daily restart) | Reset on a day change |
| L-12 | P2 | `multi_engine.py:1176-1237` | Manual trades use a fixed 1:1.3 RR and their own ATR | Fine for paper; keep `ALLOW_MANUAL_TRADES=0` for clean stats |

### 5.11 Intelligence layer (`intelligence/`)

| ID | Sev | Where | Problem | Fix |
|---|---|---|---|---|
| I-1 | P0 | `macro/collector.py:145-163`, `technical/collector.py:157-183` | **Z-scores never work.** History is built in-process by appending the *same daily value* on every refresh (every 15–30 min); the bot restarts daily, so history never holds more than one day's value, std = 0, and every z-score is 0. Technical `normalise` looks up `key.split("_")[0]` (`bnk_nifty_div` → `bnk`), and `update_history` stores keys that `fetch_daily` never produces (`banknifty`, `pcr`). **The regime is effectively constant** (`RANGE_NEUTRAL`), so the gate never fires | Build daily histories from historical data (yfinance daily, 1–2 years) persisted to disk; one value per day |
| I-2 | P1 | `macro/collector.py:116-143` | NSE `fiidiiTradeReact` parsed as `records[0]["fiiNet"]`; the endpoint returns per-category rows (FII/FPI, DII) with buy/sell/net values, so `fii_net` is probably always 0. Also blocked from AWS | Verify the response shape; use a working source |
| I-3 | P1 | `sentiment/collector.py:117-138` | Option chain scraped from nseindia.com **every call** (blocked from cloud IPs, 5–15 s timeouts); on failure it returns the **last snapshot forever** (no staleness) | Upstox option-chain API (authenticated, works from the server); staleness timestamp |
| I-4 | P1 | `sentiment/collector.py:141-175` | PCR and max pain computed over **all expiries** in `records.data` mixed together | Filter to the current expiry |
| I-5 | P1 | — | The gate has **never been backtested**; there's no historical pipeline for macro/sentiment features, so it can't be validated | Build daily feature history, then backtest the gate on/off |
| I-6 | P2 | `sentiment/collector.py:205-233` | "IV percentile" ranks against the last ≤ 50 intraday samples, not a real 1-year IV history | Daily ATM IV history |
| I-7 | P2 | `technical/collector.py:132-153` | `fetch_oi` returns `oi / oi_day_high − 1` (always ≤ 0), labelled "change vs previous close" | Use previous-day OI |
| I-8 | P2 | `technical/collector.py:96-130` | `fetch_pcr` (no cookies) unused; `features.pcr_z` always 0 | Delete or wire up |
| I-9 | P2 | `features.py:133` | Missing-check looks for `banknifty_div`; the key is `bnk_nifty_div` | Fix the key |
| I-10 | P2 | `engine.py:139-148` | `(now − last).seconds` ignores days (wrong across midnight); macro (daily data) refreshed every 30 min | `total_seconds()`; refresh macro once a day |
| I-11 | P2 | `score.py`, `engine.py:63-78` | `AlphaScore`, `agrees_with` and `size_mult` computed but never used by the live engine | Use or delete |
| I-12 | P2 | `sentiment/collector.py:70-80` | PCR interpretation (high PCR = bearish) is debatable; many treat high PCR as contrarian-bullish | Decide from data |

### 5.12 Security, scripts, docs, ops

| ID | Sev | Where | Problem | Fix |
|---|---|---|---|---|
| X-1 | P0 | `scripts/get_options_data.py` (git history, commit `d0144a8`) | A **hardcoded Upstox access token** was committed to this public repo. It expired in March 2026 (Upstox tokens die at 03:30 the next day), so it can't be used, but it identifies the account. **Removed from the current file 2026-10-21; still in history** | Optional: rewrite history (`git filter-repo`) + force-push. Never hardcode tokens |
| X-2 | P1 | `scripts/get_options_data.py:36`, `:54` | No request timeouts (can hang); `quote(key, safe='\|')` doesn't encode `\|`; `failed` counter never incremented | Timeouts, `safe=""`, count failures |
| X-3 | P1 | ops | **No alerting.** If the 09:00 start fails (no login, crash, feed dead) nobody is told | Telegram/email alert on start failure, feed stall, and the daily summary |
| X-4 | P1 | ops | Paper-trade CSVs live only on the VM (no backup, no sync) | Nightly copy to the laptop/S3, or Lightsail snapshot |
| X-5 | P2 | `src/backtester.py`, `paper_engine.py`, `paper_broker.py`, `live_ticker.py`, `dashboard.py`, `risk_manager.py`, `strategy.py`; `data_fetcher.LiveFeed` | **Dead legacy code** (not reachable from `main.py`), confusingly similar names (`risk_manager` vs `risk_management`, two `LiveFeed`s) | Delete |
| X-6 | P2 | `scripts/debug_strategy.py` | Imports the legacy `strategy.ORBStrategy.evaluate()`; broken against the current code | Delete or rewrite |
| X-7 | P2 | `scripts/probe_upstox_coverage.py:74`, `:82` | Uses `last_thursday` for all months; wrong after Sep-2025 (Tuesday expiry) | Use `nse_expiry` |
| X-8 | P2 | `scripts/test_expired_candle.py:17-21` | No timeout; hard-coded months | Delete or parametrise |
| X-9 | P2 | `scripts/analyze_trades.py:87-…` | Duplicate "temporary" block at the end; reads legacy `outputs/trades.csv`; its time-slot mining caused S1-2 | Clean up; never tune on full-sample slot stats |
| X-10 | P2 | `scripts/upstox_auth.py` | Broken since the Upstox Redirect URL moved to the server | Delete, or document "copy `.upstox_token` from the server" |
| X-11 | P2 | `README.md` | Stale: results table (invalid), "pessimistic" claim (E-2), `upstox_auth.py` daily flow, `strategy_1/backtester.py` (doesn't exist), ports, folder tree | Rewrite after the fixes; point to this doc |
| X-12 | P2 | `src/multi_dashboard.py:17` | Loads Tailwind from `cdn.tailwindcss.com` at runtime (unpinned, dev-only build) | Pin a version or ship compiled CSS |
| X-13 | P2 | ops | Flask development servers behind Caddy | Fine for one user; `waitress`/`gunicorn` if it grows |
| X-14 | P2 | ops | Region is Sydney (~150 ms to NSE) | Fine for paper; Mumbai (`ap-south-1`) before real money |

---

## 6. Recommended order of work

1. **Data first (D-1, D-2, D-4, D-5, D-6):** rebuild the futures CSV contract-correctly, validate it, and remove the destructive `fetch`.
2. **Make the backtester honest (E-1, E-2, E-3, E-4, E-5, E-7, E-8, U-2, U-3):** real costs, real lot size, SL-first, gap fills, `on_bar` every bar, day-change exit.
3. **One exit model (L-1, L-2, S1-3, S4-3):** live uses `PositionRiskManager` and the same entry convention as the backtest.
4. **Fix the 1-min/5-min confusion (S1-1, S2-4, S3-7, S5-3)** and the outright bugs (S2-1, S3-1–S3-5, S4-1, S4-2).
5. **Re-run every backtest with a real walk-forward (E-6, A-1).** Keep only strategies whose average gross pts/trade is well above about 15.
6. **Ops (X-3, X-4, L-5):** alerting, backups, feed watchdog.
7. **Intelligence (I-1 … I-5)** only after there's a strategy worth gating: build daily feature history and backtest the gate.
8. **Cleanup (X-5 … X-13, P2s).**

---

## 7. Tracker

Mark `[x]` when fixed, and add the commit hash.

- **P0:** [ ] D-1 · [ ] D-2 · [ ] E-1 · [ ] E-2 · [ ] S1-1 · [ ] S2-1 · [ ] S3-1 · [ ] S3-2 · [ ] S4-1 · [ ] S5-1 · [ ] L-1 · [ ] I-1 · [~] X-1 (removed from HEAD; history pending)
- **P1:** [ ] D-3 · [ ] D-4 · [ ] D-5 · [ ] D-6 · [ ] D-7 · [ ] U-1 · [ ] U-2 · [ ] U-3 · [ ] E-3 · [ ] E-4 · [ ] E-5 · [ ] E-6 · [ ] E-7 · [ ] E-8 · [ ] A-1 · [ ] S1-2 · [ ] S1-3 · [ ] S2-2 · [ ] S2-3 · [ ] S2-4 · [ ] S3-3 · [ ] S3-4 · [ ] S3-5 · [ ] S3-6 · [ ] S3-7 · [ ] S4-2 · [ ] S4-3 · [ ] S4-4 · [ ] S4-5 · [ ] S4-6 · [ ] S5-2 · [ ] S5-3 · [ ] L-2 · [ ] L-3 · [ ] L-4 · [ ] L-5 · [ ] L-6 · [ ] I-2 · [ ] I-3 · [ ] I-4 · [ ] I-5 · [ ] X-2 · [ ] X-3 · [ ] X-4
- **P2:** everything else in §5.

### Already done (2026-10-21)
- Live engine: realistic costs (`src/costs.py`), exchange lot size, paper trades saved to CSV, warmup from recent Upstox history, priming before the feed starts, intelligence gate off and threaded, SIGTERM clean shutdown.
- Dashboard: public HTTPS + password, CSRF protection on manual trades, `/auth` login page, offline redirect.
- Removed the hardcoded token from `scripts/get_options_data.py` (X-1, HEAD only).
