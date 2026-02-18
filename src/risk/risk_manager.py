"""Risk management for myClaw trading."""

from src.utils.config_loader import load_risk_params, get_state_dir
from src.utils.file_lock import read_json, atomic_write_json
from src.utils.logger import setup_logger
from datetime import datetime, timezone

logger = setup_logger("risk_manager")


class RiskManager:
    """Validates trade signals against risk parameters."""

    def __init__(self):
        self.params = load_risk_params()
        self.position = self.params.get("position", {})
        self.loss_limits = self.params.get("loss_limits", {})
        self.orders = self.params.get("orders", {})

    def validate_signal(
        self, signal: dict, positions: list, equity: float
    ) -> tuple[bool, str]:
        """Validate a trade signal against risk rules.

        Returns:
            (allowed, reason) tuple.
        """
        action = signal.get("action", "")

        # close is always allowed
        if action == "close":
            return True, "Close action always permitted"

        # Max concurrent positions
        max_concurrent = self.position.get("max_concurrent", 3)
        if len(positions) >= max_concurrent:
            return False, f"Max concurrent positions ({max_concurrent}) reached"

        # Leverage check
        leverage = float(signal.get("leverage", 1))
        max_leverage = self.orders.get("max_leverage", 10)
        if leverage > max_leverage:
            return False, f"Leverage {leverage}x exceeds max {max_leverage}x"

        # Single position size check (max 10% of equity, margin basis)
        max_single_pct = self.position.get("max_single_pct", 10.0)
        size = float(signal.get("size") or 0)
        entry = float(signal.get("entry_price") or 0)
        if size > 0 and entry > 0:
            notional = size * entry
            margin_required = notional / max(leverage, 1)
            if equity > 0 and (margin_required / equity) * 100 > max_single_pct:
                return False, f"Margin required {margin_required:.2f} exceeds {max_single_pct}% of equity ({equity:.2f})"
        elif size > 0 and entry == 0 and equity > 0:
            # entry_price 未設定 (成行注文): size に対してmax_singleの上限チェックをスキップするが
            # executor の _calculate_size() がエクイティベースで計算するため二重チェック不要
            pass  # executor side constraint applies

        # Total exposure check (max 30%, notional basis)
        max_total_pct = self.position.get("max_total_exposure_pct", 30.0)
        current_exposure = sum(
            abs(float(p.get("size", 0))) * float(p.get("entry_price", 0) or p.get("entryPx", 0))
            for p in positions
        )
        new_notional = size * entry if (size > 0 and entry > 0) else 0
        new_total = current_exposure + new_notional
        if equity > 0 and (new_total / equity) * 100 > max_total_pct:
            return False, f"Total exposure {new_total:.2f} would exceed {max_total_pct}% of equity ({equity:.2f})"

        return True, "Signal validated"

    def check_kill_switch(self) -> bool:
        """Check if kill switch is active."""
        state_dir = get_state_dir()
        ks_path = state_dir / "kill_switch.json"
        try:
            data = read_json(ks_path)
            return data.get("enabled", False)
        except FileNotFoundError:
            return False

    def check_daily_loss(self, daily_pnl: dict, equity: float) -> bool:
        """Check if daily loss exceeds threshold.

        Returns:
            True if kill switch should be triggered.
        """
        daily_loss_pct = self.loss_limits.get("daily_loss_pct", 5.0)
        realized = float(daily_pnl.get("realized_pnl", 0))
        unrealized = float(daily_pnl.get("unrealized_pnl", 0))
        total_pnl = realized + unrealized
        if equity > 0 and total_pnl < 0:
            loss_pct = abs(total_pnl) / equity * 100
            if loss_pct >= daily_loss_pct:
                logger.warning(
                    "Daily loss %.2f%% exceeds limit %.2f%%",
                    loss_pct,
                    daily_loss_pct,
                )
                return True
        return False

    def check_max_drawdown(
        self, current_equity: float, peak_equity: float
    ) -> bool:
        """Check if max drawdown exceeds threshold.

        Returns:
            True if kill switch should be triggered.
        """
        max_dd_pct = self.loss_limits.get("max_drawdown_pct", 15.0)
        if peak_equity > 0:
            dd_pct = (peak_equity - current_equity) / peak_equity * 100
            if dd_pct >= max_dd_pct:
                logger.warning(
                    "Drawdown %.2f%% exceeds limit %.2f%%",
                    dd_pct,
                    max_dd_pct,
                )
                return True
        return False

    def trigger_kill_switch(self, reason: str) -> None:
        """Activate the kill switch."""
        state_dir = get_state_dir()
        ks_path = state_dir / "kill_switch.json"
        data = {
            "enabled": True,
            "reason": reason,
            "triggered_at": datetime.now(timezone.utc).isoformat(),
        }
        atomic_write_json(ks_path, data)
        logger.critical("Kill switch triggered: %s", reason)
