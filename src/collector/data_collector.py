"""Market data collector for Hyperliquid."""

import os
import time
from datetime import datetime, timezone
from pathlib import Path

from hyperliquid.info import Info

from src.utils.config_loader import (
    get_data_dir,
    get_hyperliquid_url,
    load_settings,
)
from src.utils.file_lock import atomic_write_json, read_json
from src.utils.logger import setup_logger
from src.utils.retry import RetryExhausted, call_with_retry, enter_safe_hold

logger = setup_logger("data_collector")

# 各時間足のミリ秒
_INTERVAL_MS = {
    "5m":  5 * 60 * 1000,
    "15m": 15 * 60 * 1000,
    "1h":  60 * 60 * 1000,
    "4h":  4 * 60 * 60 * 1000,
}

# デフォルト取得本数
_INTERVAL_DEFAULT_COUNT = {
    "5m":  336,  # 288(24h) + 48(4hバッファ)
    "15m": 96,   # 24時間分
    "1h":  48,   # 2日分
    "4h":  50,   # 8日分 (MACD(12,26,9)計算に35本必要なため余裕を持って50本)
}


def _build_info(settings: dict) -> Info:
    """Create Hyperliquid Info client."""
    base_url = get_hyperliquid_url(settings)
    logger.info("Connecting to Hyperliquid: %s", base_url)
    return Info(base_url, skip_ws=True)


def _fetch_mid_prices(info: Info) -> dict[str, str]:
    """Fetch all mid prices."""
    return info.all_mids()


def _fetch_candles(info: Info, symbol: str, interval: str = "15m", count: int | None = None) -> list[dict]:
    """Fetch latest candles for a symbol at the given interval."""
    if count is None:
        count = _INTERVAL_DEFAULT_COUNT.get(interval, 24)
    interval_ms = _INTERVAL_MS.get(interval, 15 * 60 * 1000)
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - count * interval_ms - interval_ms  # バッファ1本分
    candles = info.candles_snapshot(
        name=symbol, interval=interval, startTime=start_ms, endTime=now_ms
    )
    return candles[-count:]


def _fetch_orderbook(info: Info, symbol: str, depth: int = 5) -> dict:
    """Fetch L2 orderbook snapshot (top N levels)."""
    snapshot = info.l2_snapshot(name=symbol)
    levels = snapshot.get("levels", [[], []])
    bids = levels[0][:depth] if len(levels) > 0 else []
    asks = levels[1][:depth] if len(levels) > 1 else []
    return {
        "bids": [{"px": lv["px"], "sz": lv["sz"]} for lv in bids],
        "asks": [{"px": lv["px"], "sz": lv["sz"]} for lv in asks],
    }


def _fetch_funding_rates(info: Info) -> dict[str, float]:
    """Fetch funding rates for all assets."""
    meta, asset_ctxs = info.meta_and_asset_ctxs()
    universe = meta.get("universe", [])
    rates = {}
    for i, asset in enumerate(universe):
        name = asset.get("name", "")
        if i < len(asset_ctxs):
            funding = asset_ctxs[i].get("funding", "0")
            rates[name] = float(funding)
    return rates



def _fetch_account_equity(info: Info, settings: dict) -> float:
    """Fetch account equity from Hyperliquid.

    統合口座(portfolio margin): spot USDC total + perps unrealized PnL。
    標準口座(spot=0): perps accountValue をそのまま使用。
    trade_executor._get_equity() と同一ロジック。
    """
    import requests as _req
    main_address = os.environ.get("HYPERLIQUID_MAIN_ADDRESS", "").strip()
    if not main_address:
        return 0.0
    try:
        # Perps side — 最も信頼できるequity源
        state = info.user_state(main_address)
        perps_equity = float(state.get("marginSummary", {}).get("accountValue", 0))

        # Spot side: USDC balance
        spot_usdc = 0.0
        base_url = get_hyperliquid_url(settings)
        resp = _req.post(base_url + "/info",
            json={"type": "spotClearinghouseState", "user": main_address}, timeout=5)
        for b in resp.json().get("balances", []):
            if b.get("coin") == "USDC":
                spot_usdc = float(b.get("total", 0))
                break

        # 統合口座(portfolio margin): spot_usdc にマージン担保が含まれ、
        # perps accountValue は担保を含まない(unrealized PnLのみ)。
        # 正しい計算: spot_usdc + sum(perps unrealized PnL)
        # 標準口座(spot=0): perps accountValue をそのまま使用。
        total_upnl = 0.0
        for p in state.get("assetPositions", []):
            total_upnl += float(p.get("position", {}).get("unrealizedPnl", 0))

        if spot_usdc > 0:
            # 統合口座: spot に担保、perps_equity が負 (含み損) でも正しく反映
            total = spot_usdc + total_upnl
        elif perps_equity > 0:
            total = perps_equity  # 標準口座
        else:
            total = 0.0

        if total > 0:
            logger.debug("Equity: perps_av=%.2f, spot=%.2f, total=%.2f",
                         perps_equity, spot_usdc, total)
            return total
    except Exception as e:
        logger.warning("Failed to fetch equity: %s", e)
    return 0.0


def collect(settings: dict | None = None) -> dict:
    """Collect market data for all configured symbols.

    Returns the full market data dict that was written to disk.
    """
    if settings is None:
        settings = load_settings()

    symbols = settings.get("trading", {}).get("symbols", [])
    orderbook_depth = settings.get("brain", {}).get("orderbook_depth", 5)
    data_dir = get_data_dir(settings)
    output_path = data_dir / "market_data.json"

    # Info クライアント初期化 (最大2回リトライ)
    try:
        info = call_with_retry(
            _build_info,
            args=(settings,),
            max_retries=2,
            base_delay=3.0,
            backoff_factor=2.0,
            max_delay=15.0,
            operation_name="Hyperliquid接続",
        )
    except RetryExhausted as e:
        logger.critical("Hyperliquid接続リトライ上限超過: %s", e)
        enter_safe_hold(f"data_collector: Hyperliquid接続失敗 (リトライ上限超過): {e.last_error}")
        raise RuntimeError(f"Hyperliquid接続失敗: {e.last_error}") from e

    # Load previous data as fallback
    prev_data: dict = {}
    try:
        prev_data = read_json(output_path)
    except (FileNotFoundError, Exception):
        pass

    # Fetch shared data (リトライ付き)
    try:
        all_mids = call_with_retry(
            _fetch_mid_prices,
            args=(info,),
            max_retries=2,
            base_delay=2.0,
            backoff_factor=2.0,
            max_delay=10.0,
            operation_name="mid価格取得",
        )
    except RetryExhausted as e:
        logger.error("mid価格取得リトライ上限超過: %s", e)
        all_mids = {}

    try:
        funding_rates = call_with_retry(
            _fetch_funding_rates,
            args=(info,),
            max_retries=2,
            base_delay=2.0,
            backoff_factor=2.0,
            max_delay=10.0,
            operation_name="資金調達率取得",
        )
    except RetryExhausted as e:
        logger.error("資金調達率取得リトライ上限超過: %s", e)
        funding_rates = {}

    # Build per-symbol data
    symbols_data: dict[str, dict] = {}
    for sym in symbols:
        prev_sym = prev_data.get("symbols", {}).get(sym, {})

        # Mid price
        mid = all_mids.get(sym)
        if mid is not None:
            mid_price = float(mid)
        elif prev_sym.get("mid_price") is not None:
            mid_price = prev_sym["mid_price"]
            logger.warning("Using previous mid_price for %s", sym)
        else:
            mid_price = None

        # Candles (3 timeframes) - リトライ付き
        candles = {}
        for interval in ("15m", "1h", "4h"):
            key = f"candles_{interval}"
            try:
                fetched = call_with_retry(
                    _fetch_candles,
                    args=(info, sym, interval),
                    max_retries=2,
                    base_delay=2.0,
                    backoff_factor=2.0,
                    max_delay=10.0,
                    operation_name=f"{sym} {interval}キャンドル取得",
                )
                candles[key] = fetched
                logger.info("Fetched %d %s candles for %s", len(candles[key]), interval, sym)
            except RetryExhausted as e:
                logger.error("Failed to fetch %s candles for %s after retries: %s", interval, sym, e)
                candles[key] = prev_sym.get(key, [])

        # Orderbook - リトライ付き
        try:
            orderbook = call_with_retry(
                _fetch_orderbook,
                args=(info, sym),
                kwargs={"depth": orderbook_depth},
                max_retries=2,
                base_delay=2.0,
                backoff_factor=2.0,
                max_delay=10.0,
                operation_name=f"{sym} オーダーブック取得",
            )
        except RetryExhausted as e:
            logger.error("Failed to fetch orderbook for %s after retries: %s", sym, e)
            orderbook = prev_sym.get("orderbook", {"bids": [], "asks": []})

        # Funding rate
        fr = funding_rates.get(sym)
        if fr is None:
            fr = prev_sym.get("funding_rate")
            if fr is not None:
                logger.warning("Using previous funding_rate for %s", sym)

        # 5m足追加取得 (ゴムの壁モデル + 将来のアルト分析用) - リトライ付き
        try:
            fetched_5m = call_with_retry(
                _fetch_candles,
                args=(info, sym, "5m", 336),
                max_retries=2,
                base_delay=2.0,
                backoff_factor=2.0,
                max_delay=10.0,
                operation_name=f"{sym} 5mキャンドル取得",
            )
            candles["candles_5m"] = fetched_5m
            logger.info("Fetched %d 5m candles for %s", len(candles["candles_5m"]), sym)
        except RetryExhausted as e:
            logger.error("Failed to fetch 5m candles for %s after retries: %s", sym, e)
            candles["candles_5m"] = prev_sym.get("candles_5m", [])

        symbols_data[sym] = {
            "mid_price": mid_price,
            **candles,
            "orderbook": orderbook,
            "funding_rate": fr,
        }

    # Equity 取得 & ポジション同期 & daily_pnl 更新
    equity = _fetch_account_equity(info, settings)
    sm = None
    positions = []
    # ポジション同期 (Hyperliquid API -> positions.json)
    main_address = os.environ.get("HYPERLIQUID_MAIN_ADDRESS", "").strip()
    if main_address:
        try:
            from src.state.state_manager import StateManager
            sm = StateManager()
            positions = sm.sync_positions(info, main_address)
        except Exception as e:
            logger.warning("Failed to sync positions: %s", e)

    if equity > 0:
        try:
            if sm is None:
                from src.state.state_manager import StateManager
                sm = StateManager()
            if positions:
                # sync 成功時のみ unrealized を更新 (空リストで 0 上書きしない)
                api_unrealized = sum(float(p.get("unrealized_pnl", 0) or 0) for p in positions)
                sm.update_daily_pnl(equity, api_unrealized_pnl=api_unrealized)
            else:
                # sync 失敗 or ポジションなし — equity のみ更新
                sm.update_daily_pnl(equity)
            logger.info("Equity updated: $%.2f", equity)
        except Exception as e:
            logger.warning("Failed to update daily_pnl: %s", e)

    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbols": symbols_data,
        "account_equity": equity,
    }

    atomic_write_json(output_path, result)
    logger.info(
        "Market data saved: %d symbols -> %s", len(symbols_data), output_path
    )

    # アーカイブ保存 (バックテスト用履歴蓄積)
    try:
        from src.hypothesis.archiver import archive_market_data, rotate_old
        archive_market_data(settings)
        rotate_old(settings)
    except Exception as e:
        logger.warning("Archive failed (non-critical): %s", e)

    return result


if __name__ == "__main__":
    collect()
