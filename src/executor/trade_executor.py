"""Trade executor for myClaw: reads signals and executes trades on Hyperliquid."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from src.api.hl_client import HLClient
from src.state.state_manager import StateManager
from src.utils.config_loader import (
    get_signals_dir,
    get_state_dir,
    load_risk_params,
    load_settings,
)
from src.utils.file_lock import read_json
from src.utils.logger import setup_logger
from src.utils.safe_parse import safe_float

logger = setup_logger("executor")


class TradeExecutor:
    """Reads trading signals and executes them via Hyperliquid SDK."""

    def __init__(self):
        self.settings = load_settings()
        self.state = StateManager()

        self.client = HLClient(self.settings)
        self.main_address = self.client._main_address

        trading_cfg = self.settings.get("trading", {})
        self.default_leverage = trading_cfg.get("default_leverage", 3)
        self.min_confidence = trading_cfg.get("min_confidence", 0.7)
        self.risk_params = load_risk_params()
        self.execution_mode = os.environ.get("EXECUTOR_MODE", "all").strip().lower() or "all"

        gate_cfg = trading_cfg.get("decision_gate", {})
        self.partial_consensus_min_confidence = float(
            gate_cfg.get("partial_consensus_min_confidence", max(self.min_confidence, 0.75))
        )
        self.entry_cooldown_minutes = int(gate_cfg.get("entry_cooldown_minutes", 10))
        self.max_equity_drift_pct = float(gate_cfg.get("max_equity_drift_pct", 20.0))
        self.max_daily_loss_for_new_entries_pct = float(
            gate_cfg.get("max_daily_loss_for_new_entries_pct", 2.0)
        )
        self.min_rr = float(gate_cfg.get("min_rr", 1.2))
        self.min_data_quality_score = int(gate_cfg.get("min_data_quality_score", 80))
        self.max_spread_bps = float(gate_cfg.get("max_spread_bps", 8.0))
        self.min_orderbook_imbalance = float(gate_cfg.get("min_orderbook_imbalance", 1.1))
        self.min_orderbook_imbalance_by_symbol = gate_cfg.get("min_orderbook_imbalance_by_symbol", {}) or {}

        logger.info(
            "TradeExecutor initialized (env=%s, address=%s, mode=%s)",
            self.settings.get("environment", "testnet"),
            self.client.address,
            self.execution_mode,
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
            positions = self.state.sync_positions(self.client)
            # 実行後のstate整合を確保
            try:
                equity = self.client.get_equity()
                api_unrealized = sum(float(p.get("unrealized_pnl", 0) or 0) for p in positions)
                if equity > 0:
                    self.state.update_daily_pnl(equity, api_unrealized_pnl=api_unrealized)
            except Exception:
                logger.exception("Failed to reconcile daily_pnl after executions")

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

        # Execution mode filter
        if self.execution_mode == "close_only" and action != "close":
            logger.info("Skipping %s %s: executor mode is close_only", action, symbol)
            return None

        if action in ("hold", "hold_position"):
            logger.info("Skipping %s: action is %s", symbol, action)
            return None
        # Skip low-confidence signals
        if action != "close" and confidence < self.min_confidence:
            logger.info("Skipping %s: confidence %.2f < %.2f", symbol, confidence, self.min_confidence)
            return None

        # Risk validation
        equity = self.client.get_equity()
        positions = self.state.get_positions()
        try:
            from src.risk.risk_manager import RiskManager
            rm = RiskManager()
            allowed, reason = rm.validate_signal(signal, positions, equity)
            if not allowed:
                logger.warning("Risk rejected %s %s: %s", action, symbol, reason)
                return {"symbol": symbol, "action": action, "status": "rejected", "reason": reason}
        except ImportError:
            logger.warning("RiskManager not available, skipping risk check")
        except Exception as exc:
            logger.error("RiskManager error: %s - rejecting signal for safety", exc)
            return {"symbol": symbol, "action": action, "status": "rejected", "reason": f"risk check error: {exc}"}

        if action in ("long", "short"):
            allowed, reason = self._composite_entry_gate(signal, equity)
            if not allowed:
                logger.warning("Composite gate rejected %s %s: %s", action, symbol, reason)
                return {"symbol": symbol, "action": action, "status": "rejected", "reason": reason}

        # Execute
        leverage = signal.get("leverage", self.default_leverage)
        try:
            if action == "close":
                result = self.close_position(symbol)
                if result and result.get("status") == "closed" and symbol in ("ETH", "SOL"):
                    self._clear_rubber_meta(symbol)
                return result
            elif action in ("long", "short"):
                size = signal.get("size")
                if size is None:
                    # Calculate size from equity and position config
                    size = self._calculate_size(symbol, leverage)
                    if size is None:
                        return {"symbol": symbol, "action": action, "status": "error", "reason": "failed to calculate size"}
                else:
                    price = self.client.get_mid_prices().get(symbol, 0.0)
                    size = self._apply_size_caps(symbol, float(size), price, equity)
                    if size is None:
                        return {"symbol": symbol, "action": action, "status": "rejected", "reason": "size blocked by hard cap"}
                result = self.open_position(symbol, action, size, leverage)
                if result and result.get("status") in ("filled", "partial"):
                    self._save_rubber_meta(signal, result)
                return result
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
        logger.info("Opening %s %s %.4f (leverage=%d)", side, symbol, size, leverage)

        order_result = self.client.place_market_order(symbol, side, size, leverage)
        status = order_result["status"]
        fill_price = order_result["fill_price"]
        resp = order_result["raw_response"]

        if status == "error":
            logger.error("Order error for %s: %s", symbol, order_result.get("error"))
            return {"symbol": symbol, "action": side, "status": "error",
                    "reason": order_result.get("error", "unknown")}

        if status == "partial":
            logger.warning("Partial fill for %s %s (order resting on book)", side, symbol)

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

        close_result = self.client.close_position(symbol)
        status = close_result["status"]
        fill_price = close_result["fill_price"]
        resp = close_result["raw_response"]

        if status == "no_position":
            logger.warning("market_close returned None for %s (position already closed?)", symbol)
            return {"symbol": symbol, "action": "close", "status": "no_position", "fill_price": 0.0}

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
                equity = self.client.get_equity()
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


    def _calculate_size(self, symbol: str, leverage: int) -> float | None:
        """Calculate position size based on equity and risk params."""
        try:
            risk_params = self.risk_params
            max_pct = risk_params.get("position", {}).get("max_single_pct", 10.0)
            max_total_pct = risk_params.get("position", {}).get("max_total_exposure_pct", 30.0)

            equity = self.client.get_equity()
            if equity <= 0:
                logger.error("Equity is zero or negative, cannot calculate size")
                return None

            # Get mid price
            mids = self.client.get_mid_prices()
            price = mids.get(symbol, 0.0)
            if price <= 0:
                logger.error("Cannot get price for %s", symbol)
                return None

            # 証拠金 = equity × max_pct% × size_multiplier
            size_mult, size_reason = self._get_size_regime_multiplier()
            margin = equity * (max_pct / 100.0) * size_mult
            notional = margin * leverage

            # Total exposure 上限チェック: 既存ポジション + 新規 <= max_total_pct%
            positions = self.state.get_positions()
            current_exposure = sum(
                abs(float(p.get("size", 0))) * float(p.get("mid_price", 0) or p.get("entry_price", 0))
                for p in positions
            )
            max_total_notional = equity * (max_total_pct / 100.0)
            remaining = max_total_notional - current_exposure
            min_order_usd = risk_params.get("orders", {}).get("min_order_size_usd", 10.0)
            if remaining < min_order_usd:
                logger.warning("Total exposure limit reached: current=%.2f, max=%.2f, equity=%.2f",
                               current_exposure, max_total_notional, equity)
                return None
            if notional > remaining:
                logger.info("Capping notional %.2f -> %.2f (total exposure limit)", notional, remaining)
                notional = remaining

            size = notional / price
            size = self._apply_size_caps(symbol, size, price, equity)
            if size is None:
                return None

            # Round to reasonable precision
            if price > 10000:
                size = round(size, 4)
            elif price > 100:
                size = round(size, 3)
            else:
                size = round(size, 2)

            logger.info("Size calculated: %s size=%.4f notional=%.2f margin=%.2f equity=%.2f exposure=%.2f",
                        symbol, size, notional, margin, equity, current_exposure + notional)
            logger.info("Size regime applied: x%.2f (%s)", size_mult, size_reason)

            return size if size > 0 else None
        except Exception:
            logger.exception("Error calculating size for %s", symbol)
            return None

    def _load_market_symbol_data(self, symbol: str) -> dict:
        path = Path("data/market_data.json")
        try:
            data = read_json(path)
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        symbols = data.get("symbols", {})
        if not isinstance(symbols, dict):
            return {}
        sym = symbols.get(symbol, {})
        return sym if isinstance(sym, dict) else {}

    def _get_size_regime_multiplier(self) -> tuple[float, str]:
        path = get_state_dir(self.settings) / "size_regime.json"
        try:
            data = read_json(path)
        except Exception:
            return 1.0, "size_regime unavailable"
        if not isinstance(data, dict):
            return 1.0, "size_regime invalid"
        try:
            mult = float(data.get("multiplier", 1.0))
        except Exception:
            mult = 1.0
        if mult <= 0:
            mult = 1.0
        reason = str(data.get("reason", "unknown"))
        return mult, reason

    def _composite_entry_gate(self, signal: dict, live_equity: float) -> tuple[bool, str]:
        """Run multi-factor gate before any new long/short entry."""
        reasons = []
        symbol = signal.get("symbol", "")
        action = signal.get("action", "")

        ok, reason = self._check_equity_consistency(live_equity)
        if not ok:
            reasons.append(reason)

        ok, reason = self._check_consensus_quality(signal)
        if not ok:
            reasons.append(reason)

        ok, reason = self._check_daily_loss_budget()
        if not ok:
            reasons.append(reason)

        ok, reason = self._check_data_quality()
        if not ok:
            reasons.append(reason)

        ok, reason = self._check_mm_context(symbol, action)
        if not ok:
            reasons.append(reason)

        ok, reason = self._check_entry_cooldown(symbol)
        if not ok:
            reasons.append(reason)

        ok, reason = self._check_rr(signal, action)
        if not ok:
            reasons.append(reason)

        if reasons:
            return False, " | ".join(reasons)
        return True, "composite gate passed"

    def _check_data_quality(self) -> tuple[bool, str]:
        """Block new entries when data quality score is below threshold."""
        report_path = get_state_dir(self.settings) / "data_health.json"
        try:
            report = read_json(report_path)
        except (FileNotFoundError, json.JSONDecodeError):
            return False, "data_health report missing"

        score = int(report.get("score", 0)) if isinstance(report, dict) else 0
        if score < self.min_data_quality_score:
            return False, f"data quality score {score} < {self.min_data_quality_score}"
        return True, f"data quality ok ({score})"

    def _check_mm_context(self, symbol: str, action: str) -> tuple[bool, str]:
        """MM-aware gate: spread must be tight and orderbook side should support direction."""
        sym = self._load_market_symbol_data(symbol)
        if not sym:
            return True, "MM check skipped (no market snapshot)"

        ob = sym.get("orderbook", {})
        if not isinstance(ob, dict):
            return False, "MM check failed (orderbook missing)"
        bids = ob.get("bids", [])
        asks = ob.get("asks", [])
        if not bids or not asks:
            return False, "MM check failed (empty orderbook)"

        try:
            best_bid = float(bids[0].get("px", 0))
            best_ask = float(asks[0].get("px", 0))
            mid = float(sym.get("mid_price", 0) or 0)
        except Exception:
            return False, "MM check failed (invalid prices)"

        if best_bid <= 0 or best_ask <= best_bid or mid <= 0:
            return False, "MM check failed (bad bid/ask geometry)"

        spread_bps = ((best_ask - best_bid) / mid) * 10000
        if spread_bps > self.max_spread_bps:
            return False, f"spread {spread_bps:.2f}bps > {self.max_spread_bps:.2f}bps"

        bid_sz = sum(float(x.get("sz", 0) or 0) for x in bids[:5])
        ask_sz = sum(float(x.get("sz", 0) or 0) for x in asks[:5])
        if bid_sz <= 0 or ask_sz <= 0:
            return False, "MM check failed (invalid depth size)"

        imbalance = bid_sz / ask_sz
        sym_threshold = self.min_orderbook_imbalance
        raw_sym_th = self.min_orderbook_imbalance_by_symbol.get(symbol)
        if raw_sym_th is not None:
            try:
                parsed = float(raw_sym_th)
                if parsed > 0:
                    sym_threshold = parsed
            except (TypeError, ValueError):
                logger.warning(
                    "Invalid min_orderbook_imbalance_by_symbol for %s: %s",
                    symbol,
                    raw_sym_th,
                )

        if action == "long" and imbalance < sym_threshold:
            return False, (
                f"long blocked by imbalance {imbalance:.2f} < "
                f"{sym_threshold:.2f}"
            )
        if action == "short":
            short_limit = 1.0 / sym_threshold
            if imbalance > short_limit:
                return False, f"short blocked by imbalance {imbalance:.2f} > {short_limit:.2f}"

        return True, f"MM ok (spread={spread_bps:.2f}bps, imbalance={imbalance:.2f})"

    def _check_equity_consistency(self, live_equity: float) -> tuple[bool, str]:
        """Reject new entries when live equity and state equity diverge too much."""
        daily = self.state.get_daily_pnl()
        state_equity = float(daily.get("equity") or 0)
        if live_equity <= 0 or state_equity <= 0:
            return False, "equity unavailable"

        drift_pct = abs(live_equity - state_equity) / state_equity * 100
        if drift_pct > self.max_equity_drift_pct:
            return False, (
                f"equity drift {drift_pct:.1f}% > {self.max_equity_drift_pct:.1f}% "
                f"(live={live_equity:.2f}, state={state_equity:.2f})"
            )
        return True, "equity consistent"

    def _check_consensus_quality(self, signal: dict) -> tuple[bool, str]:
        """Require stronger confidence for partial consensus decisions."""
        reasoning = str(signal.get("reasoning", ""))
        conf = float(signal.get("confidence") or 0)
        if "部分IN" in reasoning and conf < self.partial_consensus_min_confidence:
            return False, (
                f"partial consensus conf {conf:.2f} < "
                f"{self.partial_consensus_min_confidence:.2f}"
            )
        return True, "consensus quality ok"

    def _check_daily_loss_budget(self) -> tuple[bool, str]:
        """Block new entries after daily loss exceeds strategy budget."""
        daily = self.state.get_daily_pnl()
        start = float(daily.get("start_of_day_equity") or 0)
        if start <= 0:
            return True, "daily budget unknown"

        realized = float(daily.get("realized_pnl") or 0)
        unrealized = float(daily.get("unrealized_pnl") or 0)
        total = realized + unrealized
        loss_pct = max(0.0, (-total / start) * 100)
        if loss_pct >= self.max_daily_loss_for_new_entries_pct:
            return False, (
                f"daily loss {loss_pct:.2f}% >= "
                f"{self.max_daily_loss_for_new_entries_pct:.2f}% budget"
            )
        return True, "daily budget ok"

    def _check_entry_cooldown(self, symbol: str) -> tuple[bool, str]:
        """Prevent rapid churn by enforcing cooldown after last symbol event."""
        history_path = get_state_dir(self.settings) / "trade_history.json"
        try:
            history = read_json(history_path)
            if not isinstance(history, list) or not history:
                return True, "no trade history"
        except (FileNotFoundError, json.JSONDecodeError):
            return True, "no trade history"

        latest = None
        for trade in reversed(history):
            if trade.get("symbol") != symbol:
                continue
            ts = trade.get("closed_at") or trade.get("opened_at") or trade.get("recorded_at")
            if ts:
                latest = ts
                break
        if not latest:
            return True, "no recent symbol trade"

        try:
            last_dt = datetime.fromisoformat(latest)
        except ValueError:
            return True, "invalid trade timestamp"

        now = datetime.now(timezone.utc)
        elapsed_min = (now - last_dt).total_seconds() / 60.0
        if elapsed_min < self.entry_cooldown_minutes:
            return False, (
                f"cooldown active for {symbol}: {elapsed_min:.1f}m < "
                f"{self.entry_cooldown_minutes}m"
            )
        return True, "cooldown passed"

    def _check_rr(self, signal: dict, action: str) -> tuple[bool, str]:
        """Validate RR when entry/SL/TP are available."""
        # Time-cut signals use dummy TP — RR check は無意味
        if signal.get("exit_mode") == "time_cut":
            return True, "RR check skipped (time_cut exit)"

        entry = signal.get("entry_price")
        sl = signal.get("stop_loss")
        tp = signal.get("take_profit")

        try:
            entry = float(entry) if entry is not None else None
            sl = float(sl) if sl is not None else None
            tp = float(tp) if tp is not None else None
        except (TypeError, ValueError):
            return False, "invalid entry/SL/TP values"

        if not entry or not sl or not tp:
            return True, "RR check skipped (entry/SL/TP missing)"

        if action == "long":
            risk = entry - sl
            reward = tp - entry
        else:
            risk = sl - entry
            reward = entry - tp

        if risk <= 0 or reward <= 0:
            return False, f"invalid RR geometry (risk={risk:.6f}, reward={reward:.6f})"

        rr = reward / risk
        if rr < self.min_rr:
            return False, f"RR {rr:.2f} < minimum {self.min_rr:.2f}"
        return True, f"RR ok ({rr:.2f})"

    def _save_rubber_meta(self, signal: dict, result: dict) -> None:
        """Rubber position のメタデータを保存。executor fill 後に呼ぶ。ETH/SOL共通。"""
        symbol = signal.get("symbol")
        if symbol not in ("ETH", "SOL") or not signal.get("pattern"):
            return

        meta = {
            "pattern": signal.get("pattern"),
            "direction": signal.get("action"),
            "entry_price": result.get("fill_price", 0),
            "stop_loss": signal.get("stop_loss"),
            "take_profit": signal.get("take_profit"),
            "exit_mode": signal.get("exit_mode", "tp_sl"),
            "exit_bars": signal.get("exit_bars", 0),
            "bar_count": 0,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "vol_ratio": signal.get("vol_ratio"),
        }
        meta_path = get_state_dir(self.settings) / f"{symbol.lower()}_rubber_meta.json"
        try:
            meta_path.write_text(json.dumps(meta, indent=2))
            logger.info("%s rubber meta saved: %s %s (exit_mode=%s)",
                        symbol, meta["pattern"], meta["direction"], meta["exit_mode"])
        except Exception:
            logger.exception("Failed to save %s rubber meta", symbol)

    # Backward compatibility alias
    _save_eth_rubber_meta = _save_rubber_meta

    def _clear_rubber_meta(self, symbol: str) -> None:
        """Rubber position のメタデータを削除。close 後に呼ぶ。ETH/SOL共通。"""
        meta_path = get_state_dir(self.settings) / f"{symbol.lower()}_rubber_meta.json"
        try:
            meta_path.unlink()
            logger.info("%s rubber meta cleared", symbol)
        except FileNotFoundError:
            pass

    # Backward compatibility alias
    _clear_eth_rubber_meta = lambda self: self._clear_rubber_meta("ETH")

    def _apply_size_caps(self, symbol: str, size: float, price: float, equity: float) -> float | None:
        """Apply hard caps by symbol / notional / equity percentage."""
        if size <= 0 or price <= 0:
            return None

        position_cfg = self.risk_params.get("position", {})
        capped = size

        symbol_caps = position_cfg.get("max_size_by_symbol", {}) or {}
        sym_cap = symbol_caps.get(symbol)
        if sym_cap is not None:
            sym_cap = float(sym_cap)
            if sym_cap > 0 and capped > sym_cap:
                logger.warning("Hard-cap size for %s: %.4f -> %.4f", symbol, capped, sym_cap)
                capped = sym_cap

        max_notional_usd = float(position_cfg.get("max_notional_usd_per_trade", 0) or 0)
        if max_notional_usd > 0:
            notional_cap_size = max_notional_usd / price
            if capped > notional_cap_size:
                logger.warning(
                    "Notional cap for %s: %.4f -> %.4f (%.2f USD)",
                    symbol, capped, notional_cap_size, max_notional_usd
                )
                capped = notional_cap_size

        max_notional_pct = float(position_cfg.get("max_notional_pct_of_equity", 0) or 0)
        if max_notional_pct > 0 and equity > 0:
            notional_cap_size = (equity * (max_notional_pct / 100.0)) / price
            if capped > notional_cap_size:
                logger.warning(
                    "Equity-notional cap for %s: %.4f -> %.4f (%.1f%% equity)",
                    symbol, capped, notional_cap_size, max_notional_pct
                )
                capped = notional_cap_size

        min_order_usd = float(self.risk_params.get("orders", {}).get("min_order_size_usd", 10.0) or 10.0)
        if capped * price < min_order_usd:
            logger.warning(
                "Capped size below min order for %s: size=%.6f notional=%.2f < %.2f",
                symbol, capped, capped * price, min_order_usd
            )
            return None

        return capped if capped > 0 else None


if __name__ == "__main__":
    executor = TradeExecutor()
    results = executor.execute_signals()
    for r in results:
        print(json.dumps(r, indent=2, default=str))
