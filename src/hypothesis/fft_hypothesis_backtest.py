"""FFT仮説の探索的バックテスト runner.

本番ロジックには未接続。先に検証するための研究用ツール。
"""

import json
from dataclasses import asdict

from src.hypothesis.archiver import load_history
from src.hypothesis.backtester import backtest, strict_backtest
from src.utils.logger import setup_logger

logger = setup_logger("fft_backtest")


def _build_hypothesis(
    symbol: str,
    direction: str,
    horizon: int,
    harmonic_threshold: float,
    entropy_threshold: float,
    window_prefix: str = "fft64",
) -> dict:
    return {
        "id": f"fft_{symbol}_{direction}_{window_prefix}_h{horizon}_r{harmonic_threshold}_e{entropy_threshold}",
        "description": (
            f"{symbol} {direction} when {window_prefix} harmonic/entropy indicates synchronized regime"
        ),
        "trigger": {
            "logic": "AND",
            "conditions": [
                {
                    "symbol": symbol,
                    "field": f"{window_prefix}_harmonic_ratio",
                    "op": ">=",
                    "value": harmonic_threshold,
                },
                {
                    "symbol": symbol,
                    "field": f"{window_prefix}_spectral_entropy",
                    "op": "<=",
                    "value": entropy_threshold,
                },
            ],
        },
        "prediction": {
            "symbol": symbol,
            "direction": direction,
            "horizon_cycles": horizon,
        },
    }


def run_fft_grid(days: int = 7) -> list[dict]:
    history = load_history(days=days)
    if len(history) < 20:
        logger.warning("Not enough history: %d snapshots", len(history))
        return []

    symbols = ("BTC", "ETH", "SOL")
    directions = ("long", "short")
    horizons = (2, 3, 4)
    harmonics = (0.30, 0.35, 0.40)
    entropies = (0.70, 0.80)
    windows = ("fft64", "fft96")

    results: list[dict] = []
    for symbol in symbols:
        for direction in directions:
            for horizon in horizons:
                for hr in harmonics:
                    for se in entropies:
                        for wp in windows:
                            hyp = _build_hypothesis(symbol, direction, horizon, hr, se, window_prefix=wp)
                            base = backtest(hyp, history)
                            strict = strict_backtest(hyp, history)
                            score = (base.avg_pnl_pct * 0.5) + (strict.edge_vs_random * 0.5)
                            results.append(
                                {
                                    "id": hyp["id"],
                                    "symbol": symbol,
                                    "direction": direction,
                                    "window": wp,
                                    "horizon": horizon,
                                    "harmonic_threshold": hr,
                                    "entropy_threshold": se,
                                    "score": round(score, 6),
                                    "base": asdict(base),
                                    "strict": asdict(strict),
                                }
                            )

    results.sort(
        key=lambda x: (
            int(x["strict"]["passed"]),
            int(x["base"]["passed"]),
            x["score"],
            x["base"]["sample_count"],
        ),
        reverse=True,
    )
    return results


def main() -> None:
    results = run_fft_grid(days=7)
    top = results[:10]
    payload = {
        "tested": len(results),
        "top": top,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
