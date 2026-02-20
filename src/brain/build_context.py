"""Build context JSON for AI Brain from market data and state files."""

from datetime import datetime, timezone
from pathlib import Path

from src.utils.config_loader import get_project_root, load_settings
from src.utils.file_lock import atomic_write_json, read_json


# 時間足ごとにJSONコンテキストに含める本数
# チャート画像で視覚情報は補完されるので数値は絞る
_CANDLE_LIMITS = {
    "candles_5m":  336,  # RubberWall用: 全量 (288+48)
    "candles_15m": 24,   # 6時間分 (直近の値動き詳細)
    "candles_1h":  24,   # 24時間分 (日足レベルの構造)
    "candles_4h":  50,   # 200時間分 (MACD(12,26,9)計算用に50本確保)
}


def _truncate_candles(candles: list, max_candles: int) -> list:
    if not candles:
        return []
    return candles[-max_candles:]


def _truncate_orderbook(orderbook: dict, depth: int) -> dict:
    if not orderbook:
        return {}
    result = {}
    if "bids" in orderbook:
        result["bids"] = orderbook["bids"][:depth]
    if "asks" in orderbook:
        result["asks"] = orderbook["asks"][:depth]
    return result


def _load_optional_json(filepath: Path):
    if filepath.exists():
        try:
            return read_json(filepath)
        except Exception:
            return None
    return None


def build_context() -> dict:
    """Build the context JSON for the AI Brain."""
    root = get_project_root()
    settings = load_settings()
    brain_settings = settings.get("brain", {})
    orderbook_depth = brain_settings.get("orderbook_depth", 5)

    # Load market data (required)
    market_data = read_json(root / "data" / "market_data.json")

    # Compress market data per symbol
    compressed_markets = {}
    for symbol, data in market_data.get("symbols", {}).items():
        compressed = {}
        for key, value in data.items():
            if key in _CANDLE_LIMITS:
                compressed[key] = _truncate_candles(value, _CANDLE_LIMITS[key])
            elif key == "orderbook":
                compressed[key] = _truncate_orderbook(value, orderbook_depth)
            else:
                compressed[key] = value
        compressed_markets[symbol] = compressed

    # State files
    state_dir = root / "state"
    positions    = _load_optional_json(state_dir / "positions.json")
    trade_history = _load_optional_json(state_dir / "trade_history.json")
    daily_pnl    = _load_optional_json(state_dir / "daily_pnl.json")

    context = {
        "timestamp": market_data.get("timestamp", datetime.now(timezone.utc).isoformat()),
        "environment": settings.get("environment", "testnet"),
        "trading_config": settings.get("trading", {}),
        "market_data": compressed_markets,
    }

    if positions is not None:
        context["positions"] = positions
    if trade_history is not None:
        # 直近50件のみ
        h = trade_history if isinstance(trade_history, list) else []
        context["trade_history"] = h[-50:]
    if daily_pnl is not None:
        context["daily_pnl"] = daily_pnl

    # Reviewer feedback (Alphaが次サイクルで読む)
    review = _load_optional_json(state_dir / "review.json")
    if review is not None:
        context["reviewer_feedback"] = {
            "feedback": review.get("feedback_to_alpha", ""),
            "performance_score": review.get("performance_score"),
            "risk_alerts": review.get("risk_alerts", []),
            "reviewed_at": review.get("reviewed_at", ""),
        }

    # Hypothesis Lab: 発火中の仮説をコンテキスト注入
    try:
        from src.hypothesis.manager import check_triggers
        raw_market = read_json(root / "data" / "market_data.json")
        triggered = check_triggers(raw_market)
        if triggered:
            hyp_config = settings.get("hypothesis", {})
            bonus = hyp_config.get("proven_confidence_bonus", 0.05)
            context["hypothesis_alerts"] = [
                {
                    "id": h["id"],
                    "description": h["description"],
                    "status": h["status"],
                    "direction": h["prediction"]["direction"],
                    "symbol": h["prediction"]["symbol"],
                    "confidence_bonus": bonus if h["status"] == "proven" else 0.0,
                }
                for h in triggered
            ]
    except Exception:
        pass  # 仮説システム未初期化でも動作に影響なし

    return context


def main() -> None:
    root = get_project_root()
    output_path = root / "data" / "context.json"
    context = build_context()
    atomic_write_json(output_path, context)
    print(f"Context built: {output_path}")


if __name__ == "__main__":
    main()
