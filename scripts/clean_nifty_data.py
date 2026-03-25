import pandas as pd
import numpy as np

INPUT_FILE = "data/nifty_futures_5min.csv"
OUTPUT_FILE = "data/nifty_futures_5min_clean.csv"

# Trading session times
SESSION_START = "09:15"
SESSION_END = "15:25"
FREQ = "5min"


def load_data():
    df = pd.read_csv(INPUT_FILE, parse_dates=["datetime"])
    df = df.sort_values("datetime").reset_index(drop=True)
    return df


def fix_duplicate_timestamps(df):
    """
    Handles contract overlap:
    - Aggregates multiple rows per timestamp
    """
    df = df.groupby("datetime").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum"
    }).reset_index()

    return df


def generate_full_index(df):
    """
    Creates full expected timestamp index for all trading days
    """
    all_days = pd.date_range(
        df["datetime"].dt.date.min(),
        df["datetime"].dt.date.max(),
        freq="B"  # business days
    )

    full_index = []

    for day in all_days:
        intraday_range = pd.date_range(
            f"{day.date()} {SESSION_START}",
            f"{day.date()} {SESSION_END}",
            freq=FREQ
        )
        full_index.extend(intraday_range)

    return pd.DatetimeIndex(full_index)


def fill_missing_bars(df, full_index):
    """
    Reindex + fill missing candles
    """
    df = df.set_index("datetime").reindex(full_index)

    # Forward fill close
    df["close"] = df["close"].ffill()

    # Fill OHLC properly
    df["open"] = df["open"].fillna(df["close"])
    df["high"] = df["high"].fillna(df["close"])
    df["low"] = df["low"].fillna(df["close"])

    # Volume = 0 for synthetic bars
    df["volume"] = df["volume"].fillna(0)

    df = df.reset_index().rename(columns={"index": "datetime"})

    return df


def validate_daily_bars(df):
    """
    Ensures each day has exactly 75 bars
    """
    counts = df.groupby(df["datetime"].dt.date).size()

    bad_days = counts[counts != 75]

    if len(bad_days) > 0:
        print("\n⚠️ Days with incorrect bar count:")
        print(bad_days)
    else:
        print("\n✅ All days have exactly 75 bars")

    return df


def add_flags(df):
    """
    Adds useful flags for modeling
    """
    df["is_synthetic"] = (df["volume"] == 0).astype(int)

    # returns
    df["returns"] = df["close"].pct_change()

    return df


def main():
    print("Loading data...")
    df = load_data()

    print("Fixing duplicate timestamps (contract overlap)...")
    df = fix_duplicate_timestamps(df)

    print("Generating full trading index...")
    full_index = generate_full_index(df)

    print("Filling missing bars...")
    df = fill_missing_bars(df, full_index)

    print("Validating bar counts...")
    df = validate_daily_bars(df)

    print("Adding features...")
    df = add_flags(df)

    print("Saving cleaned data...")
    df.to_csv(OUTPUT_FILE, index=False)

    print("\n🎉 Cleaning complete!")
    print(f"Saved to: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()