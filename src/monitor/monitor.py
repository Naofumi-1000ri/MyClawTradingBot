"""Monitoring module for myClaw."""

import time
from datetime import datetime, timezone
from pathlib import Path

from src.utils.config_loader import get_signals_dir, get_state_dir
from src.utils.file_lock import read_json
from src.utils.logger import setup_logger
from src.monitor.telegram_notifier import send_message

logger = setup_logger("monitor")

# Alert thresholds
SIGNAL_STALE_SECONDS = 600  # 10 minutes
RUBBER_FALLBACK_ALERT_MINUTES = 30  # Rubber fallback 継続アラート閾値
RUBBER_FALLBACK_ALERT_COOLDOWN_MINUTES = 30  # アラート再送クールダウン


def _read_safe(path: Path) -> dict:
    """Read JSON file, returning empty dict on error."""
    try:
        return read_json(path)
    except (FileNotFoundError, Exception) as e:
        logger.debug("Could not read %s: %s", path, e)
        return {}


def _check_rubber_fallback_duration(state_dir: Path) -> str | None:
    """Rubber fallback が RUBBER_FALLBACK_ALERT_MINUTES 以上継続していればアラートメッセージを返す。

    ooda_log.json の直近エントリーを走査し、最初に fallback でないエントリーが現れるまで
    連続 fallback 期間を測定する。アラート重複送信は fallback_alert_state.json で制御。

    Returns:
        アラートメッセージ (str) または None (閾値未満 or クールダウン中)。
    """
    ooda_log_path = state_dir / "ooda_log.json"
    alert_state_path = state_dir / "fallback_alert_state.json"

    try:
        entries = read_json(ooda_log_path)
        if not isinstance(entries, list) or not entries:
            return None
    except (FileNotFoundError, Exception):
        return None

    now = datetime.now(timezone.utc)

    # 最新エントリーから遡り、連続 fallback の開始時刻を探す
    fallback_start: datetime | None = None
    for entry in reversed(entries):
        market_summary = entry.get("market_summary", "")
        ts_str = entry.get("timestamp", "")
        is_fallback = "Rubber fallback:" in market_summary

        if not is_fallback:
            # fallback でないエントリーが見つかった → ここで連続区間終了
            break

        # fallback エントリー → 開始時刻を更新
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            fallback_start = ts
        except (ValueError, TypeError):
            continue

    if fallback_start is None:
        # 全エントリーが fallback でない (= 最新が non-fallback)
        return None

    fallback_minutes = (now - fallback_start).total_seconds() / 60.0
    logger.info(
        "Rubber fallback duration: %.1f min (threshold=%d min)",
        fallback_minutes, RUBBER_FALLBACK_ALERT_MINUTES,
    )

    if fallback_minutes < RUBBER_FALLBACK_ALERT_MINUTES:
        return None

    # 閾値超過 → クールダウンチェック
    try:
        alert_state = read_json(alert_state_path)
        if not isinstance(alert_state, dict):
            alert_state = {}
    except (FileNotFoundError, Exception):
        alert_state = {}

    last_alert_str = alert_state.get("last_fallback_alert")
    if last_alert_str:
        try:
            last_alert = datetime.fromisoformat(last_alert_str)
            if last_alert.tzinfo is None:
                last_alert = last_alert.replace(tzinfo=timezone.utc)
            elapsed = (now - last_alert).total_seconds() / 60.0
            if elapsed < RUBBER_FALLBACK_ALERT_COOLDOWN_MINUTES:
                logger.debug(
                    "Rubber fallback alert suppressed: cooldown %.1f/%.1f min",
                    elapsed, RUBBER_FALLBACK_ALERT_COOLDOWN_MINUTES,
                )
                return None
        except (ValueError, TypeError):
            pass

    # 最新 fallback エントリーの reason を取得
    latest_entry = entries[-1]
    latest_summary = latest_entry.get("market_summary", "")
    reason = latest_summary.replace("Rubber fallback: ", "").strip() or "不明"

    # アラート送信時刻を記録
    alert_state["last_fallback_alert"] = now.isoformat()
    try:
        from src.utils.file_lock import atomic_write_json
        atomic_write_json(alert_state_path, alert_state)
    except Exception as e:
        logger.warning("fallback_alert_state 保存失敗: %s", e)

    msg = (
        f"Rubber fallback 継続 {fallback_minutes:.0f}分: {reason}。"
        f"市場状況・戦略パラメータを確認してください。"
    )
    logger.warning(msg)
    return msg



def _close_all_positions() -> None:
    """Emergency close all open positions."""
    try:
        from src.executor.trade_executor import TradeExecutor
        executor = TradeExecutor()
        positions = executor.state.get_positions()
        if not positions:
            logger.info("No positions to close")
            return
        for pos in positions:
            symbol = pos.get("symbol", "")
            if symbol:
                logger.warning("Emergency closing %s", symbol)
                executor.close_position(symbol)
    except Exception:
        logger.exception("Emergency close failed")


def run_monitor() -> None:
    """Run one monitoring cycle."""
    signals_dir = get_signals_dir()
    state_dir = get_state_dir()
    alerts = []

    # 1. Check signals freshness
    signals_path = signals_dir / "signals.json"
    if signals_path.exists():
        mtime = signals_path.stat().st_mtime
        age = time.time() - mtime
        if age > SIGNAL_STALE_SECONDS:
            msg = f"Signals stale: last updated {int(age)}s ago"
            logger.warning(msg)
            alerts.append(msg)
        else:
            logger.info("Signals OK (age: %ds)", int(age))
    else:
        logger.info("No signals file yet")

    # 2. Check positions (positions.json is a list)
    positions_path = state_dir / "positions.json"
    positions = []
    if positions_path.exists():
        try:
            data = read_json(positions_path)
            positions = data if isinstance(data, list) else []
        except Exception:
            pass
    if positions:
        logger.info("Active positions: %d", len(positions))
        for p in positions:
            symbol = p.get("symbol", "?")
            side = p.get("side", "?")
            unrealized = p.get("unrealized_pnl", 0)
            logger.info("  %s %s unrealizedPnL=%.2f", symbol, side, float(unrealized))
    else:
        logger.info("No active positions")

    # 3. Check daily P&L
    daily_pnl = _read_safe(state_dir / "daily_pnl.json")
    if daily_pnl:
        realized = float(daily_pnl.get("realized_pnl", 0))
        unrealized = float(daily_pnl.get("unrealized_pnl", 0))
        total = realized + unrealized
        logger.info("Daily P&L: realized=%.2f unrealized=%.2f total=%.2f", realized, unrealized, total)
        equity = float(daily_pnl.get("equity", 0))
        if total < 0 and equity > 0 and abs(total) / equity >= 0.01:
            alerts.append(f"Daily P&L negative: {total:.2f} ({abs(total)/equity*100:.1f}%)")

    # 4. Check kill switch
    ks = _read_safe(state_dir / "kill_switch.json")
    if ks.get("enabled"):
        reason = ks.get("reason", "unknown")
        msg = f"KILL SWITCH ACTIVE: {reason}"
        logger.warning(msg)
        alerts.append(msg)

    # 4a. Check agent failure warning
    if ks.get("warning"):
        warning_reason = ks.get("warning_reason", "unknown")
        warning_at = ks.get("warning_at", "")
        msg = f"WARNING: {warning_reason} (at: {warning_at})"
        logger.warning(msg)
        alerts.append(msg)


    # 4b. Risk limit checks (daily loss / max drawdown)
    if daily_pnl and float(daily_pnl.get("equity", 0)) > 0:
        try:
            from src.risk.risk_manager import RiskManager
            from src.risk.kill_switch import activate as ks_activate, is_active as ks_is_active
            if not ks_is_active():
                # サニティチェック: equity が start_of_day_equity の10%未満は異常値
                equity = float(daily_pnl.get("equity", 0))
                start_equity = float(daily_pnl.get("start_of_day_equity", equity))
                if start_equity > 0 and equity < start_equity * 0.1:
                    logger.warning(
                        "Equity sanity check FAILED: equity=%.2f vs start=%.2f (%.1f%%). "
                        "Likely stale or incorrect equity data. Skipping risk checks.",
                        equity, start_equity, (equity / start_equity) * 100
                    )
                else:
                    rm = RiskManager()
                    peak_equity = float(daily_pnl.get("peak_equity", equity))
                    if rm.check_daily_loss(daily_pnl, equity):
                        ks_activate("daily_loss_5pct_exceeded")
                        _close_all_positions()
                        msg = "KILL SWITCH: 日次損失5%超過"
                        logger.critical(msg)
                        alerts.append(msg)
                        send_message(f"*KILL SWITCH* {msg}")
                    elif rm.check_max_drawdown(equity, peak_equity):
                        ks_activate("max_drawdown_15pct_exceeded")
                        _close_all_positions()
                        msg = "KILL SWITCH: 最大DD15%超過"
                        logger.critical(msg)
                        alerts.append(msg)
                        send_message(f"*KILL SWITCH* {msg}")
        except Exception as e:
            logger.exception("Risk limit check failed")
            # サイレントフォールバック防止: risk check 例外も通知
            err_msg = f"WARNING: リスク制限チェックで予期しない例外: {e}"
            logger.critical(err_msg)
            alerts.append(err_msg)
            try:
                send_message(f"*WARNING: Risk check exception*\n{e}\nリスク監視が機能していない可能性があります。ログを確認してください。")
            except Exception:
                pass  # 通知失敗は無視 (send_message 内でリトライ済み)

    # 4c. Rubber fallback 継続アラート (30分超えでレビュー促進)
    fallback_alert = _check_rubber_fallback_duration(state_dir)
    if fallback_alert:
        alerts.append(fallback_alert)

    # 5. データ品質継続劣化チェック (data_health_summary の consecutive_low_score 監視)
    health_summary_path = state_dir / "data_health_summary.json"
    if health_summary_path.exists():
        try:
            health_summary = read_json(health_summary_path)
            if isinstance(health_summary, dict):
                consecutive_low = int(
                    health_summary.get("events", {}).get("consecutive_low_score", 0)
                )
                avg_score = float(
                    health_summary.get("score", {}).get("avg", 100)
                )
                # 3サイクル以上連続してスコア低下 (= 15分以上データ品質劣化継続)
                if consecutive_low >= 3:
                    msg = (
                        f"データ品質継続劣化: {consecutive_low}サイクル連続スコア低下 "
                        f"(avg={avg_score:.1f}/100)"
                    )
                    logger.warning(msg)
                    alerts.append(msg)
        except Exception:
            logger.debug("data_health_summary read failed (non-critical)")

    # 5b. パフォーマンス分析 (毎回実行、保存あり)
    try:
        from src.monitor.performance_tracker import run_analysis
        run_analysis(save_report=True)
    except Exception:
        logger.exception("Performance analysis failed")

    # 6. Send alerts if any
    if alerts:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        text = f"*myClaw Alert* ({now})\n" + "\n".join(f"- {a}" for a in alerts)
        send_message(text)

    logger.info("Monitor cycle complete")


if __name__ == "__main__":
    run_monitor()
