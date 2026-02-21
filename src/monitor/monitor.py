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
RUBBER_FALLBACK_ALERT_MINUTES = 30  # スパイク系fallback 継続アラート閾値
RUBBER_FALLBACK_ALERT_COOLDOWN_MINUTES = 30  # スパイク系fallbackアラート再送クールダウン
QUIET_FALLBACK_ALERT_MINUTES = 60  # スパイクなし静観 継続アラート閾値 (通常動作なので高め)
QUIET_FALLBACK_ALERT_COOLDOWN_MINUTES = 60  # スパイクなし長期継続アラート再送クールダウン


def _read_safe(path: Path) -> dict:
    """Read JSON file, returning empty dict on error."""
    try:
        return read_json(path)
    except (FileNotFoundError, Exception) as e:
        logger.debug("Could not read %s: %s", path, e)
        return {}


def _check_rubber_fallback_duration(state_dir: Path) -> str | None:
    """Rubber fallback が RUBBER_FALLBACK_ALERT_MINUTES 以上継続していればアラートメッセージを返す。

    ooda_log.json の直近エントリーを走査し、スパイクなし系のfallbackが連続している区間を測定する。
    「新規エントリー停止中」は意図的な停止状態のためカウント対象外。
    アラート重複送信は fallback_alert_state.json で制御。

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

    # 最新エントリーから遡り、スパイク検知系fallbackの連続区間を測定する
    # 「新規エントリー停止中」は意図的な状態のためアラート対象外
    fallback_start: datetime | None = None
    spike_fallback_count = 0
    reason_counts: dict[str, int] = {}

    for entry in reversed(entries):
        market_summary = entry.get("market_summary", "")
        ts_str = entry.get("timestamp", "")

        is_fallback = "Rubber fallback:" in market_summary
        # 意図的な正常状態 → アラートカウント対象外
        is_intentional_stop = "新規エントリー停止中" in market_summary
        # スパイクなし静観はゴム戦略の通常動作 (スパイク検知型なので大半の時間はスパイクなし)
        is_normal_quiet = "スパイクなし" in market_summary

        if not is_fallback or is_intentional_stop or is_normal_quiet:
            # スパイク系fallback以外のエントリーが見つかった → 連続区間終了
            break

        # スパイク系fallbackエントリー → カウントして開始時刻を更新
        spike_fallback_count += 1
        reason = market_summary.replace("Rubber fallback:", "").strip()
        reason_counts[reason] = reason_counts.get(reason, 0) + 1

        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            fallback_start = ts
        except (ValueError, TypeError):
            continue

    if fallback_start is None or spike_fallback_count == 0:
        # スパイク系fallbackなし (= 最新が non-fallback or 停止中のみ)
        return None

    fallback_minutes = (now - fallback_start).total_seconds() / 60.0
    logger.info(
        "Rubber fallback duration: %.1f min (threshold=%d min, spike_cycles=%d)",
        fallback_minutes, RUBBER_FALLBACK_ALERT_MINUTES, spike_fallback_count,
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

    # 代表理由を取得 (最多出現の reason)
    top_reason = max(reason_counts, key=lambda k: reason_counts[k]) if reason_counts else "不明"

    # ooda_log が満杯(30件)かつ全件fallbackの場合、アーカイブで開始時刻を遡って補正
    # spike_fallback_count == len(entries) は「ログ全件がfallback」 = 実際の開始はさらに古い可能性
    archive_extended = False
    if spike_fallback_count == len(entries):
        try:
            archive_dir = state_dir / "ooda_archive"
            # 最新のアーカイブファイルを1件だけ参照
            archive_files = sorted(archive_dir.glob("*.json"), reverse=True)
            if archive_files:
                arc_entries = read_json(archive_files[0])
                if isinstance(arc_entries, list) and arc_entries:
                    # アーカイブ末尾(最新エントリ)から遡りfallback連続区間を確認
                    for arc_entry in reversed(arc_entries):
                        arc_ms = arc_entry.get("market_summary", "")
                        if "Rubber fallback:" in arc_ms and "新規エントリー停止中" not in arc_ms:
                            ts_str = arc_entry.get("timestamp", "")
                            try:
                                ts = datetime.fromisoformat(ts_str)
                                if ts.tzinfo is None:
                                    ts = ts.replace(tzinfo=timezone.utc)
                                fallback_start = ts
                                spike_fallback_count += 1
                                archive_extended = True
                            except (ValueError, TypeError):
                                pass
                        else:
                            break
        except Exception:
            pass  # アーカイブ参照失敗は無視 (アラート自体は送る)

    if archive_extended:
        fallback_minutes = (now - fallback_start).total_seconds() / 60.0
        logger.info(
            "Fallback start extended via archive: %.1f min total (%d+ cycles)",
            fallback_minutes, spike_fallback_count,
        )

    # 市場データから診断情報を取得 (スパイク閾値との比較付き)
    # BTC/SOL スパイク閾値: vol_threshold=5.0x (長期平均比)
    # ETH: Pattern A reversal は big spike (別計算), Pattern C quiet は vol_ratio < 0.3x
    SPIKE_THRESHOLD = {
        "BTC": 5.0,  # BtcRubberWall default vol_threshold
        "SOL": 5.0,  # SolRubberWall default vol_threshold (BTC型と同様)
    }
    diagnosis_lines = []
    try:
        market_data_path = state_dir.parent / "data" / "market_data.json"
        market_data = read_json(market_data_path)
        symbols = market_data.get("symbols", {}) if isinstance(market_data, dict) else {}
        for sym in ["BTC", "ETH", "SOL"]:
            sym_data = symbols.get(sym, {})
            mid_price = sym_data.get("mid_price")
            candles = sym_data.get("candles_5m", [])
            if len(candles) >= 20 and mid_price:
                vols = [float(c.get("v") or 0) for c in candles]
                # 直近1本 vs 直近20本平均 (モニター用簡易計算)
                recent_vol = vols[-1]
                avg_vol = sum(vols[-20:-1]) / 19 if len(vols) >= 20 else 1
                vol_ratio = recent_vol / avg_vol if avg_vol > 0 else 0
                threshold = SPIKE_THRESHOLD.get(sym)
                if threshold:
                    gap = vol_ratio / threshold
                    gap_pct = gap * 100
                    diagnosis_lines.append(
                        f"  {sym}: vol_ratio={vol_ratio:.1f}x / 閾値{threshold:.0f}x"
                        f" ({gap_pct:.0f}%)  price={mid_price:,.0f}"
                    )
                else:
                    # ETH: quiet_long は vol_ratio < 0.3x を狙う戦略
                    diagnosis_lines.append(
                        f"  {sym}: vol_ratio={vol_ratio:.1f}x (quiet<0.3x)  price={mid_price:,.0f}"
                    )
    except Exception:
        pass  # 市場データ取得失敗は無視

    # キャッシュからスパイク閾値ボリュームを取得 (最新計算値)
    cache_info_lines = []
    try:
        btc_cache = read_json(state_dir / "rubber_wall_cache.json")
        if isinstance(btc_cache, dict) and "threshold_vol" in btc_cache:
            cache_info_lines.append(f"  BTC spike閾値 (絶対量): {btc_cache['threshold_vol']:.1f}")
    except Exception:
        pass
    try:
        sol_cache = read_json(state_dir / "sol_rubber_wall_cache.json")
        if isinstance(sol_cache, dict) and "threshold_vol" in sol_cache:
            cache_info_lines.append(f"  SOL spike閾値 (絶対量): {sol_cache['threshold_vol']:.1f}")
    except Exception:
        pass

    # 継続時間に応じた推奨アクション
    if fallback_minutes >= 120:
        action = "120分超: パラメータ緩和 or quiet_long 条件見直しを検討"
    elif fallback_minutes >= 60:
        action = "60分超: 市場状況確認 + vol_threshold の妥当性レビュー"
    else:
        action = "30分超: 次サイクルで自然解消か継続監視"

    # アラート送信時刻を記録
    alert_state["last_fallback_alert"] = now.isoformat()
    alert_state["last_fallback_duration_min"] = round(fallback_minutes, 1)
    alert_state["last_fallback_reason"] = top_reason
    try:
        from src.utils.file_lock import atomic_write_json
        atomic_write_json(alert_state_path, alert_state)
    except Exception as e:
        logger.warning("fallback_alert_state 保存失敗: %s", e)

    fallback_start_str = fallback_start.strftime("%H:%M UTC")
    msg_lines = [
        f"Rubber fallback 継続 {fallback_minutes:.0f}分 ({spike_fallback_count}+サイクル)" if archive_extended
        else f"Rubber fallback 継続 {fallback_minutes:.0f}分 ({spike_fallback_count}サイクル)",
        f"原因: {top_reason}",
        f"開始: {fallback_start_str}",
    ]
    if diagnosis_lines:
        msg_lines.append("vol_ratio状況 (vs spike閾値):")
        msg_lines.extend(diagnosis_lines)
    if cache_info_lines:
        msg_lines.extend(cache_info_lines)
    msg_lines.append(f"推奨: {action}")

    msg = "\n".join(msg_lines)
    logger.warning("Rubber fallback alert: %s", msg.replace("\n", " | "))
    return msg



def _check_quiet_fallback_duration(state_dir: Path) -> str | None:
    """「スパイクなし: 静観」fallback が QUIET_FALLBACK_ALERT_MINUTES 以上継続していればアラートを返す。

    スパイクなし静観はゴム戦略の通常動作だが、60分超継続する場合は
    市場が極めて低ボラ状態にあり、quiet_long 条件との比較や
    vol_threshold 妥当性の診断が有用になる。

    アラート状態は fallback_alert_state.json の `last_quiet_alert` で管理。

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

    # 最新エントリーから遡り「スパイクなし: 静観」の連続区間を測定する
    quiet_start: datetime | None = None
    quiet_count = 0

    for entry in reversed(entries):
        market_summary = entry.get("market_summary", "")
        ts_str = entry.get("timestamp", "")

        is_quiet_fallback = (
            "Rubber fallback:" in market_summary
            and "スパイクなし" in market_summary
        )
        if not is_quiet_fallback:
            break

        quiet_count += 1
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            quiet_start = ts
        except (ValueError, TypeError):
            continue

    if quiet_start is None or quiet_count == 0:
        return None

    quiet_minutes = (now - quiet_start).total_seconds() / 60.0
    logger.info(
        "Quiet fallback duration: %.1f min (threshold=%d min, cycles=%d)",
        quiet_minutes, QUIET_FALLBACK_ALERT_MINUTES, quiet_count,
    )

    if quiet_minutes < QUIET_FALLBACK_ALERT_MINUTES:
        return None

    # クールダウンチェック (quiet専用キー: last_quiet_alert)
    try:
        alert_state = read_json(alert_state_path)
        if not isinstance(alert_state, dict):
            alert_state = {}
    except (FileNotFoundError, Exception):
        alert_state = {}

    last_alert_str = alert_state.get("last_quiet_alert")
    if last_alert_str:
        try:
            last_alert = datetime.fromisoformat(last_alert_str)
            if last_alert.tzinfo is None:
                last_alert = last_alert.replace(tzinfo=timezone.utc)
            elapsed = (now - last_alert).total_seconds() / 60.0
            if elapsed < QUIET_FALLBACK_ALERT_COOLDOWN_MINUTES:
                logger.debug(
                    "Quiet fallback alert suppressed: cooldown %.1f/%.1f min",
                    elapsed, QUIET_FALLBACK_ALERT_COOLDOWN_MINUTES,
                )
                return None
        except (ValueError, TypeError):
            pass

    # アーカイブで開始時刻を補正 (ログ全件がquiet fallbackの場合)
    archive_extended = False
    if quiet_count == len(entries):
        try:
            archive_dir = state_dir / "ooda_archive"
            archive_files = sorted(archive_dir.glob("*.json"), reverse=True)
            if archive_files:
                arc_entries = read_json(archive_files[0])
                if isinstance(arc_entries, list) and arc_entries:
                    for arc_entry in reversed(arc_entries):
                        arc_ms = arc_entry.get("market_summary", "")
                        if (
                            "Rubber fallback:" in arc_ms
                            and "スパイクなし" in arc_ms
                        ):
                            ts_str = arc_entry.get("timestamp", "")
                            try:
                                ts = datetime.fromisoformat(ts_str)
                                if ts.tzinfo is None:
                                    ts = ts.replace(tzinfo=timezone.utc)
                                quiet_start = ts
                                quiet_count += 1
                                archive_extended = True
                            except (ValueError, TypeError):
                                pass
                        else:
                            break
        except Exception:
            pass

    if archive_extended:
        quiet_minutes = (now - quiet_start).total_seconds() / 60.0
        logger.info(
            "Quiet fallback start extended via archive: %.1f min total (%d+ cycles)",
            quiet_minutes, quiet_count,
        )

    # 市場データから現在のvol_ratio と quiet_long 条件を診断
    QUIET_LONG_VOL_MAX = 0.3  # ETH quiet_long 条件: vol_ratio < 0.3x
    SPIKE_THRESHOLD = {"BTC": 5.0, "SOL": 5.0}
    diagnosis_lines = []
    try:
        market_data_path = state_dir.parent / "data" / "market_data.json"
        market_data = read_json(market_data_path)
        symbols = market_data.get("symbols", {}) if isinstance(market_data, dict) else {}
        for sym in ["BTC", "ETH", "SOL"]:
            sym_data = symbols.get(sym, {})
            mid_price = sym_data.get("mid_price")
            candles = sym_data.get("candles_5m", [])
            if len(candles) >= 20 and mid_price:
                vols = [float(c.get("v") or 0) for c in candles]
                recent_vol = vols[-1]
                avg_vol = sum(vols[-20:-1]) / 19 if len(vols) >= 20 else 1
                vol_ratio = recent_vol / avg_vol if avg_vol > 0 else 0
                threshold = SPIKE_THRESHOLD.get(sym)
                if threshold:
                    gap_pct = (vol_ratio / threshold) * 100
                    diagnosis_lines.append(
                        f"  {sym}: vol_ratio={vol_ratio:.2f}x / spike閾値{threshold:.0f}x"
                        f" ({gap_pct:.0f}%)  price={mid_price:,.0f}"
                    )
                else:
                    # ETH: quiet_long 条件との比較
                    in_quiet = "✓" if vol_ratio < QUIET_LONG_VOL_MAX else "✗"
                    diagnosis_lines.append(
                        f"  {sym}: vol_ratio={vol_ratio:.2f}x (quiet_long条件<{QUIET_LONG_VOL_MAX}x {in_quiet})"
                        f"  price={mid_price:,.0f}"
                    )
    except Exception:
        pass

    # 継続時間に応じた推奨アクション
    if quiet_minutes >= 180:
        action = "180分超: 市場が極低ボラ帯。vol_threshold 緩和 or quiet_long 条件緩和を検討"
    elif quiet_minutes >= 120:
        action = "120分超: quiet_long 条件 (h4_range_position, vol_ratio) の現状確認を推奨"
    else:
        action = "60分超: 低ボラ継続中。quiet_long がエントリーできていない場合は条件確認"

    # アラート送信時刻を記録
    alert_state["last_quiet_alert"] = now.isoformat()
    alert_state["last_quiet_duration_min"] = round(quiet_minutes, 1)
    try:
        from src.utils.file_lock import atomic_write_json
        atomic_write_json(alert_state_path, alert_state)
    except Exception as e:
        logger.warning("fallback_alert_state (quiet) 保存失敗: %s", e)

    quiet_start_str = quiet_start.strftime("%H:%M UTC")
    suffix = "+" if archive_extended else ""
    msg_lines = [
        f"スパイクなし静観 継続 {quiet_minutes:.0f}分 ({quiet_count}{suffix}サイクル)",
        f"開始: {quiet_start_str}",
    ]
    if diagnosis_lines:
        msg_lines.append("現在のvol_ratio状況:")
        msg_lines.extend(diagnosis_lines)
    msg_lines.append(f"推奨: {action}")

    msg = "\n".join(msg_lines)
    logger.warning("Quiet fallback alert: %s", msg.replace("\n", " | "))
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
        # fallback アラートは即時 Telegram 送信 (他のアラートとは独立して通知)
        send_message(f"*Rubber Fallback Alert*\n{fallback_alert}")
        alerts.append(fallback_alert)

    # 4d. スパイクなし静観 長期継続アラート (60分超えで低ボラ診断を促進)
    quiet_alert = _check_quiet_fallback_duration(state_dir)
    if quiet_alert:
        send_message(f"*Rubber 低ボラ継続 Alert*\n{quiet_alert}")
        alerts.append(quiet_alert)

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
