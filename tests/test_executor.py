"""Tests for TradeExecutor.

HLClient, StateManager, RiskManager, read_json をモック。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from tests.conftest import MOCK_SETTINGS, MOCK_RISK_PARAMS


def _make_executor(
    settings=None,
    risk_params=None,
    equity=500.0,
    positions=None,
    kill_switch=None,
    data_health_score=100,
):
    """Create a TradeExecutor with all dependencies mocked."""
    if settings is None:
        settings = dict(MOCK_SETTINGS)
    if risk_params is None:
        risk_params = dict(MOCK_RISK_PARAMS)
    if positions is None:
        positions = []
    if kill_switch is None:
        kill_switch = {"enabled": False}

    mock_client = MagicMock()
    mock_client.get_equity.return_value = equity
    mock_client.get_positions.return_value = positions
    mock_client.get_mid_prices.return_value = {"BTC": 97000.0, "ETH": 2700.0, "SOL": 160.0}
    mock_client.address = "0xMOCK"
    mock_client._main_address = "0xMAIN"
    mock_client.place_market_order.return_value = {
        "success": True, "status": "filled", "fill_price": 97000.0, "raw_response": {}, "error": None,
    }
    mock_client.close_position.return_value = {
        "success": True, "status": "closed", "fill_price": 97100.0, "raw_response": {},
    }

    mock_state = MagicMock()
    mock_state.get_kill_switch_status.return_value = kill_switch
    mock_state.get_positions.return_value = positions
    mock_state.get_daily_pnl.return_value = {
        "equity": equity, "start_of_day_equity": equity,
        "realized_pnl": 0, "unrealized_pnl": 0,
    }
    mock_state.sync_positions.return_value = positions
    mock_state.record_trade.return_value = None

    with patch("src.executor.trade_executor.load_settings", return_value=settings), \
         patch("src.executor.trade_executor.load_risk_params", return_value=risk_params), \
         patch("src.executor.trade_executor.HLClient", return_value=mock_client), \
         patch("src.executor.trade_executor.StateManager", return_value=mock_state), \
         patch("src.executor.trade_executor.get_signals_dir", return_value=Path("/tmp/signals")), \
         patch("src.executor.trade_executor.get_state_dir", return_value=Path("/tmp/state")):
        from src.executor.trade_executor import TradeExecutor
        executor = TradeExecutor()

    return executor, mock_client, mock_state


def _make_signal(
    symbol="BTC",
    action="long",
    confidence=0.85,
    entry_price=97000.0,
    tp=97300.0,
    sl=96400.0,
    leverage=3,
    exit_mode="tp_sl",
):
    return {
        "symbol": symbol,
        "action": action,
        "confidence": confidence,
        "entry_price": entry_price,
        "take_profit": tp,
        "stop_loss": sl,
        "leverage": leverage,
        "reasoning": "test",
        "exit_mode": exit_mode,
    }


# ===========================================================================
#  Tests
# ===========================================================================


class TestHoldSkip:
    def test_hold_returns_none(self):
        """action='hold' → None."""
        executor, _, _ = _make_executor()
        result = executor.execute_signal({"symbol": "BTC", "action": "hold", "confidence": 0.85})
        assert result is None

    def test_hold_position_returns_none(self):
        """action='hold_position' → None."""
        executor, _, _ = _make_executor()
        result = executor.execute_signal({"symbol": "BTC", "action": "hold_position", "confidence": 1.0})
        assert result is None


class TestLowConfidenceSkip:
    def test_low_confidence(self):
        """confidence < 0.7 → None."""
        executor, _, _ = _make_executor()
        result = executor.execute_signal(_make_signal(confidence=0.5))
        assert result is None


class TestRiskRejected:
    def test_risk_manager_rejects(self):
        """validate_signal → False → rejected."""
        executor, _, _ = _make_executor()
        # RiskManager is imported lazily inside execute_signal, patch at source
        with patch("src.risk.risk_manager.RiskManager") as MockRM:
            rm = MockRM.return_value
            rm.validate_signal.return_value = (False, "exposure too high")
            result = executor.execute_signal(_make_signal())
        assert result is not None
        assert result["status"] == "rejected"
        assert "exposure" in result["reason"]


class TestGateEquityDrift:
    def test_equity_drift_rejects(self):
        """drift > 20% → rejected."""
        executor, mock_client, mock_state = _make_executor(equity=500.0)
        # State equity vs live equity: 500 (live) vs 300 (state) => 66% drift
        mock_state.get_daily_pnl.return_value = {
            "equity": 300.0, "start_of_day_equity": 500.0,
            "realized_pnl": 0, "unrealized_pnl": 0,
        }

        with patch("src.risk.risk_manager.RiskManager") as MockRM:
            rm = MockRM.return_value
            rm.validate_signal.return_value = (True, "ok")
            with patch("src.executor.trade_executor.read_json", return_value={"score": 100}):
                result = executor.execute_signal(_make_signal())

        assert result is not None
        assert result["status"] == "rejected"
        assert "drift" in result["reason"].lower() or "equity" in result["reason"].lower()


class TestGateDailyLoss:
    def test_daily_loss_rejects(self):
        """loss >= 2% → rejected."""
        executor, _, mock_state = _make_executor(equity=500.0)
        mock_state.get_daily_pnl.return_value = {
            "equity": 500.0, "start_of_day_equity": 500.0,
            "realized_pnl": -15.0, "unrealized_pnl": 0,  # 3% loss
        }

        with patch("src.risk.risk_manager.RiskManager") as MockRM:
            rm = MockRM.return_value
            rm.validate_signal.return_value = (True, "ok")
            with patch("src.executor.trade_executor.read_json", return_value={"score": 100}):
                result = executor.execute_signal(_make_signal())

        assert result is not None
        assert result["status"] == "rejected"
        assert "daily" in result["reason"].lower() or "loss" in result["reason"].lower() or "budget" in result["reason"].lower()


class TestGateCooldown:
    def test_cooldown_rejects(self):
        """10分以内 → rejected."""
        executor, _, mock_state = _make_executor(equity=500.0)
        mock_state.get_daily_pnl.return_value = {
            "equity": 500.0, "start_of_day_equity": 500.0,
            "realized_pnl": 0, "unrealized_pnl": 0,
        }
        recent_trade = {
            "symbol": "BTC",
            "closed_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
        }

        with patch("src.risk.risk_manager.RiskManager") as MockRM:
            rm = MockRM.return_value
            rm.validate_signal.return_value = (True, "ok")
            with patch("src.executor.trade_executor.read_json") as mock_read:
                def read_side_effect(path):
                    path_str = str(path)
                    if "trade_history" in path_str:
                        return [recent_trade]
                    if "data_health" in path_str:
                        return {"score": 100}
                    if "market_data" in path_str:
                        return {"symbols": {"BTC": {"mid_price": 97000, "orderbook": {"bids": [{"px": "97000", "sz": "10"}], "asks": [{"px": "97001", "sz": "10"}]}}}}
                    if "size_regime" in path_str:
                        return {"multiplier": 1.0, "reason": "test"}
                    raise FileNotFoundError
                mock_read.side_effect = read_side_effect
                result = executor.execute_signal(_make_signal())

        assert result is not None
        assert result["status"] == "rejected"
        assert "cooldown" in result["reason"].lower()


class TestOpenPosition:
    def test_place_market_order_called(self):
        """place_market_order 呼び出し確認。"""
        executor, mock_client, mock_state = _make_executor(equity=500.0)
        mock_state.get_daily_pnl.return_value = {
            "equity": 500.0, "start_of_day_equity": 500.0,
            "realized_pnl": 0, "unrealized_pnl": 0,
        }

        with patch("src.risk.risk_manager.RiskManager") as MockRM:
            rm = MockRM.return_value
            rm.validate_signal.return_value = (True, "ok")

            with patch.object(executor, "_composite_entry_gate", return_value=(True, "all gates passed")):
                sig = _make_signal()
                sig["size"] = 0.001
                result = executor.execute_signal(sig)

        assert result is not None
        assert result["status"] == "filled"
        mock_client.place_market_order.assert_called_once()


class TestCloseRecordsPnl:
    def test_close_records(self):
        """PnL計算 + record_trade 呼び出し。"""
        executor, mock_client, mock_state = _make_executor(
            equity=500.0,
            positions=[{
                "symbol": "BTC", "side": "long", "size": 0.001,
                "entry_price": 96000.0, "opened_at": "2026-01-01T00:00:00",
            }],
        )
        mock_state.get_positions.return_value = [{
            "symbol": "BTC", "side": "long", "size": 0.001,
            "entry_price": 96000.0, "opened_at": "2026-01-01T00:00:00",
        }]

        signal = {"symbol": "BTC", "action": "close", "confidence": 1.0, "reasoning": "SL hit"}

        with patch("src.risk.risk_manager.RiskManager") as MockRM:
            rm = MockRM.return_value
            rm.validate_signal.return_value = (True, "ok")
            result = executor.execute_signal(signal)

        assert result is not None
        assert result["status"] == "closed"
        mock_state.record_trade.assert_called_once()
        trade_record = mock_state.record_trade.call_args[0][0]
        assert trade_record["symbol"] == "BTC"
        assert trade_record["pnl"] is not None
