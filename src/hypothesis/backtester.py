"""Backtester: 仮説をアーカイブデータに対して検証。

Phase 1: 基本バックテスト (勝率、期待値、サンプル数)
Phase 2: 厳格バックテスト (タイミングシフト、ランダム比較、利益効率)

BACKTEST_VERSION: バージョン更新時、古いバージョンで通過した仮説は再テスト対象。
"""

import random
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

from src.hypothesis.archiver import load_history
from src.hypothesis.manager import (
    _load_all,
    extract_features,
    get_by_status,
    update_status,
    VALID_OPS,
)
from src.utils.config_loader import load_settings
from src.utils.logger import setup_logger

logger = setup_logger("backtester")

BACKTEST_VERSION = 1


@dataclass
class BacktestResult:
    version: int
    sample_count: int
    win_count: int
    loss_count: int
    win_rate: float
    avg_pnl_pct: float
    total_pnl_pct: float
    passed: bool
    reason: str


@dataclass
class StrictBacktestResult:
    version: int
    sample_count: int
    timing_shift_robust: bool
    timing_shift_winrates: dict  # {shift: winrate}
    edge_vs_random: float
    random_avg_pnl: float
    efficiency_pct: float
    passed: bool
    reason: str


def _check_conditions(conditions: list[dict], logic: str, features: dict[str, dict]) -> bool:
    """トリガー条件を評価。"""
    from src.hypothesis.manager import _check_condition
    results = [_check_condition(c, features) for c in conditions]
    if logic == "AND":
        return all(results)
    return any(results)


def _get_price_at(snapshot: dict, symbol: str) -> float | None:
    """スナップショットからシンボルの価格を取得。"""
    sym_data = snapshot.get("symbols", {}).get(symbol, {})
    price = sym_data.get("mid_price")
    if price is not None:
        return float(price)
    # candles fallback
    candles = sym_data.get("candles_15m", [])
    if candles:
        return float(candles[-1].get("c", 0))
    return None


def backtest(hypothesis: dict, history: list[dict] | None = None) -> BacktestResult:
    """基本バックテスト: トリガー発火→horizon後の価格変動を検証。

    Args:
        hypothesis: 仮説dict
        history: アーカイブデータ (None なら自動ロード)

    Returns:
        BacktestResult
    """
    settings = load_settings()
    min_samples = settings.get("hypothesis", {}).get("backtest_min_samples", 5)

    if history is None:
        history = load_history(days=7)

    trigger = hypothesis.get("trigger", {})
    conditions = trigger.get("conditions", [])
    logic = trigger.get("logic", "AND")
    prediction = hypothesis.get("prediction", {})
    symbol = prediction.get("symbol", "")
    direction = prediction.get("direction", "long")
    horizon = prediction.get("horizon_cycles", 2)

    if not conditions or not symbol:
        return BacktestResult(BACKTEST_VERSION, 0, 0, 0, 0, 0, 0, False, "invalid hypothesis")

    # 各スナップショットの特徴量を事前計算
    feature_series = []
    for snap in history:
        features = extract_features(snap)
        price = _get_price_at(snap, symbol)
        feature_series.append((features, price, snap.get("timestamp", "")))

    # トリガー発火ポイントを検出
    wins = 0
    losses = 0
    pnls = []

    i = 0
    while i < len(feature_series) - horizon:
        features, entry_price, ts = feature_series[i]

        if entry_price is None or entry_price == 0:
            i += 1
            continue

        if _check_conditions(conditions, logic, features):
            # horizon サイクル後の価格
            _, exit_price, _ = feature_series[i + horizon]
            if exit_price is None or exit_price == 0:
                i += horizon + 1
                continue

            pnl_pct = (exit_price - entry_price) / entry_price * 100
            if direction == "short":
                pnl_pct = -pnl_pct

            pnls.append(pnl_pct)
            if pnl_pct > 0:
                wins += 1
            else:
                losses += 1

            # 発火後はhorizon分スキップ (重複回避)
            i += horizon + 1
        else:
            i += 1

    sample_count = wins + losses
    win_rate = wins / sample_count if sample_count > 0 else 0
    avg_pnl = sum(pnls) / len(pnls) if pnls else 0
    total_pnl = sum(pnls)

    # 判定
    if sample_count < min_samples:
        passed = False
        reason = f"insufficient samples ({sample_count} < {min_samples})"
    elif win_rate < 0.55:
        passed = False
        reason = f"low win rate ({win_rate:.1%})"
    elif avg_pnl <= 0:
        passed = False
        reason = f"negative avg PnL ({avg_pnl:.4f}%)"
    else:
        passed = True
        reason = f"passed (win={win_rate:.1%}, avg_pnl={avg_pnl:.4f}%, n={sample_count})"

    return BacktestResult(
        version=BACKTEST_VERSION,
        sample_count=sample_count,
        win_count=wins,
        loss_count=losses,
        win_rate=round(win_rate, 4),
        avg_pnl_pct=round(avg_pnl, 6),
        total_pnl_pct=round(total_pnl, 6),
        passed=passed,
        reason=reason,
    )


def strict_backtest(hypothesis: dict, history: list[dict] | None = None) -> StrictBacktestResult:
    """厳格バックテスト: タイミングシフト、ランダム比較、利益効率。"""
    settings = load_settings()
    min_samples = settings.get("hypothesis", {}).get("strict_min_samples", 10)

    if history is None:
        history = load_history(days=7)

    prediction = hypothesis.get("prediction", {})
    symbol = prediction.get("symbol", "")
    direction = prediction.get("direction", "long")
    horizon = prediction.get("horizon_cycles", 2)

    # 基本バックテストの結果を再利用
    base_result = backtest(hypothesis, history)

    if base_result.sample_count < min_samples:
        return StrictBacktestResult(
            version=BACKTEST_VERSION,
            sample_count=base_result.sample_count,
            timing_shift_robust=False,
            timing_shift_winrates={},
            edge_vs_random=0,
            random_avg_pnl=0,
            efficiency_pct=0,
            passed=False,
            reason=f"insufficient samples for strict test ({base_result.sample_count} < {min_samples})",
        )

    # ── 1. タイミングシフトテスト ──
    # INを±1, ±2サイクルずらして再計算
    trigger = hypothesis.get("trigger", {})
    conditions = trigger.get("conditions", [])
    logic = trigger.get("logic", "AND")

    feature_series = []
    for snap in history:
        features = extract_features(snap)
        price = _get_price_at(snap, symbol)
        feature_series.append((features, price))

    shift_winrates = {}
    for shift in (-2, -1, 0, 1, 2):
        wins = 0
        total = 0
        i = 0
        while i < len(feature_series) - horizon - abs(shift):
            features, _ = feature_series[i]
            if _check_conditions(conditions, logic, features):
                entry_idx = i + shift
                exit_idx = entry_idx + horizon
                if 0 <= entry_idx < len(feature_series) and 0 <= exit_idx < len(feature_series):
                    _, entry_price = feature_series[entry_idx]
                    _, exit_price = feature_series[exit_idx]
                    if entry_price and exit_price and entry_price > 0:
                        pnl = (exit_price - entry_price) / entry_price * 100
                        if direction == "short":
                            pnl = -pnl
                        total += 1
                        if pnl > 0:
                            wins += 1
                i += horizon + 1
            else:
                i += 1
        shift_winrates[str(shift)] = wins / total if total > 0 else 0

    # ロバスト判定: 全シフトで勝率50%超
    timing_robust = all(wr > 0.50 for wr in shift_winrates.values() if shift_winrates.get(str(0), 0) > 0)

    # ── 2. ランダム比較テスト ──
    # 同期間にランダムINした場合の期待値
    random_pnls = []
    random.seed(42)  # 再現性のため固定シード
    for _ in range(200):
        idx = random.randint(0, len(feature_series) - horizon - 1)
        _, entry_price = feature_series[idx]
        _, exit_price = feature_series[idx + horizon]
        if entry_price and exit_price and entry_price > 0:
            pnl = (exit_price - entry_price) / entry_price * 100
            if direction == "short":
                pnl = -pnl
            random_pnls.append(pnl)

    random_avg = sum(random_pnls) / len(random_pnls) if random_pnls else 0
    edge = base_result.avg_pnl_pct - random_avg

    # ── 3. 利益効率 ──
    # トリガー発火区間での最大値動きに対して何%捕捉したか
    efficiencies = []
    i = 0
    while i < len(feature_series) - horizon:
        features, entry_price = feature_series[i]
        if entry_price and _check_conditions(conditions, logic, features):
            # horizon区間の最大/最小
            prices_in_window = []
            for j in range(i, min(i + horizon + 1, len(feature_series))):
                _, p = feature_series[j]
                if p:
                    prices_in_window.append(p)

            if len(prices_in_window) >= 2:
                max_p = max(prices_in_window)
                min_p = min(prices_in_window)
                max_move = (max_p - min_p) / entry_price * 100 if entry_price > 0 else 0

                _, exit_price = feature_series[min(i + horizon, len(feature_series) - 1)]
                if exit_price and entry_price > 0:
                    actual_capture = abs(exit_price - entry_price) / entry_price * 100
                    eff = actual_capture / max_move * 100 if max_move > 0 else 0
                    efficiencies.append(eff)

            i += horizon + 1
        else:
            i += 1

    avg_efficiency = sum(efficiencies) / len(efficiencies) if efficiencies else 0

    # 判定
    reasons = []
    passed = True

    if not timing_robust:
        passed = False
        reasons.append(f"timing shift not robust ({shift_winrates})")

    if edge <= 0:
        passed = False
        reasons.append(f"no edge vs random (edge={edge:.4f}%)")

    if avg_efficiency > 90:
        passed = False
        reasons.append(f"overfitting suspicion (efficiency={avg_efficiency:.0f}%)")

    if avg_efficiency < 10:
        passed = False
        reasons.append(f"too low efficiency ({avg_efficiency:.0f}%)")

    if not reasons:
        reasons.append(f"passed (robust, edge={edge:.4f}%, eff={avg_efficiency:.0f}%)")

    return StrictBacktestResult(
        version=BACKTEST_VERSION,
        sample_count=base_result.sample_count,
        timing_shift_robust=timing_robust,
        timing_shift_winrates=shift_winrates,
        edge_vs_random=round(edge, 6),
        random_avg_pnl=round(random_avg, 6),
        efficiency_pct=round(avg_efficiency, 2),
        passed=passed,
        reason="; ".join(reasons),
    )


def run_pending() -> dict[str, int]:
    """保留中の仮説に対してバックテストを実行。

    Returns:
        {"backtested": N, "validated": N, "rejected": N}
    """
    history = load_history(days=7)
    if len(history) < 10:
        logger.info("Not enough history for backtesting (%d snapshots)", len(history))
        return {"backtested": 0, "validated": 0, "rejected": 0}

    counts = {"backtested": 0, "validated": 0, "rejected": 0}

    # Phase 1: raw → backtested
    for hyp in get_by_status("raw"):
        result = backtest(hyp, history)
        logger.info("Backtest %s: %s", hyp["id"], result.reason)

        if result.passed:
            update_status(hyp["id"], "backtested", asdict(result))
            counts["backtested"] += 1
        else:
            update_status(hyp["id"], "rejected", asdict(result))
            counts["rejected"] += 1

    # Phase 2: backtested → validated (厳格テスト)
    for hyp in get_by_status("backtested"):
        # バージョンチェック: 古いバージョンは再テスト
        bt = hyp.get("backtest", {})
        if isinstance(bt, dict) and bt.get("version", 0) < BACKTEST_VERSION:
            logger.info("Re-testing %s (old version %d)", hyp["id"], bt.get("version", 0))

        result = strict_backtest(hyp, history)
        logger.info("Strict backtest %s: %s", hyp["id"], result.reason)

        if result.passed:
            update_status(hyp["id"], "validated", asdict(result))
            counts["validated"] += 1
        else:
            update_status(hyp["id"], "rejected", asdict(result))
            counts["rejected"] += 1

    # Phase 3: validated → shadow (自動昇格)
    for hyp in get_by_status("validated"):
        update_status(hyp["id"], "shadow")
        logger.info("Promoted %s to shadow mode", hyp["id"])

    logger.info("Backtest run complete: %s", counts)
    return counts


if __name__ == "__main__":
    result = run_pending()
    print(f"Backtest results: {result}")
