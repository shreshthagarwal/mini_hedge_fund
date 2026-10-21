"""
costs.py — realistic round-trip charges for one NSE index-futures trade.

Replaces the old flat COMMISSION_PER_LOT, which understated costs ~13x.
Statutory rates change with budgets/circulars — verify and override via .env:

    BROKERAGE_PER_ORDER   flat ₹ per executed order       (Upstox: 20)
    STT_FUTURES_RATE      on the SELL leg's notional      (0.05% from Budget 2026; was 0.02%)
    EXCH_TXN_RATE         NSE F&O, on both legs           (0.00173%)
    SEBI_FEE_RATE         on both legs                    (₹10 / crore)
    STAMP_DUTY_RATE       on the BUY leg                  (0.002%)
    GST_RATE              on brokerage + exch + SEBI fee  (18%)
"""

import os


def _rate(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def round_trip_cost(direction: str, entry: float, exit_price: float,
                    lots: int, lot_size: int) -> float:
    """Total ₹ charges for opening and closing `lots` lots."""
    qty = lots * lot_size
    if direction == "LONG":
        buy_val, sell_val = entry * qty, exit_price * qty
    else:
        buy_val, sell_val = exit_price * qty, entry * qty
    turnover = buy_val + sell_val

    brokerage = 2 * _rate("BROKERAGE_PER_ORDER", 20.0)
    stt       = sell_val * _rate("STT_FUTURES_RATE", 0.0005)
    exch      = turnover * _rate("EXCH_TXN_RATE",    0.0000173)
    sebi      = turnover * _rate("SEBI_FEE_RATE",    0.000001)
    stamp     = buy_val  * _rate("STAMP_DUTY_RATE",  0.00002)
    gst       = (brokerage + exch + sebi) * _rate("GST_RATE", 0.18)

    return round(brokerage + stt + exch + sebi + stamp + gst, 2)
