"""Trade executor for myClaw: reads signals and executes trades on Hyperliquid."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info

from src.state.state_manager import StateManager
from src.utils.config_loader import (
    get_hyperliquid_url,
    get_signals_dir,
    load_settings,
)
from src.utils.file_lock import read_json
from src.utils.logger import setup_logger

logger = setup_logger("executor")


class TradeExecutor:
    """Reads trading signals and executes them via Hyperliquid SDK."""

    def __init__(self):
        self.settings = load_settings()
        self.state = StateManager()

        # Resolve private key
        private_key = os.environ.get("HYPERLIQUID_PRIVATE_KEY")
        if not private_key:
            from src.utils.crypto import get_hyperliquid_key
            private_key = get_hyperliquid_key()

        self.account = Account.from_key(private_key)
        self.address = self.account.address

        # メインアカウントアドレス (API walletとは別)
        main_address = os.environ.get("HYPERLIQUID_MAIN_ADDRESS", "").strip()
        self.main_address = main_address if main_address else self.address

        base_url = get_hyperliquid_url(self.settings)
        # API wallet がサインし、main_address のアカウントで執行
        self.exchange = Exchange(self.account, base_url, account_address=self.main_address)
        self.info = Info(base_url, skip_ws=True)

        trading_cfg = self.settings.get("trading", {})
        self.default_leverage = trading_cfg.get("default_leverage", 3)
        self.min_confidence = trading_cfg.get("min_confidence", 0.7)

        logger.info(
            "TradeExecutor initialized (env=%s, address=%s)",
            self.settings.get("environment", "testnet"),
            self.address,
        )

    # -- Public API --

    def execute_signals(self) -> list[dict]:
        """Load signals/signals.json and execute each actionable signal.

        Returns:
            List of execution result dicts.
        """
        # Kill switch check
        ks = self.state.get_kill_switch_status()
        if ks.get("enabled"):
            logger.warning("Kill switch is active (%s). Skipping execution.", ks.get("reason"))
            return []

        signals_path = get_signals_dir(self.settings) / "signals.json"
        if not signals_path.exists():
            logger.info("No signals file found at %s", signals_path)
            return []

        try:
            data = read_json(signals_path)
        except (json.JSONDecodeError, FileNotFoundError) as exc:
            logger.error("Failed to read signals: %s", exc)
            return []

        signals = data.get("signals", []) if isinstance(data, dict) else []
        results = []
        for signal in signals:
            result = self.execute_signal(signal)
            if result:
                results.append(result)

        # Sync positions after all executions
        if results:
            self.state.sync_positions(self.info, self.main_address)

        return results

    def execute_signal(self, signal: dict) -> dict | None:
        """Evaluate and execute a single trading signal.

        Args:
            signal: Signal dict with keys: symbol, action, confidence, etc.

        Returns:
            Execution result dict, or None if skipped.
        """
        symbol = signal.get("symbol", "")
        action = signal.get("action", "hold")
        confidence = signal.get("confidence", 0)

        # Skip low-confidence or hold signals
        if confidence < self.min_confidence:
            logger.info("Skipping %s: confidence %.2f < %.2f", symbol, confidence, self.min_confidence)
            return None
        if action == "hold":
            logger.info("Skipping %s: action is hold", symbol)
            return None

        # Risk validation
        try:
            from src.risk.risk_manager import RiskManager
            rm = RiskManager()
            positions = self.state.get_positions()
            equity = self._get_equity()
            allowed, reason = rm.validate_signal(signal, positions, equity)
            if not allowed:
                logger.warning("Risk rejected %s %s: %s", action, symbol, reason)
                return {"symbol": symbol, "action": action, "status": "rejected", "reason": reason}
        except ImportError:
            logger.warning("RiskManager not available, skipping risk check")
        except Exception as exc:
            logger.error("RiskManager error: %s - rejecting signal for safety", exc)
            return {"symbol": symbol, "action": action, "status": "rejected", "reason": f"risk check error: {exc}"}

        # Execute
        leverage = signal.get("leverage", self.default_leverage)
        try:
            if action == "close":
                return self.close_position(symbol)
            elif action in ("long", "short"):
                size = signal.get("size")
                if size is None:
                    # Calculate size from equity and position config
                    size = self._calculate_size(symbol, leverage)
                    if size is None:
                        return {"symbol": symbol, "action": action, "status": "error", "reason": "failed to calculate size"}
                return self.open_position(symbol, action, size, leverage)
            else:
                logger.warning("Unknown action '%s' for %s", action, symbol)
                return None
        except Exception:
            logger.exception("Error executing %s %s", action, symbol)
            return {"symbol": symbol, "action": action, "status": "error", "reason": "execution exception"}

    def open_position(self, symbol: str, side: str, size: float, leverage: int) -> dict:
        """Open a position via market order.

        Args:
            symbol: Coin symbol (e.g. "BTC").
            side: "long" or "short".
            size: Position size in coin units.
            leverage: Leverage multiplier.

        Returns:
            Result dict with execution details.
        """
        is_buy = side == "long"
        logger.info("Opening %s %s %.4f (leverage=%d)", side, symbol, size, leverage)

        # Set leverage first
        self.exchange.update_leverage(leverage, symbol)

        # Market order
        resp = self.exchange.market_open(symbol, is_buy, size, px=None, slippage=0.01)
        logger.info("Order response: %s", resp)

        fill_price = _extract_fill_price(resp)
        if _is_order_success(resp) and fill_price > 0:
            status = "filled"
        elif _is_order_partial(resp):
            status = "partial"
            logger.warning("Partial fill for %s %s (order resting on book)", side, symbol)
        else:
            status = "failed"

        if status in ("filled", "partial") and fill_price > 0:
            self.state.record_trade({
                "symbol": symbol,
                "side": side,
                "size": size,
                "entry_price": fill_price,
                "exit_price": None,
                "pnl": None,
                "opened_at": datetime.now(timezone.utc).isoformat(),
                "closed_at": None,
            })

        return {
            "symbol": symbol,
            "action": side,
            "status": status,
            "size": size,
            "leverage": leverage,
            "fill_price": fill_price,
            "response": resp,
        }

    def close_position(self, symbol: str) -> dict:
        """Close an existing position for the given symbol.

        Args:
            symbol: Coin symbol to close.

        Returns:
            Result dict with execution details.
        """
        logger.info("Closing position for %s", symbol)

        # Find existing position for P&L recording
        positions = self.state.get_positions()
        existing = next((p for p in positions if p.get("symbol") == symbol), None)

        resp = self.exchange.market_close(symbol)
        logger.info("Close response: %s", resp)

        status = "closed" if _is_order_success(resp) else "failed"
        fill_price = _extract_fill_price(resp)

        if status == "closed" and existing:
            entry_price = existing.get("entry_price", 0)
            size = existing.get("size", 0)
            side = existing.get("side", "long")
            pnl = (fill_price - entry_price) * size if side == "long" else (entry_price - fill_price) * size

            self.state.record_trade({
                "symbol": symbol,
                "side": side,
                "size": size,
                "entry_price": entry_price,
                "exit_price": fill_price,
                "pnl": pnl,
                "opened_at": existing.get("opened_at"),
                "closed_at": datetime.now(timezone.utc).isoformat(),
            })

            # Update daily P&L with realized
            try:
                equity = self._get_equity()
                self.state.update_daily_pnl(equity, realized_pnl=pnl)
            except Exception:
                logger.exception("Failed to update daily P&L after close")

        return {
            "symbol": symbol,
            "action": "close",
            "status": status,
            "fill_price": fill_price,
            "response": resp,
        }

    # -- Helpers --


    def _get_equity(self) -> float:
        """Get account equity. Supports both regular and unified (portfolio margin) accounts.

        For unified accounts, spot USDC is used as equity since marginSummary shows 0.
        """
        import requests
        try:
            # まず marginSummary を確認
            user_state = self.info.user_state(self.main_address)
            equity = float(user_state.get("marginSummary", {}).get("accountValue", 0))
            if equity > 0:
                return equity

            # 統合口座: spot USDC を使用
            base_url = get_hyperliquid_url(self.settings)
            resp = requests.post(
                base_url + "/info",
                json={"type": "spotClearinghouseState", "user": self.main_address},
                timeout=5,
            )
            for b in resp.json().get("balances", []):
                if b.get("coin") == "USDC":
                    return float(b.get("total", 0))
        except Exception:
            logger.exception("Failed to get equity")
        return 0.0

    def _calculate_size(self, symbol: str, leverage: int) -> float | None:
        """Calculate position size based on equity and risk params."""
        try:
            from src.utils.config_loader import load_risk_params
            risk_params = load_risk_params()
            max_pct = risk_params.get("position", {}).get("max_single_pct", 10.0)

            user_state = self.info.user_state(self.main_address)
            equity = float(user_state.get("marginSummary", {}).get("accountValue", 0))

            # Get mid price
            mids = self.info.all_mids()
            price = float(mids.get(symbol, 0))
            if price <= 0:
                logger.error("Cannot get price for %s", symbol)
                return None

            # 証拠金 = equity × max_pct%、notional = 証拠金 × leverage
            # (leverage を証拠金に掛けてnotionalを算出 — 証拠金はmax_pct%以内に制限)
            margin = equity * (max_pct / 100.0)
            notional = margin * leverage
            size = notional / price

            # Round to reasonable precision
            if price > 10000:
                size = round(size, 4)
            elif price > 100:
                size = round(size, 3)
            else:
                size = round(size, 2)

            return size if size > 0 else None
        except Exception:
            logger.exception("Error calculating size for %s", symbol)
            return None


def _is_order_success(resp: dict) -> bool:
    """Check if an exchange response indicates a fully filled order."""
    if not isinstance(resp, dict) or resp.get("status") != "ok":
        return False
    response = resp.get("response", {})
    if isinstance(response, dict) and response.get("type") == "order":
        data = response.get("data", {})
        if isinstance(data, dict) and data.get("statuses"):
            return any(s.get("filled") for s in data["statuses"])
    return False


def _is_order_partial(resp: dict) -> bool:
    """Check if an order is resting (partial fill or unfilled)."""
    if not isinstance(resp, dict) or resp.get("status") != "ok":
        return False
    response = resp.get("response", {})
    if isinstance(response, dict) and response.get("type") == "order":
        data = response.get("data", {})
        if isinstance(data, dict) and data.get("statuses"):
            return any(s.get("resting") for s in data["statuses"])
    return False


def _extract_fill_price(resp: dict) -> float:
    """Extract the fill price from an exchange response, or 0.0 if unavailable."""
    try:
        response = resp.get("response", {})
        if isinstance(response, dict) and response.get("type") == "order":
            data = response.get("data", {})
            if isinstance(data, dict):
                for s in data.get("statuses", []):
                    filled = s.get("filled")
                    if isinstance(filled, dict):
                        return float(filled.get("avgPx", 0))
    except (AttributeError, TypeError, ValueError):
        pass
    return 0.0


if __name__ == "__main__":
    executor = TradeExecutor()
    results = executor.execute_signals()
    for r in results:
        print(json.dumps(r, indent=2, default=str))
