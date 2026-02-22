"""テスト用キャンドルデータ生成ファクトリ。

戦略テストで使うOHLCV足を簡単に作成する。
"""

from __future__ import annotations

import random

_BASE_T = 1_700_000_000_000  # epoch ms baseline


def make_candle(
    close: float = 100.0,
    open_: float | None = None,
    high: float | None = None,
    low: float | None = None,
    volume: float = 100.0,
    t: int | None = None,
    idx: int = 0,
) -> dict:
    """単一キャンドルを生成。"""
    o = open_ if open_ is not None else close
    h = high if high is not None else max(o, close) * 1.001
    l = low if low is not None else min(o, close) * 0.999
    return {
        "t": t if t is not None else _BASE_T + idx * 300_000,
        "o": o,
        "c": close,
        "h": h,
        "l": l,
        "v": volume,
    }


def make_candles(
    n: int = 300,
    base_price: float = 100.0,
    base_volume: float = 100.0,
    trend: float = 0.0,
    volatility: float = 0.002,
    seed: int | None = 42,
) -> list[dict]:
    """N本の安定したキャンドル列を生成。

    Args:
        n: 本数
        base_price: 開始価格
        base_volume: 平均出来高
        trend: 1足あたりの価格変化率 (0.001 = +0.1%/bar)
        volatility: 価格のランダム変動幅
        seed: 乱数シード (再現性)
    """
    if seed is not None:
        random.seed(seed)

    candles = []
    price = base_price
    for i in range(n):
        change = price * (trend + random.uniform(-volatility, volatility))
        close = price + change
        high = max(price, close) * (1 + random.uniform(0, volatility))
        low = min(price, close) * (1 - random.uniform(0, volatility))
        vol = base_volume * random.uniform(0.5, 1.5)
        candles.append({
            "t": _BASE_T + i * 300_000,
            "o": round(price, 6),
            "c": round(close, 6),
            "h": round(high, 6),
            "l": round(low, 6),
            "v": round(vol, 4),
        })
        price = close
    return candles


def inject_spike(
    candles: list[dict],
    idx: int,
    vol_multiplier: float = 8.0,
    bear: bool = True,
    price_change_pct: float = -0.005,
) -> list[dict]:
    """指定インデックスにスパイク足を注入。

    Args:
        candles: 元のキャンドル列 (変更される)
        idx: スパイク足のインデックス
        vol_multiplier: 平均出来高に対する倍率
        bear: True=BEAR candle (close < open)
        price_change_pct: open からの価格変化率
    """
    # 周辺の平均出来高を計算
    start = max(0, idx - 288)
    avg_vol = sum(c["v"] for c in candles[start:idx]) / max(1, idx - start)

    c = candles[idx]
    open_price = c["o"]
    close_price = open_price * (1 + price_change_pct)
    if bear and close_price >= open_price:
        close_price = open_price * 0.995
    elif not bear and close_price <= open_price:
        close_price = open_price * 1.005

    candles[idx] = {
        "t": c["t"],
        "o": round(open_price, 6),
        "c": round(close_price, 6),
        "h": round(max(open_price, close_price) * 1.001, 6),
        "l": round(min(open_price, close_price) * 0.999, 6),
        "v": round(avg_vol * vol_multiplier, 4),
    }
    return candles


def make_uptrend_candles(
    n: int = 300,
    base_price: float = 100.0,
    base_volume: float = 100.0,
    seed: int | None = 42,
) -> list[dict]:
    """上昇トレンド (EMA9>EMA21) のキャンドル列を生成。"""
    return make_candles(
        n=n,
        base_price=base_price,
        base_volume=base_volume,
        trend=0.0005,
        volatility=0.001,
        seed=seed,
    )


def make_low_vol_candles(
    candles: list[dict],
    start_idx: int,
    end_idx: int,
    vol_ratio: float = 0.3,
) -> list[dict]:
    """指定範囲のキャンドルを低出来高に変更。"""
    avg_start = max(0, start_idx - 100)
    avg_vol = sum(c["v"] for c in candles[avg_start:start_idx]) / max(1, start_idx - avg_start)
    for i in range(start_idx, min(end_idx, len(candles))):
        candles[i]["v"] = round(avg_vol * vol_ratio, 4)
    return candles
