"""Wave Rider: US Open 1h bar momentum strategy for BTC.

208-day backtest: PnL +21.4%, PF=1.68, Sharpe=3.13.
Reversion add-on (up_large only): +3.3%, WR=94.7%, p=0.008.

Entry: UTC 15:00 based on UTC 14:00-15:00 1h candle open_move.
Exit: SL 0.8% + time stop UTC 20:00.
Reversion: after up_large close, SHORT with TP=0.3%, SL=0.8%.

Adaptive Holding (2026-02-21):
  Volatility-adaptive SL trailing during holding period.
  - Breakeven trail: profit >= sl_pct/2 → move SL to entry (0 loss)
  - High vol (ATR ratio > 1.5): SL widens by 20% to avoid whipsaws
  - Low vol (ATR ratio < 0.7): SL tightens by 15% to lock profit
"""


class WaveRider:
    """Pure strategy logic. No state management, no I/O."""

    def __init__(self, config: dict):
        self.up_large_th = config.get("up_large_th", 0.006)
        self.down_large_th = config.get("down_large_th", 0.008)
        self.up_medium_th = config.get("up_medium_th", 0.002)
        self.sl_pct = config.get("sl_pct", 0.008)
        self.rev_tp_pct = config.get("rev_tp_pct", 0.003)
        self.rev_sl_pct = config.get("rev_sl_pct", 0.008)
        self.rev_deviation_th = config.get("rev_deviation_th", 0.008)
        # Adaptive SL thresholds
        self.breakeven_trigger_pct = config.get("breakeven_trigger_pct", 0.004)
        self.high_vol_atr_ratio = config.get("high_vol_atr_ratio", 1.5)
        self.low_vol_atr_ratio = config.get("low_vol_atr_ratio", 0.7)
        self.high_vol_sl_factor = config.get("high_vol_sl_factor", 1.20)
        self.low_vol_sl_factor = config.get("low_vol_sl_factor", 0.85)

    def decide_entry(self, open_move: float) -> tuple[str, str, float] | None:
        """Decide entry based on 1h bar open_move (close/open - 1).

        Args:
            open_move: (close - open) / open of the UTC 14:00-15:00 bar.

        Returns:
            (direction, pattern, confidence) or None.
            direction: "long" or "short"
            pattern: "wr_up_large", "wr_down_large", "wr_up_medium_fade"
            confidence: 0.75-0.85
        """
        if open_move >= self.up_large_th:
            return ("long", "wr_up_large", 0.80)
        elif open_move <= -self.down_large_th:
            return ("short", "wr_down_large", 0.85)
        elif self.up_medium_th <= open_move < self.up_large_th:
            return ("short", "wr_up_medium_fade", 0.75)
        return None

    def compute_sl(self, entry_price: float, direction: str) -> float:
        """Compute stop loss price.

        Args:
            entry_price: Entry price.
            direction: "long" or "short".

        Returns:
            Stop loss price.
        """
        if direction == "long":
            return entry_price * (1 - self.sl_pct)
        else:
            return entry_price * (1 + self.sl_pct)

    def should_trigger_reversion(self, observe_open: float, close_price: float) -> bool:
        """Check if reversion should trigger after WR close (up_large only).

        Args:
            observe_open: Open price of the observe bar (UTC 14:00).
            close_price: Price at UTC 20:00 close.

        Returns:
            True if deviation >= threshold.
        """
        deviation = abs(close_price - observe_open) / observe_open
        return deviation >= self.rev_deviation_th

    def compute_rev_sl(self, entry_price: float) -> float:
        """Compute reversion SHORT stop loss (above entry)."""
        return entry_price * (1 + self.rev_sl_pct)

    def compute_rev_tp(self, entry_price: float) -> float:
        """Compute reversion SHORT take profit (below entry)."""
        return entry_price * (1 - self.rev_tp_pct)

    def compute_adaptive_sl(
        self,
        entry_price: float,
        current_price: float,
        current_sl: float,
        direction: str,
        atr_ratio: float = 1.0,
    ) -> tuple[float, str]:
        """Compute volatility-adaptive trailing SL during holding.

        Logic (does NOT widen SL beyond original; only tightens or trails):
          1. Breakeven trail: if profit >= breakeven_trigger_pct,
             move SL to entry price (zero-loss protection).
          2. Volatility adjustment applied to the tighter of
             (current_sl, breakeven_sl):
             - high_vol (atr_ratio > high_vol_atr_ratio):
                 widen by high_vol_sl_factor — but never worse than original SL
             - low_vol (atr_ratio < low_vol_atr_ratio):
                 tighten by low_vol_sl_factor

        Args:
            entry_price: Original entry price.
            current_price: Current market price (mid).
            current_sl: Current stop loss price stored in meta.
            direction: "long" or "short".
            atr_ratio: short_atr / long_atr (from BaseStrategy._atr_volatility_multiplier).

        Returns:
            (new_sl, reason_label) — new_sl may equal current_sl if no update needed.
        """
        if direction == "long":
            profit_pct = (current_price - entry_price) / entry_price
            # Breakeven: if profit >= threshold, floor SL at entry
            if profit_pct >= self.breakeven_trigger_pct:
                breakeven_sl = entry_price  # zero-loss protection
                candidate_sl = max(current_sl, breakeven_sl)
            else:
                candidate_sl = current_sl

            # Vol adjustment: adjust distance from current_price
            if atr_ratio > self.high_vol_atr_ratio:
                # High vol: widen (lower SL relative to price), but never below original SL
                dist = current_price - candidate_sl
                adjusted_sl = current_price - dist * self.high_vol_sl_factor
                # Must not go below original SL
                original_sl = entry_price * (1 - self.sl_pct)
                new_sl = max(adjusted_sl, original_sl, current_sl)
                label = f"high_vol(x{atr_ratio:.2f})"
            elif atr_ratio < self.low_vol_atr_ratio:
                # Low vol: tighten (raise SL toward price)
                dist = current_price - candidate_sl
                adjusted_sl = current_price - dist * self.low_vol_sl_factor
                new_sl = max(adjusted_sl, candidate_sl)
                label = f"low_vol(x{atr_ratio:.2f})"
            else:
                new_sl = candidate_sl
                label = f"normal_vol(x{atr_ratio:.2f})"

        else:  # short
            profit_pct = (entry_price - current_price) / entry_price
            # Breakeven: if profit >= threshold, ceiling SL at entry
            if profit_pct >= self.breakeven_trigger_pct:
                breakeven_sl = entry_price  # zero-loss protection
                candidate_sl = min(current_sl, breakeven_sl)
            else:
                candidate_sl = current_sl

            # Vol adjustment
            if atr_ratio > self.high_vol_atr_ratio:
                # High vol: widen (raise SL relative to price), but never above original SL
                dist = candidate_sl - current_price
                adjusted_sl = current_price + dist * self.high_vol_sl_factor
                original_sl = entry_price * (1 + self.sl_pct)
                new_sl = min(adjusted_sl, original_sl, current_sl)
                label = f"high_vol(x{atr_ratio:.2f})"
            elif atr_ratio < self.low_vol_atr_ratio:
                # Low vol: tighten (lower SL toward price)
                dist = candidate_sl - current_price
                adjusted_sl = current_price + dist * self.low_vol_sl_factor
                new_sl = min(adjusted_sl, candidate_sl)
                label = f"low_vol(x{atr_ratio:.2f})"
            else:
                new_sl = candidate_sl
                label = f"normal_vol(x{atr_ratio:.2f})"

        return new_sl, label
