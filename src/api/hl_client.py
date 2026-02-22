"""Hyperliquid API wrapper.

SDK の地雷 (equity二重計上、szi符号、leverage多態、STRING値等) を
このクラスに封印し、ビジネスロジック側で直接APIを触らせない。
"""

import os
import time

import requests
from hyperliquid.info import Info

from src.utils.config_loader import get_hyperliquid_url, load_settings
from src.utils.logger import setup_logger
from src.utils.safe_parse import parse_leverage, safe_dict_get, safe_float

logger = setup_logger("hl_client")

# Candle interval → milliseconds
_INTERVAL_MS = {
    "5m": 5 * 60 * 1000,
    "15m": 15 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
}

# Default candle counts per interval
_INTERVAL_DEFAULT_COUNT = {
    "5m": 336,
    "15m": 96,
    "1h": 48,
    "4h": 50,
}


class HLClient:
    """Unified Hyperliquid API client.

    Args:
        settings: Config dict. Falls back to load_settings().
        read_only: If True, only Info client is created (no Exchange).
    """

    def __init__(self, settings=None, read_only=False):
        if settings is None:
            settings = load_settings()
        self._settings = settings
        self._read_only = read_only

        self._base_url = get_hyperliquid_url(settings)

        # Main account address (for portfolio margin queries)
        main_address = os.environ.get("HYPERLIQUID_MAIN_ADDRESS", "").strip()

        # Info client (always created)
        self.info = Info(self._base_url, skip_ws=True)

        # Exchange client (only for trading)
        self.exchange = None
        self.address = None
        if not read_only:
            from eth_account import Account as EthAccount
            from hyperliquid.exchange import Exchange

            private_key = os.environ.get("HYPERLIQUID_PRIVATE_KEY")
            if not private_key:
                from src.utils.crypto import get_hyperliquid_key
                private_key = get_hyperliquid_key()

            account = EthAccount.from_key(private_key)
            self.address = account.address
            self._main_address = main_address if main_address else self.address
            self.exchange = Exchange(
                account, self._base_url, account_address=self._main_address
            )
        else:
            self._main_address = main_address if main_address else ""

        logger.info(
            "HLClient initialized (url=%s, read_only=%s, address=%s)",
            self._base_url, read_only, self._main_address[:10] + "..." if self._main_address else "N/A",
        )

    # ------------------------------------------------------------------ #
    #  Read methods (Info)
    # ------------------------------------------------------------------ #

    def get_equity(self) -> float:
        """Fetch account equity.

        Portfolio Margin: spot_usdc + sum(perps unrealized PnL).
        Standard account (spot=0): perps accountValue.

        地雷: marginSummary.accountValue != 口座残高。
        担保はspot側にあり、perps側は ~$20 程度。
        """
        if not self._main_address:
            return 0.0
        try:
            # Perps side
            state = self.info.user_state(self._main_address)
            if not isinstance(state, dict):
                logger.error("user_state returned non-dict: %s", type(state))
                return 0.0

            margin_summary = safe_dict_get(state, "marginSummary", {})
            perps_equity = safe_float(
                safe_dict_get(margin_summary, "accountValue", 0),
                label="perps_equity",
            )

            # Sum unrealized PnL
            total_upnl = 0.0
            asset_positions = state.get("assetPositions", [])
            if isinstance(asset_positions, list):
                for p in asset_positions:
                    pos = safe_dict_get(p, "position", {})
                    total_upnl += safe_float(
                        safe_dict_get(pos, "unrealizedPnl", 0),
                        label="unrealizedPnl",
                    )

            # Spot side: USDC balance
            spot_usdc = self._fetch_spot_usdc()

            # Portfolio margin: spot_usdc + upnl
            # Standard (spot=0): perps accountValue
            if spot_usdc > 0:
                total = spot_usdc + total_upnl
            elif perps_equity > 0:
                total = perps_equity
            else:
                total = 0.0

            if total > 0:
                logger.debug(
                    "Equity: perps_av=%.2f, spot=%.2f, upnl=%.2f, total=%.2f",
                    perps_equity, spot_usdc, total_upnl, total,
                )
                return total
        except Exception as e:
            logger.warning("Failed to fetch equity: %s", e)
        return 0.0

    def _fetch_spot_usdc(self) -> float:
        """Fetch spot USDC balance via raw HTTP (SDK未検証のため)."""
        try:
            resp = requests.post(
                self._base_url + "/info",
                json={"type": "spotClearinghouseState", "user": self._main_address},
                timeout=5,
            )
            resp.raise_for_status()
            spot_data = resp.json()
            if isinstance(spot_data, dict):
                balances = spot_data.get("balances", [])
                if isinstance(balances, list):
                    for b in balances:
                        if isinstance(b, dict) and b.get("coin") == "USDC":
                            return safe_float(b.get("total", 0), label="spot_usdc")
        except (requests.RequestException, ValueError) as e:
            logger.warning("Spot API failed: %s", e)
        return 0.0

    def get_positions(self) -> list[dict]:
        """Fetch and normalize positions from API.

        地雷: szi は符号付き文字列 (正=long, 負=short)。
        地雷: leverage は dict {"value": N} またはスカラー。
        """
        if not self._main_address:
            return []
        try:
            user_state = self.info.user_state(self._main_address)
            mids = self.info.all_mids()
            if not isinstance(user_state, dict):
                return []
            if not isinstance(mids, dict):
                mids = {}

            positions = []
            asset_positions = user_state.get("assetPositions", [])
            if not isinstance(asset_positions, list):
                return []

            for pos_wrapper in asset_positions:
                if not isinstance(pos_wrapper, dict):
                    continue
                p = pos_wrapper.get("position", {})
                if not isinstance(p, dict):
                    continue
                szi = safe_float(p.get("szi", 0), label="position.szi")
                if szi == 0:
                    continue
                coin = p.get("coin", "")
                positions.append({
                    "symbol": coin,
                    "side": "long" if szi > 0 else "short",
                    "size": abs(szi),
                    "entry_price": safe_float(p.get("entryPx", 0), label=f"entryPx({coin})"),
                    "leverage": parse_leverage(p.get("leverage")),
                    "opened_at": None,
                    "unrealized_pnl": safe_float(p.get("unrealizedPnl", 0), label=f"unrealizedPnl({coin})"),
                    "mid_price": safe_float(mids.get(coin, 0), label=f"mid({coin})"),
                })
            return positions
        except Exception as e:
            logger.error("Failed to get positions: %s", e)
            return []

    def get_mid_prices(self) -> dict[str, float]:
        """Fetch all mid prices, converting STRING values to float.

        地雷: Hyperliquid API は全値をSTRINGで返す。
        """
        raw = self.info.all_mids()
        if not isinstance(raw, dict):
            return {}
        result = {}
        for coin, price_str in raw.items():
            val = safe_float(price_str, default=0.0, label=f"mid_price({coin})")
            if val > 0:
                result[coin] = val
        return result

    def get_candles(self, coin: str, interval: str = "15m", count: int | None = None) -> list[dict]:
        """Fetch candles for a symbol.

        地雷: 時間範囲の計算ミスでデータ欠損。バッファ1本分を追加。
        """
        if count is None:
            count = _INTERVAL_DEFAULT_COUNT.get(interval, 24)
        interval_ms = _INTERVAL_MS.get(interval, 15 * 60 * 1000)
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - count * interval_ms - interval_ms  # buffer 1 bar
        candles = self.info.candles_snapshot(
            name=coin, interval=interval, startTime=start_ms, endTime=now_ms
        )
        return candles[-count:]

    def get_orderbook(self, coin: str, depth: int = 5) -> dict:
        """Fetch L2 orderbook snapshot.

        地雷: levels[0]=bids, levels[1]=asks。構造が壊れていることがある。
        """
        snapshot = self.info.l2_snapshot(name=coin)
        if not isinstance(snapshot, dict):
            logger.warning("l2_snapshot returned non-dict for %s: %s", coin, type(snapshot))
            return {"bids": [], "asks": []}
        levels = snapshot.get("levels", [[], []])
        if not isinstance(levels, list) or len(levels) < 2:
            logger.warning("Unexpected levels structure for %s: %s", coin, type(levels))
            return {"bids": [], "asks": []}

        bids_raw = levels[0][:depth] if isinstance(levels[0], list) else []
        asks_raw = levels[1][:depth] if isinstance(levels[1], list) else []

        bids = [
            {"px": lv["px"], "sz": lv["sz"]}
            for lv in bids_raw
            if isinstance(lv, dict) and "px" in lv and "sz" in lv
        ]
        asks = [
            {"px": lv["px"], "sz": lv["sz"]}
            for lv in asks_raw
            if isinstance(lv, dict) and "px" in lv and "sz" in lv
        ]
        return {"bids": bids, "asks": asks}

    def get_funding_rates(self) -> dict[str, float]:
        """Fetch funding rates for all assets.

        地雷: meta_and_asset_ctxs() は list を返す (tuple ではない)。
        """
        result = self.info.meta_and_asset_ctxs()
        if not isinstance(result, (list, tuple)) or len(result) < 2:
            logger.warning("meta_and_asset_ctxs returned unexpected format: %s", type(result))
            return {}
        meta, asset_ctxs = result[0], result[1]
        universe = meta.get("universe", []) if isinstance(meta, dict) else []
        if not isinstance(asset_ctxs, list):
            logger.warning("asset_ctxs is not a list: %s", type(asset_ctxs))
            return {}
        rates = {}
        for i, asset in enumerate(universe):
            if not isinstance(asset, dict):
                continue
            name = asset.get("name", "")
            if i < len(asset_ctxs) and isinstance(asset_ctxs[i], dict):
                funding = asset_ctxs[i].get("funding", "0")
                rates[name] = safe_float(funding, 0.0, label=f"funding({name})")
        return rates

    def get_user_state(self) -> dict:
        """Raw user state passthrough."""
        if not self._main_address:
            return {}
        state = self.info.user_state(self._main_address)
        return state if isinstance(state, dict) else {}

    def get_open_orders(self) -> list[dict]:
        """Passthrough for open orders."""
        if not self._main_address:
            return []
        orders = self.info.open_orders(self._main_address)
        return orders if isinstance(orders, list) else []

    # ------------------------------------------------------------------ #
    #  Trade methods (Exchange)
    # ------------------------------------------------------------------ #

    def _require_exchange(self):
        """Guard: raise if read_only."""
        if self._read_only or self.exchange is None:
            raise RuntimeError("HLClient is read_only: trading methods are disabled")

    def place_market_order(self, coin: str, side: str, size: float, leverage: int) -> dict:
        """Place a market order with leverage setting.

        地雷: leverage設定→注文の順序を守らないとデフォルト20xで約定する。
        地雷: market_open の slippage パラメータ。

        Returns:
            Standard result dict: {success, status, fill_price, raw_response, error}
        """
        self._require_exchange()
        is_buy = side == "long"

        # Set leverage first (防止: default 20x)
        try:
            lev_resp = self.exchange.update_leverage(leverage, coin)
            if isinstance(lev_resp, dict) and lev_resp.get("status") == "err":
                return {
                    "success": False,
                    "status": "error",
                    "fill_price": 0.0,
                    "raw_response": lev_resp,
                    "error": f"leverage update failed: {lev_resp}",
                }
        except Exception as e:
            return {
                "success": False,
                "status": "error",
                "fill_price": 0.0,
                "raw_response": None,
                "error": f"leverage update exception: {e}",
            }

        # Market order
        resp = self.exchange.market_open(coin, is_buy, size, px=None, slippage=0.01)
        logger.info("Order response for %s: %s", coin, resp)

        fill_price = _extract_fill_price(resp)
        if _is_order_success(resp) and fill_price > 0:
            status = "filled"
        elif _is_order_partial(resp):
            status = "partial"
        else:
            status = "failed"

        return {
            "success": status in ("filled", "partial"),
            "status": status,
            "fill_price": fill_price,
            "raw_response": resp,
            "error": None if status != "failed" else "order not filled",
        }

    def close_position(self, coin: str) -> dict:
        """Close an existing position.

        地雷: market_close() は ポジションなし時に None を返す (SDK bug)。

        Returns:
            Standard result dict.
        """
        self._require_exchange()

        resp = self.exchange.market_close(coin)
        logger.info("Close response for %s: %s", coin, resp)

        if resp is None:
            return {
                "success": True,
                "status": "no_position",
                "fill_price": 0.0,
                "raw_response": None,
                "error": None,
            }

        fill_price = _extract_fill_price(resp)
        if _is_order_success(resp):
            status = "closed"
        else:
            status = "failed"

        return {
            "success": status == "closed",
            "status": status,
            "fill_price": fill_price,
            "raw_response": resp,
            "error": None if status == "closed" else "close order failed",
        }

    def cancel_order(self, coin: str, oid: int) -> dict:
        """Cancel an order.

        地雷: exchange.cancel() は位置引数のみ (kwargs不可)。

        Returns:
            Standard result dict.
        """
        self._require_exchange()

        try:
            resp = self.exchange.cancel(coin, oid)
            success = isinstance(resp, dict) and resp.get("status") == "ok"
            return {
                "success": success,
                "status": "cancelled" if success else "failed",
                "fill_price": 0.0,
                "raw_response": resp,
                "error": None if success else "cancel failed",
            }
        except Exception as e:
            return {
                "success": False,
                "status": "error",
                "fill_price": 0.0,
                "raw_response": None,
                "error": str(e),
            }


# ------------------------------------------------------------------ #
#  Response parsers (module-level, used by HLClient and externally)
# ------------------------------------------------------------------ #

def _is_order_success(resp: dict) -> bool:
    """Check if exchange response indicates a fully filled order."""
    if not isinstance(resp, dict) or resp.get("status") != "ok":
        return False
    response = resp.get("response", {})
    if isinstance(response, dict) and response.get("type") == "order":
        data = response.get("data", {})
        if isinstance(data, dict):
            statuses = data.get("statuses", [])
            if isinstance(statuses, list) and statuses:
                for s in statuses:
                    if not isinstance(s, dict):
                        continue
                    if "error" in s:
                        logger.warning("Order error in statuses: %s", s["error"])
                        return False
                    if "filled" in s:
                        return True
    return False


def _is_order_partial(resp: dict) -> bool:
    """Check if an order is resting (partial fill or unfilled)."""
    if not isinstance(resp, dict) or resp.get("status") != "ok":
        return False
    response = resp.get("response", {})
    if isinstance(response, dict) and response.get("type") == "order":
        data = response.get("data", {})
        if isinstance(data, dict):
            statuses = data.get("statuses", [])
            if isinstance(statuses, list) and statuses:
                return any(isinstance(s, dict) and s.get("resting") for s in statuses)
    return False


def _extract_fill_price(resp: dict) -> float:
    """Extract fill price from exchange response, or 0.0."""
    try:
        if not isinstance(resp, dict):
            return 0.0
        response = resp.get("response", {})
        if isinstance(response, dict) and response.get("type") == "order":
            data = response.get("data", {})
            if isinstance(data, dict):
                for s in data.get("statuses", []):
                    if isinstance(s, dict):
                        filled = s.get("filled")
                        if isinstance(filled, dict):
                            return safe_float(filled.get("avgPx", 0), label="fill_price")
    except (AttributeError, TypeError, ValueError) as e:
        logger.warning("Failed to extract fill price: %s", e)
    return 0.0
