"""Brain: ゴム戦略 (BTC RubberWall + ETH RubberBand + SOL RubberWall)。

ルールベースのスパイク検知 → シグナル生成。
LLM合議は使わない。
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from src.brain.build_context import build_context
from src.strategy.wave_rider import WaveRider
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
        # メタがない場合でも実際にポジションがあれば警告 (meta 保存失敗の検知)
        if _has_live_position(symbol):
            logger.warning(
                "%s: live position exists but rubber meta missing! "
                "Entry may have occurred without meta save. Emitting hold_position to prevent fallback.",
                symbol,
            )
            return [{
                "symbol": symbol,
                "action": "hold_position",
                "direction": "hold_position",
                "confidence": 1.0,
                "reasoning": (
                    f"{symbol}Rubber holding (meta-less): live position detected, "
                    f"meta file missing. Manual close may be required."
                ),
                "zone": "holding",
                "pattern": "unknown",
            }]
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
            # ポジション保有継続中: fallback出力を防ぐため「hold_position」シグナルを返す
            return [{
                "symbol": symbol,
                "action": "hold_position",
                "direction": "hold_position",
                "confidence": 1.0,
                "reasoning": (
                    f"{symbol}Rubber holding ({pattern}): bar {bar_count}/{exit_bars}, "
                    f"mid={mid_price:.4f}, SL={sl_price:.4f}"
                ),
                "zone": "holding",
                "pattern": pattern,
            }]

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

    # tp_sl モードでSL/TP未達: ポジション保有継続
    logger.info("%s %s: holding (mid=%.4f, SL=%.4f, exit_mode=%s)",
                symbol, pattern, mid_price, sl_price, exit_mode)
    # ポジション保有継続中: fallback出力を防ぐため「hold_position」シグナルを返す
    return [{
        "symbol": symbol,
        "action": "hold_position",
        "direction": "hold_position",
        "confidence": 1.0,
        "reasoning": (
            f"{symbol}Rubber holding ({pattern}): mid={mid_price:.4f}, "
            f"SL={sl_price:.4f}, TP={tp_price:.4f} ({exit_mode})"
        ),
        "zone": "holding",
        "pattern": pattern,
    }]


# Backward compatibility wrapper
def _check_eth_rubber_exits(context: dict) -> list[dict]:
    return _check_rubber_exits("ETH", context)


def _has_live_position(symbol: str) -> bool:
    """state/positions.json に実際のポジションが存在するか確認。

    meta ファイルと positions.json の両方をチェックすることで、
    meta 保存失敗時でも誤エントリーを防ぐ二重ガードとして機能する。
    """
    positions = _load_json_safe(STATE_DIR / "positions.json")
    if not isinstance(positions, list):
        return False
    return any(
        isinstance(p, dict) and p.get("symbol") == symbol and float(p.get("size", 0)) != 0
        for p in positions
    )


def _has_rubber_position(symbol: str) -> bool:
    """Rubber meta またはライブポジションが存在するか (ポジション有無の判定)。

    meta ファイルが存在する場合は meta を優先。
    meta がなくても positions.json にポジションがあれば True を返し、
    誤重複エントリーを防ぐ (meta 保存失敗のフォールバック)。
    """
    meta = _load_json_safe(STATE_DIR / f"{symbol.lower()}_rubber_meta.json")
    if isinstance(meta, dict) and bool(meta.get("direction")):
        return True
    # meta がない場合でも実際のポジションがあれば True
    return _has_live_position(symbol)


def _has_eth_rubber_position() -> bool:
    return _has_rubber_position("ETH")


def _compute_btc_atr_ratio(
    sym_data: dict,
    short_window: int = 24,
    long_window: int = 288,
) -> tuple[float, str]:
    """5m candles から短期/長期ATR比率を計算する (WaveRider 適応型SL用)。

    Args:
        sym_data: context["market_data"]["BTC"]
        short_window: 短期ATR窓 (デフォルト24本 = 2h)
        long_window: 長期ATR窓 (デフォルト288本 = 24h)

    Returns:
        (atr_ratio, label)
          atr_ratio: short_atr / long_atr (高ボラ>1 / 低ボラ<1)
          label: "high_vol" / "low_vol" / "normal"
    """
    candles = sym_data.get("candles_5m", [])
    if not candles or len(candles) < short_window:
        return 1.0, "normal"

    def _atr(chunk: list) -> float:
        total = sum(float(c.get("h", 0)) - float(c.get("l", 0)) for c in chunk)
        return total / len(chunk) if chunk else 0.0

    short_chunk = candles[-short_window:]
    long_chunk = candles[-long_window:] if len(candles) >= long_window else candles

    short_atr = _atr(short_chunk)
    long_atr = _atr(long_chunk)

    if long_atr <= 0 or short_atr <= 0:
        return 1.0, "normal"

    ratio = short_atr / long_atr
    if ratio > 1.5:
        label = "high_vol"
    elif ratio < 0.7:
        label = "low_vol"
    else:
        label = "normal"
    return ratio, label


def _run_wave_rider_btc(settings: dict, context: dict) -> list[dict]:
    """Wave Rider BTC: US Open 1h bar momentum + post-session reversion.

    Lifecycle:
      UTC 15:00: Observe bar (14:00-15:00) → decide entry → write meta
      UTC 15:05-19:55: SL monitoring
      UTC 20:00: Time stop → close, optionally trigger reversion pending
      UTC 20:15: Reversion SHORT entry (if pending)
      Overnight: SL/TP monitoring for reversion
      UTC 08:00: Reversion time stop

    State files:
      state/btc_wave_rider_meta.json — active position tracking
      state/btc_wr_rev_pending.json — reversion entry pending (15min delay)

    Returns:
        List of signals (0-2 items: hold_position, entry, close).
    """
    wr_config = settings.get("strategy", {}).get("wave_rider", {})
    if not wr_config.get("enabled", False):
        return []

    now = datetime.now(timezone.utc)
    hour = now.hour
    weekday = now.weekday()  # 0=Mon, 6=Sun

    wr = WaveRider(wr_config)
    signals = []

    sym_data = context.get("market_data", {}).get("BTC", {})
    mid_price = float(sym_data.get("mid_price", 0) or 0)
    if mid_price <= 0:
        logger.warning("WaveRider BTC: no mid price, skipping")
        return []

    meta_path = STATE_DIR / "btc_wave_rider_meta.json"
    pending_path = STATE_DIR / "btc_wr_rev_pending.json"
    meta = _load_json_safe(meta_path)
    if not isinstance(meta, dict) or not meta.get("phase"):
        meta = None

    # ── 1. Exit monitoring (existing position) ──
    if meta:
        phase = meta["phase"]
        direction = meta.get("direction", "long")
        sl_price = float(meta.get("stop_loss", 0))
        pattern = meta.get("pattern", "?")

        if phase == "wave_rider":
            # SL check
            sl_hit = False
            if direction == "long" and mid_price <= sl_price:
                sl_hit = True
            elif direction == "short" and mid_price >= sl_price:
                sl_hit = True

            if sl_hit:
                logger.info("WaveRider BTC: SL hit (%s) mid=%.2f SL=%.2f", pattern, mid_price, sl_price)
                signals.append({
                    "symbol": "BTC",
                    "action": "close",
                    "direction": "close",
                    "confidence": 1.0,
                    "reasoning": f"WaveRider SL hit ({pattern}): mid={mid_price:.2f} vs SL={sl_price:.2f}",
                    "zone": "exit",
                    "pattern": pattern,
                })
                # Clear meta
                try:
                    meta_path.unlink()
                except FileNotFoundError:
                    pass
                return signals

            # Time stop: hour >= 20
            if hour >= 20:
                logger.info("WaveRider BTC: time stop (%s) at hour=%d, mid=%.2f", pattern, hour, mid_price)
                signals.append({
                    "symbol": "BTC",
                    "action": "close",
                    "direction": "close",
                    "confidence": 1.0,
                    "reasoning": f"WaveRider time stop ({pattern}): hour={hour}, mid={mid_price:.2f}",
                    "zone": "exit",
                    "pattern": pattern,
                })

                # Check reversion trigger (up_large only)
                if (
                    pattern == "wr_up_large"
                    and wr_config.get("reversion_enabled", False)
                ):
                    observe_open = float(meta.get("observe_bar_open", 0))
                    if observe_open > 0 and wr.should_trigger_reversion(observe_open, mid_price):
                        deviation = (mid_price - observe_open) / observe_open
                        entry_after = now.replace(minute=0, second=0, microsecond=0)
                        # 15min delay from now to avoid entry cooldown
                        from datetime import timedelta
                        entry_after = now + timedelta(minutes=15)
                        pending_data = {
                            "pattern": "wr_up_large_rev",
                            "us_close_price": mid_price,
                            "deviation": round(deviation, 6),
                            "entry_after": entry_after.isoformat(),
                        }
                        STATE_DIR.mkdir(parents=True, exist_ok=True)
                        atomic_write_json(pending_path, pending_data)
                        logger.info(
                            "WaveRider BTC: reversion pending written (dev=%.4f, entry_after=%s)",
                            deviation, entry_after.isoformat(),
                        )

                # Clear WR meta
                try:
                    meta_path.unlink()
                except FileNotFoundError:
                    pass
                return signals

            # Holding — not SL, not time stop
            # Adaptive SL: compute volatility-adaptive trailing stop
            entry_price = float(meta.get("entry_price", 0) or mid_price)
            atr_ratio, vol_label = _compute_btc_atr_ratio(sym_data)
            new_sl, adapt_label = wr.compute_adaptive_sl(
                entry_price, mid_price, sl_price, direction, atr_ratio
            )
            sl_updated = False
            if abs(new_sl - sl_price) > 0.01:
                meta["stop_loss"] = new_sl
                STATE_DIR.mkdir(parents=True, exist_ok=True)
                atomic_write_json(meta_path, meta)
                sl_updated = True
                logger.info(
                    "WaveRider BTC: adaptive SL updated %s %.2f→%.2f (%s, atr_ratio=%.2f)",
                    direction, sl_price, new_sl, adapt_label, atr_ratio,
                )
                sl_price = new_sl

            logger.info(
                "WaveRider BTC: holding %s (%s) mid=%.2f SL=%.2f [%s%s]",
                direction, pattern, mid_price, sl_price, adapt_label,
                " SL_updated" if sl_updated else "",
            )
            return [{
                "symbol": "BTC",
                "action": "hold_position",
                "direction": "hold_position",
                "confidence": 1.0,
                "reasoning": (
                    f"WaveRider holding ({pattern}): mid={mid_price:.2f}, SL={sl_price:.2f} "
                    f"[adaptive: {adapt_label}]"
                ),
                "zone": "holding",
                "pattern": pattern,
            }]

        elif phase == "reversion":
            tp_price = float(meta.get("take_profit", 0))

            # SL check (reversion is always SHORT)
            if mid_price >= sl_price:
                logger.info("WaveRider REV: SL hit mid=%.2f >= SL=%.2f", mid_price, sl_price)
                signals.append({
                    "symbol": "BTC",
                    "action": "close",
                    "direction": "close",
                    "confidence": 1.0,
                    "reasoning": f"WaveRider REV SL hit ({pattern}): mid={mid_price:.2f} vs SL={sl_price:.2f}",
                    "zone": "exit",
                    "pattern": pattern,
                })
                try:
                    meta_path.unlink()
                except FileNotFoundError:
                    pass
                return signals

            # TP check (SHORT: price <= tp)
            if tp_price > 0 and mid_price <= tp_price:
                logger.info("WaveRider REV: TP hit mid=%.2f <= TP=%.2f", mid_price, tp_price)
                signals.append({
                    "symbol": "BTC",
                    "action": "close",
                    "direction": "close",
                    "confidence": 1.0,
                    "reasoning": f"WaveRider REV TP hit ({pattern}): mid={mid_price:.2f} vs TP={tp_price:.2f}",
                    "zone": "exit",
                    "pattern": pattern,
                })
                try:
                    meta_path.unlink()
                except FileNotFoundError:
                    pass
                return signals

            # Reversion time stop: UTC 08:00-14:00
            if 8 <= hour < 14:
                logger.info("WaveRider REV: time stop at hour=%d, mid=%.2f", hour, mid_price)
                signals.append({
                    "symbol": "BTC",
                    "action": "close",
                    "direction": "close",
                    "confidence": 1.0,
                    "reasoning": f"WaveRider REV time stop ({pattern}): hour={hour}, mid={mid_price:.2f}",
                    "zone": "exit",
                    "pattern": pattern,
                })
                try:
                    meta_path.unlink()
                except FileNotFoundError:
                    pass
                return signals

            # Holding reversion — adaptive SL for SHORT
            rev_entry_price = float(meta.get("entry_price", 0) or mid_price)
            atr_ratio, _vol_label = _compute_btc_atr_ratio(sym_data)
            new_sl, adapt_label = wr.compute_adaptive_sl(
                rev_entry_price, mid_price, sl_price, "short", atr_ratio
            )
            if abs(new_sl - sl_price) > 0.01:
                meta["stop_loss"] = new_sl
                STATE_DIR.mkdir(parents=True, exist_ok=True)
                atomic_write_json(meta_path, meta)
                logger.info(
                    "WaveRider REV: adaptive SL updated %.2f→%.2f (%s)",
                    sl_price, new_sl, adapt_label,
                )
                sl_price = new_sl

            logger.info("WaveRider REV: holding SHORT (%s) mid=%.2f SL=%.2f TP=%.2f [%s]",
                        pattern, mid_price, sl_price, tp_price, adapt_label)
            return [{
                "symbol": "BTC",
                "action": "hold_position",
                "direction": "hold_position",
                "confidence": 1.0,
                "reasoning": (
                    f"WaveRider REV holding ({pattern}): mid={mid_price:.2f}, "
                    f"SL={sl_price:.2f}, TP={tp_price:.2f} [adaptive: {adapt_label}]"
                ),
                "zone": "holding",
                "pattern": pattern,
            }]

    # ── 2. Reversion pending check ──
    pending = _load_json_safe(pending_path)
    if isinstance(pending, dict) and pending.get("entry_after"):
        entry_after = datetime.fromisoformat(pending["entry_after"])
        if now >= entry_after:
            # Emit reversion SHORT entry
            rev_sl = wr.compute_rev_sl(mid_price)
            rev_tp = wr.compute_rev_tp(mid_price)
            pattern = pending.get("pattern", "wr_up_large_rev")
            deviation = pending.get("deviation", 0)

            logger.info(
                "WaveRider REV: entering SHORT at %.2f (dev=%.4f, TP=%.2f, SL=%.2f)",
                mid_price, deviation, rev_tp, rev_sl,
            )

            signals.append({
                "symbol": "BTC",
                "action": "short",
                "direction": "short",
                "confidence": 0.80,
                "entry_price": mid_price,
                "stop_loss": rev_sl,
                "leverage": 3,
                "reasoning": f"WaveRider REV entry ({pattern}): dev={deviation:.4f}, TP={rev_tp:.2f}, SL={rev_sl:.2f}",
                "zone": "wave_rider_rev",
                "pattern": pattern,
            })

            # Write reversion meta
            rev_meta = {
                "phase": "reversion",
                "pattern": pattern,
                "direction": "short",
                "entry_price": mid_price,
                "stop_loss": rev_sl,
                "take_profit": rev_tp,
                "deviation": deviation,
                "entry_time": now.isoformat(),
            }
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            atomic_write_json(meta_path, rev_meta)

            # Delete pending file
            try:
                pending_path.unlink()
            except FileNotFoundError:
                pass
            return signals
        else:
            logger.info(
                "WaveRider REV: pending, waiting until %s (now=%s)",
                pending["entry_after"], now.isoformat(),
            )
            # pending中もhold_positionを返しfallbackを防ぐ
            pattern = pending.get("pattern", "wr_up_large_rev")
            return [{
                "symbol": "BTC",
                "action": "hold_position",
                "direction": "hold_position",
                "confidence": 1.0,
                "reasoning": (
                    f"WaveRider REV pending ({pattern}): "
                    f"waiting until {pending['entry_after']}"
                ),
                "zone": "holding",
                "pattern": pattern,
            }]

    # ── 3. New WR entry (no meta, no pending, weekday, hour == 15) ──
    if weekday >= 5:
        logger.info("WaveRider BTC: weekend (weekday=%d), skip", weekday)
        return []

    if hour != 15:
        return []

    # Find the UTC 14:00-15:00 1h candle
    candles_1h = sym_data.get("candles_1h", [])
    if not candles_1h:
        logger.warning("WaveRider BTC: no 1h candles available")
        return []

    observe_bar = None
    for c in candles_1h:
        bar_time = c.get("t") or c.get("time") or c.get("timestamp")
        if bar_time is None:
            continue
        # Handle both ISO string and epoch ms
        if isinstance(bar_time, str):
            try:
                bt = datetime.fromisoformat(bar_time.replace("Z", "+00:00"))
            except ValueError:
                continue
        else:
            bt = datetime.fromtimestamp(int(bar_time) / 1000, tz=timezone.utc)
        if bt.hour == 14 and bt.date() == now.date():
            observe_bar = c
            break

    if observe_bar is None:
        logger.info("WaveRider BTC: UTC 14:00 bar not found in 1h candles")
        return []

    bar_open = float(observe_bar.get("o") or observe_bar.get("open", 0))
    bar_close = float(observe_bar.get("c") or observe_bar.get("close", 0))
    if bar_open <= 0 or bar_close <= 0:
        logger.warning("WaveRider BTC: invalid bar OHLC (open=%.2f, close=%.2f)", bar_open, bar_close)
        return []

    open_move = (bar_close - bar_open) / bar_open
    result = wr.decide_entry(open_move)

    if result is None:
        logger.info("WaveRider BTC: open_move=%.4f (%.2f%%), no entry", open_move, open_move * 100)
        return []

    direction, pattern, confidence = result
    entry_price = mid_price
    sl_price = wr.compute_sl(entry_price, direction)

    logger.info(
        "WaveRider BTC: ENTRY %s (%s) open_move=%.4f (%.2f%%), entry=%.2f, SL=%.2f",
        direction, pattern, open_move, open_move * 100, entry_price, sl_price,
    )

    signals.append({
        "symbol": "BTC",
        "action": direction,
        "direction": direction,
        "confidence": confidence,
        "entry_price": entry_price,
        "stop_loss": sl_price,
        "leverage": 3,
        "reasoning": (
            f"WaveRider entry ({pattern}): open_move={open_move:.4f} ({open_move * 100:.2f}%), "
            f"entry={entry_price:.2f}, SL={sl_price:.2f}"
        ),
        "zone": "wave_rider",
        "pattern": pattern,
    })

    # Write WR meta
    wr_meta = {
        "phase": "wave_rider",
        "pattern": pattern,
        "direction": direction,
        "entry_price": entry_price,
        "stop_loss": sl_price,
        "observe_bar_open": bar_open,
        "observe_bar_close": bar_close,
        "entry_time": now.isoformat(),
    }
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(meta_path, wr_meta)
    _log_rubber_signal(signals[-1])

    return signals


def _run_wave_rider_hype(settings: dict, context: dict) -> list[dict]:
    """Wave Rider HYPE: 木曜限定 US Open 1h bar momentum (BTC低相関ヘッジ).

    BTC木曜WRとのPnL相関 r=-0.82。BTC負け時にHYPE勝ちのヘッジ構造。
    リバージョンなし。Adaptive SLなし（シンプル運用）。

    State: state/hype_wave_rider_meta.json
    """
    wr_config = settings.get("strategy", {}).get("wave_rider_hype", {})
    if not wr_config.get("enabled", False):
        return []

    now = datetime.now(timezone.utc)
    hour = now.hour
    weekday = now.weekday()

    # 木曜限定チェック (新規エントリー時のみ。保有中は毎日監視)
    thursday_only = wr_config.get("thursday_only", True)

    wr = WaveRider(wr_config)
    signals = []

    sym_data = context.get("market_data", {}).get("HYPE", {})
    mid_price = float(sym_data.get("mid_price", 0) or 0)
    if mid_price <= 0:
        logger.warning("WaveRider HYPE: no mid price, skipping")
        return []

    meta_path = STATE_DIR / "hype_wave_rider_meta.json"
    meta = _load_json_safe(meta_path)
    if not isinstance(meta, dict) or not meta.get("phase"):
        meta = None

    # ── 1. Exit monitoring (既存ポジション) ──
    if meta and meta.get("phase") == "wave_rider":
        sl_price = float(meta.get("stop_loss", 0))
        direction = meta.get("direction", "long")
        pattern = meta.get("pattern", "unknown")

        # SL check
        if direction == "long" and mid_price <= sl_price:
            logger.info("WaveRider HYPE: SL hit %s mid=%.4f <= SL=%.4f", direction, mid_price, sl_price)
            signals.append({
                "symbol": "HYPE", "action": "close", "direction": "close",
                "confidence": 1.0,
                "reasoning": f"WaveRider HYPE SL hit ({pattern}): mid={mid_price:.4f} vs SL={sl_price:.4f}",
                "zone": "exit", "pattern": pattern,
            })
            try:
                meta_path.unlink()
            except FileNotFoundError:
                pass
            return signals

        if direction == "short" and mid_price >= sl_price:
            logger.info("WaveRider HYPE: SL hit %s mid=%.4f >= SL=%.4f", direction, mid_price, sl_price)
            signals.append({
                "symbol": "HYPE", "action": "close", "direction": "close",
                "confidence": 1.0,
                "reasoning": f"WaveRider HYPE SL hit ({pattern}): mid={mid_price:.4f} vs SL={sl_price:.4f}",
                "zone": "exit", "pattern": pattern,
            })
            try:
                meta_path.unlink()
            except FileNotFoundError:
                pass
            return signals

        # Time stop: hour >= 20
        if hour >= 20:
            logger.info("WaveRider HYPE: time stop (%s) at hour=%d, mid=%.4f", pattern, hour, mid_price)
            signals.append({
                "symbol": "HYPE", "action": "close", "direction": "close",
                "confidence": 1.0,
                "reasoning": f"WaveRider HYPE time stop ({pattern}): hour={hour}, mid={mid_price:.4f}",
                "zone": "exit", "pattern": pattern,
            })
            try:
                meta_path.unlink()
            except FileNotFoundError:
                pass
            return signals

        # Holding
        logger.info(
            "WaveRider HYPE: holding %s (%s) mid=%.4f SL=%.4f",
            direction, pattern, mid_price, sl_price,
        )
        return [{
            "symbol": "HYPE", "action": "hold_position", "direction": "hold_position",
            "confidence": 1.0,
            "reasoning": f"WaveRider HYPE holding ({pattern}): mid={mid_price:.4f}, SL={sl_price:.4f}",
            "zone": "holding", "pattern": pattern,
        }]

    # ── 2. New entry (木曜, hour == 15, no meta) ──
    if meta:
        return []

    if weekday >= 5:
        logger.info("WaveRider HYPE: weekend (weekday=%d), skip", weekday)
        return []

    if thursday_only and weekday != 3:
        return []

    if hour != 15:
        return []

    # Find the UTC 14:00-15:00 1h candle
    candles_1h = sym_data.get("candles_1h", [])
    if not candles_1h:
        logger.warning("WaveRider HYPE: no 1h candles available")
        return []

    observe_bar = None
    for c in candles_1h:
        bar_time = c.get("t") or c.get("time") or c.get("timestamp")
        if bar_time is None:
            continue
        if isinstance(bar_time, str):
            try:
                bt = datetime.fromisoformat(bar_time.replace("Z", "+00:00"))
            except ValueError:
                continue
        else:
            bt = datetime.fromtimestamp(int(bar_time) / 1000, tz=timezone.utc)
        if bt.hour == 14 and bt.date() == now.date():
            observe_bar = c
            break

    if observe_bar is None:
        logger.info("WaveRider HYPE: UTC 14:00 bar not found in 1h candles")
        return []

    bar_open = float(observe_bar.get("o") or observe_bar.get("open", 0))
    bar_close = float(observe_bar.get("c") or observe_bar.get("close", 0))
    if bar_open <= 0 or bar_close <= 0:
        logger.warning("WaveRider HYPE: invalid bar OHLC (open=%.4f, close=%.4f)", bar_open, bar_close)
        return []

    open_move = (bar_close - bar_open) / bar_open
    result = wr.decide_entry(open_move)

    if result is None:
        logger.info("WaveRider HYPE: open_move=%.4f (%.2f%%), no entry", open_move, open_move * 100)
        return []

    direction, pattern, confidence = result
    entry_price = mid_price
    sl_price = wr.compute_sl(entry_price, direction)

    logger.info(
        "WaveRider HYPE: ENTRY %s (%s) open_move=%.4f (%.2f%%), entry=%.4f, SL=%.4f",
        direction, pattern, open_move, open_move * 100, entry_price, sl_price,
    )

    signals.append({
        "symbol": "HYPE", "action": direction, "direction": direction,
        "confidence": confidence,
        "entry_price": entry_price,
        "stop_loss": sl_price,
        "leverage": 3,
        "reasoning": (
            f"WaveRider HYPE entry ({pattern}): open_move={open_move:.4f} ({open_move * 100:.2f}%), "
            f"entry={entry_price:.4f}, SL={sl_price:.4f}"
        ),
        "zone": "wave_rider",
        "pattern": pattern,
    })

    # Write meta
    wr_meta = {
        "phase": "wave_rider",
        "pattern": pattern,
        "direction": direction,
        "entry_price": entry_price,
        "stop_loss": sl_price,
        "observe_bar_open": bar_open,
        "observe_bar_close": bar_close,
        "entry_time": now.isoformat(),
    }
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(meta_path, wr_meta)
    _log_rubber_signal(signals[-1])

    return signals


def _run_rubber_wall(settings: dict, context: dict) -> bool:
    """ゴム戦略実行。BTC RubberWall + ETH RubberBand + SOL RubberWall を並列スキャン。

    閾値キャッシュ方式: 前サイクルで次の足の閾値volumeを事前計算済み。
    キャッシュヒット時は O(1) で判定完了。

    Returns:
        True=スキャン完全失敗 (全銘柄データ不足), False=少なくとも1銘柄スキャン完了。
    """
    # ──────────────────────────────────────────────────────────
    # 2026-02-21: ゴム戦略 スパイク系のみ復帰
    # - ISSUE-001対処: quiet系 (スパイクなしエントリー) は config で無効化済み
    #   (quiet_long_enabled: false, quiet_short_enabled: false)
    # - スパイク検知ベースの本来のゴム理論は継続稼働
    # - Wave Rider (BTC/HYPE) は時間ベースで独立稼働
    # ──────────────────────────────────────────────────────────
    RUBBER_NEW_ENTRY_ENABLED = True

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
    # スキャン失敗カウント (データ不足で戦略を実行できなかった銘柄数)
    scan_failed_count = 0

    if RUBBER_NEW_ENTRY_ENABLED:
        has_btc_pos = _has_rubber_position("BTC")
        rw_config = strategy_cfg.get("rubber_wall", {})
        btc_5m = context.get("market_data", {}).get("BTC", {}).get("candles_5m", [])

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
    else:
        logger.info("RubberWall BTC: new entry DISABLED (rubber_stopped 2026-02-21)")

    # --- BTC Wave Rider ---
    # ゴム停止後の代替戦略: US Open 1h bar momentum + post-session reversion
    # 独自meta (btc_wave_rider_meta.json) を使用 → executor/state_manager と干渉なし
    wr_signals = _run_wave_rider_btc(settings, context)
    signals_list.extend(wr_signals)
    if wr_signals:
        logger.info("WaveRider BTC: %d signal(s) emitted", len(wr_signals))

    # --- HYPE Wave Rider (木曜限定ヘッジ) ---
    # BTC木曜WRとPnL相関 r=-0.82。独自meta (hype_wave_rider_meta.json) を使用
    hype_wr_signals = _run_wave_rider_hype(settings, context)
    signals_list.extend(hype_wr_signals)
    if hype_wr_signals:
        logger.info("WaveRider HYPE: %d signal(s) emitted", len(hype_wr_signals))

    # --- ETH RubberBand ---
    # 1) 既存ポジションの exit 監視 (SL/TP/時間カット)
    eth_exit_signals = _check_rubber_exits("ETH", context)
    signals_list.extend(eth_exit_signals)

    # 2) 新規シグナルスキャン (ポジションがなければ)
    if RUBBER_NEW_ENTRY_ENABLED:
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
    else:
        logger.info("RubberBand ETH: new entry DISABLED (rubber_stopped 2026-02-21)")

    # --- SOL RubberWall ---
    # 1) 既存ポジションの exit 監視
    sol_exit_signals = _check_rubber_exits("SOL", context)
    signals_list.extend(sol_exit_signals)

    # 2) 新規シグナルスキャン
    if RUBBER_NEW_ENTRY_ENABLED:
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
    else:
        logger.info("RubberWall SOL: new entry DISABLED (rubber_stopped 2026-02-21)")

    # --- 統合出力 ---
    if signals_list:
        merged = _signals_to_merged(signals_list)
    elif not RUBBER_NEW_ENTRY_ENABLED:
        # 新規エントリー全停止中 (RUBBER_NEW_ENTRY_ENABLED=False):
        # exitもhold_positionもない → 監視待機状態
        all_symbols = ["BTC", "ETH", "SOL"]
        merged = _fallback_output(all_symbols, "新規エントリー停止中: 既存ポジションなし")
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


def _caps_leverage(confidence: float, sig_leverage: int | None, base: int = 3) -> int:
    """Confidence-Adaptive Position Sizing (CAPS): confidence に応じたレバレッジを返す。

    戦略側で既に設定済みの leverage を優先する。
    未設定 (None) の場合は confidence から推定してフォールバック。

    マッピング (base=3):
      confidence >= 0.80: 3x (スパイク系)
      confidence >= 0.74: 2x (中確信度 quiet)
      confidence <  0.74: 1x (低確信度 quiet)
    """
    if sig_leverage is not None:
        return int(sig_leverage)
    # leverageが未指定の場合: confidenceから推定
    if confidence >= 0.80:
        return base
    elif confidence >= 0.74:
        return max(1, base - 1)
    else:
        return max(1, base - 2)


def _signals_to_merged(signals: list[dict]) -> dict:
    """複数シグナルを signals.json 形式に変換。

    hold_position シグナルのみの場合 (ポジション保有継続中):
      action_type=hold として出力。executor は hold_position を誤解釈しない。
    trade/close シグナルが含まれる場合:
      action_type=trade として通常処理。

    CAPS (Confidence-Adaptive Position Sizing):
      各シグナルの confidence に基づいてレバレッジを検証・補正する。
      戦略側で設定済みの leverage を優先し、未設定時のみ confidence から推定。
    """
    summaries = []
    sig_list = []
    for sig in signals:
        action = sig.get("direction", "hold")
        symbol = sig.get("symbol", "?")
        confidence = sig.get("confidence", 0.85)
        raw_leverage = sig.get("leverage")
        leverage = _caps_leverage(confidence, raw_leverage)
        if raw_leverage is not None and leverage != int(raw_leverage):
            logger.info(
                "CAPS: %s %s leverage override %d→%d (confidence=%.2f)",
                action, symbol, raw_leverage, leverage, confidence,
            )
        summaries.append(f"{action} {symbol} ({sig.get('zone', '?')})")
        sig_entry = {
            "symbol": symbol,
            "action": action,
            "confidence": confidence,
            "entry_price": sig.get("entry_price"),
            "stop_loss": sig.get("stop_loss"),
            "take_profit": sig.get("take_profit"),
            "leverage": leverage,
            "reasoning": sig.get("reasoning", ""),
        }
        # Rubber metadata → executor が position meta 保存に使用
        for key in ("exit_mode", "exit_bars", "pattern", "zone", "vol_ratio", "spike_time"):
            if key in sig:
                sig_entry[key] = sig[key]
        sig_list.append(sig_entry)

    reasons = [s.get("reasoning", "") for s in signals]

    # hold_position のみ (= ポジション保有継続、新規エントリーもexitもなし) かを判定
    actionable_directions = {s.get("direction") for s in signals}
    has_trade_or_exit = bool(actionable_directions - {"hold_position"})
    action_type = "trade" if has_trade_or_exit else "hold"
    observe_prefix = "Rubber: " if has_trade_or_exit else "Rubber holding: "

    return {
        "ooda": {
            "observe": observe_prefix + "; ".join(reasons),
            "orient": ", ".join(summaries),
            "decide": ", ".join(summaries),
        },
        "action_type": action_type,
        "signals": sig_list,
        "market_summary": observe_prefix + ", ".join(summaries),
        "journal_entry": "\n".join(reasons),
        "self_assessment": "Rubber forward test" if has_trade_or_exit else "Rubber position monitoring",
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
