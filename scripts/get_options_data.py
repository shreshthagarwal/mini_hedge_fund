import requests
import pandas as pd
import os
from urllib.parse import quote
from time import sleep

# ================= CONFIG =================
# Token comes from .upstox_token (or UPSTOX_ACCESS_TOKEN) — never hardcode it.
ACCESS_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip() or (
    open(".upstox_token").read().strip() if os.path.exists(".upstox_token") else ""
)
UNDERLYING = "NSE_INDEX|Nifty 50"
EXPIRY = "2024-10-31"
FROM_DATE = "2024-10-01"
TO_DATE = "2024-10-31"
INTERVAL = "5minute"

SAVE_DIR = "options_data"
os.makedirs(SAVE_DIR, exist_ok=True)


# ================= HEADERS =================
def get_headers():
    return {
        'Accept': 'application/json',
        'Authorization': f'Bearer {ACCESS_TOKEN}'
    }


# ================= FETCH ALL CONTRACTS =================
def fetch_all_contracts():
    url = "https://api.upstox.com/v2/expired-instruments/option/contract"

    params = {
        'instrument_key': UNDERLYING,
        'expiry_date': EXPIRY
    }

    resp = requests.get(url, headers=get_headers(), params=params)

    if resp.status_code != 200:
        print("❌ Error fetching contracts:", resp.text)
        return []

    data = resp.json().get("data", [])
    print(f"✅ Total contracts found: {len(data)}")

    return data


# ================= FETCH CANDLES =================
def fetch_candles(instrument_key):
    encoded_key = quote(instrument_key, safe='|')

    url = f"https://api.upstox.com/v2/expired-instruments/historical-candle/{encoded_key}/{INTERVAL}/{TO_DATE}/{FROM_DATE}"

    resp = requests.get(url, headers=get_headers())

    if resp.status_code != 200:
        print(f"❌ Candle error ({instrument_key}):", resp.text)
        return None

    candles = resp.json().get("data", {}).get("candles", [])

    if not candles:
        return None

    df = pd.DataFrame(candles, columns=[
        'timestamp', 'open', 'high', 'low', 'close', 'volume', 'oi'
    ])

    return df


# ================= SAVE CSV =================
def save_csv(df, contract):
    symbol = contract['trading_symbol'].replace(" ", "_")
    filename = f"{symbol}.csv"

    path = os.path.join(SAVE_DIR, filename)

    df.to_csv(path, index=False)


# ================= MAIN PIPELINE =================
def main():
    contracts = fetch_all_contracts()

    success = 0
    failed = 0
    skipped = 0

    for i, contract in enumerate(contracts):
        key = contract['instrument_key']
        symbol = contract['trading_symbol']

        print(f"\n[{i+1}/{len(contracts)}] Fetching: {symbol}")

        df = fetch_candles(key)

        if df is None or df.empty:
            print("⚠️ No data")
            skipped += 1
            continue

        save_csv(df, contract)
        print("✅ Saved")

        success += 1

        # avoid rate limits
        sleep(0.2)

    print("\n========= SUMMARY =========")
    print(f"✅ Success: {success}")
    print(f"⚠️ Skipped (no data): {skipped}")
    print(f"❌ Failed: {failed}")


# ================= RUN =================
if __name__ == "__main__":
    main()