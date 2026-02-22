"""Market data collector for Hyperliquid."""

from datetime import datetime, timezone
from pathlib import Path

from src.api.hl_client import HLClient
from src.utils.config_loader import (
    get_data_dir,
    load_settings,
)
from src.utils.file_lock import atomic_write_json, read_json
from src.utils.logger import setup_logger
from src.utils.retry import RetryExhausted, call_with_retry, enter_safe_hold
from src.utils.safe_parse import safe_float

logger = setup_logger("data_collector")


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

    # HLClient初期化 (最大2回リトライ)
    try:
        client = call_with_retry(
            lambda: HLClient(settings, read_only=True),
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
    # all_mids は raw string dict のまま取得 (後続の safe_float で個別変換)
    try:
        all_mids = call_with_retry(
            lambda: client.info.all_mids(),
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
            client.get_funding_rates,
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
    # フォールバック使用状況を追跡 (サイレントフォールバック防止)
    fallback_events: list[str] = []

    for sym in symbols:
        prev_sym = prev_data.get("symbols", {}).get(sym, {})

        # Mid price (allMids は全値がSTRING)
        mid = all_mids.get(sym)
        if mid is not None:
            mid_price = safe_float(mid, default=0.0, label=f"mid_price({sym})")
            if mid_price <= 0:
                mid_price = None
        elif prev_sym.get("mid_price") is not None:
            mid_price = prev_sym["mid_price"]
            logger.warning("Using previous mid_price for %s", sym)
            fallback_events.append(f"{sym}:mid_price")
        else:
            mid_price = None
            fallback_events.append(f"{sym}:mid_price(None)")

        # Candles (3 timeframes) - リトライ付き
        candles = {}
        for interval in ("15m", "1h", "4h"):
            key = f"candles_{interval}"
            try:
                fetched = call_with_retry(
                    client.get_candles,
                    args=(sym, interval),
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
                fallback_events.append(f"{sym}:{interval}_candles")

        # Orderbook - リトライ付き
        try:
            orderbook = call_with_retry(
                client.get_orderbook,
                args=(sym,),
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
            fallback_events.append(f"{sym}:orderbook")

        # Funding rate
        fr = funding_rates.get(sym)
        if fr is None:
            fr = prev_sym.get("funding_rate")
            if fr is not None:
                logger.warning("Using previous funding_rate for %s", sym)
                fallback_events.append(f"{sym}:funding_rate")

        # 5m足追加取得 (ゴムの壁モデル + 将来のアルト分析用) - リトライ付き
        try:
            fetched_5m = call_with_retry(
                client.get_candles,
                args=(sym, "5m", 336),
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
            fallback_events.append(f"{sym}:5m_candles")

        symbols_data[sym] = {
            "mid_price": mid_price,
            **candles,
            "orderbook": orderbook,
            "funding_rate": fr,
        }

    # Equity 取得 & ポジション同期 & daily_pnl 更新
    equity = client.get_equity()
    sm = None
    positions = []
    # ポジション同期 (Hyperliquid API -> positions.json)
    try:
        from src.state.state_manager import StateManager
        sm = StateManager()
        positions = sm.sync_positions(client)
    except Exception as e:
        logger.warning("Failed to sync positions: %s", e)

    if equity > 0:
        try:
            if sm is None:
                from src.state.state_manager import StateManager
                sm = StateManager()
            if positions:
                # sync 成功時のみ unrealized を更新 (空リストで 0 上書きしない)
                api_unrealized = sum(safe_float(p.get("unrealized_pnl", 0), label="sync_upnl") for p in positions)
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

    # フォールバック多発時アラート (サイレントフォールバック防止)
    # 重要データ (5m_candles) のフォールバックが1件以上発生した場合に通知
    critical_fallbacks = [e for e in fallback_events if "5m_candles" in e or "mid_price" in e]
    if critical_fallbacks:
        logger.warning(
            "Data collection fallbacks detected (%d events): %s",
            len(fallback_events), ", ".join(fallback_events),
        )
        try:
            from src.monitor.telegram_notifier import send_message
            # フォールバック状態ファイルで重複通知を抑制 (30分クールダウン)
            data_dir_path = Path(str(data_dir))
            fb_state_path = data_dir_path.parent / "state" / "collector_fallback_state.json"
            should_notify = True
            try:
                fb_state = read_json(fb_state_path)
                if isinstance(fb_state, dict):
                    last_ts = fb_state.get("last_alert")
                    if last_ts:
                        elapsed = (datetime.now(timezone.utc) -
                                   datetime.fromisoformat(last_ts)).total_seconds()
                        if elapsed < 1800:
                            should_notify = False
            except Exception:
                pass

            if should_notify:
                send_message(
                    f"*WARNING: データ収集フォールバック*\n"
                    f"APIリトライ後も取得失敗 → 前回データ使用\n"
                    f"対象: {', '.join(fallback_events[:5])}"
                )
                atomic_write_json(fb_state_path, {
                    "last_alert": datetime.now(timezone.utc).isoformat(),
                    "fallback_events": fallback_events,
                })
        except Exception as e:
            logger.warning("Failed to send fallback alert: %s", e)
    elif fallback_events:
        # 非重要フォールバック (funding_rate等) はログのみ
        logger.info("Minor fallbacks (non-critical): %s", ", ".join(fallback_events))

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
