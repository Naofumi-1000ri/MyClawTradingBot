"""簡易バックテストランナー (pretest-strategy 用)。

戦略の scan() をヒストリカルキャンドルの全足に適用し、
TP/SL 判定で PF・勝率を算出する。
"""

from __future__ import annotations


def run_backtest(
    strategy_class,
    candles: list[dict],
    config: dict | None = None,
    window: int = 300,
) -> dict:
    """戦略をキャンドル列に適用してバックテスト。

    Args:
        strategy_class: BaseStrategy のサブクラス
        candles: 全キャンドル列
        config: 戦略設定
        window: scan に渡すウィンドウサイズ

    Returns:
        {"trades": [...], "pf": float, "win_rate": float, "total": int}
    """
    trades = []
    i = window
    while i < len(candles) - 1:
        chunk = candles[max(0, i - window):i + 2]  # +2 for scan_idx = len-2
        strategy = strategy_class(chunk, config)
        result = strategy.scan(cache=None)

        # scan() returns tuple (signal, cache) or just signal
        if isinstance(result, tuple):
            signal = result[0]
        else:
            signal = result

        if signal is None:
            i += 1
            continue

        entry = signal.get("entry_price", 0)
        tp = signal.get("take_profit", 0)
        sl = signal.get("stop_loss", 0)
        direction = signal.get("action") or signal.get("direction")
        exit_bars = signal.get("exit_bars", 50)

        if not entry or not direction or direction in ("hold", "hold_position"):
            i += 1
            continue

        # Simulate forward
        outcome = _simulate_forward(candles, i, entry, tp, sl, direction, exit_bars)
        trades.append({
            "bar": i,
            "direction": direction,
            "entry": entry,
            "tp": tp,
            "sl": sl,
            **outcome,
        })
        i += max(1, outcome.get("bars_held", 1))

    return _summarize(trades)


def _simulate_forward(
    candles: list[dict],
    entry_bar: int,
    entry: float,
    tp: float,
    sl: float,
    direction: str,
    max_bars: int,
) -> dict:
    """エントリー後のTP/SL/タイムアウト判定。"""
    for j in range(1, min(max_bars + 1, len(candles) - entry_bar)):
        c = candles[entry_bar + j]
        mid = (c["h"] + c["l"]) / 2

        if direction == "long":
            if sl > 0 and c["l"] <= sl:
                return {"result": "sl", "exit_price": sl, "bars_held": j}
            if tp > 0 and c["h"] >= tp:
                return {"result": "tp", "exit_price": tp, "bars_held": j}
        elif direction == "short":
            if sl > 0 and c["h"] >= sl:
                return {"result": "sl", "exit_price": sl, "bars_held": j}
            if tp > 0 and c["l"] <= tp:
                return {"result": "tp", "exit_price": tp, "bars_held": j}

    # Timeout
    last_bar = min(entry_bar + max_bars, len(candles) - 1)
    exit_price = candles[last_bar]["c"]
    return {"result": "timeout", "exit_price": exit_price, "bars_held": max_bars}


def _summarize(trades: list[dict]) -> dict:
    """トレード結果をサマリ。"""
    if not trades:
        return {"trades": [], "pf": 0.0, "win_rate": 0.0, "total": 0}

    wins = 0
    gross_profit = 0.0
    gross_loss = 0.0

    for t in trades:
        entry = t["entry"]
        exit_p = t["exit_price"]
        if t["direction"] == "long":
            pnl = exit_p - entry
        else:
            pnl = entry - exit_p

        t["pnl"] = pnl
        if pnl > 0:
            wins += 1
            gross_profit += pnl
        else:
            gross_loss += abs(pnl)

    total = len(trades)
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    win_rate = wins / total if total > 0 else 0.0

    return {
        "trades": trades,
        "pf": round(pf, 3),
        "win_rate": round(win_rate, 4),
        "total": total,
    }
