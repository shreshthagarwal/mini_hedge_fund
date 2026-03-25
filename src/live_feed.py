"""
live_feed.py — Upstox MarketDataStreamerV3 WebSocket feed

Connects, subscribes on open, parses LTP from full feed messages,
builds 1-min candles, fires on_candle() on each bar close.
"""

import threading
from typing import Callable, Optional
from zoneinfo import ZoneInfo
import pandas as pd

from utils import get_logger

logger = get_logger("live_feed")
IST    = ZoneInfo("Asia/Kolkata")


class LiveFeed:

    def __init__(
        self,
        token:          str,
        instrument_key: str,
        on_candle:      Callable[[pd.Series], None],
        on_tick:        Optional[Callable[[float, pd.Timestamp], None]] = None,
    ):
        self.token          = token
        self.instrument_key = instrument_key
        self.on_candle      = on_candle
        self.on_tick        = on_tick
        self._streamer      = None

        # Candle state
        self._o:      Optional[float]        = None
        self._h:      float                  = 0.0
        self._l:      float                  = float("inf")
        self._c:      float                  = 0.0
        self._v:      float                  = 0.0
        self._bar_ts: Optional[pd.Timestamp] = None

        self.last_price:   float                  = 0.0
        self.last_tick_ts: Optional[pd.Timestamp] = None

    def start(self):
        t = threading.Thread(target=self._connect, daemon=True)
        t.start()
        logger.info(f"Live feed starting for {self.instrument_key}")

    def stop(self):
        if self._streamer:
            try:
                self._streamer.disconnect()
            except:
                pass
        logger.info("Live feed stopped")

    def _connect(self):
        import upstox_client

        config = upstox_client.Configuration()
        config.access_token = self.token
        client  = upstox_client.ApiClient(config)

        # Correct pattern: instantiate without keys, subscribe inside on_open
        self._streamer = upstox_client.MarketDataStreamerV3(client)

        def on_open(*args):
            logger.info("WS connected — subscribing...")
            self._streamer.subscribe([self.instrument_key], "full")
            logger.info(f"Subscribed to {self.instrument_key}")

        self._streamer.on("open",    on_open)
        self._streamer.on("message", self._on_message)
        self._streamer.on("error",   lambda *args: logger.warning(f"WS error: {args}"))
        self._streamer.on("close",   lambda *args: logger.info("WS closed"))
        self._streamer.connect()

    def _extract_ltp(self, msg: dict) -> Optional[float]:
        """
        Extract LTP from Upstox V3 full feed message.
        Message structure (full mode):
          feeds → {key} → fullFeed → marketFF → ltpc → ltp
          feeds → {key} → ltpc → ltp              (ltpc mode / simple messages)
        """
        feeds = msg.get("feeds", {})
        feed  = feeds.get(self.instrument_key, {})

        if not feed:
            return None

        # Try fullFeed path first (full subscription mode)
        try:
            ltp = feed["fullFeed"]["marketFF"]["ltpc"]["ltp"]
            return float(ltp)
        except (KeyError, TypeError):
            pass

        # Try direct ltpc path (ltpc mode or simplified messages)
        try:
            ltp = feed["ltpc"]["ltp"]
            return float(ltp)
        except (KeyError, TypeError):
            pass

        # Try ff path (older SDK versions)
        try:
            ltp = feed["ff"]["marketFF"]["ltpc"]["ltp"]
            return float(ltp)
        except (KeyError, TypeError):
            pass

        return None

    def _on_message(self, msg):
        try:
            ltp = self._extract_ltp(msg)
            if ltp is None:
                return

            ts = pd.Timestamp.now(tz=IST)
            self.last_price   = ltp
            self.last_tick_ts = ts

            if self.on_tick:
                self.on_tick(ltp, ts)

            self._update_candle(ltp, 0.0, ts)

        except Exception as e:
            logger.debug(f"Tick parse error: {e}  msg={str(msg)[:200]}")

    @staticmethod
    def _floor_1min(ts: pd.Timestamp) -> pd.Timestamp:
        return ts.replace(second=0, microsecond=0)

    def _update_candle(self, price: float, volume: float, ts: pd.Timestamp):
        bucket = self._floor_1min(ts)

        if self._bar_ts is None:
            self._bar_ts = bucket

        if bucket != self._bar_ts and self._o is not None:
            candle = pd.Series({
                "open":   self._o, "high": self._h,
                "low":    self._l, "close": self._c,
                "volume": self._v,
            }, name=self._bar_ts)
            logger.debug(
                f"[1m BAR] {self._bar_ts.strftime('%H:%M')}  "
                f"O:{self._o:.0f} H:{self._h:.0f} "
                f"L:{self._l:.0f} C:{self._c:.0f}"
            )
            self.on_candle(candle)
            self._o = None; self._h = 0.0
            self._l = float("inf"); self._v = 0.0
            self._bar_ts = bucket

        if self._o is None:
            self._o = price
        self._h  = max(self._h, price)
        self._l  = min(self._l, price)
        self._c  = price
        self._v += volume