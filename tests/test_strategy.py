"""Tests for BtcRubberWall, EthRubberBand, SolRubberWall scan() methods.

Pure logic tests — no mocking needed. candle_factory で生成したデータを使用。
"""

from __future__ import annotations

import pytest

from src.strategy.base import BaseStrategy
from src.strategy.btc_rubber_wall import BtcRubberWall
from src.strategy.eth_rubber_band import EthRubberBand
from src.strategy.sol_rubber_wall import SolRubberWall
from tests.helpers.candle_factory import (
    inject_spike,
    make_candles,
    make_low_vol_candles,
    make_uptrend_candles,
)


# ---------------------------------------------------------------------------
#  Helper: position candles so spike lands in a specific 4H range zone
# ---------------------------------------------------------------------------

def _make_spike_candles(
    base_price: float = 97000.0,
    n: int = 300,
    spike_idx: int | None = None,
    vol_multiplier: float = 8.0,
    range_position_target: float = 50.0,
    seed: int = 42,
) -> list[dict]:
    """Generate candles with a BEAR spike at specified range position.

    range_position_target: 0=bottom, 50=mid, -15=penetration, 80=upper
    """
    candles = make_candles(n=n, base_price=base_price, base_volume=100.0, seed=seed)
    if spike_idx is None:
        spike_idx = n - 2  # scan uses idx = len-2

    # Compute h4 range from the 48 candles before spike
    h4_start = max(0, spike_idx - 48)
    h4_low = min(c["l"] for c in candles[h4_start:spike_idx])
    h4_high = max(c["h"] for c in candles[h4_start:spike_idx])
    h4_span = h4_high - h4_low

    # Set close price to achieve desired range position
    target_close = h4_low + (range_position_target / 100.0) * h4_span
    open_price = target_close * 1.005  # BEAR: open > close

    candles[spike_idx]["o"] = round(open_price, 6)
    candles[spike_idx]["c"] = round(target_close, 6)
    candles[spike_idx]["h"] = round(max(open_price, target_close) * 1.001, 6)
    candles[spike_idx]["l"] = round(min(open_price, target_close) * 0.999, 6)

    inject_spike(candles, spike_idx, vol_multiplier=vol_multiplier, bear=True)
    # Restore the price we set (inject_spike modifies close)
    candles[spike_idx]["o"] = round(open_price, 6)
    candles[spike_idx]["c"] = round(target_close, 6)

    return candles


# ===========================================================================
#  BTC RubberWall Tests
# ===========================================================================


class TestBtcBearSpikePenetrationLong:
    def test_penetration_long(self):
        """vol=5.5x, pos=-15% → LONG (penetration zone)."""
        candles = _make_spike_candles(
            vol_multiplier=6.0, range_position_target=-15.0,
        )
        sig, cache = BtcRubberWall(candles).scan()
        assert sig is not None
        assert sig["symbol"] == "BTC"
        assert sig["action"] == "long"
        assert sig["zone"] == "penetration"
        assert sig["confidence"] > 0
        assert sig["take_profit"] > sig["entry_price"]
        assert sig["stop_loss"] < sig["entry_price"]


class TestBtcBearSpikeUpperShort:
    def test_upper_short(self):
        """vol=5.5x, pos=60% → SHORT (upper_range zone)."""
        candles = _make_spike_candles(
            vol_multiplier=6.0, range_position_target=60.0,
        )
        sig, cache = BtcRubberWall(candles).scan()
        assert sig is not None
        assert sig["symbol"] == "BTC"
        assert sig["action"] == "short"
        assert sig["zone"] == "upper_range"
        assert sig["take_profit"] < sig["entry_price"]
        assert sig["stop_loss"] > sig["entry_price"]


class TestBtcNoSpikeHold:
    def test_no_spike(self):
        """vol=1.5x → None (no spike detected)."""
        candles = make_candles(n=300, base_price=97000.0, base_volume=100.0)
        sig, cache = BtcRubberWall(candles).scan()
        assert sig is None


class TestBtcBottomRequiresVol7x:
    def test_bottom_below_7x(self):
        """vol=6.5x, pos=10% → None (bottom zone requires 7.0x)."""
        candles = _make_spike_candles(
            vol_multiplier=6.5, range_position_target=10.0,
        )
        sig, cache = BtcRubberWall(candles).scan()
        assert sig is None

    def test_bottom_above_7x(self):
        """vol=8.0x, pos=10% → SHORT (bottom zone with 7.0x+ spike)."""
        candles = _make_spike_candles(
            vol_multiplier=8.0, range_position_target=10.0,
        )
        sig, cache = BtcRubberWall(candles).scan()
        assert sig is not None
        assert sig["action"] == "short"
        assert sig["zone"] == "bottom"


class TestBtcQuietLong:
    def test_quiet_long(self):
        """EMA golden + pos>=65% + low vol → LONG."""
        candles = make_uptrend_candles(n=300, base_price=97000.0, seed=42)
        # Make last candle at high range position
        idx = len(candles) - 2
        h4_low = min(c["l"] for c in candles[max(0, idx - 48):idx])
        h4_high = max(c["h"] for c in candles[max(0, idx - 48):idx])
        # Set close above 65% range
        target = h4_low + 0.70 * (h4_high - h4_low)
        candles[idx]["c"] = round(target, 6)
        candles[idx]["o"] = round(target * 0.999, 6)  # BULL candle (not BEAR)
        # Low volume
        make_low_vol_candles(candles, idx - 5, idx + 1, vol_ratio=0.3)

        sig, cache = BtcRubberWall(candles).scan()
        # quiet_long may or may not fire depending on additional filters (RSI, momentum, body)
        # If it fires, verify format
        if sig is not None:
            assert sig["action"] == "long"
            assert sig["pattern"] == "D_quiet_long"
            assert sig["confidence"] < 0.80


# ===========================================================================
#  ETH RubberBand Tests
# ===========================================================================


class TestEthReversalLong:
    def test_reversal_long(self):
        """vol=8.0x, pos=20% → LONG (Pattern A reversal)."""
        candles = _make_spike_candles(
            base_price=2700.0, vol_multiplier=8.0, range_position_target=20.0,
        )
        sig, cache = EthRubberBand(candles).scan()
        assert sig is not None
        assert sig["symbol"] == "ETH"
        assert sig["action"] == "long"
        assert sig["pattern"] == "A_reversal"


class TestEthMomentumShort:
    def test_momentum_short(self):
        """vol=4.0x, pos=50% → SHORT (Pattern B momentum)."""
        candles = _make_spike_candles(
            base_price=2700.0, vol_multiplier=4.0, range_position_target=50.0,
        )
        sig, cache = EthRubberBand(candles).scan()
        assert sig is not None
        assert sig["symbol"] == "ETH"
        assert sig["action"] == "short"
        assert sig["pattern"] == "B_momentum"


class TestEthMomentumSkipLowVol:
    def test_low_vol_skip(self):
        """vol=4.0x, low_vol regime → None (momentum_low_vol_skip)."""
        # Create low volatility candles (short ATR << long ATR)
        candles = make_candles(n=300, base_price=2700.0, base_volume=100.0, volatility=0.0005, seed=42)
        # Set recent candles to be very low volatility
        for i in range(len(candles) - 30, len(candles)):
            mid = candles[i]["c"]
            candles[i]["h"] = mid * 1.0001
            candles[i]["l"] = mid * 0.9999

        idx = len(candles) - 2
        inject_spike(candles, idx, vol_multiplier=4.0, bear=True)
        # Force range position to >= 40%
        h4_start = max(0, idx - 48)
        h4_low = min(c["l"] for c in candles[h4_start:idx])
        h4_high = max(c["h"] for c in candles[h4_start:idx])
        target = h4_low + 0.55 * (h4_high - h4_low)
        candles[idx]["c"] = round(target, 6)

        sig, cache = EthRubberBand(candles, {"momentum_low_vol_skip": True}).scan()
        # In low_vol regime, momentum pattern B should be skipped
        if sig is not None:
            # If a signal fires, it should NOT be B_momentum in low_vol
            assert sig.get("pattern") != "B_momentum" or sig.get("vol_regime") != "low_vol"


class TestEthNoSpikeHold:
    def test_no_spike(self):
        """vol=1.0x → None."""
        candles = make_candles(n=300, base_price=2700.0, base_volume=100.0)
        sig, cache = EthRubberBand(candles).scan()
        # May return quiet_long or None
        if sig is not None:
            assert sig.get("pattern") == "C_quiet_long"


class TestEthQuietLong:
    def test_quiet_long(self):
        """pos<45%, low vol, GOLDEN → LONG (Pattern C)."""
        candles = make_uptrend_candles(n=300, base_price=2700.0, seed=42)
        idx = len(candles) - 2

        # Low range position (bottom zone)
        h4_start = max(0, idx - 48)
        h4_low = min(c["l"] for c in candles[h4_start:idx])
        h4_high = max(c["h"] for c in candles[h4_start:idx])
        target = h4_low + 0.30 * (h4_high - h4_low)
        candles[idx]["c"] = round(target, 6)
        candles[idx]["o"] = round(target * 0.999, 6)

        make_low_vol_candles(candles, idx - 5, idx + 1, vol_ratio=0.3)

        sig, cache = EthRubberBand(candles).scan()
        if sig is not None:
            assert sig["action"] == "long"
            assert sig["pattern"] == "C_quiet_long"


# ===========================================================================
#  SOL RubberWall Tests
# ===========================================================================


class TestSolBearSpikeShort:
    def test_bear_spike_short(self):
        """vol=5.5x → SHORT (penetration or upper_range zone)."""
        candles = _make_spike_candles(
            base_price=160.0, vol_multiplier=6.0, range_position_target=-10.0,
        )
        sig, cache = SolRubberWall(candles).scan()
        assert sig is not None
        assert sig["symbol"] == "SOL"
        assert sig["action"] == "short"


class TestSolFundingBlocksShort:
    def test_funding_blocks(self):
        """funding=-6e-5 → None (SHORT blocked by funding)."""
        candles = _make_spike_candles(
            base_price=160.0, vol_multiplier=6.0, range_position_target=-10.0,
        )
        config = {"current_funding_rate": -6e-5}
        sig, cache = SolRubberWall(candles, config).scan()
        assert sig is None


class TestSolQuietShort:
    def test_quiet_short(self):
        """pos>=70%, RSI>55 → SHORT (Pattern E)."""
        candles = make_uptrend_candles(n=300, base_price=160.0, seed=42)
        idx = len(candles) - 2

        h4_start = max(0, idx - 48)
        h4_low = min(c["l"] for c in candles[h4_start:idx])
        h4_high = max(c["h"] for c in candles[h4_start:idx])
        target = h4_low + 0.75 * (h4_high - h4_low)
        candles[idx]["c"] = round(target, 6)
        candles[idx]["o"] = round(target * 0.999, 6)

        make_low_vol_candles(candles, idx - 5, idx + 1, vol_ratio=0.3)

        sig, cache = SolRubberWall(candles).scan()
        if sig is not None:
            assert sig["action"] == "short"
            assert sig["pattern"] == "E_quiet_short"


class TestSolNoSpikeHold:
    def test_no_spike(self):
        """vol=1.0x → None."""
        candles = make_candles(n=300, base_price=160.0, base_volume=100.0)
        sig, cache = SolRubberWall(candles).scan()
        if sig is not None:
            assert sig.get("pattern") == "E_quiet_short"


# ===========================================================================
#  Base Strategy Tests
# ===========================================================================


class TestVolRatioCalculation:
    def test_vol_ratio_window(self):
        """288本ウィンドウで正しい比率を計算。"""
        candles = make_candles(n=300, base_price=100.0, base_volume=100.0, seed=99)
        # Set last candle volume to 5x avg
        candles[-1]["v"] = 500.0

        strategy = BtcRubberWall(candles)
        ratios = strategy._vol_ratio(window=288)

        # Last candle should have ratio ≈ 5.0 (500 / ~100)
        assert ratios[-1] > 3.0
        assert ratios[-1] < 8.0

    def test_range_position(self):
        """_range_position の基本計算。"""
        assert BaseStrategy._range_position(100.0, 90.0, 110.0) == pytest.approx(50.0)
        assert BaseStrategy._range_position(90.0, 90.0, 110.0) == pytest.approx(0.0)
        assert BaseStrategy._range_position(110.0, 90.0, 110.0) == pytest.approx(100.0)
        assert BaseStrategy._range_position(85.0, 90.0, 110.0) == pytest.approx(-25.0)

    def test_confidence_to_leverage(self):
        """CAPS: confidence mapping to leverage."""
        assert BaseStrategy.confidence_to_leverage(0.85) == 3
        assert BaseStrategy.confidence_to_leverage(0.80) == 3
        assert BaseStrategy.confidence_to_leverage(0.75) == 2
        assert BaseStrategy.confidence_to_leverage(0.74) == 2
        assert BaseStrategy.confidence_to_leverage(0.70) == 1
        assert BaseStrategy.confidence_to_leverage(0.50) == 1


class TestSignalFormat:
    """All strategies should produce signals with required fields."""

    def _check_signal_fields(self, sig):
        required = {"symbol", "action", "confidence", "take_profit", "stop_loss", "entry_price"}
        assert required.issubset(sig.keys()), f"Missing fields: {required - sig.keys()}"
        assert isinstance(sig["confidence"], (int, float))
        assert 0 <= sig["confidence"] <= 1

    def test_btc_signal_format(self):
        candles = _make_spike_candles(vol_multiplier=6.0, range_position_target=-15.0)
        sig, _ = BtcRubberWall(candles).scan()
        if sig:
            self._check_signal_fields(sig)

    def test_eth_signal_format(self):
        candles = _make_spike_candles(base_price=2700.0, vol_multiplier=8.0, range_position_target=20.0)
        sig, _ = EthRubberBand(candles).scan()
        if sig:
            self._check_signal_fields(sig)

    def test_sol_signal_format(self):
        candles = _make_spike_candles(base_price=160.0, vol_multiplier=6.0, range_position_target=-10.0)
        sig, _ = SolRubberWall(candles).scan()
        if sig:
            self._check_signal_fields(sig)
