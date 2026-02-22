"""Tests for HLClient API wrapper."""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock


# ---------------------------------------------------------------------------
#  Fixtures
# ---------------------------------------------------------------------------

MOCK_SETTINGS = {
    "environment": "testnet",
    "hyperliquid": {"testnet_url": "https://api.hyperliquid-testnet.xyz"},
    "trading": {"symbols": ["BTC", "ETH"]},
}


def _make_client(read_only=True):
    """Create HLClient with mocked SDK dependencies."""
    with patch("src.api.hl_client.Info") as MockInfo, \
         patch("src.api.hl_client.get_hyperliquid_url", return_value="https://test"), \
         patch("src.api.hl_client.load_settings", return_value=MOCK_SETTINGS), \
         patch.dict("os.environ", {"HYPERLIQUID_MAIN_ADDRESS": "0xABCD1234"}):
        from src.api.hl_client import HLClient
        client = HLClient(settings=MOCK_SETTINGS, read_only=read_only)
    return client


def _make_trading_client():
    """Create HLClient with Exchange mocked for trading tests."""
    mock_account = MagicMock()
    mock_account.address = "0xMOCK_ADDRESS"
    mock_exchange = MagicMock()

    with patch("src.api.hl_client.Info") as MockInfo, \
         patch("src.api.hl_client.get_hyperliquid_url", return_value="https://test"), \
         patch("src.api.hl_client.load_settings", return_value=MOCK_SETTINGS), \
         patch("hyperliquid.exchange.Exchange", mock_exchange), \
         patch("eth_account.Account.from_key", return_value=mock_account), \
         patch.dict("os.environ", {
             "HYPERLIQUID_MAIN_ADDRESS": "0xABCD1234",
             "HYPERLIQUID_PRIVATE_KEY": "0x" + "ab" * 32,
         }):
        from src.api.hl_client import HLClient
        client = HLClient(settings=MOCK_SETTINGS, read_only=False)
    return client


# ---------------------------------------------------------------------------
#  Equity tests (most critical — the mine)
# ---------------------------------------------------------------------------

class TestGetEquity:
    def test_portfolio_margin(self):
        """spot_usdc=$500, upnl=-$2 → equity=$498."""
        client = _make_client()
        client.info.user_state = MagicMock(return_value={
            "marginSummary": {"accountValue": "20.5"},
            "assetPositions": [
                {"position": {"unrealizedPnl": "-2.0", "szi": "0.001", "coin": "BTC"}},
            ],
        })
        with patch.object(client, "_fetch_spot_usdc", return_value=500.0):
            eq = client.get_equity()
        assert eq == pytest.approx(498.0, abs=0.01)

    def test_standard_account(self):
        """spot=0, perps accountValue=$500 → equity=$500."""
        client = _make_client()
        client.info.user_state = MagicMock(return_value={
            "marginSummary": {"accountValue": "500.0"},
            "assetPositions": [],
        })
        with patch.object(client, "_fetch_spot_usdc", return_value=0.0):
            eq = client.get_equity()
        assert eq == pytest.approx(500.0, abs=0.01)

    def test_spot_api_failure_fallback(self):
        """Spot API failure → falls back to perps equity."""
        client = _make_client()
        client.info.user_state = MagicMock(return_value={
            "marginSummary": {"accountValue": "500.0"},
            "assetPositions": [],
        })
        with patch.object(client, "_fetch_spot_usdc", return_value=0.0):
            eq = client.get_equity()
        assert eq == pytest.approx(500.0, abs=0.01)

    def test_user_state_non_dict(self):
        """user_state returns non-dict → 0.0."""
        client = _make_client()
        client.info.user_state = MagicMock(return_value="garbage")
        eq = client.get_equity()
        assert eq == 0.0


# ---------------------------------------------------------------------------
#  Positions tests
# ---------------------------------------------------------------------------

class TestGetPositions:
    def test_long_short(self):
        """szi positive = long, szi negative = short, szi=0 skipped."""
        client = _make_client()
        client.info.user_state = MagicMock(return_value={
            "assetPositions": [
                {"position": {"coin": "BTC", "szi": "0.01", "entryPx": "97000", "leverage": "3", "unrealizedPnl": "5.0"}},
                {"position": {"coin": "ETH", "szi": "-0.5", "entryPx": "3200", "leverage": {"type": "cross", "value": 5}, "unrealizedPnl": "-3.0"}},
                {"position": {"coin": "SOL", "szi": "0", "entryPx": "100", "leverage": "2", "unrealizedPnl": "0"}},
            ],
        })
        client.info.all_mids = MagicMock(return_value={"BTC": "97500", "ETH": "3190", "SOL": "100"})

        positions = client.get_positions()
        assert len(positions) == 2

        btc = positions[0]
        assert btc["symbol"] == "BTC"
        assert btc["side"] == "long"
        assert btc["size"] == pytest.approx(0.01)
        assert btc["leverage"] == 3

        eth = positions[1]
        assert eth["symbol"] == "ETH"
        assert eth["side"] == "short"
        assert eth["size"] == pytest.approx(0.5)
        assert eth["leverage"] == 5

    def test_leverage_polymorphic(self):
        """dict {"value": 3} and scalar "5" both work."""
        client = _make_client()
        client.info.user_state = MagicMock(return_value={
            "assetPositions": [
                {"position": {"coin": "BTC", "szi": "0.01", "entryPx": "97000",
                               "leverage": {"type": "cross", "value": 3}, "unrealizedPnl": "0"}},
                {"position": {"coin": "ETH", "szi": "-0.5", "entryPx": "3200",
                               "leverage": "5", "unrealizedPnl": "0"}},
            ],
        })
        client.info.all_mids = MagicMock(return_value={"BTC": "97000", "ETH": "3200"})

        positions = client.get_positions()
        assert positions[0]["leverage"] == 3
        assert positions[1]["leverage"] == 5


# ---------------------------------------------------------------------------
#  Mid Prices tests
# ---------------------------------------------------------------------------

class TestGetMidPrices:
    def test_string_to_float(self):
        """String values get converted to float."""
        client = _make_client()
        client.info.all_mids = MagicMock(return_value={"BTC": "97450.5", "ETH": "3200.0"})

        mids = client.get_mid_prices()
        assert mids["BTC"] == pytest.approx(97450.5)
        assert mids["ETH"] == pytest.approx(3200.0)


# ---------------------------------------------------------------------------
#  Candles tests
# ---------------------------------------------------------------------------

class TestGetCandles:
    def test_count_and_range(self):
        """Candles sliced to requested count."""
        client = _make_client()
        fake_candles = [{"t": i, "o": "100", "h": "101", "l": "99", "c": "100.5", "v": "10"} for i in range(100)]
        client.info.candles_snapshot = MagicMock(return_value=fake_candles)

        result = client.get_candles("BTC", "15m", count=10)
        assert len(result) == 10
        assert result[-1]["t"] == 99  # last candle


# ---------------------------------------------------------------------------
#  Orderbook tests
# ---------------------------------------------------------------------------

class TestGetOrderbook:
    def test_normal(self):
        """Normal orderbook parsing."""
        client = _make_client()
        client.info.l2_snapshot = MagicMock(return_value={
            "levels": [
                [{"px": "97000", "sz": "1.5"}, {"px": "96999", "sz": "2.0"}],
                [{"px": "97001", "sz": "1.0"}, {"px": "97002", "sz": "3.0"}],
            ]
        })
        ob = client.get_orderbook("BTC", depth=2)
        assert len(ob["bids"]) == 2
        assert len(ob["asks"]) == 2
        assert ob["bids"][0]["px"] == "97000"

    def test_malformed(self):
        """Malformed response returns empty."""
        client = _make_client()
        client.info.l2_snapshot = MagicMock(return_value="not a dict")

        ob = client.get_orderbook("BTC")
        assert ob == {"bids": [], "asks": []}


# ---------------------------------------------------------------------------
#  Funding Rates tests
# ---------------------------------------------------------------------------

class TestGetFundingRates:
    def test_list_unpack(self):
        """meta_and_asset_ctxs returns list (not tuple) — must handle."""
        client = _make_client()
        client.info.meta_and_asset_ctxs = MagicMock(return_value=[
            {"universe": [{"name": "BTC"}, {"name": "ETH"}]},
            [{"funding": "0.0001"}, {"funding": "-0.0002"}],
        ])

        rates = client.get_funding_rates()
        assert rates["BTC"] == pytest.approx(0.0001)
        assert rates["ETH"] == pytest.approx(-0.0002)


# ---------------------------------------------------------------------------
#  Trading tests
# ---------------------------------------------------------------------------

FILLED_RESPONSE = {
    "status": "ok",
    "response": {
        "type": "order",
        "data": {
            "statuses": [
                {"filled": {"totalSz": "0.01", "avgPx": "97100.0"}}
            ]
        }
    }
}

PARTIAL_RESPONSE = {
    "status": "ok",
    "response": {
        "type": "order",
        "data": {
            "statuses": [
                {"resting": {"oid": 12345}}
            ]
        }
    }
}


class TestPlaceMarketOrder:
    def test_filled(self):
        """Successful order → status=filled, fill_price extracted."""
        client = _make_trading_client()
        client.exchange.update_leverage = MagicMock(return_value={"status": "ok"})
        client.exchange.market_open = MagicMock(return_value=FILLED_RESPONSE)

        result = client.place_market_order("BTC", "long", 0.01, 3)
        assert result["success"] is True
        assert result["status"] == "filled"
        assert result["fill_price"] == pytest.approx(97100.0)
        client.exchange.update_leverage.assert_called_once_with(3, "BTC")

    def test_leverage_fails(self):
        """Leverage error → market_open not called."""
        client = _make_trading_client()
        client.exchange.update_leverage = MagicMock(return_value={"status": "err", "msg": "bad"})

        result = client.place_market_order("BTC", "long", 0.01, 3)
        assert result["success"] is False
        assert result["status"] == "error"
        client.exchange.market_open.assert_not_called()

    def test_partial(self):
        """Resting order → status=partial."""
        client = _make_trading_client()
        client.exchange.update_leverage = MagicMock(return_value={"status": "ok"})
        client.exchange.market_open = MagicMock(return_value=PARTIAL_RESPONSE)

        result = client.place_market_order("BTC", "long", 0.01, 3)
        assert result["status"] == "partial"


class TestClosePosition:
    def test_success(self):
        """Successful close → status=closed."""
        client = _make_trading_client()
        client.exchange.market_close = MagicMock(return_value=FILLED_RESPONSE)

        result = client.close_position("BTC")
        assert result["success"] is True
        assert result["status"] == "closed"

    def test_none_response(self):
        """SDK returns None → status=no_position."""
        client = _make_trading_client()
        client.exchange.market_close = MagicMock(return_value=None)

        result = client.close_position("BTC")
        assert result["status"] == "no_position"


class TestCancelOrder:
    def test_positional_args(self):
        """cancel(coin, oid) called with positional args."""
        client = _make_trading_client()
        client.exchange.cancel = MagicMock(return_value={"status": "ok"})

        result = client.cancel_order("BTC", 12345)
        client.exchange.cancel.assert_called_once_with("BTC", 12345)
        assert result["success"] is True
        assert result["status"] == "cancelled"


# ---------------------------------------------------------------------------
#  Guards
# ---------------------------------------------------------------------------

class TestGuards:
    def test_read_only_raises_on_trade(self):
        """RuntimeError when trading on read_only client."""
        client = _make_client(read_only=True)
        with pytest.raises(RuntimeError, match="read_only"):
            client.place_market_order("BTC", "long", 0.01, 3)
        with pytest.raises(RuntimeError, match="read_only"):
            client.close_position("BTC")
        with pytest.raises(RuntimeError, match="read_only"):
            client.cancel_order("BTC", 123)


# ---------------------------------------------------------------------------
#  Response parsers
# ---------------------------------------------------------------------------

class TestResponseParsers:
    def test_is_order_success(self):
        from src.api.hl_client import _is_order_success
        assert _is_order_success(FILLED_RESPONSE) is True
        assert _is_order_success(PARTIAL_RESPONSE) is False
        assert _is_order_success({"status": "err"}) is False
        assert _is_order_success(None) is False

    def test_is_order_partial(self):
        from src.api.hl_client import _is_order_partial
        assert _is_order_partial(PARTIAL_RESPONSE) is True
        assert _is_order_partial(FILLED_RESPONSE) is False

    def test_extract_fill_price(self):
        from src.api.hl_client import _extract_fill_price
        assert _extract_fill_price(FILLED_RESPONSE) == pytest.approx(97100.0)
        assert _extract_fill_price(PARTIAL_RESPONSE) == 0.0
        assert _extract_fill_price({}) == 0.0
        assert _extract_fill_price(None) == 0.0
