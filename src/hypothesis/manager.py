"""Hypothesis Manager: 仮説のCRUD、ライフサイクル管理、トリガーチェック。

仮説ステータス遷移:
  raw → backtested → validated → shadow → proven → (demoted)
  各段階で rejected に落ちる可能性がある。
"""

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.utils.config_loader import get_state_dir, load_settings
from src.utils.file_lock import atomic_write_json, read_json
from src.utils.logger import setup_logger

logger = setup_logger("hypothesis")

VALID_STATUSES = ("raw", "backtested", "validated", "shadow", "proven", "demoted", "rejected")
VALID_OPS = (">", "<", ">=", "<=", "==", "!=")


def _hyp_path() -> Path:
    return get_state_dir() / "hypotheses.json"


def _load_all() -> list[dict]:
    path = _hyp_path()
    if not path.exists():
        return []
    try:
        data = read_json(path)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_all(hypotheses: list[dict]) -> None:
    atomic_write_json(_hyp_path(), hypotheses)


def _gen_id() -> str:
    now = datetime.now(timezone.utc)
    existing = _load_all()
    today_prefix = f"hyp_{now.strftime('%Y%m%d')}"
    today_count = sum(1 for h in existing if h.get("id", "").startswith(today_prefix))
    return f"{today_prefix}_{today_count + 1:03d}"


# ─── CRUD ─────────────────────────────────────────────────

def create_hypothesis(
    description: str,
    trigger: dict,
    prediction: dict,
    source: str = "",
) -> dict:
    """新規仮説を作成して保存。"""
    settings = load_settings()
    max_hyp = settings.get("hypothesis", {}).get("max_hypotheses", 50)

    hypotheses = _load_all()

    # 上限チェック: active な仮説のみカウント
    active = [h for h in hypotheses if h["status"] not in ("rejected", "demoted")]
    if len(active) >= max_hyp:
        logger.warning("Hypothesis limit reached (%d/%d). Rejecting.", len(active), max_hyp)
        return {}

    hyp = {
        "id": _gen_id(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "raw",
        "source": source,
        "description": description,
        "trigger": trigger,
        "prediction": prediction,
        "backtest": None,
        "strict_backtest": None,
        "shadow": {"activations": 0, "wins": 0, "losses": 0, "total_pnl": 0.0, "results": []},
        "live": {"activations": 0, "wins": 0, "losses": 0, "total_pnl": 0.0},
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    hypotheses.append(hyp)
    _save_all(hypotheses)
    logger.info("Created hypothesis: %s - %s", hyp["id"], description[:80])
    return hyp


def update_status(hyp_id: str, new_status: str, results: dict | None = None) -> bool:
    """仮説のステータスを更新。"""
    if new_status not in VALID_STATUSES:
        logger.error("Invalid status: %s", new_status)
        return False

    hypotheses = _load_all()
    for h in hypotheses:
        if h["id"] == hyp_id:
            old_status = h["status"]
            h["status"] = new_status
            h["updated_at"] = datetime.now(timezone.utc).isoformat()
            if results:
                if new_status == "backtested":
                    h["backtest"] = results
                elif new_status == "validated":
                    h["strict_backtest"] = results
                elif new_status in ("shadow", "proven", "demoted"):
                    if "shadow" in results:
                        h["shadow"] = results["shadow"]
                    if "live" in results:
                        h["live"] = results["live"]
            _save_all(hypotheses)
            logger.info("Hypothesis %s: %s → %s", hyp_id, old_status, new_status)
            return True

    logger.warning("Hypothesis not found: %s", hyp_id)
    return False


def get_by_status(*statuses: str) -> list[dict]:
    """指定ステータスの仮説を取得。"""
    return [h for h in _load_all() if h.get("status") in statuses]


def get_proven() -> list[dict]:
    """proven 仮説を取得 (コンテキスト注入用)。"""
    return get_by_status("proven")


def get_active_shadows() -> list[dict]:
    """shadow 中の仮説を取得。"""
    return get_by_status("shadow")


# ─── トリガーチェック ──────────────────────────────────────

def extract_features(market_data: dict) -> dict[str, dict]:
    """market_dataから仮説トリガー用の特徴量を抽出。

    Returns:
        {symbol: {field: value}} のネストdict
    """
    features = {}

    for symbol, data in market_data.get("symbols", {}).items():
        f = {}

        # 価格系
        f["price"] = data.get("mid_price", 0)

        # 15m candles から変動率
        candles_15m = data.get("candles_15m", [])
        closes_15m = [float(c.get("c", 0)) for c in candles_15m if float(c.get("c", 0)) > 0]
        if len(candles_15m) >= 2:
            current = float(candles_15m[-1].get("c", 0))
            prev = float(candles_15m[-2].get("c", 0))
            f["price_change_15m"] = (current - prev) / prev * 100 if prev else 0

        # 1h candles
        candles_1h = data.get("candles_1h", [])
        if len(candles_1h) >= 2:
            current = float(candles_1h[-1].get("c", 0))
            first = float(candles_1h[-2].get("c", 0))
            f["price_change_1h"] = (current - first) / first * 100 if first else 0

        # 4h candles
        candles_4h = data.get("candles_4h", [])
        if len(candles_4h) >= 2:
            current = float(candles_4h[-1].get("c", 0))
            first = float(candles_4h[-2].get("c", 0))
            f["price_change_4h"] = (current - first) / first * 100 if first else 0

        # 4H レンジ位置 (0-100%) と 4H EMAクロス
        if len(candles_4h) >= 10:
            highs_4h = [float(c.get("h", 0)) for c in candles_4h[-20:]]
            lows_4h = [float(c.get("l", 0)) for c in candles_4h[-20:]]
            h4_high = max(highs_4h) if highs_4h else 0
            h4_low = min(lows_4h) if lows_4h else 0
            h4_range = h4_high - h4_low
            price_now = f.get("price", 0)
            f["h4_range_position"] = (price_now - h4_low) / h4_range * 100 if h4_range > 0 else 50.0

            closes_4h = [float(c.get("c", 0)) for c in candles_4h if float(c.get("c", 0)) > 0]
            if len(closes_4h) >= 21:
                ema9_4h = _ema(closes_4h, 9)
                ema21_4h = _ema(closes_4h, 21)
                f["ema_cross_4h"] = "golden" if ema9_4h > ema21_4h else "dead"

        # EMA (15m candles から算出)
        if len(candles_15m) >= 21:
            f["ema9"] = _ema(closes_15m, 9)
            f["ema21"] = _ema(closes_15m, 21)
            if f["ema21"] != 0:
                f["ema_gap_pct"] = (f["ema9"] - f["ema21"]) / f["ema21"] * 100
            f["ema_cross"] = "golden" if f["ema9"] > f["ema21"] else "dead"

        # MACD
        if len(candles_15m) >= 26:
            ema12 = _ema(closes_15m, 12)
            ema26 = _ema(closes_15m, 26)
            f["macd_histogram"] = ema12 - ema26
            if len(candles_15m) >= 27:
                closes_prev = closes_15m[:-1]
                ema12_prev = _ema(closes_prev, 12)
                ema26_prev = _ema(closes_prev, 26)
                prev_hist = ema12_prev - ema26_prev
                f["macd_direction"] = "expanding" if abs(f["macd_histogram"]) > abs(prev_hist) else "contracting"

        # Rolling FFT features (recent regime only)
        fft64 = _fft_spectral_features(closes_15m, window=64)
        if fft64:
            f["fft64_dominant_period_bars"] = fft64["dominant_period_bars"]
            f["fft64_harmonic_ratio"] = fft64["harmonic_ratio"]
            f["fft64_spectral_entropy"] = fft64["spectral_entropy"]

        fft96 = _fft_spectral_features(closes_15m, window=96)
        if fft96:
            f["fft96_dominant_period_bars"] = fft96["dominant_period_bars"]
            f["fft96_harmonic_ratio"] = fft96["harmonic_ratio"]
            f["fft96_spectral_entropy"] = fft96["spectral_entropy"]

        # Funding rate
        f["funding_rate"] = data.get("funding_rate", 0) or 0

        # Orderbook
        orderbook = data.get("orderbook", {})
        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])
        bid_total = sum(float(b.get("sz", 0)) for b in bids)
        ask_total = sum(float(a.get("sz", 0)) for a in asks)

        f["orderbook.bid_total"] = bid_total
        f["orderbook.ask_total"] = ask_total
        f["orderbook.bid_wall_max"] = max((float(b.get("sz", 0)) for b in bids), default=0)
        f["orderbook.ask_wall_max"] = max((float(a.get("sz", 0)) for a in asks), default=0)
        f["orderbook.imbalance"] = bid_total / ask_total if ask_total > 0 else 1.0

        # 出来高 (15m最新)
        if candles_15m:
            vols = [float(c.get("v", 0)) for c in candles_15m]
            avg_vol = sum(vols) / len(vols) if vols else 1
            f["volume_ratio"] = vols[-1] / avg_vol if avg_vol > 0 else 1.0

        # 5分足出来高の直近5本平均/100本平均 (vol_ratio_5bar)
        candles_5m = data.get("candles_5m", [])
        if len(candles_5m) >= 10:
            vols_5m = [float(c.get("v", 0)) for c in candles_5m]
            avg100 = sum(vols_5m[-100:]) / max(len(vols_5m[-100:]), 1)
            avg5 = sum(vols_5m[-5:]) / 5 if len(vols_5m) >= 5 else 0
            f["volume_ratio_5bar"] = avg5 / avg100 if avg100 > 0 else 1.0
        elif len(candles_15m) >= 10:
            # 5m足なければ15m足で代替
            avg_all = sum(vols) / max(len(vols), 1) if candles_15m else 1
            avg5_15m = sum(vols[-5:]) / 5 if len(vols) >= 5 else 0
            f["volume_ratio_5bar"] = avg5_15m / avg_all if avg_all > 0 else 1.0

        # 連続陽線/陰線
        if candles_15m:
            bull_count = 0
            bear_count = 0
            for c in reversed(candles_15m):
                if float(c.get("c", 0)) >= float(c.get("o", 0)):
                    if bear_count > 0:
                        break
                    bull_count += 1
                else:
                    if bull_count > 0:
                        break
                    bear_count += 1
            f["consecutive_bull_candles"] = bull_count
            f["consecutive_bear_candles"] = bear_count

        features[symbol] = f

    return features


def _ema(values: list[float], span: int) -> float:
    """最終EMA値を算出。"""
    if not values or span <= 0:
        return 0.0
    multiplier = 2 / (span + 1)
    ema = values[0]
    for v in values[1:]:
        ema = v * multiplier + ema * (1 - multiplier)
    return ema


def _log_returns(values: list[float]) -> list[float]:
    """価格系列からlog return系列を作成。"""
    out = []
    for i in range(1, len(values)):
        prev = values[i - 1]
        cur = values[i]
        if prev <= 0 or cur <= 0:
            continue
        out.append(math.log(cur / prev))
    return out


def _hann_window(n: int) -> list[float]:
    if n <= 1:
        return [1.0] * max(n, 0)
    return [0.5 - 0.5 * math.cos((2.0 * math.pi * i) / (n - 1)) for i in range(n)]


def _fft_spectral_features(closes: list[float], window: int) -> dict[str, float]:
    """直近window本のcloseからスペクトル特徴量を算出。

    - dominant_period_bars: 最大パワー周波数の周期(バー単位)
    - harmonic_ratio: 最大パワー / 全パワー
    - spectral_entropy: 正規化スペクトルエントロピー (0=秩序, 1=ノイズ)
    """
    if len(closes) < window:
        return {}

    recent = closes[-window:]
    series = _log_returns(recent)
    n = len(series)
    if n < 16:
        return {}

    mean = sum(series) / n
    detrended = [x - mean for x in series]
    weights = _hann_window(n)
    x = [v * w for v, w in zip(detrended, weights)]

    powers = []
    max_power = 0.0
    max_k = 1

    # k=0(DC)は除外、ナイキストまで
    for k in range(1, (n // 2) + 1):
        re = 0.0
        im = 0.0
        for t, val in enumerate(x):
            ang = (2.0 * math.pi * k * t) / n
            re += val * math.cos(ang)
            im -= val * math.sin(ang)
        p = (re * re) + (im * im)
        powers.append(p)
        if p > max_power:
            max_power = p
            max_k = k

    total_power = sum(powers)
    if total_power <= 0:
        return {}

    dominant_period_bars = n / max_k if max_k > 0 else float(n)
    harmonic_ratio = max_power / total_power

    probs = [p / total_power for p in powers if p > 0]
    if probs and len(powers) > 1:
        ent = -sum(p * math.log(p) for p in probs)
        spectral_entropy = ent / math.log(len(powers))
    else:
        spectral_entropy = 1.0

    return {
        "dominant_period_bars": round(dominant_period_bars, 4),
        "harmonic_ratio": round(harmonic_ratio, 6),
        "spectral_entropy": round(spectral_entropy, 6),
    }


def _check_condition(condition: dict, features: dict[str, dict]) -> bool:
    """1つのトリガー条件を評価。"""
    field = condition.get("field", "")
    symbol = condition.get("symbol", "")
    op = condition.get("op", "")
    value = condition.get("value")

    if op not in VALID_OPS or value is None:
        return False

    sym_features = features.get(symbol, {})
    actual = sym_features.get(field)

    if actual is None:
        return False

    # 文字列比較
    if isinstance(actual, str):
        if op == "==":
            return actual == str(value)
        if op == "!=":
            return actual != str(value)
        return False

    # 数値比較
    try:
        actual = float(actual)
        value = float(value)
    except (TypeError, ValueError):
        return False

    if op == ">":
        return actual > value
    if op == "<":
        return actual < value
    if op == ">=":
        return actual >= value
    if op == "<=":
        return actual <= value
    if op == "==":
        return math.isclose(actual, value, rel_tol=1e-6)
    if op == "!=":
        return not math.isclose(actual, value, rel_tol=1e-6)
    return False


def check_triggers(market_data: dict) -> list[dict]:
    """現在の市場データで発火するshadow/proven仮説を返す。"""
    features = extract_features(market_data)
    triggered = []

    for hyp in _load_all():
        if hyp["status"] not in ("shadow", "proven"):
            continue

        trigger = hyp.get("trigger", {})
        conditions = trigger.get("conditions", [])
        logic = trigger.get("logic", "AND")

        if not conditions:
            continue

        results = [_check_condition(c, features) for c in conditions]

        if logic == "AND" and all(results):
            triggered.append(hyp)
        elif logic == "OR" and any(results):
            triggered.append(hyp)

    return triggered


def record_shadow_result(hyp_id: str, won: bool, pnl_pct: float) -> None:
    """shadow仮説の検証結果を記録。"""
    hypotheses = _load_all()
    for h in hypotheses:
        if h["id"] == hyp_id:
            shadow = h.get("shadow", {"activations": 0, "wins": 0, "losses": 0, "total_pnl": 0.0, "results": []})
            shadow["activations"] += 1
            if won:
                shadow["wins"] += 1
            else:
                shadow["losses"] += 1
            shadow["total_pnl"] += pnl_pct
            shadow["results"].append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "won": won,
                "pnl_pct": pnl_pct,
            })
            # 直近20件のみ保持
            shadow["results"] = shadow["results"][-20:]
            h["shadow"] = shadow
            h["updated_at"] = datetime.now(timezone.utc).isoformat()
            _save_all(hypotheses)
            logger.info("Shadow result for %s: %s (pnl=%.3f%%)", hyp_id, "WIN" if won else "LOSS", pnl_pct)
            return


def promote_or_demote() -> list[str]:
    """shadow仮説の成績に基づいて昇格/降格を判定。

    Returns:
        変更があった仮説IDのリスト
    """
    settings = load_settings()
    hyp_config = settings.get("hypothesis", {})
    min_activations = hyp_config.get("shadow_min_activations", 5)
    min_winrate = hyp_config.get("shadow_min_winrate", 0.55)

    changed = []

    for hyp in get_active_shadows():
        shadow = hyp.get("shadow", {})
        activations = shadow.get("activations", 0)

        if activations < min_activations:
            continue

        wins = shadow.get("wins", 0)
        winrate = wins / activations if activations > 0 else 0

        if winrate >= min_winrate and shadow.get("total_pnl", 0) > 0:
            update_status(hyp["id"], "proven", {"shadow": shadow})
            changed.append(hyp["id"])
            logger.info("PROMOTED %s to proven (winrate=%.1f%%, pnl=%.3f%%)",
                       hyp["id"], winrate * 100, shadow["total_pnl"])
        elif activations >= min_activations * 2 and winrate < 0.45:
            update_status(hyp["id"], "demoted", {"shadow": shadow})
            changed.append(hyp["id"])
            logger.info("DEMOTED %s (winrate=%.1f%%, pnl=%.3f%%)",
                       hyp["id"], winrate * 100, shadow["total_pnl"])

    return changed


def rotate_old() -> int:
    """古い rejected/demoted 仮説を削除。"""
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    hypotheses = _load_all()
    original_len = len(hypotheses)

    hypotheses = [
        h for h in hypotheses
        if not (
            h.get("status") in ("rejected", "demoted")
            and h.get("updated_at", "") < cutoff.isoformat()
        )
    ]

    removed = original_len - len(hypotheses)
    if removed > 0:
        _save_all(hypotheses)
        logger.info("Rotated %d old hypotheses", removed)
    return removed


def process_reviewer_output(review: dict) -> list[dict]:
    """Reviewer AIの出力から新規仮説を作成。

    Args:
        review: Reviewer の JSON 出力

    Returns:
        作成された仮説のリスト
    """
    created = []
    for hyp_data in review.get("hypotheses", []):
        desc = hyp_data.get("description", "")
        trigger = hyp_data.get("trigger", {})
        prediction = hyp_data.get("prediction", {})

        if not desc or not trigger.get("conditions") or not prediction.get("symbol"):
            logger.warning("Skipping incomplete hypothesis: %s", desc[:50])
            continue

        hyp = create_hypothesis(
            description=desc,
            trigger=trigger,
            prediction=prediction,
            source=f"reviewer_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M')}",
        )
        if hyp:
            created.append(hyp)

    logger.info("Created %d hypotheses from reviewer output", len(created))
    return created


if __name__ == "__main__":
    # テスト
    print(f"Hypotheses: {len(_load_all())}")
    for s in VALID_STATUSES:
        count = len(get_by_status(s))
        if count:
            print(f"  {s}: {count}")
