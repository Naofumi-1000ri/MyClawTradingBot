"""Brain Consensus: 3エージェント合議制トレーディング判断。

Agent T (Technician/Haiku): チャート + テクニカルデータ
Agent F (Flow Trader/Sonnet): オーダーブック + funding + フローデータ
Agent R (Risk Manager/Haiku): T+Fの出力 + ポジション状態 → 最終判断

brain.sh を置換する Python 実装。
"""

import base64
import glob
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from src.brain.build_context import build_context
from src.brain.gemini_client import GeminiClient
from src.brain.signal_merger import merge_signals
from src.collector.chart_generator import generate_all_charts
from src.utils.config_loader import get_project_root, load_settings
from src.utils.file_lock import atomic_write_json, read_json
from src.utils.logger import setup_logger

logger = setup_logger("brain_consensus")

ROOT = get_project_root()
STATE_DIR = ROOT / "state"
SIGNALS_DIR = ROOT / "signals"
SCHEMAS_DIR = ROOT / "src" / "brain" / "schemas"
PROMPTS_DIR = ROOT / "src" / "brain" / "prompts"
CHARTS_DIR = ROOT / "data" / "charts"


def _read_file(path: Path) -> str:
    """ファイル内容を文字列で読み込む。"""
    return path.read_text(encoding="utf-8")


def _load_json_safe(path: Path) -> dict | list | None:
    """JSONファイルを安全に読み込む。存在しなければNone。"""
    try:
        return read_json(path)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _extract_json_payload(raw_text: str) -> dict | None:
    """レスポンステキストからJSONオブジェクトを抽出して返す。"""
    if not raw_text:
        return None

    content = raw_text.strip()
    code_block_match = re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", content)
    if code_block_match:
        content = code_block_match.group(1)
    else:
        content = re.sub(r"^```json\s*", "", content)
        content = re.sub(r"```\s*$", "", content)

    if not content.strip():
        return None

    try:
        parsed = json.loads(content)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


# -- Agent Health Tracking --

_AGENT_HEALTH_FILE = STATE_DIR / "agent_health.json"
_AGENT_HEALTH_SKIP_THRESHOLD = 5   # 連続N回失敗でスキップ
_AGENT_HEALTH_COOLDOWN_SEC = 900   # 15分後に再試行


def _load_agent_health() -> dict:
    """agent_health.json を読み込む。"""
    try:
        return read_json(_AGENT_HEALTH_FILE)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_agent_health(health: dict) -> None:
    """agent_health.json を保存。"""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(_AGENT_HEALTH_FILE, health)


def _record_agent_failure(agent_name: str) -> None:
    """エージェント失敗を記録。"""
    health = _load_agent_health()
    entry = health.get(agent_name, {"consecutive_failures": 0})
    entry["consecutive_failures"] = entry.get("consecutive_failures", 0) + 1
    entry["last_failure"] = datetime.now(timezone.utc).isoformat()
    health[agent_name] = entry
    _save_agent_health(health)
    logger.warning("Agent %s failure #%d recorded", agent_name, entry["consecutive_failures"])


def _record_agent_success(agent_name: str) -> None:
    """エージェント成功を記録（カウンタリセット）。"""
    health = _load_agent_health()
    if agent_name in health and health[agent_name].get("consecutive_failures", 0) > 0:
        health[agent_name] = {"consecutive_failures": 0, "last_success": datetime.now(timezone.utc).isoformat()}
        _save_agent_health(health)


def _should_skip_agent(agent_name: str) -> bool:
    """連続失敗が閾値を超え、かつクールダウン期間内ならスキップ。"""
    health = _load_agent_health()
    entry = health.get(agent_name, {})
    failures = entry.get("consecutive_failures", 0)
    if failures < _AGENT_HEALTH_SKIP_THRESHOLD:
        return False
    last_failure = entry.get("last_failure", "")
    if last_failure:
        try:
            last_dt = datetime.fromisoformat(last_failure)
            elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
            if elapsed > _AGENT_HEALTH_COOLDOWN_SEC:
                logger.info("Agent %s cooldown expired (%.0fs), retrying", agent_name, elapsed)
                return False
        except ValueError:
            pass
    logger.warning("Agent %s skipped: %d consecutive failures (cooldown %ds)",
                   agent_name, failures, _AGENT_HEALTH_COOLDOWN_SEC)
    return True


_ALL_AGENTS_FAILED_THRESHOLD = 3   # 連続N回全員失敗でアラート


def _check_and_alert_all_agents_failed(health: dict) -> bool:
    """T/F/R全員が連続N回失敗していればwarningフラグとjournal記録を行う。

    Returns:
        True if all three core agents have failed for >= threshold cycles.
    """
    core_agents = ["technician", "flow", "risk"]
    min_failures = min(
        health.get(agent, {}).get("consecutive_failures", 0)
        for agent in core_agents
    )

    if min_failures < _ALL_AGENTS_FAILED_THRESHOLD:
        return False

    now_iso = datetime.now(timezone.utc).isoformat()
    logger.critical(
        "CRITICAL: agent_failure - All agents (T/F/R) failed %d consecutive cycles",
        min_failures,
    )

    # kill_switch.json に warning フラグを立てる (enabled は変更しない)
    ks_path = STATE_DIR / "kill_switch.json"
    try:
        try:
            ks = read_json(ks_path)
        except (FileNotFoundError, json.JSONDecodeError):
            ks = {"enabled": False, "reason": "", "triggered_at": "", "deactivated_at": ""}

        ks["warning"] = True
        ks["warning_reason"] = f"CRITICAL: agent_failure - T/F/R全員 {min_failures} サイクル連続失敗"
        ks["warning_at"] = now_iso
        atomic_write_json(ks_path, ks)
        logger.critical("Kill switch warning flag set: %s", ks_path)
    except Exception as e:
        logger.error("Failed to set kill_switch warning: %s", e)

    # journal に CRITICAL エントリを記録
    _append_agent_failure_journal(min_failures, now_iso)

    return True


def _append_agent_failure_journal(consecutive_failures: int, now_iso: str) -> None:
    """journal/YYYY-MM-DD.md に CRITICAL: agent_failure エントリを追記。"""
    journal_dir = ROOT / "journal"
    journal_dir.mkdir(parents=True, exist_ok=True)

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    journal_path = journal_dir / f"{date_str}.md"

    entry = (
        f"\n## CRITICAL: agent_failure [{now_iso}]\n\n"
        f"- **severity**: CRITICAL\n"
        f"- **event**: T/F/R 全エージェント {consecutive_failures} サイクル連続失敗\n"
        f"- **action_taken**: kill_switch.json に warning フラグを設定\n"
        f"- **impact**: 全サイクルでフォールバック(hold)実行中。エージェント障害を確認せよ。\n"
        f"  - Agent T (technician): consecutive_failures >= {consecutive_failures}\n"
        f"  - Agent F (flow): consecutive_failures >= {consecutive_failures}\n"
        f"  - Agent R (risk): consecutive_failures >= {consecutive_failures}\n"
    )

    try:
        if journal_path.exists():
            existing = journal_path.read_text(encoding="utf-8")
            journal_path.write_text(existing + entry, encoding="utf-8")
        else:
            header = f"# myClaw Journal - {date_str}\n"
            journal_path.write_text(header + entry, encoding="utf-8")
        logger.critical("CRITICAL agent_failure journal written: %s", journal_path)
    except Exception as e:
        logger.error("Failed to write agent_failure journal: %s", e)


def _load_chart_parts(chart_files: list[str]) -> list[dict]:
    """チャート画像を REST API の inlineData 形式で返す。"""
    parts = []
    for path in chart_files:
        try:
            data = Path(path).read_bytes()
            b64 = base64.b64encode(data).decode("ascii")
            parts.append({"inlineData": {"mimeType": "image/png", "data": b64}})
            parts.append({"text": f"[Chart: {Path(path).stem}]"})
        except Exception as e:
            parts.append({"text": f"[Chart load failed: {path}: {e}]"})
    return parts


# -- Gemini client singleton --
_gemini_client: GeminiClient | None = None


def _get_gemini_client() -> GeminiClient:
    """Gemini クライアントをシングルトンで返す。"""
    global _gemini_client
    if _gemini_client is None:
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY environment variable not set")
        _gemini_client = GeminiClient(api_key)
    return _gemini_client


def _call_gemini(
    context_json: str,
    prompt: str,
    system_prompt: str,
    schema_path: Path,
    agent_name: str,
    model: str = "gemini-2.5-flash-lite",
    max_retries: int = 3,
    chart_files: list[str] | None = None,
    max_output_tokens: int = 8192,
) -> dict | None:
    """Gemini REST API で JSON応答を取得。

    Args:
        context_json: コンテキストJSON文字列
        prompt: ユーザープロンプト
        system_prompt: システムプロンプト
        schema_path: JSONスキーマファイルパス
        agent_name: エージェント名 (ログ用)
        model: Geminiモデル名
        max_retries: 最大リトライ回数
        chart_files: チャート画像パスリスト (Agent T 用)
        max_output_tokens: 最大出力トークン数

    Returns:
        パースされたJSON dict、失敗時はNone
    """
    client = _get_gemini_client()

    # user_parts 組み立て
    parts = [{"text": prompt + "\n\nコンテキスト:\n" + context_json}]
    if chart_files:
        parts.extend(_load_chart_parts(chart_files))

    logger.info("Calling Gemini (agent=%s, model=%s, retries=%d)", agent_name, model, max_retries)

    result = client.call(
        model=model,
        system_prompt=system_prompt,
        user_parts=parts,
        max_output_tokens=max_output_tokens,
        max_retries=max_retries,
    )

    if result["success"]:
        logger.info(
            "Gemini OK (agent=%s): %.1fs, %d in/%d out tokens",
            agent_name, result["elapsed_sec"], result["input_tokens"], result["output_tokens"],
        )
        return result["parsed_json"]

    logger.warning(
        "Gemini FAILED (agent=%s): %s (%.1fs)",
        agent_name, result.get("error", "unknown"), result["elapsed_sec"],
    )
    return None


def _build_technician_context(context: dict) -> str:
    """Agent T 用コンテキスト: 価格データ + EMA/MACD (オーダーブック・funding除外)"""
    t_context = {
        "timestamp": context.get("timestamp", ""),
        "market_data": {},
    }
    for symbol, data in context.get("market_data", {}).items():
        t_data = {}
        for key, value in data.items():
            # オーダーブックとfundingを除外
            if key in ("orderbook", "funding_rate"):
                continue
            t_data[key] = value
        t_context["market_data"][symbol] = t_data

    return json.dumps(t_context, ensure_ascii=False)


def _build_flow_context(context: dict) -> str:
    """Agent F 用コンテキスト: オーダーブック + funding + 出来高 + 価格 (ローソク足詳細を圧縮)"""
    f_context = {
        "timestamp": context.get("timestamp", ""),
        "market_data": {},
    }
    for symbol, data in context.get("market_data", {}).items():
        f_data = {
            "price": data.get("price"),
            "orderbook": data.get("orderbook", {}),
            "funding_rate": data.get("funding_rate"),
            "volume_24h": data.get("volume_24h"),
        }
        # ローソク足は直近5本のみ (価格変動の参考程度)
        for candle_key in ("candles_15m", "candles_1h", "candles_4h"):
            candles = data.get(candle_key, [])
            if candles:
                f_data[candle_key] = candles[-5:]
        f_context["market_data"][symbol] = f_data

    return json.dumps(f_context, ensure_ascii=False)


def _build_risk_context(
    technician_output: dict,
    flow_output: dict,
    context: dict,
) -> str:
    """Agent R 用コンテキスト: T+F出力 + ポジション + P&L + kill_switch"""
    r_context = {
        "technician_analysis": technician_output,
        "flow_analysis": flow_output,
        "positions": context.get("positions", []),
        "daily_pnl": context.get("daily_pnl", {}),
        "kill_switch": _load_json_safe(STATE_DIR / "kill_switch.json") or {"active": False},
        "trading_config": context.get("trading_config", {}),
    }
    return json.dumps(r_context, ensure_ascii=False)


def _get_chart_files() -> list[str]:
    """チャートファイルのパスリストを取得。"""
    chart_files = []
    for tf in ("15m", "1h", "4h"):
        for f in sorted(glob.glob(str(CHARTS_DIR / f"*_{tf}.png"))):
            chart_files.append(f)
    return chart_files


def _build_technician_prompt(chart_files: list[str], schema: str) -> str:
    """Agent T 用プロンプト構築。"""
    chart_instruction = ""
    if chart_files:
        chart_list = "\n".join(chart_files)
        chart_instruction = f"""
## チャート画像 (添付済み)
各シンボルの 15m/1H/4H チャート画像が添付されている。視覚分析すること。
EMA9/EMA21クロス、MACDヒストグラム、ボリューム動向を確認せよ。

{chart_list}"""

    return f"""市場データとチャート画像を分析し、テクニカル観点からシグナルを出力せよ。
コードブロックなし、純粋なJSONのみ。
{chart_instruction}

JSON Schema:
{schema}"""


def _build_flow_prompt(schema: str) -> str:
    """Agent F 用プロンプト構築。"""
    return f"""オーダーブック、funding rate、出来高データを分析し、フロー観点からシグナルを出力せよ。
チャート画像は存在しない。数値データのみで判断せよ。
コードブロックなし、純粋なJSONのみ。

JSON Schema:
{schema}"""


def _build_risk_prompt(schema: str) -> str:
    """Agent R 用プロンプト構築。"""
    return f"""Technician (T) と Flow Trader (F) の分析結果を評価し、リスク観点から最終判断を下せ。
各銘柄についてapprove/reject/modifyの判断と、最終的なアクション・レバレッジを決定せよ。
コードブロックなし、純粋なJSONのみ。

JSON Schema:
{schema}"""


def _build_advisor_prompt(role_instruction: str, schema: str) -> str:
    """Advisor 用プロンプト構築。"""
    return f"""以下の役割指示に従い、各銘柄の可否判定を返せ。
コードブロックなし、純粋なJSONのみ。

Role:
{role_instruction}

JSON Schema:
{schema}"""


def _fallback_output(symbols: list[str], reason: str) -> dict:
    """全エージェント失敗時のフォールバック出力。"""
    return {
        "ooda": {
            "observe": f"合議制フォールバック: {reason}",
            "orient": "全エージェント失敗のため安全側にフォールバック",
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
                "reasoning": f"合議制フォールバック: {reason}",
            }
            for s in symbols
        ],
        "market_summary": f"合議制フォールバック: {reason}",
        "journal_entry": f"合議制フォールバック: {reason}",
        "self_assessment": "エージェント呼び出し失敗。次サイクルで再試行。",
    }


def _default_agent_output(symbols: list[str]) -> dict:
    """エージェント失敗時のデフォルト出力。"""
    return {
        "signals": [
            {
                "symbol": s,
                "direction": "neutral",
                "confidence": 0.0,
                "action": "hold",
                "entry_price": None,
                "stop_loss": None,
                "take_profit": None,
                "reasoning": "エージェント呼び出し失敗",
            }
            for s in symbols
        ],
        "market_view": "エージェント呼び出し失敗",
    }


def _default_risk_output(symbols: list[str]) -> dict:
    """Risk Manager 失敗時のデフォルト出力。"""
    return {
        "decisions": [
            {
                "symbol": s,
                "verdict": "reject",
                "final_action": "hold",
                "leverage": 3,
                "reasoning": "Risk Manager呼び出し失敗 - 安全側にreject",
            }
            for s in symbols
        ],
        "risk_assessment": "Risk Manager呼び出し失敗",
    }


def _default_advisor_output(symbols: list[str], reason: str) -> dict:
    """Advisor 失敗時のデフォルト出力。"""
    return {
        "decisions": [
            {
                "symbol": s,
                "verdict": "neutral",
                "confidence": 0.0,
                "reasoning": reason,
            }
            for s in symbols
        ],
        "advisor_view": reason,
    }


def _build_advisor_context(
    merged_signals: dict,
    context: dict,
    advisor_name: str,
) -> str:
    """Advisor 用コンテキスト構築。"""
    payload = {
        "advisor": advisor_name,
        "timestamp": context.get("timestamp", ""),
        "preliminary_signals": merged_signals.get("signals", []),
        "positions": context.get("positions", []),
        "daily_pnl": context.get("daily_pnl", {}),
        "market_data": context.get("market_data", {}),
    }
    return json.dumps(payload, ensure_ascii=False)


def _apply_advisor_committee(
    merged: dict,
    advisor_outputs: list[dict],
    reject_quorum: int = 2,
    reject_conf_threshold: float = 0.7,
    min_confidence: float = 0.7,
) -> dict:
    """Advisor 合議で新規エントリー(long/short)を最終ゲートする。"""
    signals = merged.get("signals", [])
    if not isinstance(signals, list):
        return merged

    # symbol -> list[(advisor_idx, confidence, reasoning)]
    rejects: dict[str, list[tuple[int, float, str]]] = {}
    for idx, out in enumerate(advisor_outputs):
        for d in out.get("decisions", []):
            sym = d.get("symbol")
            if not sym:
                continue
            verdict = str(d.get("verdict", "neutral"))
            conf = float(d.get("confidence") or 0.0)
            if verdict == "reject" and conf >= reject_conf_threshold:
                rejects.setdefault(sym, []).append((idx + 1, conf, str(d.get("reasoning", ""))))

    updated = []
    committee_blocked = 0
    for sig in signals:
        action = sig.get("action")
        symbol = sig.get("symbol", "")
        if action not in ("long", "short"):
            updated.append(sig)
            continue

        votes = rejects.get(symbol, [])
        if len(votes) >= reject_quorum:
            reasons = "; ".join(
                f"A{aid}:reject({conf:.2f}) {reason[:80]}"
                for aid, conf, reason in votes
            )
            blocked = dict(sig)
            blocked["action"] = "hold"
            blocked["confidence"] = 0.0
            blocked["reasoning"] = f"{sig.get('reasoning', '')} | [AdvisorGate] blocked: {reasons}"
            updated.append(blocked)
            committee_blocked += 1
        else:
            updated.append(sig)

    has_trade = any(
        s.get("action") in ("long", "short", "close") and float(s.get("confidence", 0)) >= min_confidence
        for s in updated
    )
    has_close = any(s.get("action") == "close" for s in updated)
    merged["action_type"] = "trade" if (has_trade or has_close) else "hold"
    merged["signals"] = updated

    if committee_blocked > 0:
        merged["market_summary"] = (
            f"{merged.get('market_summary', '')} | advisor_blocked={committee_blocked}"
        )
        merged["journal_entry"] = (
            f"{merged.get('journal_entry', '')}\nAdvisor gate blocked entries: {committee_blocked}"
        )

    return merged


def _run_rubber_wall(settings: dict, context: dict) -> None:
    """ゴム戦略実行。BTC RubberWall + ETH RubberBand を並列スキャン。

    閾値キャッシュ方式: 前サイクルで次の足の閾値volumeを事前計算済み。
    キャッシュヒット時は O(1) で判定完了。
    """
    from src.strategy.btc_rubber_wall import BtcRubberWall
    from src.strategy.eth_rubber_band import EthRubberBand

    strategy_cfg = settings.get("strategy", {})
    signals_list = []

    # --- BTC RubberWall ---
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
            signals_list.append(btc_signal)
            _log_rubber_signal(btc_signal)
            logger.info("RubberWall BTC: %s (zone=%s, vr=%.1f)",
                        btc_signal["direction"], btc_signal.get("zone"), btc_signal.get("vol_ratio"))
        else:
            logger.info("RubberWall BTC: no spike → hold")
    else:
        logger.warning("No BTC 5m candles available")

    # --- ETH RubberBand ---
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
            signals_list.append(eth_signal)
            _log_rubber_signal(eth_signal)
            logger.info("RubberBand ETH: %s %s (pattern=%s, vr=%.1f)",
                        eth_signal["direction"], eth_signal["symbol"],
                        eth_signal.get("pattern"), eth_signal.get("vol_ratio"))
        else:
            logger.info("RubberBand ETH: no spike → hold")
    else:
        logger.warning("No ETH 5m candles available")

    # --- 統合出力 ---
    if signals_list:
        merged = _signals_to_merged(signals_list)
    else:
        all_symbols = ["BTC", "ETH"]
        merged = _fallback_output(all_symbols, "スパイクなし: 静観")

    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(SIGNALS_DIR / "signals.json", merged)
    logger.info("=== Rubber Complete: action_type=%s, signals=%d ===",
                merged.get("action_type"), len(signals_list))


def _signals_to_merged(signals: list[dict]) -> dict:
    """複数シグナルを signals.json 形式に変換。"""
    summaries = []
    sig_list = []
    for sig in signals:
        action = sig.get("direction", "hold")
        symbol = sig.get("symbol", "?")
        summaries.append(f"{action} {symbol} ({sig.get('zone', '?')})")
        sig_list.append({
            "symbol": symbol,
            "action": action,
            "confidence": sig.get("confidence", 0.85),
            "entry_price": sig.get("entry_price"),
            "stop_loss": sig.get("stop_loss"),
            "take_profit": sig.get("take_profit"),
            "leverage": sig.get("leverage", 3),
            "reasoning": sig.get("reasoning", ""),
        })

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
    """メイン実行: コンテキスト構築 → 3エージェント呼び出し → マージ → 出力。"""
    settings = load_settings()
    strategy_mode = settings.get("strategy", {}).get("mode", "consensus")
    brain_config = settings.get("brain", {})
    symbols = settings.get("trading", {}).get("symbols", ["BTC", "ETH", "SOL"])

    # 1. コンテキスト構築 (全モード共通)
    logger.info("[1/N] Building context...")
    try:
        context = build_context()
        context_path = ROOT / "data" / "context.json"
        atomic_write_json(context_path, context)
        logger.info("Context built: %s", context_path)
    except Exception as e:
        logger.error("Context build failed: %s", e)
        _write_fallback_and_exit(symbols, f"コンテキスト構築失敗: {e}")
        return

    # 1b. equity sanity check: 異常値は前回値にフォールバック
    _sanitize_equity_in_context(context)

    # rubber_wall モード: LLM合議をバイパス
    if strategy_mode == "rubber_wall":
        logger.info("=== RubberWall Mode ===")
        _run_rubber_wall(settings, context)
        return

    # --- 以下: consensus モード (3エージェント合議) ---

    # モデル設定
    t_model = brain_config.get("technician_model", "gemini-2.5-flash-lite")
    f_model = brain_config.get("flow_model", "gemini-2.5-flash-lite")
    r_model = brain_config.get("risk_model", "gemini-2.5-flash-lite")

    logger.info("=== Brain Consensus Start (T:%s, F:%s, R:%s) ===", t_model, f_model, r_model)

    # 2. チャート生成
    logger.info("[2/6] Generating charts...")
    try:
        generate_all_charts(settings)
    except Exception as e:
        logger.warning("Chart generation failed (continuing without charts): %s", e)

    # 3. スキーマ読み込み
    t_schema = _read_file(SCHEMAS_DIR / "technician_schema.json")
    f_schema = _read_file(SCHEMAS_DIR / "flow_schema.json")
    r_schema = _read_file(SCHEMAS_DIR / "risk_schema.json")
    a_schema = _read_file(SCHEMAS_DIR / "advisor_schema.json")

    # プロンプト読み込み
    t_system_prompt = _read_file(PROMPTS_DIR / "technician_prompt.md")
    f_system_prompt = _read_file(PROMPTS_DIR / "flow_prompt.md")
    r_system_prompt = _read_file(PROMPTS_DIR / "risk_prompt.md")
    macro_system_prompt = _read_file(PROMPTS_DIR / "macro_advisor_prompt.md")
    exec_system_prompt = _read_file(PROMPTS_DIR / "execution_advisor_prompt.md")

    # 4. Agent T (Technician)
    logger.info("[3/6] Calling Agent T (Technician / %s)...", t_model)
    chart_files = _get_chart_files()
    t_context = _build_technician_context(context)
    t_prompt = _build_technician_prompt(chart_files, t_schema)

    if _should_skip_agent("technician"):
        t_output = _default_agent_output(symbols)
    else:
        t_output = _call_gemini(
            context_json=t_context,
            prompt=t_prompt,
            system_prompt=t_system_prompt,
            schema_path=SCHEMAS_DIR / "technician_schema.json",
            agent_name="technician",
            model=t_model,
            chart_files=chart_files,
        )
    if t_output is None:
        logger.warning("Agent T failed, using default output")
        _record_agent_failure("technician")
        t_output = _default_agent_output(symbols)
    else:
        _record_agent_success("technician")
    logger.info("Agent T result: %s", [(s.get("symbol"), s.get("action"), s.get("confidence"))
                                        for s in t_output.get("signals", [])])

    # 5. Agent F (Flow Trader)
    logger.info("[4/6] Calling Agent F (Flow Trader / %s)...", f_model)
    f_context = _build_flow_context(context)
    f_prompt = _build_flow_prompt(f_schema)

    if _should_skip_agent("flow"):
        f_output = _default_agent_output(symbols)
    else:
        f_output = _call_gemini(
            context_json=f_context,
            prompt=f_prompt,
            system_prompt=f_system_prompt,
            schema_path=SCHEMAS_DIR / "flow_schema.json",
            agent_name="flow",
            model=f_model,
        )
    if f_output is None:
        logger.warning("Agent F failed, using default output")
        _record_agent_failure("flow")
        f_output = _default_agent_output(symbols)
    else:
        _record_agent_success("flow")
    logger.info("Agent F result: %s", [(s.get("symbol"), s.get("action"), s.get("confidence"))
                                        for s in f_output.get("signals", [])])

    # 6. Agent R (Risk Manager)
    logger.info("[5/6] Calling Agent R (Risk Manager / %s)...", r_model)
    r_context = _build_risk_context(t_output, f_output, context)
    r_prompt = _build_risk_prompt(r_schema)

    if _should_skip_agent("risk"):
        r_output = _default_risk_output(symbols)
    else:
        r_output = _call_gemini(
            context_json=r_context,
            prompt=r_prompt,
            system_prompt=r_system_prompt,
            schema_path=SCHEMAS_DIR / "risk_schema.json",
            agent_name="risk",
            model=r_model,
        )
    if r_output is None:
        logger.warning("Agent R failed, using default reject output")
        _record_agent_failure("risk")
        r_output = _default_risk_output(symbols)
    else:
        _record_agent_success("risk")
    logger.info("Agent R result: %s", [(d.get("symbol"), d.get("verdict"), d.get("final_action"))
                                        for d in r_output.get("decisions", [])])

    # 全エージェント連続失敗アラートチェック
    _check_and_alert_all_agents_failed(_load_agent_health())

    # 7. マージ
    logger.info("[6/6] Merging signals...")
    positions = context.get("positions", [])
    min_conf = brain_config.get("min_confidence", settings.get("trading", {}).get("min_confidence", 0.7))
    merged = merge_signals(t_output, f_output, r_output, symbols, positions, min_confidence=min_conf)

    # 8. 追加アドバイザー合議 (Macro + Execution/MM)
    advisor_cfg = brain_config.get("advisors", {})
    advisors_enabled = bool(advisor_cfg.get("enabled", True))
    if advisors_enabled:
        advisor_model = advisor_cfg.get("model", "gemini-2.5-flash-lite")
        advisor_reject_quorum = int(advisor_cfg.get("reject_quorum", 2))
        advisor_reject_conf = float(advisor_cfg.get("reject_confidence", 0.7))
        advisor_outputs = []

        # Macro advisor
        macro_context = _build_advisor_context(merged, context, "macro")
        macro_prompt = _build_advisor_prompt(
            "Macro regime expert. Focus on market regime mismatch and squeeze/crash risk.",
            a_schema,
        )
        macro_out = _call_gemini(
            context_json=macro_context,
            prompt=macro_prompt,
            system_prompt=macro_system_prompt,
            schema_path=SCHEMAS_DIR / "advisor_schema.json",
            agent_name="advisor_macro",
            model=advisor_model,
        )
        if macro_out is None:
            macro_out = _default_advisor_output(symbols, "macro advisor unavailable")
        advisor_outputs.append(macro_out)
        logger.info("Advisor Macro: %s", [(d.get("symbol"), d.get("verdict"), d.get("confidence")) for d in macro_out.get("decisions", [])])

        # Execution/MM advisor
        exec_context = _build_advisor_context(merged, context, "execution")
        exec_prompt = _build_advisor_prompt(
            "Execution and market-making expert. Focus on spread, depth, slippage and fill quality risk.",
            a_schema,
        )
        exec_out = _call_gemini(
            context_json=exec_context,
            prompt=exec_prompt,
            system_prompt=exec_system_prompt,
            schema_path=SCHEMAS_DIR / "advisor_schema.json",
            agent_name="advisor_execution",
            model=advisor_model,
        )
        if exec_out is None:
            exec_out = _default_advisor_output(symbols, "execution advisor unavailable")
        advisor_outputs.append(exec_out)
        logger.info("Advisor Exec: %s", [(d.get("symbol"), d.get("verdict"), d.get("confidence")) for d in exec_out.get("decisions", [])])

        merged = _apply_advisor_committee(
            merged,
            advisor_outputs,
            reject_quorum=advisor_reject_quorum,
            reject_conf_threshold=advisor_reject_conf,
            min_confidence=min_conf,
        )
        logger.info(
            "Advisor committee applied (quorum=%d, conf>=%.2f): action_type=%s",
            advisor_reject_quorum,
            advisor_reject_conf,
            merged.get("action_type"),
        )

    # signals.json 出力
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(SIGNALS_DIR / "signals.json", merged)

    logger.info("=== Brain Consensus Complete: action_type=%s ===", merged.get("action_type"))

    # サマリー出力
    for sig in merged.get("signals", []):
        logger.info("  %s: %s (conf=%.2f)", sig["symbol"], sig["action"], sig["confidence"])


def _write_fallback_and_exit(symbols: list[str], reason: str) -> None:
    """フォールバック出力を書き込む。"""
    fallback = _fallback_output(symbols, reason)
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(SIGNALS_DIR / "signals.json", fallback)
    logger.warning("Fallback output written: %s", reason)


if __name__ == "__main__":
    main()
