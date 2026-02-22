"""Strategy precheck tests (make pretest-strategy).

新戦略追加時のゲート: フォーマット・リスク準拠・品質を検証。
使用法:
    make pretest-strategy STRATEGY=src.strategy.btc_rubber_wall
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.helpers.backtest_runner import run_backtest
from tests.helpers.candle_factory import make_candles, make_uptrend_candles, inject_spike


# ---------------------------------------------------------------------------
#  Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def strategy_candles():
    """Candles with some spikes for precheck testing."""
    candles = make_candles(n=600, base_price=97000.0, base_volume=100.0, seed=42)
    # Inject some spikes at various positions
    for offset, vm in [(200, 6.0), (300, 8.0), (400, 5.5), (500, 7.0)]:
        inject_spike(candles, offset, vol_multiplier=vm, bear=True)
    return candles


@pytest.fixture
def strategy_signals(strategy_module, strategy_candles):
    """Collect all signals from scanning the candle set."""
    signals = []
    window = 300
    i = window
    while i < len(strategy_candles) - 1:
        chunk = strategy_candles[max(0, i - window):i + 2]
        s = strategy_module(chunk)
        result = s.scan(cache=None)
        sig = result[0] if isinstance(result, tuple) else result
        if sig is not None:
            signals.append(sig)
        i += 1
    return signals


# ---------------------------------------------------------------------------
#  Format Tests
# ---------------------------------------------------------------------------


class TestRequiredFields:
    def test_required_fields(self, strategy_signals):
        """symbol, action, confidence, tp, sl, entry_price must be present."""
        if not strategy_signals:
            pytest.skip("No signals generated")
        for sig in strategy_signals:
            assert "symbol" in sig
            assert "action" in sig or "direction" in sig
            assert "confidence" in sig
            assert "take_profit" in sig
            assert "stop_loss" in sig
            assert "entry_price" in sig


class TestActionValues:
    def test_valid_actions(self, strategy_signals):
        """action ∈ {"long", "short", "hold", "close", "hold_position"}."""
        valid = {"long", "short", "hold", "close", "hold_position"}
        for sig in strategy_signals:
            action = sig.get("action") or sig.get("direction")
            assert action in valid, f"Invalid action: {action}"


class TestConfidenceRange:
    def test_confidence_bounds(self, strategy_signals):
        """0 ≤ confidence ≤ 1."""
        for sig in strategy_signals:
            c = sig["confidence"]
            assert 0 <= c <= 1, f"Confidence out of range: {c}"


class TestTpSlGeometry:
    def test_tp_sl_direction(self, strategy_signals):
        """long: tp>entry, sl<entry / short: tp<entry, sl>entry."""
        for sig in strategy_signals:
            action = sig.get("action") or sig.get("direction")
            if action not in ("long", "short"):
                continue
            entry = sig.get("entry_price", 0)
            tp = sig.get("take_profit", 0)
            sl = sig.get("stop_loss", 0)
            if not entry or not tp or not sl:
                continue
            if action == "long":
                assert tp > entry, f"Long: TP {tp} <= entry {entry}"
                assert sl < entry, f"Long: SL {sl} >= entry {entry}"
            else:
                assert tp < entry, f"Short: TP {tp} >= entry {entry}"
                assert sl > entry, f"Short: SL {sl} <= entry {entry}"


# ---------------------------------------------------------------------------
#  Risk Compliance Tests
# ---------------------------------------------------------------------------


class TestLeverageLimit:
    def test_max_leverage(self, strategy_signals):
        """leverage ≤ max_leverage (10)."""
        max_lev = 10
        for sig in strategy_signals:
            lev = sig.get("leverage", 1)
            assert lev <= max_lev, f"Leverage {lev} > {max_lev}"


class TestSizeLimit:
    def test_no_oversized(self, strategy_signals):
        """size (if present) should be reasonable."""
        for sig in strategy_signals:
            size = sig.get("size")
            if size is not None:
                assert size > 0, f"Negative or zero size: {size}"
                assert size < 1000, f"Unreasonably large size: {size}"


# ---------------------------------------------------------------------------
#  Quality Tests (Backtest)
# ---------------------------------------------------------------------------


class TestBacktestPF:
    def test_profit_factor(self, strategy_module, strategy_candles):
        """直近データで PF > 0.5."""
        result = run_backtest(strategy_module, strategy_candles, window=300)
        if result["total"] == 0:
            pytest.skip("No trades in backtest")
        assert result["pf"] > 0.5, f"PF {result['pf']} < 0.5"


class TestBacktestWinRate:
    def test_win_rate(self, strategy_module, strategy_candles):
        """勝率 > 10%."""
        result = run_backtest(strategy_module, strategy_candles, window=300)
        if result["total"] == 0:
            pytest.skip("No trades in backtest")
        assert result["win_rate"] > 0.10, f"Win rate {result['win_rate']} < 10%"
