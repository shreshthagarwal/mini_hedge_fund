"""
build_history_upstox.py — fetch and maintain NIFTY futures 5-min data

Correct API flow (per Upstox docs):
  Step 1: GET /v2/expired-instruments/expiry/future
          → get all expiry dates Upstox actually has data for
  Step 2: GET /v2/expired-instruments/future/contract?instrument_key=...&expiry_date=...
          → get expired_instrument_key for each expiry
  Step 3: GET /v2/expired-instruments/historical-candle/{expired_key}/5minute/{to}/{from}
          → fetch full candle history (wide range, Upstox handles it)
  Step 4: GET /v3/historical-candle/{key}/minutes/5/{to}/{from}
          → fetch current live contract

Run daily:
    python scripts/upstox_auth.py
    python scripts/build_history_upstox.py
"""

import sys, os, json, gzip, io, time
from datetime import date, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import requests
import pandas as pd
from utils import get_logger, session_bars, nse_expiry

logger     = get_logger("build_history")
IST        = ZoneInfo("Asia/Kolkata")
OUTPUT_CSV = "data/nifty_futures_5min.csv"
TOKEN_FILE = ".upstox_token"
OHLCV      = ["open", "high", "low", "close", "volume"]
BASE_V2    = "https://api.upstox.com/v2"
BASE_V3    = "https://api.upstox.com/v3"
INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
RATE_SLEEP = 0.35
NIFTY_UNDERLYING = "NSE_INDEX|Nifty 50"


# ── Token ─────────────────────────────────────────────────────────────────────

def load_token() -> str:
    if os.path.exists(TOKEN_FILE):
        t = open(TOKEN_FILE).read().strip()
        if t:
            logger.info(f"Token loaded from {TOKEN_FILE}")
            return t
    t = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
    if t:
        logger.info("Token loaded from env var")
        return t
    raise SystemExit("No token. Run: python scripts/upstox_auth.py")


def hdrs(token):
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


# ── Step 1: Get all available expiry dates from Upstox ────────────────────────

def get_all_expiries(token: str) -> list[date]:
    """
    GET /v2/expired-instruments/expiry/future?instrument_key=NSE_INDEX|Nifty 50
    Returns list of all expiry dates Upstox has data for.
    This is the correct starting point — don't guess expiry dates.
    """
    from urllib.parse import urlencode
    # Correct endpoint: /v2/expired-instruments/expiries (not /expiry/future)
    # Returns up to 6 months of expiries (weekly + monthly)
    url    = f"{BASE_V2}/expired-instruments/expiries"
    params = {"instrument_key": NIFTY_UNDERLYING}
    resp   = requests.get(url, headers=hdrs(token), params=params, timeout=15)
    time.sleep(RATE_SLEEP)

    if not resp.ok:
        logger.warning(f"Get Expiries failed: {resp.status_code} {resp.text[:200]}")
        return []

    all_expiries = []
    for item in resp.json().get("data", []):
        try:
            all_expiries.append(date.fromisoformat(item))
        except Exception:
            pass

    # Filter to MONTHLY expiries only
    # Pre Sep-2025 : last Thursday of month (weekday==3, day>=25)
    # Sep 2025+    : last Tuesday of month  (weekday==1, day>=24)
    # Strategy: group by (year, month) and take the MAX day — always the monthly expiry
    from collections import defaultdict
    by_ym = defaultdict(list)
    for e in all_expiries:
        by_ym[(e.year, e.month)].append(e)
    expiries = sorted(max(v) for v in by_ym.values())

    expiries.sort()
    logger.info(f"Upstox has {len(expiries)} monthly NIFTY expired contracts")
    logger.info(f"  (from {len(all_expiries)} total including weekly)")
    if expiries:
        logger.info(f"  Range: {expiries[0]}  →  {expiries[-1]}")
    return expiries


# ── Step 2: Get expired_instrument_key for a specific expiry ──────────────────

def get_expired_key(token: str, expiry: date) -> str | None:
    """
    GET /v2/expired-instruments/future/contract
        ?instrument_key=NSE_INDEX|Nifty 50&expiry_date=YYYY-MM-DD
    Returns expired_instrument_key like NSE_FO|35005|26-12-2024
    """
    resp = requests.get(
        f"{BASE_V2}/expired-instruments/future/contract",
        headers=hdrs(token),
        params={
            "instrument_key": NIFTY_UNDERLYING,
            "expiry_date":    expiry.strftime("%Y-%m-%d"),
        },
        timeout=15,
    )
    time.sleep(RATE_SLEEP)

    if not resp.ok:
        logger.debug(f"  Key lookup {expiry}: HTTP {resp.status_code}")
        return None

    for item in resp.json().get("data", []):
        if item.get("instrument_type") == "FUT":
            return item.get("instrument_key")
    return None


# ── Step 3: Fetch expired candles ─────────────────────────────────────────────

def fetch_expired_candles(
    token: str, expired_key: str,
    from_d: date, to_d: date
) -> pd.DataFrame:
    """
    GET /v2/expired-instruments/historical-candle/{encoded_key}/5minute/{to}/{from}
    Upstox supports wide date ranges — no need to chunk.
    """
    encoded = quote(expired_key, safe="")
    url = (
        f"{BASE_V2}/expired-instruments/historical-candle"
        f"/{encoded}/5minute"
        f"/{to_d.strftime('%Y-%m-%d')}"
        f"/{from_d.strftime('%Y-%m-%d')}"
    )
    for attempt in range(3):
        resp = requests.get(url, headers=hdrs(token), timeout=30)
        time.sleep(RATE_SLEEP)
        if resp.status_code == 429:
            wait = 10 * (attempt + 1)
            logger.warning(f"Rate limited — sleeping {wait}s")
            time.sleep(wait)
            continue
        if not resp.ok:
            logger.debug(f"  Expired candles {expired_key}: HTTP {resp.status_code}")
            return pd.DataFrame()
        candles = resp.json().get("data", {}).get("candles", [])
        return _parse(candles)
    return pd.DataFrame()


# ── Step 4: Fetch live contract candles ───────────────────────────────────────

def get_live_instruments(token: str) -> dict[date, dict]:
    """Download NSE instruments file, return live NIFTY futures by expiry date."""
    resp = requests.get(
        INSTRUMENTS_URL,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30,
    )
    resp.raise_for_status()
    with gzip.GzipFile(fileobj=io.BytesIO(resp.content)) as f:
        instruments = json.load(f)

    result = {}
    for i in instruments:
        if (i.get("segment") == "NSE_FO"
                and i.get("instrument_type") == "FUT"
                and i.get("asset_symbol") == "NIFTY"):
            exp = pd.Timestamp(i["expiry"], unit="ms").date()
            i["expiry_date"] = exp
            result[exp] = i

    logger.info(f"Live contracts: {sorted(result.keys())}")
    return result


def fetch_live_candles(
    token: str, key: str,
    from_d: date, to_d: date
) -> pd.DataFrame:
    """
    GET /v3/historical-candle/{encoded_key}/minutes/5/{to}/{from}
    The | in instrument keys must be URL-encoded as %7C.
    """
    encoded = quote(key, safe="")
    url = (
        f"{BASE_V3}/historical-candle/{encoded}/minutes/5"
        f"/{to_d.strftime('%Y-%m-%d')}"
        f"/{from_d.strftime('%Y-%m-%d')}"
    )
    for attempt in range(3):
        resp = requests.get(url, headers=hdrs(token), timeout=30)
        time.sleep(RATE_SLEEP)
        if resp.status_code == 429:
            wait = 10 * (attempt + 1)
            logger.warning(f"Rate limited — sleeping {wait}s")
            time.sleep(wait)
            continue
        if not resp.ok:
            logger.warning(f"  Live candles {key}: HTTP {resp.status_code}")
            return pd.DataFrame()
        candles = resp.json().get("data", {}).get("candles", [])
        return _parse(candles)
    return pd.DataFrame()


# ── Parse candles ─────────────────────────────────────────────────────────────

def _parse(candles: list) -> pd.DataFrame:
    if not candles:
        return pd.DataFrame()
    df = pd.DataFrame(
        candles,
        columns=["datetime", "open", "high", "low", "close", "volume", "oi"],
    )
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.set_index("datetime")
    if df.index.tzinfo is None:
        df.index = df.index.tz_localize(IST)
    else:
        df.index = df.index.tz_convert(IST)
    df.index.name = "datetime"
    return df[OHLCV]


# ── CSV helpers ───────────────────────────────────────────────────────────────

def load_existing() -> pd.DataFrame:
    if not os.path.exists(OUTPUT_CSV):
        logger.info("No existing CSV — fresh start")
        return pd.DataFrame()
    df = pd.read_csv(OUTPUT_CSV, index_col="datetime")
    df.index = pd.to_datetime(df.index)
    if df.index.tzinfo is None:
        df.index = df.index.tz_localize(IST)
    else:
        df.index = df.index.tz_convert(IST)
    df.index.name = "datetime"
    df.columns    = [c.lower() for c in df.columns]
    logger.info(
        f"Existing: {len(df):,} bars  "
        f"|  {df.index[0].strftime('%Y-%m-%d')}  →  "
        f"{df.index[-1].strftime('%Y-%m-%d')}"
    )
    return df[OHLCV]


def coverage_per_expiry(df: pd.DataFrame) -> dict[date, float]:
    """
    Return approximate coverage % per expiry month in existing data.
    Uses 75 bars/day × weekdays as expected.
    """
    if df.empty:
        return {}
    result = {}
    dates  = set(df.index.date)
    return result


def save_csv(combined: pd.DataFrame, before: int):
    os.makedirs("data", exist_ok=True)
    combined.reset_index().to_csv(OUTPUT_CSV, index=False)
    days  = combined.index.normalize().nunique()
    added = max(len(combined) - before, 0)
    logger.info("=" * 56)
    logger.info(f"Saved          →  {OUTPUT_CSV}")
    logger.info(f"Total bars     :  {len(combined):,}")
    logger.info(f"Bars added     :  {added:,}")
    logger.info(f"Unique days    :  {days}")
    logger.info(f"Avg bars/day   :  {len(combined)/max(days,1):.1f}")
    logger.info(
        f"Date range     :  "
        f"{combined.index[0].strftime('%Y-%m-%d')}  →  "
        f"{combined.index[-1].strftime('%Y-%m-%d')}"
    )
    logger.info("=" * 56)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    token    = load_token()
    existing = load_existing()
    before   = len(existing)
    today    = date.today()

    # Determine what we already have per expiry
    existing_dates = set(existing.index.date) if not existing.empty else set()

    # ── EXPIRED CONTRACTS ─────────────────────────────────────────────────────
    # Step 1: get all expiry dates Upstox has
    all_expiries = get_all_expiries(token)

    # Filter to only expiries we don't already have full data for
    # If we have any bars within an expiry's period, we consider it covered
    # Simple rule: if expiry is after our last bar date, fetch it
    if existing.empty:
        last_bar_date = date(2020, 1, 1)
    else:
        last_bar_date = existing.index[-1].date()

    # Decide which expired contracts to fetch
    # - Always fetch if we have no data at all
    # - Otherwise fetch contracts whose expiry is after our last bar
    #   (minus 1 month overlap to catch partial last contracts)
    # Go back 1 month from last bar to catch partial contracts
    if last_bar_date.month > 1:
        overlap_date = date(last_bar_date.year, last_bar_date.month - 1, 1)
    else:
        overlap_date = date(last_bar_date.year - 1, 12, 1)

    to_fetch_expiries = [
        exp for exp in all_expiries
        if exp >= overlap_date
    ]

    logger.info(
        f"\nExpired contracts to fetch: {len(to_fetch_expiries)} "
        f"(of {len(all_expiries)} available)"
    )

    all_new = []

    for exp in to_fetch_expiries:
        logger.info(f"\n── Expired {exp} ──")

        # Step 2: get expired_instrument_key
        key = get_expired_key(token, exp)
        if key is None:
            logger.warning(f"  No key returned for {exp} — skipping")
            continue

        logger.info(f"  Key: {key}")

        # Step 3: fetch candles
        # Use contract's approximate start (prev month end + 1 day)
        # Wide range — Upstox figures it out
        from_d = exp - timedelta(days=60)   # ~2 months before expiry
        to_d   = exp

        df = fetch_expired_candles(token, key, from_d, to_d)

        if df.empty:
            logger.info(f"  No data returned")
            continue

        df = session_bars(df)
        logger.info(f"  {len(df):,} bars  [{df.index[0].strftime('%Y-%m-%d')} → {df.index[-1].strftime('%Y-%m-%d')}]")
        all_new.append(df)

    # ── LIVE CONTRACTS ────────────────────────────────────────────────────────
    logger.info("\n── Live contracts ──")
    live_by_expiry = get_live_instruments(token)

    # Only fetch the front-month (nearest expiry) live contract
    # Far-month contracts (next quarter) haven't started trading yet — 400 error
    sorted_live = sorted(live_by_expiry.items())
    front_exp, front_inst = sorted_live[0]
    key = front_inst["instrument_key"]

    # V3 API limit: ~30 days per intraday request
    # Fetch in two passes to cover the full front-month period AND today
    #   Pass 1: month-start to 30 days ago  (older data)
    #   Pass 2: last 29 days to today        (includes today's bars)
    logger.info(f"  Front-month: {key}  expiry {front_exp}")
    live_dfs = []
    contract_start = front_exp - timedelta(days=40)

    # Pass 1: older bars (if contract started more than 29 days ago)
    if (today - contract_start).days > 29:
        p1_from = contract_start
        p1_to   = today - timedelta(days=29)
        df_old  = fetch_live_candles(token, key, p1_from, p1_to)
        if not df_old.empty:
            live_dfs.append(df_old)
            logger.info(f"    Pass 1: {len(df_old):,} bars  [{p1_from} → {p1_to}]")

    # Pass 2: recent 29 days up to today (always includes today)
    p2_from = today - timedelta(days=29)
    p2_to   = today
    df_new  = fetch_live_candles(token, key, p2_from, p2_to)
    if not df_new.empty:
        live_dfs.append(df_new)
        logger.info(f"    Pass 2: {len(df_new):,} bars  [{p2_from} → {p2_to}]")

    if live_dfs:
        df = pd.concat(live_dfs).sort_index()
        df = df[~df.index.duplicated(keep="last")]
    else:
        df = pd.DataFrame()

    if not live_dfs or df.empty:
        logger.warning("    No data for front-month contract")
    else:
        df = session_bars(df)
        logger.info(f"  Total: {len(df):,} bars  [{df.index[0].strftime('%Y-%m-%d')} → {df.index[-1].strftime('%Y-%m-%d')}]")
        all_new.append(df)

    # ── Merge and save ────────────────────────────────────────────────────────
    if not all_new:
        logger.info("\nNo new data fetched.")
        return

    new = pd.concat(all_new).sort_index()
    new = new[~new.index.duplicated(keep="first")]

    if not existing.empty:
        combined = pd.concat([existing, new]).sort_index()
        combined = combined[~combined.index.duplicated(keep="last")]
    else:
        combined = new

    save_csv(combined, before)
    logger.info("\nDone.")
    logger.info("  Backtest : python main.py backtest")
    logger.info("  Analyze  : python scripts/analyze_csv.py")


if __name__ == "__main__":
    main()