"""
src/risk_management.py — Advanced Position Risk Management

Solves the core problem: targets too far, price nearly reaches then reverses,
hits stop loss. Result: a near-winner becomes a full loser.

Four mechanisms, all bar-by-bar, applied to every open position:

1. DUAL TARGET SYSTEM
   T1 (partial exit) = 1.0x SL distance — easier to reach, locks in profit
   T2 (full exit)    = original target  — let remainder run
   At T1: exit 50% of position, move SL to breakeven on remainder
   At T2: exit remainder
   Net effect: guaranteed profit on half the position once T1 is touched.

2. TRAILING STOP LOSS
   Once price moves > TRAIL_ACTIVATION_R × SL_dist in favour:
     - SL trails at TRAIL_OFFSET_R × SL_dist behind the best price seen
   This replaces the fixed SL with a dynamic one that captures more
   of the move when price trends, while cutting losses when it reverses.

3. BREAKEVEN STOP
   Once price moves > BE_ACTIVATION_R × SL_dist in favour:
     - SL moved to entry price + 1 tick (never lose on this trade)
   Activates before trailing stop — earlier protection.

4. TIME-BASED STOP
   If trade is open for MAX_BARS_HELD bars without reaching T1:
     - Exit at market. The thesis didn't play out in reasonable time.
   Avoids "forever trades" that occupy capital while drifting.

These are applied in priority order per bar:
  square-off > T2 hit > trailing SL hit > T1 hit > breakeven trigger
  > trailing activation > time stop > original SL

Usage in multi_engine.py — managed inside StrategyState:
  state.enter(direction, entry, sl, target, ts, sl_dist=sig.sl_dist)
  # PositionRiskManager is created automatically inside enter()
  # check_exits() calls rm.check() every bar
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import time
from typing import Optional

logger = logging.getLogger("risk_mgmt")


# ── Risk config ───────────────────────────────────────────────────────────────

@dataclass
class RiskConfig:
    """
    Risk management parameters. One config per strategy.
    All distances expressed as multiples of the initial SL distance.
    """

    # ── Dual target ──────────────────────────────────────────────────────────
    use_dual_target:      bool  = True
    t1_r:                 float = 1.0    # T1 = entry ± t1_r × sl_dist
    t1_exit_fraction:     float = 0.5    # fraction to exit at T1

    # ── Breakeven stop ────────────────────────────────────────────────────────
    use_breakeven:        bool  = True
    be_activation_r:      float = 0.6    # move SL to BE once price moves this far
    be_buffer_ticks:      int   = 2      # SL = entry ± N ticks (small buffer)

    # ── Trailing stop ─────────────────────────────────────────────────────────
    use_trailing:         bool  = True
    trail_activation_r:   float = 1.0    # start trailing once price moves this far
    trail_offset_r:       float = 0.5    # trail SL this far behind best price

    # ── Time stop ─────────────────────────────────────────────────────────────
    use_time_stop:        bool  = True
    max_bars_in_trade:    int   = 24     # exit if T1 not hit after N bars

    # ── Hard limits ───────────────────────────────────────────────────────────
    max_loss_per_trade_r: float = 1.0
    tick_size:            float = 0.05


# ── Per-strategy risk configs ─────────────────────────────────────────────────

RISK_CONFIGS = {
    "S1": RiskConfig(
        use_dual_target    = True,
        t1_r               = 1.0,
        t1_exit_fraction   = 0.5,
        use_breakeven      = True,
        be_activation_r    = 0.6,
        use_trailing       = True,
        trail_activation_r = 1.2,
        trail_offset_r     = 0.4,
        use_time_stop      = True,
        max_bars_in_trade  = 24,
    ),
    "S2": RiskConfig(
        use_dual_target    = True,
        t1_r               = 0.8,
        t1_exit_fraction   = 0.6,
        use_breakeven      = True,
        be_activation_r    = 0.5,
        use_trailing       = False,   # mean reversion — trailing hurts
        use_time_stop      = True,
        max_bars_in_trade  = 10,
    ),
    "S3": RiskConfig(
        use_dual_target    = True,
        t1_r               = 1.0,
        t1_exit_fraction   = 0.5,
        use_breakeven      = True,
        be_activation_r    = 0.7,
        use_trailing       = True,
        trail_activation_r = 1.0,
        trail_offset_r     = 0.5,
        use_time_stop      = True,
        max_bars_in_trade  = 18,
    ),
    "S4": RiskConfig(
        use_dual_target    = True,
        t1_r               = 0.8,
        t1_exit_fraction   = 0.7,
        use_breakeven      = True,
        be_activation_r    = 0.4,
        use_trailing       = True,
        trail_activation_r = 0.8,
        trail_offset_r     = 0.3,
        use_time_stop      = True,
        max_bars_in_trade  = 12,
    ),
    "S5": RiskConfig(
        use_dual_target    = False,   # basis arb — single target
        use_breakeven      = True,
        be_activation_r    = 0.5,
        use_trailing       = False,   # basis reverts cleanly
        use_time_stop      = True,
        max_bars_in_trade  = 30,
    ),
}


# ── Position risk state ───────────────────────────────────────────────────────

@dataclass
class PositionRisk:
    """
    All risk management state for one open position.
    Updated bar-by-bar by PositionRiskManager.
    """
    # Fixed at entry
    direction:       str
    entry:           float
    sl_dist:         float    # original SL distance in points
    sl:              float    # current SL (only ever moves in trader's favour)
    target:          float    # T2 — full exit target
    t1:              float    # T1 — partial exit target (0 if dual target off)
    cfg:             RiskConfig

    # Dynamic state (all False/0 at entry)
    best_price:      float = 0.0
    sl_moved_to_be:  bool  = False
    trailing_active: bool  = False
    t1_hit:          bool  = False
    lots_remaining:  int   = 1
    bars_held:       int   = 0

    @classmethod
    def create(
        cls,
        direction:  str,
        entry:      float,
        sl_dist:    float,
        target:     float,
        lots:       int,
        cfg:        RiskConfig,
        tick_size:  float = 0.05,
    ) -> "PositionRisk":
        def rt(price: float) -> float:
            return round(round(price / tick_size) * tick_size, 2)

        if direction == "LONG":
            sl = rt(entry - sl_dist)
            t1 = rt(entry + cfg.t1_r * sl_dist) if cfg.use_dual_target else 0.0
        else:
            sl = rt(entry + sl_dist)
            t1 = rt(entry - cfg.t1_r * sl_dist) if cfg.use_dual_target else 0.0

        return cls(
            direction      = direction,
            entry          = entry,
            sl_dist        = sl_dist,
            sl             = sl,
            target         = target,
            t1             = t1,
            cfg            = cfg,
            best_price     = entry,
            lots_remaining = lots,
        )

    def favour_dist(self, price: float) -> float:
        """Points moved in our favour from entry."""
        return (price - self.entry) if self.direction == "LONG" else (self.entry - price)

    def update_best(self, high: float, low: float):
        if self.direction == "LONG":
            self.best_price = max(self.best_price, high)
        else:
            self.best_price = min(self.best_price, low)


# ── PositionRiskManager ───────────────────────────────────────────────────────

class PositionRiskManager:
    """
    Bar-by-bar risk manager for one open position.

    Call check() on every completed bar.
    Returns (exit_price, reason, lots_to_exit):
      - lots_to_exit == 0            → no exit, position continues
      - lots_to_exit == lots_remaining → full exit
      - 0 < lots_to_exit < lots_remaining → partial exit (T1)

    Priority order per bar:
      1. Square-off time
      2. T2 hit (full exit at target)
      3. Current SL hit (original / BE / trailing)
      4. T1 hit (partial exit → SL to BE, trailing starts)
      5. Trailing stop update (SL ratchets, no exit)
      6. Breakeven activation (SL moves to entry, no exit)
      7. Time stop (T1 never hit after max_bars_in_trade)
    """

    def __init__(self, pos: PositionRisk):
        self.pos = pos

    # ── Logging helpers ───────────────────────────────────────────────────────

    def _tag(self) -> str:
        """Short prefix for log lines: [S4|SHORT@22961]"""
        p = self.pos
        return f"[{p.direction}@{p.entry:.0f}]"

    def _log(self, msg: str):
        logger.info(f"{self._tag()} {msg}")

    # ── Main check ────────────────────────────────────────────────────────────

    def check(
        self,
        high:       float,
        low:        float,
        bar_open:   float,
        ts,
        square_off: time,
    ) -> tuple[Optional[float], Optional[str], int]:
        """
        Returns (exit_price, reason, lots_to_exit).
        lots_to_exit == 0 means no exit this bar.
        """
        p   = self.pos
        cfg = p.cfg
        p.bars_held += 1
        p.update_best(high, low)

        def rt(price: float) -> float:
            return round(round(price / cfg.tick_size) * cfg.tick_size, 2)

        favour = p.favour_dist(p.best_price)

        # ── 1. Square-off ─────────────────────────────────────────────────────
        if hasattr(ts, "time") and ts.time() >= square_off:
            self._log(
                f"SQUARE-OFF @ {bar_open:.2f}  "
                f"bar={p.bars_held}  favour={favour:+.1f}pts"
            )
            return bar_open, "square-off", p.lots_remaining

        # ── 2. T2 hit ─────────────────────────────────────────────────────────
        t2_hit = (
            (p.direction == "LONG"  and high >= p.target) or
            (p.direction == "SHORT" and low  <= p.target)
        )
        if t2_hit:
            self._log(
                f"TARGET T2 hit @ {p.target:.2f}  "
                f"bar={p.bars_held}  favour={favour:+.1f}pts"
            )
            return p.target, "target-T2", p.lots_remaining

        # ── 3. Current SL hit ─────────────────────────────────────────────────
        sl_hit = (
            (p.direction == "LONG"  and low  <= p.sl) or
            (p.direction == "SHORT" and high >= p.sl)
        )
        if sl_hit:
            sl_type = (
                "stop-loss(trailing)" if p.trailing_active
                else "stop-loss(BE)"  if p.sl_moved_to_be
                else "stop-loss"
            )
            self._log(
                f"SL HIT @ {p.sl:.2f}  type={sl_type}  "
                f"bar={p.bars_held}  best={p.best_price:.2f}  favour={favour:+.1f}pts"
            )
            return p.sl, sl_type, p.lots_remaining

        # ── 4. T1 hit (partial exit) ──────────────────────────────────────────
        if cfg.use_dual_target and not p.t1_hit and p.t1 > 0:
            t1_hit = (
                (p.direction == "LONG"  and high >= p.t1) or
                (p.direction == "SHORT" and low  <= p.t1)
            )
            if t1_hit:
                p.t1_hit   = True
                lots_exit  = max(1, round(p.lots_remaining * cfg.t1_exit_fraction))
                p.lots_remaining -= lots_exit

                # Move SL to breakeven + buffer ticks
                buf = cfg.be_buffer_ticks * cfg.tick_size
                if p.direction == "LONG":
                    new_sl = rt(p.entry + buf)
                    p.sl   = max(p.sl, new_sl)
                else:
                    new_sl = rt(p.entry - buf)
                    p.sl   = min(p.sl, new_sl)

                p.sl_moved_to_be  = True
                p.trailing_active = True   # begin trailing remainder

                self._log(
                    f"T1 HIT @ {p.t1:.2f}  "
                    f"exit {lots_exit} lots ({cfg.t1_exit_fraction*100:.0f}%)  "
                    f"SL -> BE @ {p.sl:.2f}  "
                    f"trailing ON  "
                    f"{p.lots_remaining} lots remain  "
                    f"bar={p.bars_held}"
                )
                return p.t1, "target-T1", lots_exit

        # ── 5. Trailing stop update ────────────────────────────────────────────
        if cfg.use_trailing:
            # Activation
            if not p.trailing_active and favour >= cfg.trail_activation_r * p.sl_dist:
                p.trailing_active = True
                self._log(
                    f"TRAILING ACTIVATED  "
                    f"favour={favour:+.1f}pts "
                    f"(threshold={cfg.trail_activation_r * p.sl_dist:.1f}pts)  "
                    f"best={p.best_price:.2f}"
                )

            # Ratchet SL
            if p.trailing_active:
                trail_dist = cfg.trail_offset_r * p.sl_dist
                if p.direction == "LONG":
                    new_trail_sl = rt(p.best_price - trail_dist)
                    if new_trail_sl > p.sl:
                        old_sl = p.sl
                        p.sl   = new_trail_sl
                        self._log(
                            f"TRAILING SL raised {old_sl:.2f} -> {p.sl:.2f}  "
                            f"best={p.best_price:.2f}  offset={trail_dist:.1f}pts"
                        )
                else:
                    new_trail_sl = rt(p.best_price + trail_dist)
                    if new_trail_sl < p.sl:
                        old_sl = p.sl
                        p.sl   = new_trail_sl
                        self._log(
                            f"TRAILING SL lowered {old_sl:.2f} -> {p.sl:.2f}  "
                            f"best={p.best_price:.2f}  offset={trail_dist:.1f}pts"
                        )

        # ── 6. Breakeven activation ────────────────────────────────────────────
        if cfg.use_breakeven and not p.sl_moved_to_be:
            if favour >= cfg.be_activation_r * p.sl_dist:
                buf = cfg.be_buffer_ticks * cfg.tick_size
                moved = False
                if p.direction == "LONG":
                    new_be = rt(p.entry + buf)
                    if new_be > p.sl:
                        old_sl, p.sl = p.sl, new_be
                        moved = True
                else:
                    new_be = rt(p.entry - buf)
                    if new_be < p.sl:
                        old_sl, p.sl = p.sl, new_be
                        moved = True

                if moved:
                    p.sl_moved_to_be = True
                    self._log(
                        f"BREAKEVEN SET  SL {old_sl:.2f} -> {p.sl:.2f}  "
                        f"favour={favour:+.1f}pts "
                        f"(threshold={cfg.be_activation_r * p.sl_dist:.1f}pts)"
                    )

        # ── 7. Time stop ──────────────────────────────────────────────────────
        if cfg.use_time_stop and not p.t1_hit:
            if p.bars_held >= cfg.max_bars_in_trade:
                self._log(
                    f"TIME STOP  {p.bars_held} bars held, T1 never reached  "
                    f"favour={favour:+.1f}pts  best={p.best_price:.2f}  "
                    f"exit @ {bar_open:.2f}"
                )
                return bar_open, "time-stop", p.lots_remaining

        return None, None, 0

    # ── Properties / helpers ──────────────────────────────────────────────────

    @property
    def current_sl(self) -> float:
        return self.pos.sl

    @property
    def t1_hit(self) -> bool:
        return self.pos.t1_hit

    def state_dict(self) -> dict:
        """Snapshot for dashboard strategy card."""
        p = self.pos
        return {
            "sl":              p.sl,
            "t1":              p.t1,
            "target":          p.target,
            "t1_hit":          p.t1_hit,
            "sl_at_be":        p.sl_moved_to_be,
            "trailing_active": p.trailing_active,
            "best_price":      p.best_price,
            "bars_held":       p.bars_held,
            "lots_remaining":  p.lots_remaining,
            "favour_pct":      round(
                p.favour_dist(p.best_price) / p.sl_dist * 100, 1
            ) if p.sl_dist > 0 else 0,
        }