"""Integration tests: data→brain→executor の流れ。

HLClient のみモック。tmpdir でファイルI/O隔離。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import MOCK_SETTINGS, MOCK_RISK_PARAMS
from tests.helpers.candle_factory import make_candles


def _setup_integration(tmp_path, candles=None, equity=500.0, kill_switch_enabled=False, data_health_score=100):
    """Set up isolated dirs and mock files for integration test."""
    data_dir = tmp_path / "data"
    signals_dir = tmp_path / "signals"
    state_dir = tmp_path / "state"
    for d in (data_dir, signals_dir, state_dir):
        d.mkdir()

    if candles is None:
        candles = make_candles(n=336, base_price=97000.0)

    # Write kill switch
    ks = {"enabled": kill_switch_enabled}
    if kill_switch_enabled:
        ks["reason"] = "test kill switch"
    (state_dir / "kill_switch.json").write_text(json.dumps(ks))

    # Write positions (empty)
    (state_dir / "positions.json").write_text("[]")

    # Write daily PnL
    daily = {
        "equity": equity, "start_of_day_equity": equity,
        "realized_pnl": 0, "unrealized_pnl": 0,
    }
    (state_dir / "daily_pnl.json").write_text(json.dumps(daily))

    # Write data health
    (state_dir / "data_health.json").write_text(json.dumps({"score": data_health_score}))

    # Write size regime
    (state_dir / "size_regime.json").write_text(json.dumps({
        "multiplier": 1.0, "reason": "test", "phase": "phase0",
    }))

    # Market data
    market_data = {
        "timestamp": "2026-02-22T00:00:00+00:00",
        "symbols": {
            "BTC": {
                "mid_price": 97000.0,
                "candles_5m": candles,
                "candles_15m": candles[-100:],
                "candles_1h": candles[-50:],
                "candles_4h": candles[-20:],
                "orderbook": {"bids": [{"px": "97000", "sz": "10"}], "asks": [{"px": "97001", "sz": "10"}]},
                "funding_rate": 0.0001,
            },
            "ETH": {
                "mid_price": 2700.0,
                "candles_5m": make_candles(n=336, base_price=2700.0, seed=99),
                "candles_15m": [],
                "candles_1h": [],
                "candles_4h": [],
                "orderbook": {"bids": [{"px": "2700", "sz": "10"}], "asks": [{"px": "2701", "sz": "10"}]},
                "funding_rate": -0.0001,
            },
            "SOL": {
                "mid_price": 160.0,
                "candles_5m": make_candles(n=336, base_price=160.0, seed=77),
                "candles_15m": [],
                "candles_1h": [],
                "candles_4h": [],
                "orderbook": {"bids": [{"px": "160", "sz": "100"}], "asks": [{"px": "160.01", "sz": "100"}]},
                "funding_rate": 0.0,
            },
        },
        "account_equity": equity,
    }
    (data_dir / "market_data.json").write_text(json.dumps(market_data))

    return {
        "data_dir": data_dir,
        "signals_dir": signals_dir,
        "state_dir": state_dir,
        "market_data": market_data,
        "root": tmp_path,
    }


def _run_brain(dirs, settings=None):
    """Run _run_rubber_wall directly with mocked context."""
    from src.brain.brain_consensus import _run_rubber_wall, _fallback_output

    if settings is None:
        settings = dict(MOCK_SETTINGS)

    market_data = json.loads((dirs["data_dir"] / "market_data.json").read_text())

    context = {
        "market_data": market_data.get("symbols", {}),
        "daily_pnl": json.loads((dirs["state_dir"] / "daily_pnl.json").read_text()),
    }

    with patch("src.brain.brain_consensus.ROOT", dirs["root"]), \
         patch("src.brain.brain_consensus.STATE_DIR", dirs["state_dir"]), \
         patch("src.brain.brain_consensus.SIGNALS_DIR", dirs["signals_dir"]), \
         patch("src.brain.brain_consensus._FALLBACK_TRACKER_PATH", dirs["state_dir"] / "fallback_tracker.json"):
        all_failed = _run_rubber_wall(settings, context)

    # Read signals output
    sig_path = dirs["signals_dir"] / "signals.json"
    if sig_path.exists():
        signals = json.loads(sig_path.read_text())
    else:
        signals = None

    return all_failed, signals


class TestFullCycleHold:
    def test_hold_no_execute(self, tmp_path):
        """hold → execute されない。"""
        dirs = _setup_integration(tmp_path)
        all_failed, signals = _run_brain(dirs)

        assert all_failed is False
        assert signals is not None
        # With no spikes, should be hold
        assert signals["action_type"] == "hold"
        # All signals should be hold
        for sig in signals["signals"]:
            assert sig["action"] in ("hold", "hold_position")


class TestFullCycleWithSignal:
    def test_signal_propagation(self, tmp_path):
        """シグナルが signals.json に書き込まれる。"""
        dirs = _setup_integration(tmp_path)
        all_failed, signals = _run_brain(dirs)

        assert signals is not None
        assert "signals" in signals
        assert "action_type" in signals
        assert isinstance(signals["signals"], list)


class TestKillSwitchBlocks:
    def test_kill_switch_blocks_execution(self, tmp_path):
        """kill_switch=True → executor returns empty list."""
        dirs = _setup_integration(tmp_path, kill_switch_enabled=True)

        # Write a trade signal to signals.json
        sig_data = {
            "action_type": "trade",
            "signals": [{
                "symbol": "BTC", "action": "long", "confidence": 0.85,
                "entry_price": 97000.0, "take_profit": 97300.0, "stop_loss": 96400.0,
                "leverage": 3, "reasoning": "test",
            }],
        }
        (dirs["signals_dir"] / "signals.json").write_text(json.dumps(sig_data))

        # Create executor with kill switch
        mock_client = MagicMock()
        mock_client.get_equity.return_value = 500.0
        mock_client.address = "0xMOCK"
        mock_client._main_address = "0xMAIN"

        mock_state = MagicMock()
        mock_state.get_kill_switch_status.return_value = {"enabled": True, "reason": "test"}

        with patch("src.executor.trade_executor.load_settings", return_value=MOCK_SETTINGS), \
             patch("src.executor.trade_executor.load_risk_params", return_value=MOCK_RISK_PARAMS), \
             patch("src.executor.trade_executor.HLClient", return_value=mock_client), \
             patch("src.executor.trade_executor.StateManager", return_value=mock_state), \
             patch("src.executor.trade_executor.get_signals_dir", return_value=dirs["signals_dir"]), \
             patch("src.executor.trade_executor.get_state_dir", return_value=dirs["state_dir"]):
            from src.executor.trade_executor import TradeExecutor
            executor = TradeExecutor()
            results = executor.execute_signals()

        assert results == []


class TestDataHealthBlocksEntry:
    def test_low_data_health(self, tmp_path):
        """score < 80 → rejected."""
        dirs = _setup_integration(tmp_path, data_health_score=50)

        mock_client = MagicMock()
        mock_client.get_equity.return_value = 500.0
        mock_client.get_positions.return_value = []
        mock_client.get_mid_prices.return_value = {"BTC": 97000.0}
        mock_client.address = "0xMOCK"
        mock_client._main_address = "0xMAIN"

        mock_state = MagicMock()
        mock_state.get_kill_switch_status.return_value = {"enabled": False}
        mock_state.get_positions.return_value = []
        mock_state.get_daily_pnl.return_value = {
            "equity": 500.0, "start_of_day_equity": 500.0,
            "realized_pnl": 0, "unrealized_pnl": 0,
        }

        with patch("src.executor.trade_executor.load_settings", return_value=MOCK_SETTINGS), \
             patch("src.executor.trade_executor.load_risk_params", return_value=MOCK_RISK_PARAMS), \
             patch("src.executor.trade_executor.HLClient", return_value=mock_client), \
             patch("src.executor.trade_executor.StateManager", return_value=mock_state), \
             patch("src.executor.trade_executor.get_signals_dir", return_value=dirs["signals_dir"]), \
             patch("src.executor.trade_executor.get_state_dir", return_value=dirs["state_dir"]):
            from src.executor.trade_executor import TradeExecutor
            executor = TradeExecutor()

            signal = {
                "symbol": "BTC", "action": "long", "confidence": 0.85,
                "entry_price": 97000.0, "take_profit": 97300.0, "stop_loss": 96400.0,
                "leverage": 3, "reasoning": "test", "exit_mode": "tp_sl",
            }

            with patch("src.risk.risk_manager.RiskManager") as MockRM:
                rm = MockRM.return_value
                rm.validate_signal.return_value = (True, "ok")
                result = executor.execute_signal(signal)

        assert result is not None
        assert result["status"] == "rejected"
        assert "data quality" in result["reason"].lower() or "data_health" in result["reason"].lower()
