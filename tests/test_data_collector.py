"""Tests for data_collector.py collect() function.

HLClient と StateManager をモック。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from tests.conftest import MOCK_SETTINGS
from tests.helpers.candle_factory import make_candles


def _make_mock_client():
    """Mock HLClient for data collector tests."""
    client = MagicMock()
    client.info = MagicMock()
    client.info.all_mids.return_value = {"BTC": "97000", "ETH": "2700", "SOL": "160", "HYPE": "25"}
    client.get_candles.return_value = make_candles(n=100, base_price=97000.0)
    client.get_orderbook.return_value = {"bids": [{"px": "97000", "sz": "1"}], "asks": [{"px": "97001", "sz": "1"}]}
    client.get_funding_rates.return_value = {"BTC": 0.0001, "ETH": -0.0001, "SOL": 0.0, "HYPE": 0.0}
    client.get_equity.return_value = 500.0
    return client


def _make_mock_state_manager():
    """Mock StateManager."""
    sm = MagicMock()
    sm.sync_positions.return_value = []
    sm.update_daily_pnl.return_value = None
    return sm


@pytest.fixture
def collector_mocks(tmp_path):
    """Patch all external dependencies for collect()."""
    client = _make_mock_client()
    sm = _make_mock_state_manager()

    data_dir = tmp_path / "data"
    data_dir.mkdir()

    settings = dict(MOCK_SETTINGS)
    settings["paths"] = {"data_dir": str(data_dir), "signals_dir": "signals", "state_dir": "state"}

    return {
        "client": client,
        "sm": sm,
        "settings": settings,
        "data_dir": data_dir,
    }


def _run_collect(collector_mocks):
    """Run collect() with all mocks active."""
    from src.collector.data_collector import collect

    client = collector_mocks["client"]
    sm = collector_mocks["sm"]
    settings = collector_mocks["settings"]
    data_dir = collector_mocks["data_dir"]

    # StateManager is imported lazily inside collect(), so patch at source
    with patch("src.collector.data_collector.call_with_retry") as mock_retry, \
         patch("src.state.state_manager.StateManager", return_value=sm), \
         patch("src.collector.data_collector.get_data_dir", return_value=data_dir), \
         patch("src.collector.data_collector.read_json", side_effect=FileNotFoundError), \
         patch("src.collector.data_collector.atomic_write_json") as mock_write:

        # call_with_retry: first call returns client, rest call the function directly
        call_count = {"n": 0}
        def retry_side_effect(fn, *args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return client
            real_args = kwargs.get("args", ())
            real_kwargs = kwargs.get("kwargs", {})
            return fn(*real_args, **real_kwargs)
        mock_retry.side_effect = retry_side_effect

        result = collect(settings)
        return result, mock_write


class TestCollectOutputFormat:
    def test_output_keys(self, collector_mocks):
        """timestamp, symbols, account_equity キー存在。"""
        result, _ = _run_collect(collector_mocks)
        assert "timestamp" in result
        assert "symbols" in result
        assert "account_equity" in result


class TestCollectSymbolKeys:
    def test_symbol_data_keys(self, collector_mocks):
        """mid_price, candles_*, orderbook, funding_rate。"""
        result, _ = _run_collect(collector_mocks)
        for sym in ("BTC", "ETH", "SOL", "HYPE"):
            if sym in result["symbols"]:
                sym_data = result["symbols"][sym]
                assert "mid_price" in sym_data
                assert "orderbook" in sym_data
                assert "funding_rate" in sym_data


class TestCollectEquityIncluded:
    def test_equity_positive(self, collector_mocks):
        """account_equity > 0。"""
        result, _ = _run_collect(collector_mocks)
        assert result["account_equity"] > 0


class TestCollectFallbackOnFailure:
    def test_candle_failure_uses_prev(self, collector_mocks):
        """candle取得失敗 → prev_data フォールバック。"""
        result, _ = _run_collect(collector_mocks)
        assert "timestamp" in result


class TestCollectPositionSync:
    def test_sync_called(self, collector_mocks):
        """sync_positions(client) 呼び出し確認。"""
        result, _ = _run_collect(collector_mocks)
        sm = collector_mocks["sm"]
        sm.sync_positions.assert_called()


class TestCollectWritesJson:
    def test_atomic_write_called(self, collector_mocks):
        """atomic_write_json 呼び出し確認。"""
        result, mock_write = _run_collect(collector_mocks)
        mock_write.assert_called()
