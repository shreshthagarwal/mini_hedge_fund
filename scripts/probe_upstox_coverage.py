"""
probe_upstox_coverage.py — find exactly which months Upstox has data for

Queries the expired future contract API for every month from 2022 to today,
then tries to fetch 1 candle to confirm data actually exists.

python scripts/probe_upstox_coverage.py
"""

import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import requests
from datetime import date, timedelta
from urllib.parse import quote
from utils import last_thursday, get_logger

logger = get_logger("probe")

TOKEN_FILE = ".upstox_token"
BASE       = "https://api.upstox.com/v2"
SLEEP      = 0.4


def load_token():
    with open(TOKEN_FILE) as f:
        return f.read().strip()


def hdrs(token):
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def get_key(token, expiry_str):
    resp = requests.get(
        f"{BASE}/expired-instruments/future/contract",
        headers=hdrs(token),
        params={"instrument_key": "NSE_INDEX|Nifty 50", "expiry_date": expiry_str},
        timeout=15,
    )
    time.sleep(SLEEP)
    if not resp.ok:
        return None, f"HTTP {resp.status_code}"
    data = resp.json().get("data", [])
    for item in data:
        if item.get("instrument_type") == "FUT":
            return item.get("instrument_key"), None
    return None, "empty"


def check_candles(token, key, from_d, to_d):
    encoded = quote(key, safe="")
    url = (f"{BASE}/expired-instruments/historical-candle"
           f"/{encoded}/5minute/{to_d}/{from_d}")
    resp = requests.get(url, headers=hdrs(token), timeout=15)
    time.sleep(SLEEP)
    if not resp.ok:
        return 0, f"HTTP {resp.status_code}"
    candles = resp.json().get("data", {}).get("candles", [])
    return len(candles), None


if __name__ == "__main__":
    token = load_token()
    today = date.today()

    print(f"\n{'Month':<12} {'Expiry':<12} {'Key found':<10} {'Candles':<10} {'Status'}")
    print("─" * 65)

    results = []
    y, m = 2022, 1

    while date(y, m, 1) <= today:
        exp = last_thursday(y, m)
        if exp >= today:
            m += 1
            if m > 12: m, y = 1, y+1
            continue

        exp_str  = exp.strftime("%Y-%m-%d")
        pm, py   = (m-1, y) if m > 1 else (12, y-1)
        prev_exp = last_thursday(py, pm)
        from_d   = (prev_exp + timedelta(days=1)).strftime("%Y-%m-%d")
        to_d     = exp_str

        # Step 1: get key
        key, err = get_key(token, exp_str)

        if key is None:
            status = f"NO KEY ({err})"
            print(f"{y}-{m:02d}       {exp_str}   {'NO':<10} {'--':<10} {status}")
            results.append((y, m, exp, False, 0))
        else:
            # Step 2: check candles (just fetch 1 day to confirm)
            # Use first day of period only for speed
            first_day = (prev_exp + timedelta(days=1)).strftime("%Y-%m-%d")
            next_day  = (prev_exp + timedelta(days=5)).strftime("%Y-%m-%d")
            n, err2   = check_candles(token, key, first_day, next_day)
            has_data  = n > 0
            status    = "OK" if has_data else "KEY EXISTS BUT NO DATA"
            print(f"{y}-{m:02d}       {exp_str}   {'YES':<10} {n:<10} {status}")
            results.append((y, m, exp, has_data, n))

        m += 1
        if m > 12: m, y = 1, y+1

    print("\n" + "─" * 65)
    print("SUMMARY")
    print("─" * 65)
    ok      = [(y,m) for y,m,_,has,_ in results if has]
    no_key  = [(y,m) for y,m,_,has,_ in results if not has and _ == 0]
    no_data = [(y,m) for y,m,exp,has,n in results if not has and n == 0 and exp < today]

    print(f"Months with data    : {len(ok)}")
    print(f"Months with no key  : {len([r for r in results if not r[3] and r[4]==0])}")
    print(f"Key exists, no data : {len([r for r in results if not r[3] and r[4]==0 and r[2] < today])}")
    print("\nMonths available for fetch:")
    for y, m, exp, has, n in results:
        if has:
            print(f"  {y}-{m:02d}  [{exp}]")
    print("\nMonths missing (no key):")
    for y, m, exp, has, n in results:
        if not has:
            print(f"  {y}-{m:02d}  [{exp}]")