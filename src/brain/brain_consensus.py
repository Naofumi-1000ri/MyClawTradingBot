"""Brain: ゴム戦略 (BTC RubberWall + ETH RubberBand + SOL RubberWall)。

ルールベースのスパイク検知 → シグナル生成。
LLM合議は使わない。
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from src.brain.build_context import build_context
from src.utils.config_loader import get_project_root, load_settings
from src.utils.file_lock import atomic_write_json, read_json
from src.utils.logger import setup_logger
from src.utils.retry import RetryExhausted, call_with_retry, enter_safe_hold

logger = setup_logger("brain_consensus")

ROOT = get_project_root()
STATE_DIR = ROOT / "state"
SIGNALS_DIR = ROOT / "signals"

# 連続失敗アラートの閾値
_AGENT_FAILURE_THRESHOLD = 3
_AGENT_FAILURE_STATE_PATH = STATE_DIR / "agent_failure_count.json"
_JOURNAL_DIR = ROOT / "journal"


def _track_agent_failure(failed: bool) -> None:
    """全戦略スキャン失敗を追跡し、3回連続失敗時にアラートを発行する。

    Args:
        failed: True=今サイクル失敗 (データ不足で全シンボルスキャン不可),
                False=正常 (少なくとも1シンボルスキャン完了)。
    """
    # 現在の失敗カウントを読み込む
    try:
        state = read_json(_AGENT_FAILURE_STATE_PATH)
        if not isinstance(state, dict):
            state = {}
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}

    consecutive = int(state.get("consecutive_failures", 0))

    if not failed:
        # 正常サイクル: カウントリセット
        if consecutive > 0:
            logger.info("agent_failure: reset (was %d consecutive failures)", consecutive)
        state["consecutive_failures"] = 0
        state["last_success"] = datetime.now(timezone.utc).isoformat()
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write_json(_AGENT_FAILURE_STATE_PATH, state)
        return

    # 失敗サイクル: カウントインクリメント
    consecutive += 1
    state["consecutive_failures"] = consecutive
    state["last_failure"] = datetime.now(timezone.utc).isoformat()
    logger.warning("agent_failure: consecutive=%d (threshold=%d)", consecutive, _AGENT_FAILURE_THRESHOLD)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(_AGENT_FAILURE_STATE_PATH, state)

    if consecutive >= _AGENT_FAILURE_THRESHOLD:
        # 初回 (==3) + 以降は2サイクルおきに繰り返しアラート (5, 7, 9... 回目)
        if consecutive == _AGENT_FAILURE_THRESHOLD or (consecutive % 2 == 1):
            _trigger_agent_failure_alert(consecutive)


def _trigger_agent_failure_alert(consecutive: int) -> None:
    """3回連続全戦略失敗: kill_switch.jsonにwarningフラグを立て、journalにCRITICALを記録。"""
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    # --- kill_switch.json に warning フラグを追加 ---
    ks_path = STATE_DIR / "kill_switch.json"
    try:
        ks = read_json(ks_path)
        if not isinstance(ks, dict):
            ks = {}
    except (FileNotFoundError, json.JSONDecodeError):
        ks = {}

    ks["warning"] = True
    ks["warning_reason"] = f"agent_failure: {consecutive}サイクル連続で全戦略スキャン失敗"
    ks["warning_at"] = now_iso
    atomic_write_json(ks_path, ks)
    logger.critical(
        "CRITICAL: agent_failure - %d consecutive cycles all strategies failed. "
        "Warning flag set in kill_switch.json.",
        consecutive,
    )

    # --- journal に CRITICAL エントリを追記 ---
    journal_path = _JOURNAL_DIR / f"{now.strftime('%Y-%m-%d')}.md"
    _JOURNAL_DIR.mkdir(parents=True, exist_ok=True)

    entry = (
        f"\n## CRITICAL: agent_failure ({now.strftime('%Y-%m-%d %H:%M UTC')})\n\n"
        f"- **連続失敗サイクル数**: {consecutive}\n"
        f"- **内容**: 全戦略 (BTC/ETH/SOL) がスキャン不能"
        f" (データ不足 / コンテキスト構築失敗 / ゴム戦略クラッシュのいずれか)\n"
        f"- **対処**: kill_switch.json に `warning=true` を設定済み\n"
        f"- **要確認**: データ収集 (data_collector.py) / API接続 / brain_consensus.py ログを確認すること\n"
    )

    try:
        existing = journal_path.read_text(encoding="utf-8") if journal_path.exists() else ""
        journal_path.write_text(existing + entry, encoding="utf-8")
        logger.info("agent_failure: CRITICAL journal entry written to %s", journal_path)
    except Exception as e:
        logger.error("agent_failure: failed to write journal: %s", e)

    # --- Telegram 通知 ---
    try:
        from src.monitor.telegram_notifier import send_message
        send_message(
            f"*CRITICAL: agent_failure*\n"
            f"{consecutive}サイクル連続で全戦略スキャン失敗。\n"
            f"データ収集 / API接続を確認してください。"
        )
    except Exception as e:
        logger.warning("agent_failure: telegram notification failed: %s", e)


def _load_json_safe(path: Path) -> dict | list | None:
    """JSONファイルを安全に読み込む。存在しなければNone。"""
    try:
        return read_json(path)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _fallback_output(symbols: list[str], reason: str) -> dict:
    """フォールバック出力（スパイクなし or エラー時）。"""
    return {
        "ooda": {
            "observe": f"Rubber fallback: {reason}",
            "orient": "シグナルなし → 安全側にフォールバック",
            "decide": "全銘柄hold",
        },
        "action_type": "hold",
        "signals": [
            {
                "symbol": s,
                "action": "hold",
                "confidence": 0.0,
                "entry_price": None,
                "stop_loss": None,
                "take_profit": None,
                "leverage": 3,
                "reasoning": f"Rubber fallback: {reason}",
            }
            for s in symbols
        ],
        "market_summary": f"Rubber fallback: {reason}",
        "journal_entry": f"Rubber fallback: {reason}",
        "self_assessment": "スパイク未検出。次サイクルで再スキャン。",
    }


def _check_rubber_exits(symbol: str, context: dict) -> list[dict]:
    """Rubber position の出口監視 (ETH/SOL共通)。

    state/{symbol}_rubber_meta.json を読み、SL到達 / TP到達 / 時間カットをチェック。
    close signal のリストを返す (0 or 1件)。
    """
    meta_path = STATE_DIR / f"{symbol.lower()}_rubber_meta.json"
    meta = _load_json_safe(meta_path)
    if not isinstance(meta, dict) or not meta.get("direction"):
        return []

    sym_data = context.get("market_data", {}).get(symbol, {})
    mid_price = float(sym_data.get("mid_price", 0) or 0)
    if mid_price <= 0:
        logger.warning("%s exit check: no mid price, skipping", symbol)
        return []

    direction = meta["direction"]
    sl_price = float(meta.get("stop_loss", 0))
    tp_price = float(meta.get("take_profit", 0))
    exit_mode = meta.get("exit_mode", "tp_sl")
    exit_bars = int(meta.get("exit_bars", 0))
    bar_count = int(meta.get("bar_count", 0))
    pattern = meta.get("pattern", "?")

    close_reason = None

    # SL check
    if sl_price > 0:
        if direction == "long" and mid_price <= sl_price:
            close_reason = f"SL hit: mid={mid_price:.4f} <= SL={sl_price:.4f}"
        elif direction == "short" and mid_price >= sl_price:
            close_reason = f"SL hit: mid={mid_price:.4f} >= SL={sl_price:.4f}"

    # TP check (tp_sl mode)
    if not close_reason and exit_mode == "tp_sl" and tp_price > 0:
        if direction == "long" and mid_price >= tp_price:
            close_reason = f"TP hit: mid={mid_price:.4f} >= TP={tp_price:.4f}"
        elif direction == "short" and mid_price <= tp_price:
            close_reason = f"TP hit: mid={mid_price:.4f} <= TP={tp_price:.4f}"

    # Time-cut check (time_cut mode)
    if not close_reason and exit_mode == "time_cut" and exit_bars > 0:
        bar_count += 1
        meta["bar_count"] = bar_count
        if bar_count >= exit_bars:
            close_reason = f"Time cut: {bar_count}/{exit_bars} bars"
        else:
            atomic_write_json(meta_path, meta)
            logger.info("%s %s: bar %d/%d (time_cut pending, mid=%.4f, SL=%.4f)",
                        symbol, pattern, bar_count, exit_bars, mid_price, sl_price)
            return []

    if close_reason:
        logger.info("%s EXIT (%s): %s", symbol, pattern, close_reason)
        # メタファイルは executor の close 成功後に削除 (_clear_rubber_meta)。
        return [{
            "symbol": symbol,
            "action": "close",
            "direction": "close",
            "confidence": 1.0,
            "reasoning": f"{symbol}Rubber exit ({pattern}): {close_reason}",
            "zone": "exit",
            "pattern": pattern,
        }]

    logger.info("%s %s: holding (mid=%.4f, SL=%.4f, exit_mode=%s)",
                symbol, pattern, mid_price, sl_price, exit_mode)
    return []


# Backward compatibility wrapper
def _check_eth_rubber_exits(context: dict) -> list[dict]:
    return _check_rubber_exits("ETH", context)


def _has_rubber_position(symbol: str) -> bool:
    """Rubber meta が存在するか (ポジション有無の判定)。"""
    meta = _load_json_safe(STATE_DIR / f"{symbol.lower()}_rubber_meta.json")
    return isinstance(meta, dict) and bool(meta.get("direction"))


def _has_eth_rubber_position() -> bool:
    return _has_rubber_position("ETH")


def _run_rubber_wall(settings: dict, context: dict) -> bool:
    """ゴム戦略実行。BTC RubberWall + ETH RubberBand + SOL RubberWall を並列スキャン。

    閾値キャッシュ方式: 前サイクルで次の足の閾値volumeを事前計算済み。
    キャッシュヒット時は O(1) で判定完了。

    Returns:
        True=スキャン完全失敗 (全銘柄データ不足), False=少なくとも1銘柄スキャン完了。
    """
    from src.strategy.btc_rubber_wall import BtcRubberWall
    from src.strategy.eth_rubber_band import EthRubberBand
    from src.strategy.sol_rubber_wall import SolRubberWall

    strategy_cfg = settings.get("strategy", {})
    signals_list = []

    # --- BTC RubberWall ---
    # 1) 既存ポジションの exit 監視 (SL/TP)
    btc_exit_signals = _check_rubber_exits("BTC", context)
    signals_list.extend(btc_exit_signals)

    # 2) 新規シグナルスキャン
    has_btc_pos = _has_rubber_position("BTC")

    rw_config = strategy_cfg.get("rubber_wall", {})
    btc_5m = context.get("market_data", {}).get("BTC", {}).get("candles_5m", [])

    # スキャン失敗カウント (データ不足で戦略を実行できなかった銘柄数)
    scan_failed_count = 0

    if btc_5m:
        cache_path = STATE_DIR / "rubber_wall_cache.json"
        cache = _load_json_safe(cache_path)

        logger.info("RubberWall BTC: scanning %d 5m candles (cache=%s)",
                     len(btc_5m), "hit" if cache else "cold")

        btc_signal, btc_next_cache = BtcRubberWall(btc_5m, rw_config).scan(cache)

        if btc_next_cache:
            atomic_write_json(cache_path, btc_next_cache)

        if btc_signal:
            if has_btc_pos:
                logger.info("RubberWall BTC: signal %s but position already open, skip",
                            btc_signal.get("zone"))
            elif btc_exit_signals:
                logger.info("RubberWall BTC: signal %s but exit in progress, skip",
                            btc_signal.get("zone"))
            else:
                signals_list.append(btc_signal)
                _log_rubber_signal(btc_signal)
                logger.info("RubberWall BTC: %s (zone=%s, vr=%.1f)",
                            btc_signal["direction"], btc_signal.get("zone"), btc_signal.get("vol_ratio"))
        else:
            logger.info("RubberWall BTC: no spike → hold")
    else:
        logger.warning("No BTC 5m candles available")
        scan_failed_count += 1

    # --- ETH RubberBand ---
    # 1) 既存ポジションの exit 監視 (SL/TP/時間カット)
    eth_exit_signals = _check_rubber_exits("ETH", context)
    signals_list.extend(eth_exit_signals)

    # 2) 新規シグナルスキャン (ポジションがなければ)
    has_eth_pos = _has_rubber_position("ETH")

    rb_config = strategy_cfg.get("rubber_band", {})
    eth_5m = context.get("market_data", {}).get("ETH", {}).get("candles_5m", [])

    if eth_5m:
        cache_path = STATE_DIR / "rubber_band_cache.json"
        cache = _load_json_safe(cache_path)

        logger.info("RubberBand ETH: scanning %d 5m candles (cache=%s)",
                     len(eth_5m), "hit" if cache else "cold")

        eth_signal, eth_next_cache = EthRubberBand(eth_5m, rb_config).scan(cache)

        if eth_next_cache:
            atomic_write_json(cache_path, eth_next_cache)

        if eth_signal:
            if has_eth_pos:
                logger.info("RubberBand ETH: signal %s but position already open, skip",
                            eth_signal.get("pattern"))
            elif eth_exit_signals:
                logger.info("RubberBand ETH: signal %s but exit in progress, skip",
                            eth_signal.get("pattern"))
            else:
                signals_list.append(eth_signal)
                _log_rubber_signal(eth_signal)
                logger.info("RubberBand ETH: %s %s (pattern=%s, vr=%.1f)",
                            eth_signal["direction"], eth_signal["symbol"],
                            eth_signal.get("pattern"), eth_signal.get("vol_ratio"))
        else:
            logger.info("RubberBand ETH: no spike → hold")
    else:
        logger.warning("No ETH 5m candles available")
        scan_failed_count += 1

    # --- SOL RubberWall ---
    # 1) 既存ポジションの exit 監視
    sol_exit_signals = _check_rubber_exits("SOL", context)
    signals_list.extend(sol_exit_signals)

    # 2) 新規シグナルスキャン
    has_sol_pos = _has_rubber_position("SOL")

    sol_rw_config = strategy_cfg.get("sol_rubber_wall", {})
    sol_5m = context.get("market_data", {}).get("SOL", {}).get("candles_5m", [])
    sol_funding_rate = context.get("market_data", {}).get("SOL", {}).get("funding_rate", 0.0)

    if sol_5m:
        cache_path = STATE_DIR / "sol_rubber_wall_cache.json"
        cache = _load_json_safe(cache_path)

        # funding_rate をconfigに注入してSolRubberWall側でフィルタリング可能にする
        sol_rw_config_with_funding = dict(sol_rw_config)
        sol_rw_config_with_funding["current_funding_rate"] = sol_funding_rate

        logger.info("RubberWall SOL: scanning %d 5m candles (cache=%s, funding=%.2e)",
                     len(sol_5m), "hit" if cache else "cold", sol_funding_rate)

        sol_signal, sol_next_cache = SolRubberWall(sol_5m, sol_rw_config_with_funding).scan(cache)

        if sol_next_cache:
            atomic_write_json(cache_path, sol_next_cache)

        if sol_signal:
            if has_sol_pos:
                logger.info("RubberWall SOL: signal %s but position already open, skip",
                            sol_signal.get("zone"))
            elif sol_exit_signals:
                logger.info("RubberWall SOL: signal %s but exit in progress, skip",
                            sol_signal.get("zone"))
            else:
                signals_list.append(sol_signal)
                _log_rubber_signal(sol_signal)
                logger.info("RubberWall SOL: %s (zone=%s, vr=%.1f)",
                            sol_signal["direction"], sol_signal.get("zone"),
                            sol_signal.get("vol_ratio"))
        else:
            logger.info("RubberWall SOL: no spike → hold")
    else:
        logger.warning("No SOL 5m candles available")
        scan_failed_count += 1

    # --- 統合出力 ---
    if signals_list:
        merged = _signals_to_merged(signals_list)
    else:
        all_symbols = ["BTC", "ETH", "SOL"]
        merged = _fallback_output(all_symbols, "スパイクなし: 静観")

    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(SIGNALS_DIR / "signals.json", merged)
    logger.info("=== Rubber Complete: action_type=%s, signals=%d, scan_failed=%d/3 ===",
                merged.get("action_type"), len(signals_list), scan_failed_count)

    # --- 連続失敗追跡 ---
    # 全3銘柄のキャンドルデータが揃わない場合を「スキャン完全失敗」と判定
    # (部分的なデータ欠損は警告のみ、全滅時だけカウント)
    all_scan_failed = (scan_failed_count >= 3)
    if scan_failed_count > 0:
        logger.warning("agent_failure: %d/3 symbols had no candle data this cycle", scan_failed_count)
    return all_scan_failed


def _signals_to_merged(signals: list[dict]) -> dict:
    """複数シグナルを signals.json 形式に変換。"""
    summaries = []
    sig_list = []
    for sig in signals:
        action = sig.get("direction", "hold")
        symbol = sig.get("symbol", "?")
        summaries.append(f"{action} {symbol} ({sig.get('zone', '?')})")
        sig_entry = {
            "symbol": symbol,
            "action": action,
            "confidence": sig.get("confidence", 0.85),
            "entry_price": sig.get("entry_price"),
            "stop_loss": sig.get("stop_loss"),
            "take_profit": sig.get("take_profit"),
            "leverage": sig.get("leverage", 3),
            "reasoning": sig.get("reasoning", ""),
        }
        # Rubber metadata → executor が position meta 保存に使用
        for key in ("exit_mode", "exit_bars", "pattern", "zone", "vol_ratio", "spike_time"):
            if key in sig:
                sig_entry[key] = sig[key]
        sig_list.append(sig_entry)

    reasons = [s.get("reasoning", "") for s in signals]
    return {
        "ooda": {
            "observe": "Rubber: " + "; ".join(reasons),
            "orient": ", ".join(summaries),
            "decide": ", ".join(summaries),
        },
        "action_type": "trade",
        "signals": sig_list,
        "market_summary": "Rubber: " + ", ".join(summaries),
        "journal_entry": "\n".join(reasons),
        "self_assessment": "Rubber forward test",
    }


def _log_rubber_signal(signal: dict) -> None:
    """state/rubber_signal_log.json にシグナルを追記。"""
    log_path = STATE_DIR / "rubber_signal_log.json"
    try:
        logs = read_json(log_path)
        if not isinstance(logs, list):
            logs = []
    except (FileNotFoundError, json.JSONDecodeError):
        logs = []

    logs.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **signal,
    })
    logs = logs[-200:]

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(log_path, logs)


_EQUITY_MIN_USD = 50.0
_EQUITY_MAX_USD = 10_000.0


def _sanitize_equity_in_context(context: dict) -> None:
    """daily_pnl.equityの異常値を検出し、前回値(daily_pnl.json)にフォールバックする。

    4.10 USD などの間欠的な異常値がAgent Rのリスク判断を歪める問題への対処。
    検証範囲: 50 USD <= equity <= 10,000 USD
    範囲外の場合: daily_pnl.jsonの値を維持し、contextを上書きする。
    """
    daily_pnl = context.get("daily_pnl")
    if not isinstance(daily_pnl, dict):
        return

    raw_equity = daily_pnl.get("equity")
    try:
        equity = float(raw_equity)
    except (TypeError, ValueError):
        logger.warning("equity sanity: unparseable equity=%r, skipping check", raw_equity)
        return

    if _EQUITY_MIN_USD <= equity <= _EQUITY_MAX_USD:
        return  # 正常範囲

    # 異常値 → daily_pnl.jsonから前回値を読み直す
    logger.warning(
        "equity sanity: ABNORMAL equity=%.2f USD (range %.0f-%.0f), loading fallback from daily_pnl.json",
        equity, _EQUITY_MIN_USD, _EQUITY_MAX_USD,
    )
    persisted = _load_json_safe(STATE_DIR / "daily_pnl.json")
    if not isinstance(persisted, dict):
        logger.error("equity sanity: cannot load daily_pnl.json for fallback")
        return

    fallback_equity = persisted.get("equity")
    try:
        fallback_equity = float(fallback_equity)
    except (TypeError, ValueError):
        logger.error("equity sanity: fallback equity also invalid=%r", fallback_equity)
        return

    if _EQUITY_MIN_USD <= fallback_equity <= _EQUITY_MAX_USD:
        context["daily_pnl"]["equity"] = fallback_equity
        logger.info(
            "equity sanity: fallback applied %.2f -> %.2f USD",
            equity, fallback_equity,
        )
    else:
        # start_of_day_equityを試みる
        start_equity = persisted.get("start_of_day_equity")
        try:
            start_equity = float(start_equity)
            if _EQUITY_MIN_USD <= start_equity <= _EQUITY_MAX_USD:
                context["daily_pnl"]["equity"] = start_equity
                logger.warning(
                    "equity sanity: using start_of_day_equity %.2f as last resort",
                    start_equity,
                )
                return
        except (TypeError, ValueError):
            pass
        logger.error(
            "equity sanity: both current=%.2f and persisted=%.2f are out of range; "
            "leaving context unchanged",
            equity, fallback_equity,
        )


def main() -> None:
    """メイン実行: コンテキスト構築 → ゴム戦略 (BTC+ETH+SOL) → 出力。

    各フェーズはリトライ付きで実行される。
    最大リトライ回数を超えた場合は安全なホールド状態に移行し、Telegramアラートを発報する。
    """
    settings = load_settings()
    symbols = settings.get("trading", {}).get("symbols", ["BTC", "ETH"])

    # 1. コンテキスト構築 (最大2回リトライ: 計3回試行)
    logger.info("[1/2] Building context (with retry)...")
    context = None
    try:
        context = call_with_retry(
            build_context,
            max_retries=2,
            base_delay=3.0,
            backoff_factor=2.0,
            max_delay=15.0,
            operation_name="コンテキスト構築",
        )
        context_path = ROOT / "data" / "context.json"
        atomic_write_json(context_path, context)
        logger.info("Context built: %s", context_path)
    except RetryExhausted as e:
        logger.error("Context build exhausted retries: %s", e)
        _write_fallback_and_exit(symbols, f"コンテキスト構築失敗 (リトライ上限超過): {e.last_error}")
        _track_agent_failure(failed=True)
        enter_safe_hold(f"brain_consensus: コンテキスト構築リトライ上限超過 ({e.last_error})")
        return
    except Exception as e:
        logger.error("Context build failed: %s", e)
        _write_fallback_and_exit(symbols, f"コンテキスト構築失敗: {e}")
        _track_agent_failure(failed=True)
        return

    _sanitize_equity_in_context(context)

    # 2. ゴム戦略 (BTC RubberWall + ETH RubberBand) (最大2回リトライ: 計3回試行)
    logger.info("[2/2] Running rubber strategies (with retry)...")
    try:
        all_scan_failed: bool = call_with_retry(
            _run_rubber_wall,
            args=(settings, context),
            max_retries=2,
            base_delay=3.0,
            backoff_factor=2.0,
            max_delay=15.0,
            operation_name="ゴム戦略",
        )
        # 正常完了: 戻り値で失敗/成功を判定
        _track_agent_failure(failed=all_scan_failed)
    except RetryExhausted as e:
        logger.error("Rubber strategy exhausted retries: %s", e)
        _write_fallback_and_exit(symbols, f"ゴム戦略クラッシュ (リトライ上限超過): {e.last_error}")
        _track_agent_failure(failed=True)
        enter_safe_hold(f"brain_consensus: ゴム戦略リトライ上限超過 ({e.last_error})")
    except Exception as e:
        logger.error("Rubber strategy crashed: %s", e)
        _write_fallback_and_exit(symbols, f"ゴム戦略クラッシュ: {e}")
        _track_agent_failure(failed=True)


def _write_fallback_and_exit(symbols: list[str], reason: str) -> None:
    """フォールバック出力を書き込む。"""
    fallback = _fallback_output(symbols, reason)
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(SIGNALS_DIR / "signals.json", fallback)
    logger.warning("Fallback output written: %s", reason)


if __name__ == "__main__":
    main()
