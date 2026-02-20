"""Data health check for market snapshots before consensus/decision.

Flow:
1) Validate collected files.
2) If invalid, optionally recollect once and validate again.
3) Persist health report to state/data_health.json.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.collector.data_collector import collect
from src.utils.config_loader import get_project_root, load_settings
from src.utils.file_lock import atomic_write_json, read_json
from src.utils.logger import setup_logger

logger = setup_logger("data_health")


@dataclass
class HealthResult:
    healthy: bool
    score: int
    execution_mode: str
    recommend_kill_switch: bool
    errors: list[str]
    warnings: list[str]
    checked_at: str
    attempted_recollect: bool = False

    def to_dict(self) -> dict:
        return {
            "healthy": self.healthy,
            "score": self.score,
            "execution_mode": self.execution_mode,
            "recommend_kill_switch": self.recommend_kill_switch,
            "errors": self.errors,
            "warnings": self.warnings,
            "checked_at": self.checked_at,
            "attempted_recollect": self.attempted_recollect,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_read_json(path: Path):
    try:
        return read_json(path)
    except Exception:
        return None


def _stale_seconds(ts: str) -> float | None:
    try:
        dt = datetime.fromisoformat(ts)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return None


def _derive_policy(settings: dict, score: int) -> tuple[str, bool]:
    gate_cfg = settings.get("trading", {}).get("decision_gate", {})
    close_only_th = int(gate_cfg.get("close_only_score_threshold", 80))
    kill_prop_th = int(gate_cfg.get("kill_switch_proposal_score_threshold", 60))

    execution_mode = "all" if score >= close_only_th else "close_only"
    recommend_kill_switch = score < kill_prop_th
    return execution_mode, recommend_kill_switch


def _validate_once(settings: dict) -> HealthResult:
    root = get_project_root()
    data_path = root / "data" / "market_data.json"
    state_dir = root / "state"
    daily_pnl_path = state_dir / "daily_pnl.json"
    positions_path = state_dir / "positions.json"

    errors: list[str] = []
    warnings: list[str] = []
    score = 100

    market = _safe_read_json(data_path)
    if not isinstance(market, dict):
        errors.append("market_data.json is missing or invalid JSON")
        mode, recommend_ks = _derive_policy(settings, 0)
        return HealthResult(False, 0, mode, recommend_ks, errors, warnings, _now_iso())

    ts = market.get("timestamp")
    if not isinstance(ts, str):
        errors.append("market_data.timestamp missing")
    else:
        stale = _stale_seconds(ts)
        max_stale = int(settings.get("cycle", {}).get("data_staleness_seconds", 300))
        if stale is None:
            errors.append("market_data.timestamp parse failed")
        elif stale > max_stale:
            errors.append(f"market_data is stale: {stale:.0f}s > {max_stale}s")

    symbols = settings.get("trading", {}).get("symbols", [])
    symbol_map = market.get("symbols")
    if not isinstance(symbol_map, dict):
        errors.append("market_data.symbols missing")
        symbol_map = {}

    for sym in symbols:
        s = symbol_map.get(sym)
        if not isinstance(s, dict):
            errors.append(f"{sym}: missing symbol payload")
            continue

        mid = s.get("mid_price")
        try:
            mid_f = float(mid)
            if mid_f <= 0:
                errors.append(f"{sym}: mid_price <= 0")
        except Exception:
            errors.append(f"{sym}: invalid mid_price")

        for key, min_len in (("candles_15m", 48), ("candles_1h", 24), ("candles_4h", 20)):
            arr = s.get(key)
            if not isinstance(arr, list) or len(arr) < min_len:
                errors.append(f"{sym}: insufficient {key} ({0 if not isinstance(arr, list) else len(arr)}<{min_len})")

        ob = s.get("orderbook", {})
        bids = ob.get("bids", []) if isinstance(ob, dict) else []
        asks = ob.get("asks", []) if isinstance(ob, dict) else []
        if not bids or not asks:
                errors.append(f"{sym}: orderbook empty")

    eq = market.get("account_equity")
    try:
        eq_f = float(eq)
        if eq_f <= 0:
            errors.append("account_equity <= 0")
    except Exception:
        errors.append("account_equity invalid")
        eq_f = 0.0

    daily = _safe_read_json(daily_pnl_path)
    if isinstance(daily, dict):
        day_eq = float(daily.get("equity") or 0)
        drift_threshold = float(
            settings.get("trading", {}).get("decision_gate", {}).get("max_equity_drift_pct", 20.0)
        )
        if day_eq > 0 and eq_f > 0:
            drift_pct = abs(eq_f - day_eq) / day_eq * 100
            if drift_pct > drift_threshold:
                errors.append(
                    f"equity drift too large: live={eq_f:.2f}, state={day_eq:.2f}, drift={drift_pct:.1f}%>{drift_threshold:.1f}%"
                )
    else:
        warnings.append("daily_pnl.json missing or invalid")

    positions = _safe_read_json(positions_path)
    if isinstance(positions, list) and isinstance(daily, dict):
        unrealized = float(daily.get("unrealized_pnl") or 0)
        if len(positions) == 0 and abs(unrealized) > 5.0:
            warnings.append(
                f"positions empty but unrealized_pnl is {unrealized:.2f} (possible state lag)"
            )

    # Score: heavy penalty on hard errors, light penalty on warnings
    score -= 20 * len(errors)
    score -= 5 * len(warnings)
    score = max(0, min(100, score))
    mode, recommend_ks = _derive_policy(settings, score)
    return HealthResult(len(errors) == 0, score, mode, recommend_ks, errors, warnings, _now_iso())


def run_health_check(settings: dict | None = None, attempt_recollect: bool = True) -> HealthResult:
    if settings is None:
        settings = load_settings()

    result = _validate_once(settings)
    if result.healthy:
        return result

    if not attempt_recollect:
        return result

    logger.warning("Data health check failed, recollecting once: %s", "; ".join(result.errors))
    try:
        collect(settings)
    except Exception as exc:
        result.errors.append(f"recollect failed: {exc}")
        return result

    second = _validate_once(settings)
    second.attempted_recollect = True
    return second


def _persist_report(result: HealthResult, settings: dict | None = None) -> None:
    if settings is None:
        settings = load_settings()
    root = get_project_root()
    report_path = root / settings.get("paths", {}).get("state_dir", "state") / "data_health.json"
    atomic_write_json(report_path, result.to_dict())


def _append_history(result: HealthResult, settings: dict | None = None) -> None:
    if settings is None:
        settings = load_settings()
    root = get_project_root()
    state_dir = root / settings.get("paths", {}).get("state_dir", "state")
    history_path = state_dir / "data_health_history.json"
    try:
        history = read_json(history_path)
        if not isinstance(history, list):
            history = []
    except Exception:
        history = []

    history.append(result.to_dict())
    # keep bounded (about 7 days at 5m cadence ~= 2016)
    history = history[-2500:]
    atomic_write_json(history_path, history)


def _update_summary(settings: dict | None = None) -> None:
    if settings is None:
        settings = load_settings()
    root = get_project_root()
    state_dir = root / settings.get("paths", {}).get("state_dir", "state")
    history_path = state_dir / "data_health_history.json"
    summary_path = state_dir / "data_health_summary.json"

    try:
        history = read_json(history_path)
        if not isinstance(history, list):
            history = []
    except Exception:
        history = []

    now = datetime.now(timezone.utc)
    last_24h = []
    for h in history:
        ts = h.get("checked_at")
        if not isinstance(ts, str):
            continue
        try:
            dt = datetime.fromisoformat(ts)
        except Exception:
            continue
        if (now - dt).total_seconds() <= 24 * 3600:
            last_24h.append(h)

    scores = [int(h.get("score", 0)) for h in last_24h]
    total = len(last_24h)
    all_mode = sum(1 for h in last_24h if h.get("execution_mode") == "all")
    close_only_mode = sum(1 for h in last_24h if h.get("execution_mode") == "close_only")
    failures = sum(1 for h in last_24h if not bool(h.get("healthy")))
    ks_reco = sum(1 for h in last_24h if bool(h.get("recommend_kill_switch")))

    consecutive_low = 0
    for h in reversed(history):
        if int(h.get("score", 0)) < 80:
            consecutive_low += 1
        else:
            break

    summary = {
        "updated_at": now.isoformat(),
        "window_hours": 24,
        "samples": total,
        "score": {
            "avg": round(sum(scores) / total, 2) if total else 0,
            "min": min(scores) if scores else 0,
            "max": max(scores) if scores else 0,
        },
        "modes": {
            "all": all_mode,
            "close_only": close_only_mode,
        },
        "events": {
            "failed_checks": failures,
            "kill_switch_recommendations": ks_reco,
            "consecutive_low_score": consecutive_low,
        },
    }
    atomic_write_json(summary_path, summary)


def _append_request(kind: str, message: str, settings: dict | None = None) -> None:
    if settings is None:
        settings = load_settings()
    root = get_project_root()
    state_dir = root / settings.get("paths", {}).get("state_dir", "state")
    req_path = state_dir / "requests.json"
    try:
        reqs = read_json(req_path)
        if not isinstance(reqs, list):
            reqs = []
    except Exception:
        reqs = []

    reqs.append({
        "type": kind,
        "message": message,
        "timestamp": _now_iso(),
    })
    # keep file bounded
    reqs = reqs[-200:]
    atomic_write_json(req_path, reqs)


def _should_send_alert(state_dir: Path, alert_type: str, cooldown_seconds: int = 1800) -> bool:
    """同種アラートのクールダウン管理 (デフォルト30分)。

    state/data_health_alert_state.json に最終送信時刻を記録し、
    クールダウン期間内の重複送信を防ぐ。
    """
    alert_state_path = state_dir / "data_health_alert_state.json"
    try:
        alert_state = read_json(alert_state_path)
        if not isinstance(alert_state, dict):
            alert_state = {}
    except Exception:
        alert_state = {}

    last_sent_str = alert_state.get(alert_type)
    if last_sent_str:
        try:
            last_sent = datetime.fromisoformat(last_sent_str)
            elapsed = (datetime.now(timezone.utc) - last_sent).total_seconds()
            if elapsed < cooldown_seconds:
                logger.debug(
                    "Alert '%s' suppressed (cooldown: %ds remaining)",
                    alert_type, int(cooldown_seconds - elapsed),
                )
                return False
        except Exception:
            pass

    # 送信時刻を記録
    alert_state[alert_type] = datetime.now(timezone.utc).isoformat()
    try:
        atomic_write_json(alert_state_path, alert_state)
    except Exception as e:
        logger.warning("Failed to update alert state: %s", e)

    return True


def _send_health_alert(result: HealthResult, settings: dict) -> None:
    """データ品質劣化をTelegramに通知する。

    - score < 80 (close_only mode): 警告
    - recommend_kill_switch (score < 60): 重大警告
    クールダウン30分で重複通知を抑制。
    """
    try:
        from src.monitor.telegram_notifier import send_message
    except ImportError:
        logger.warning("telegram_notifier not available, skipping health alert")
        return

    root = get_project_root()
    state_dir = root / settings.get("paths", {}).get("state_dir", "state")

    if result.recommend_kill_switch:
        alert_type = "health_critical"
        cooldown = 1800  # 30分
        msg = (
            f"*CRITICAL: データ品質劣化*\n"
            f"スコア: {result.score}/100 (kill_switch推奨)\n"
            f"モード: {result.execution_mode}\n"
            f"エラー: {'; '.join(result.errors[:3])}"
        )
    elif not result.healthy or result.execution_mode == "close_only":
        alert_type = "health_warning"
        cooldown = 1800  # 30分
        msg = (
            f"*WARNING: データ品質低下*\n"
            f"スコア: {result.score}/100\n"
            f"モード: {result.execution_mode}\n"
            f"エラー: {'; '.join(result.errors[:3])}"
        )
    else:
        return  # 正常 → 通知不要

    if _should_send_alert(state_dir, alert_type, cooldown):
        send_message(msg)
        logger.info("Health alert sent (type=%s, score=%d)", alert_type, result.score)


def main() -> int:
    settings = load_settings()
    result = run_health_check(settings, attempt_recollect=True)
    _persist_report(result, settings)
    _append_history(result, settings)
    _update_summary(settings)
    if result.recommend_kill_switch:
        # de-duplicate same recommendation within the last 60 minutes
        state_dir = get_project_root() / settings.get("paths", {}).get("state_dir", "state")
        req_path = state_dir / "requests.json"
        should_append = True
        try:
            reqs = read_json(req_path)
            if isinstance(reqs, list):
                now = datetime.now(timezone.utc)
                for r in reversed(reqs):
                    if r.get("type") != "kill_switch_recommendation":
                        continue
                    ts = r.get("timestamp")
                    if not isinstance(ts, str):
                        break
                    try:
                        dt = datetime.fromisoformat(ts)
                    except Exception:
                        break
                    if (now - dt).total_seconds() < 3600:
                        should_append = False
                    break
        except Exception:
            pass

        if should_append:
            _append_request(
                "kill_switch_recommendation",
                f"Data quality score is {result.score}. Recommend activating kill switch and manual review.",
                settings,
            )

    # データ品質劣化時にTelegramアラートを送信 (サイレントフォールバック防止)
    _send_health_alert(result, settings)

    if result.healthy:
        logger.info("Data health check passed (score=%d, mode=%s)", result.score, result.execution_mode)
        if result.warnings:
            logger.warning("Data health warnings: %s", "; ".join(result.warnings))
        return 0

    logger.error(
        "Data health check failed (score=%d, mode=%s): %s",
        result.score, result.execution_mode, "; ".join(result.errors),
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
