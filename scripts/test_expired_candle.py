"""
Test expired candle fetch directly — run to debug missing months.
python scripts/test_expired_candle.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import requests, json
from urllib.parse import quote

TOKEN_FILE = ".upstox_token"
token = open(TOKEN_FILE).read().strip()
hdrs  = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
BASE  = "https://api.upstox.com/v2"

# First — lookup the expired key for each month
def get_key(expiry_str):
    resp = requests.get(
        f"{BASE}/expired-instruments/future/contract",
        headers=hdrs,
        params={"instrument_key": "NSE_INDEX|Nifty 50", "expiry_date": expiry_str}
    )
    data = resp.json().get("data", [])
    for item in data:
        if item.get("instrument_type") == "FUT":
            return item.get("instrument_key")
    return None

# Test months
months = [
    ("Oct 2024", "2024-10-31", "2024-09-27", "2024-10-31"),
    ("Dec 2024", "2024-12-26", "2024-11-29", "2024-12-26"),
    ("Jan 2025", "2025-01-30", "2024-12-27", "2025-01-30"),
    ("Sep 2025", "2025-09-25", "2025-08-29", "2025-09-25"),
    ("Oct 2025", "2025-10-30", "2025-09-26", "2025-10-30"),
]

for name, expiry, from_d, to_d in months:
    print(f"\n{'='*50}")
    print(f"{name}  (expiry: {expiry})")

    key = get_key(expiry)
    if key is None:
        print(f"  Key lookup: NO KEY FOUND — contract not in expired database")
        continue
    print(f"  Key: {key}")

    # Fetch candles with URL-encoded key
    encoded = quote(key, safe="")
    url = f"{BASE}/expired-instruments/historical-candle/{encoded}/5minute/{to_d}/{from_d}"
    print(f"  URL: {url[:100]}...")

    resp = requests.get(url, headers=hdrs, timeout=15)
    data = resp.json()
    candles = data.get("data", {}).get("candles", [])

    print(f"  Status: {resp.status_code}  Candles: {len(candles)}")
    if candles:
        print(f"  First bar: {candles[-1][0]}  O:{candles[-1][1]}  C:{candles[-1][4]}")
        print(f"  Last bar:  {candles[0][0]}  O:{candles[0][1]}  C:{candles[0][4]}")
    elif resp.status_code != 200:
        print(f"  Error: {resp.text[:300]}")
    else:
        print(f"  Empty candles — data not available for this contract")