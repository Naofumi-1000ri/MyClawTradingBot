"""Tests for brain_consensus.py internal functions.

_get_fallback_adjusted_settings() と _signals_to_merged() を直接テスト。
main() は呼ばない。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import MOCK_SETTINGS


# ---------------------------------------------------------------------------
#  _signals_to_merged tests
# ---------------------------------------------------------------------------


class TestSignalFormat:
    def test_required_fields(self):
        """シグナルに必須フィールドが含まれるか。"""
        from src.brain.brain_consensus import _signals_to_merged

        signals = [{
            "symbol": "BTC",
            "action": "long",
            "direction": "long",
            "confidence": 0.85,
            "entry_price": 97000.0,
            "take_profit": 97300.0,
            "stop_loss": 96400.0,
            "leverage": 3,
            "reasoning": "test signal",
            "zone": "penetration",
        }]
        merged = _signals_to_merged(signals)
        assert "signals" in merged
        assert "action_type" in merged
        assert "ooda" in merged

        sig = merged["signals"][0]
        for key in ("symbol", "action", "confidence", "take_profit", "stop_loss"):
            assert key in sig, f"Missing key: {key}"


class TestHoldWhenNoSpike:
    def test_fallback_output(self):
        """全戦略がNone返却 → action_type='hold'."""
        from src.brain.brain_consensus import _fallback_output

        result = _fallback_output(["BTC", "ETH", "SOL"], "スパイクなし: 静観")
        assert result["action_type"] == "hold"
        assert len(result["signals"]) == 3
        for sig in result["signals"]:
            assert sig["action"] == "hold"
            assert sig["confidence"] == 0.0


class TestTradeWhenSignalExists:
    def test_signals_to_merged_trade(self):
        """BtcRubberWall.scan()がシグナル返却 → action_type='trade'."""
        from src.brain.brain_consensus import _signals_to_merged

        signals = [{
            "symbol": "BTC",
            "action": "long",
            "direction": "long",
            "confidence": 0.85,
            "entry_price": 97000.0,
            "take_profit": 97300.0,
            "stop_loss": 96400.0,
            "leverage": 3,
            "reasoning": "RubberWall penetration",
            "zone": "penetration",
        }]
        merged = _signals_to_merged(signals)
        assert merged["action_type"] == "trade"


# ---------------------------------------------------------------------------
#  _get_fallback_adjusted_settings tests
# ---------------------------------------------------------------------------


class TestFallbackPhase1Thresholds:
    def test_phase1_adjustment(self):
        """6サイクル連続 → vol_threshold 5.0→4.5 (-10%)."""
        from src.brain.brain_consensus import _get_fallback_adjusted_settings

        settings = {
            "strategy": {
                "rubber_wall": {"vol_threshold": 5.0},
                "rubber_band": {"reversal_threshold": 7.0, "momentum_low_vol_skip": True},
                "sol_rubber_wall": {"vol_threshold": 5.0},
            }
        }
        adjusted = _get_fallback_adjusted_settings(settings, 6)
        assert adjusted is not settings  # deep copy
        assert adjusted["strategy"]["rubber_wall"]["vol_threshold"] == pytest.approx(4.5, abs=0.1)
        assert adjusted["strategy"]["sol_rubber_wall"]["vol_threshold"] == pytest.approx(4.5, abs=0.1)
        # momentum_low_vol_skip should be disabled
        assert adjusted["strategy"]["rubber_band"]["momentum_low_vol_skip"] is False


class TestFallbackPhase2Thresholds:
    def test_phase2_adjustment(self):
        """12サイクル連続 → vol_threshold 5.0→4.0 (-20%)."""
        from src.brain.brain_consensus import _get_fallback_adjusted_settings

        settings = {
            "strategy": {
                "rubber_wall": {"vol_threshold": 5.0},
                "rubber_band": {"reversal_threshold": 7.0, "momentum_low_vol_skip": True},
                "sol_rubber_wall": {"vol_threshold": 5.0},
            }
        }
        adjusted = _get_fallback_adjusted_settings(settings, 12)
        assert adjusted["strategy"]["rubber_wall"]["vol_threshold"] == pytest.approx(4.0, abs=0.1)
        assert adjusted["strategy"]["sol_rubber_wall"]["vol_threshold"] == pytest.approx(4.0, abs=0.1)
        # ETH reversal_threshold: 7.0 * 0.79 ≈ 5.53
        assert adjusted["strategy"]["rubber_band"]["reversal_threshold"] == pytest.approx(5.53, abs=0.1)


class TestFallbackResetOnTrade:
    def test_no_adjustment_below_threshold(self):
        """5サイクル以下 → 調整なし (同じsettings返却)."""
        from src.brain.brain_consensus import _get_fallback_adjusted_settings

        settings = {
            "strategy": {
                "rubber_wall": {"vol_threshold": 5.0},
                "rubber_band": {"reversal_threshold": 7.0},
                "sol_rubber_wall": {"vol_threshold": 5.0},
            }
        }
        result = _get_fallback_adjusted_settings(settings, 5)
        assert result is settings  # same object, no copy


class TestMultipleSymbolSignals:
    def test_btc_eth_simultaneous(self):
        """BTC long + ETH short が同時に出力可能。"""
        from src.brain.brain_consensus import _signals_to_merged

        signals = [
            {
                "symbol": "BTC", "action": "long", "direction": "long",
                "confidence": 0.85, "entry_price": 97000.0,
                "take_profit": 97300.0, "stop_loss": 96400.0,
                "leverage": 3, "reasoning": "BTC signal",
            },
            {
                "symbol": "ETH", "action": "short", "direction": "short",
                "confidence": 0.80, "entry_price": 2700.0,
                "take_profit": 2670.0, "stop_loss": 2730.0,
                "leverage": 3, "reasoning": "ETH signal",
            },
        ]
        merged = _signals_to_merged(signals)
        assert merged["action_type"] == "trade"
        assert len(merged["signals"]) == 2
        symbols = {s["symbol"] for s in merged["signals"]}
        assert symbols == {"BTC", "ETH"}


class TestExitPriorityOverEntry:
    def test_close_in_signals(self):
        """closeシグナルがtrade action_typeで出力される。"""
        from src.brain.brain_consensus import _signals_to_merged

        signals = [
            {
                "symbol": "ETH", "action": "close", "direction": "close",
                "confidence": 1.0, "reasoning": "SL hit",
            },
            {
                "symbol": "BTC", "action": "long", "direction": "long",
                "confidence": 0.85, "entry_price": 97000.0,
                "take_profit": 97300.0, "stop_loss": 96400.0,
                "leverage": 3, "reasoning": "new entry",
            },
        ]
        merged = _signals_to_merged(signals)
        assert merged["action_type"] == "trade"
        actions = {s["action"] for s in merged["signals"]}
        assert "close" in actions


class TestHoldPositionForActive:
    def test_hold_position_only_is_hold(self):
        """hold_positionシグナルのみ → action_type='hold'."""
        from src.brain.brain_consensus import _signals_to_merged

        signals = [
            {
                "symbol": "ETH", "action": "hold_position", "direction": "hold_position",
                "confidence": 1.0, "reasoning": "holding",
            },
        ]
        merged = _signals_to_merged(signals)
        assert merged["action_type"] == "hold"
