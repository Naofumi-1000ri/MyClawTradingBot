"""Shared fixtures and pytest configuration for myClaw tests."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.helpers.candle_factory import make_candles, make_uptrend_candles


# ---------------------------------------------------------------------------
#  pytest custom options
# ---------------------------------------------------------------------------

def pytest_addoption(parser):
    parser.addoption(
        "--strategy-module",
        action="store",
        default=None,
        help="Strategy module path for precheck tests (e.g. src.strategy.btc_rubber_wall)",
    )


@pytest.fixture
def strategy_module(request):
    """Load strategy class from --strategy-module option."""
    mod_path = request.config.getoption("--strategy-module")
    if mod_path is None:
        pytest.skip("--strategy-module not specified")
    mod = importlib.import_module(mod_path)
    # Find the strategy class (first subclass of BaseStrategy)
    from src.strategy.base import BaseStrategy
    for attr_name in dir(mod):
        attr = getattr(mod, attr_name)
        if (
            isinstance(attr, type)
            and issubclass(attr, BaseStrategy)
            and attr is not BaseStrategy
        ):
            return attr
    pytest.fail(f"No BaseStrategy subclass found in {mod_path}")


# ---------------------------------------------------------------------------
#  Candle fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def stable_candles():
    """300本の安定したキャンドル列 (trend=0, vol≈100)。"""
    return make_candles(n=300, base_price=97000.0, base_volume=100.0)


@pytest.fixture
def uptrend_candles():
    """300本の上昇トレンドキャンドル列 (EMA9>EMA21)。"""
    return make_uptrend_candles(n=300, base_price=97000.0, base_volume=100.0)


# ---------------------------------------------------------------------------
#  Settings / config fixtures
# ---------------------------------------------------------------------------

MOCK_SETTINGS = {
    "environment": "testnet",
    "hyperliquid": {"testnet_url": "https://api.hyperliquid-testnet.xyz"},
    "trading": {
        "symbols": ["BTC", "ETH", "SOL", "HYPE"],
        "default_leverage": 3,
        "min_confidence": 0.7,
        "decision_gate": {
            "entry_cooldown_minutes": 10,
            "max_equity_drift_pct": 20.0,
            "max_daily_loss_for_new_entries_pct": 2.0,
            "min_rr": 1.2,
            "min_data_quality_score": 80,
            "max_spread_bps": 8.0,
            "min_orderbook_imbalance": 1.1,
        },
    },
    "strategy": {
        "rubber_wall": {"vol_threshold": 5.0},
        "rubber_band": {"reversal_threshold": 7.0, "momentum_threshold": 3.0},
        "sol_rubber_wall": {"vol_threshold": 5.0},
        "wave_rider": {"enabled": False},
        "wave_rider_hype": {"enabled": False},
    },
    "brain": {"orderbook_depth": 5},
    "paths": {"data_dir": "data", "signals_dir": "signals", "state_dir": "state"},
    "cycle": {"interval_minutes": 5},
}

MOCK_RISK_PARAMS = {
    "position": {
        "max_single_pct": 10.0,
        "max_total_exposure_pct": 30.0,
        "max_concurrent": 3,
        "max_size_by_symbol": {"BTC": 0.01, "ETH": 0.5, "SOL": 10.0},
        "max_notional_usd_per_trade": 100,
        "max_notional_pct_of_equity": 20,
    },
    "loss_limits": {"daily_loss_pct": 3.0, "max_drawdown_pct": 10.0},
    "kill_switch": {"cooldown_hours": 1, "auto_close_positions": True},
    "orders": {"max_leverage": 10, "min_order_size_usd": 10.0, "max_slippage_pct": 0.5},
}


@pytest.fixture
def mock_settings():
    return dict(MOCK_SETTINGS)


@pytest.fixture
def mock_risk_params():
    return dict(MOCK_RISK_PARAMS)


# ---------------------------------------------------------------------------
#  Temp directory fixtures (for file I/O isolation)
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_dirs(tmp_path):
    """Create isolated data/signals/state directories and patch config_loader."""
    data_dir = tmp_path / "data"
    signals_dir = tmp_path / "signals"
    state_dir = tmp_path / "state"
    for d in (data_dir, signals_dir, state_dir):
        d.mkdir()

    return {
        "root": tmp_path,
        "data": data_dir,
        "signals": signals_dir,
        "state": state_dir,
    }


# ---------------------------------------------------------------------------
#  HLClient mock fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_hl_client():
    """Pre-configured HLClient mock."""
    client = MagicMock()
    client.get_equity.return_value = 500.0
    client.get_positions.return_value = []
    client.get_mid_prices.return_value = {"BTC": 97000.0, "ETH": 2700.0, "SOL": 160.0, "HYPE": 25.0}
    client.get_candles.return_value = make_candles(n=336, base_price=97000.0)
    client.get_orderbook.return_value = {"bids": [{"px": "97000", "sz": "1.0"}], "asks": [{"px": "97001", "sz": "1.0"}]}
    client.get_funding_rates.return_value = {"BTC": 0.0001, "ETH": -0.0001, "SOL": 0.0, "HYPE": 0.0}
    client.info = MagicMock()
    client.info.all_mids.return_value = {"BTC": "97000", "ETH": "2700", "SOL": "160", "HYPE": "25"}
    client.place_market_order.return_value = {
        "success": True, "status": "filled", "fill_price": 97000.0, "raw_response": {}, "error": None,
    }
    client.close_position.return_value = {
        "success": True, "status": "closed", "fill_price": 97100.0, "raw_response": {},
    }
    return client
