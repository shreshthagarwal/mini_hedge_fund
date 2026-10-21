"""
live_feed.py — Upstox MarketDataStreamerV3 WebSocket feed

Connects, subscribes on open, parses LTP (+ cumulative volume) from feed
messages, builds candles of `interval_min` minutes and fires on_candle() on
each bar close.

Optionally also subscribes to the spot index (ltpc mode) and forwards each
spot tick to on_spot_tick() — used by S5 for live futures-vs-spot basis.

Volume: the full feed carries `vtt` (volume traded today, cumulative).
Per-candle volume = increase in vtt over the candle's ticks.
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
        interval_min:   int = 5,
        spot_key:       Optional[str] = None,
        on_spot_tick:   Optional[Callable[[float, pd.Timestamp], None]] = None,
    ):
        self.token          = token
        self.instrument_key = instrument_key
        self.on_candle      = on_candle
        self.on_tick        = on_tick
        self.interval_min   = interval_min
        self.spot_key       = spot_key
        self.on_spot_tick   = on_spot_tick
        self._streamer      = None

        # Candle state
        self._o:      Optional[float]        = None
        self._h:      float                  = 0.0
        self._l:      float                  = float("inf")
        self._c:      float                  = 0.0
        self._v:      float                  = 0.0
        self._bar_ts: Optional[pd.Timestamp] = None

        # Cumulative day volume from the previous tick (None until first tick)
        self._last_vtt: Optional[float] = None

        self.last_price:   float                  = 0.0
        self.last_tick_ts: Optional[pd.Timestamp] = None
        self.last_spot:    float                  = 0.0

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
            if self.spot_key:
                self._streamer.subscribe([self.spot_key], "ltpc")
                logger.info(f"Subscribed to {self.spot_key} (spot, ltpc)")

        self._streamer.on("open",    on_open)
        self._streamer.on("message", self._on_message)
        self._streamer.on("error",   lambda *args: logger.warning(f"WS error: {args}"))
        self._streamer.on("close",   lambda *args: logger.info("WS closed"))
        self._streamer.connect()

    @staticmethod
    def _extract_ltp(feed: dict) -> Optional[float]:
        """
        Extract LTP from one instrument's entry in a Upstox V3 feed message.
        Message structure:
          {key} → fullFeed → marketFF → ltpc → ltp   (full mode, F&O / equity)
          {key} → fullFeed → indexFF  → ltpc → ltp   (full mode, index)
          {key} → ltpc → ltp                         (ltpc mode)
          {key} → ff → marketFF → ltpc → ltp         (older SDK versions)
        """
        for path in (("fullFeed", "marketFF", "ltpc", "ltp"),
                     ("fullFeed", "indexFF", "ltpc", "ltp"),
                     ("ltpc", "ltp"),
                     ("ff", "marketFF", "ltpc", "ltp")):
            node = feed
            try:
                for k in path:
                    node = node[k]
                return float(node)
            except (KeyError, TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _extract_vtt(feed: dict) -> Optional[float]:
        """Cumulative volume traded today (full mode only). Sent as a string."""
        for path in (("fullFeed", "marketFF", "vtt"), ("ff", "marketFF", "vtt")):
            node = feed
            try:
                for k in path:
                    node = node[k]
                return float(node)
            except (KeyError, TypeError, ValueError):
                continue
        return None

    def _on_message(self, msg):
        try:
            feeds = msg.get("feeds", {})
            ts    = pd.Timestamp.now(tz=IST)

            if self.spot_key and self.spot_key in feeds:
                spot = self._extract_ltp(feeds[self.spot_key])
                if spot is not None:
                    self.last_spot = spot
                    if self.on_spot_tick:
                        self.on_spot_tick(spot, ts)

            feed = feeds.get(self.instrument_key)
            if not feed:
                return
            ltp = self._extract_ltp(feed)
            if ltp is None:
                return

            # Per-tick volume = increase in cumulative day volume
            vtt    = self._extract_vtt(feed)
            volume = 0.0
            if vtt is not None:
                if self._last_vtt is not None and vtt >= self._last_vtt:
                    volume = vtt - self._last_vtt
                self._last_vtt = vtt

            self.last_price   = ltp
            self.last_tick_ts = ts

            if self.on_tick:
                self.on_tick(ltp, ts)

            self._update_candle(ltp, volume, ts)

        except Exception as e:
            logger.debug(f"Tick parse error: {e}  msg={str(msg)[:200]}")

    def _floor(self, ts: pd.Timestamp) -> pd.Timestamp:
        m = (ts.minute // self.interval_min) * self.interval_min
        return ts.replace(minute=m, second=0, microsecond=0)

    def _update_candle(self, price: float, volume: float, ts: pd.Timestamp):
        bucket = self._floor(ts)

        if self._bar_ts is None:
            self._bar_ts = bucket

        if bucket != self._bar_ts and self._o is not None:
            candle = pd.Series({
                "open":   self._o, "high": self._h,
                "low":    self._l, "close": self._c,
                "volume": self._v,
            }, name=self._bar_ts)
            logger.debug(
                f"[{self.interval_min}m BAR] {self._bar_ts.strftime('%H:%M')}  "
                f"O:{self._o:.0f} H:{self._h:.0f} "
                f"L:{self._l:.0f} C:{self._c:.0f} V:{self._v:.0f}"
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
