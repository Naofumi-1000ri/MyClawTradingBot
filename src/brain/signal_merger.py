"""Signal Merger: 3エージェントの出力を合議して最終シグナルを生成。

非対称コンセンサスルール:
- IN 完全合議: T+F同方向 + R承認 + confidence >= 0.7
- IN 部分合議: T or F が方向性 + もう片方hold + R承認 + confidence >= 0.7 (lev上限3x)
- 矛盾: T=long vs F=short (またはその逆) → hold
- OUT (決済): いずれか1エージェントがclose推奨で即決済

4Hトレンドフィルター (部分合議F単独long限定):
- 4H EMA9 < EMA21 かつ MACD_histogram < 0 → F単独longをhold強制
- 完全合議(T+F両方long)はフィルター対象外

最低保有時間ルール:
- エントリーから2サイクル（約10分）以内のcloseは、
  RエージェントがCRITICAL close信号 (confidence >= 0.90) を出した場合のみ許可
- それ以外はholdに変換してエントリー直後のchurnを防止
"""

from datetime import datetime, timezone

from src.utils.logger import setup_logger

logger = setup_logger("signal_merger")


def _calc_ema(values: list[float], period: int) -> list[float]:
    """終値リストからEMAを計算して返す。"""
    if len(values) < period:
        return []
    k = 2.0 / (period + 1)
    ema = [sum(values[:period]) / period]
    for v in values[period:]:
        ema.append(v * k + ema[-1] * (1 - k))
    return ema


def _calc_macd_histogram(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> float | None:
    """MACD histogramの最新値を計算して返す。データ不足の場合はNone。"""
    if len(closes) < slow + signal:
        return None
    ema_fast = _calc_ema(closes, fast)
    ema_slow = _calc_ema(closes, slow)
    if not ema_fast or not ema_slow:
        return None
    # 長さを揃える (slowの方が短い)
    min_len = min(len(ema_fast), len(ema_slow))
    macd_line = [f - s for f, s in zip(ema_fast[-min_len:], ema_slow[-min_len:])]
    if len(macd_line) < signal:
        return None
    signal_line = _calc_ema(macd_line, signal)
    if not signal_line:
        return None
    return macd_line[-1] - signal_line[-1]


def _get_4h_trend_filter(market_data: dict, symbol: str) -> dict:
    """4H EMA9/EMA21とMACD_histogramを計算してトレンド判定を返す。

    Returns:
        {
            "bearish": bool,   # True = 4H下降トレンド (EMA9<EMA21 かつ MACD_hist<0)
            "ema9": float | None,
            "ema21": float | None,
            "macd_hist": float | None,
            "reason": str,
        }
    """
    sym_data = market_data.get("symbols", {}).get(symbol, {})
    candles_4h = sym_data.get("candles_4h", [])

    if len(candles_4h) < 26:
        return {"bearish": False, "ema9": None, "ema21": None, "macd_hist": None,
                "reason": f"4Hデータ不足({len(candles_4h)}本)"}

    closes = [float(c["c"]) for c in candles_4h]
    ema9_list = _calc_ema(closes, 9)
    ema21_list = _calc_ema(closes, 21)
    macd_hist = _calc_macd_histogram(closes)

    ema9 = ema9_list[-1] if ema9_list else None
    ema21 = ema21_list[-1] if ema21_list else None

    if ema9 is None or ema21 is None or macd_hist is None:
        return {"bearish": False, "ema9": ema9, "ema21": ema21, "macd_hist": macd_hist,
                "reason": "EMA/MACD計算失敗"}

    bearish = (ema9 < ema21) and (macd_hist < 0)
    reason = (
        f"4H EMA9={ema9:.2f}, EMA21={ema21:.2f}, MACD_hist={macd_hist:.4f} "
        f"→ {'下降トレンド' if bearish else 'トレンドフィルター非該当'}"
    )
    return {"bearish": bearish, "ema9": ema9, "ema21": ema21, "macd_hist": macd_hist, "reason": reason}


def _get_agent_signal(agent_output: dict, symbol: str) -> dict | None:
    """エージェント出力から指定銘柄のシグナルを取得。"""
    for sig in agent_output.get("signals", []):
        if sig.get("symbol") == symbol:
            return sig
    return None


def _build_hold_signal(symbol: str, reasoning: str) -> dict:
    """hold シグナルを生成。"""
    return {
        "symbol": symbol,
        "action": "hold",
        "confidence": 0.0,
        "entry_price": None,
        "stop_loss": None,
        "take_profit": None,
        "leverage": 3,
        "reasoning": reasoning,
    }


_MIN_HOLD_CYCLES = 2          # エントリーから何サイクル保有を最低限要求するか
_CYCLE_MINUTES = 5            # 1サイクルの所要時間（分）
_MIN_HOLD_MINUTES = _MIN_HOLD_CYCLES * _CYCLE_MINUTES  # = 10分
_R_CRITICAL_CLOSE_CONF = 0.90  # 最低保有時間内にcloseを許可するR信号の最低confidence


def _get_position_opened_at(
    positions: list | None,
    symbol: str,
    trade_history: list | None = None,
) -> datetime | None:
    """ポジションのエントリー時刻を返す。

    優先度:
    1. positions.json の opened_at（trade_executorがエントリー後にsync前に設定する場合）
    2. trade_history.json の最新 opened_at（closed_atがない = オープン中の最終エントリー）
    いずれも取得できない場合はNone → 最低保有時間チェックをスキップ（安全側）。
    """
    # 1. positions から opened_at を試みる
    if positions:
        for pos in positions:
            if pos.get("symbol") == symbol:
                opened_at = pos.get("opened_at")
                if opened_at:
                    try:
                        dt = datetime.fromisoformat(opened_at)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        return dt
                    except ValueError:
                        break  # パース失敗は trade_history にフォールバック

    # 2. trade_history から最新のオープン中エントリーを探す
    if trade_history:
        latest_entry: datetime | None = None
        for trade in reversed(trade_history):
            if trade.get("symbol") != symbol:
                continue
            if trade.get("closed_at"):
                # クローズ済みトレードはスキップ。最新のオープン中エントリーを探す
                continue
            ts = trade.get("opened_at") or trade.get("recorded_at")
            if ts:
                try:
                    dt = datetime.fromisoformat(ts)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if latest_entry is None or dt > latest_entry:
                        latest_entry = dt
                except ValueError:
                    continue
        if latest_entry:
            return latest_entry

    return None


def _is_within_min_hold_period(opened_at: datetime | None) -> bool:
    """エントリーから最低保有時間内かどうかを判定する。"""
    if opened_at is None:
        return False
    elapsed_min = (datetime.now(timezone.utc) - opened_at).total_seconds() / 60.0
    return elapsed_min < _MIN_HOLD_MINUTES


def merge_signals(
    technician_output: dict,
    flow_output: dict,
    risk_output: dict,
    symbols: list[str],
    positions: list | None = None,
    min_confidence: float = 0.7,
    market_data: dict | None = None,
    trade_history: list | None = None,
) -> dict:
    """3エージェントの出力を合議して最終signal_schema.json互換の出力を生成。

    Args:
        technician_output: Agent T の出力 (technician_schema)
        flow_output: Agent F の出力 (flow_schema)
        risk_output: Agent R の出力 (risk_schema)
        symbols: 対象銘柄リスト
        positions: 現在ポジション (決済判断用・最低保有時間チェック用)
        market_data: 市場データ (4Hトレンドフィルター用、省略可)
        trade_history: トレード履歴 (最低保有時間チェック用、省略可)

    Returns:
        signal_schema.json 互換の dict
    """
    merged_signals = []
    consensus_log = []
    merge_stats = {
        "close": 0,
        "r_reject_hold": 0,
        "full_consensus_entry": 0,
        "partial_consensus_entry": 0,
        "conflict_hold": 0,
        "both_hold": 0,
    }

    for symbol in symbols:
        t_sig = _get_agent_signal(technician_output, symbol)
        f_sig = _get_agent_signal(flow_output, symbol)

        # Risk Manager の判断取得
        r_decision = None
        for d in risk_output.get("decisions", []):
            if d.get("symbol") == symbol:
                r_decision = d
                break

        # エージェントが出力しなかった場合のデフォルト
        t_action = t_sig.get("action", "hold") if t_sig else "hold"
        t_conf = t_sig.get("confidence", 0.0) if t_sig else 0.0
        t_direction = t_sig.get("direction", "neutral") if t_sig else "neutral"

        f_action = f_sig.get("action", "hold") if f_sig else "hold"
        f_conf = f_sig.get("confidence", 0.0) if f_sig else 0.0
        f_direction = f_sig.get("direction", "neutral") if f_sig else "neutral"

        r_verdict = r_decision.get("verdict", "reject") if r_decision else "reject"
        r_action = r_decision.get("final_action", "hold") if r_decision else "hold"

        # --- 決済判断 (OUT = 寛容、ただし最低保有時間ルールあり) ---
        any_close = (t_action == "close" or f_action == "close" or r_action == "close")
        if any_close:
            reasoning_parts = []
            if t_action == "close":
                reasoning_parts.append(f"T:close({t_sig.get('reasoning', '')})")
            if f_action == "close":
                reasoning_parts.append(f"F:close({f_sig.get('reasoning', '')})")
            if r_action == "close":
                reasoning_parts.append(f"R:close({r_decision.get('reasoning', '')})")

            # --- 最低保有時間ルール ---
            # エントリーから2サイクル（10分）以内は、R critical close (conf>=0.90) のみ許可
            opened_at = _get_position_opened_at(positions, symbol, trade_history)
            if _is_within_min_hold_period(opened_at):
                r_conf = float(r_decision.get("confidence", 0.0)) if r_decision else 0.0
                r_is_critical_close = (r_action == "close" and r_conf >= _R_CRITICAL_CLOSE_CONF)
                if not r_is_critical_close:
                    # 最低保有時間内、かつR critical closeでない → holdに変換
                    elapsed_min = (datetime.now(timezone.utc) - opened_at).total_seconds() / 60.0
                    hold_reason = (
                        f"[最低保有時間] エントリーから{elapsed_min:.1f}分 < {_MIN_HOLD_MINUTES}分。"
                        f"R critical close (conf>={_R_CRITICAL_CLOSE_CONF}) 未達のためhold。"
                        f"元の判断: {'; '.join(reasoning_parts)}"
                    )
                    merged_signals.append(_build_hold_signal(symbol, hold_reason))
                    consensus_log.append(
                        f"{symbol}: HOLD (最低保有時間: {elapsed_min:.1f}m < {_MIN_HOLD_MINUTES}m, "
                        f"R_conf={r_conf:.2f})"
                    )
                    merge_stats["both_hold"] += 1
                    logger.info(
                        "Min hold period blocked close: symbol=%s, elapsed=%.1fm, r_conf=%.2f",
                        symbol, elapsed_min, r_conf,
                    )
                    continue
                else:
                    # R critical closeは最低保有時間内でも許可
                    elapsed_min = (datetime.now(timezone.utc) - opened_at).total_seconds() / 60.0
                    reasoning_parts.append(
                        f"[R_CRITICAL_CLOSE: conf={r_conf:.2f}>={_R_CRITICAL_CLOSE_CONF}, "
                        f"elapsed={elapsed_min:.1f}m]"
                    )
                    logger.info(
                        "Min hold period: R critical close ALLOWED: symbol=%s, elapsed=%.1fm, r_conf=%.2f",
                        symbol, elapsed_min, r_conf,
                    )

            merged_signals.append({
                "symbol": symbol,
                "action": "close",
                "confidence": 0.9,
                "entry_price": None,
                "stop_loss": None,
                "take_profit": None,
                "leverage": 3,
                "reasoning": f"[合議:OUT] {'; '.join(reasoning_parts)}",
            })
            consensus_log.append(f"{symbol}: CLOSE (いずれかがclose推奨)")
            merge_stats["close"] += 1
            continue

        # --- R が reject → hold ---
        if r_verdict == "reject":
            reasoning = (
                f"[合議:REJECT] T:{t_action}({t_conf:.2f}), F:{f_action}({f_conf:.2f}), "
                f"R:reject({r_decision.get('reasoning', '') if r_decision else 'no output'})"
            )
            merged_signals.append(_build_hold_signal(symbol, reasoning))
            consensus_log.append(f"{symbol}: HOLD (R reject)")
            merge_stats["r_reject_hold"] += 1
            continue

        # --- エントリー判断 (IN = 厳格) ---
        # action を直接使用。direction は分析的観察であり trade action ではない。
        # hold(bearish) を "short" と解釈すると F=long との偽矛盾が生じる。
        t_trade_action = t_action if t_action in ("long", "short") else "hold"
        f_trade_action = f_action if f_action in ("long", "short") else "hold"

        # T と F が同方向かチェック
        if t_trade_action in ("long", "short") and t_trade_action == f_trade_action:
            # 完全合議: 同方向 → R の approve/modify を確認
            avg_conf = (t_conf + f_conf) / 2

            # R が modify の場合、R の修正値を適用
            if r_verdict == "modify" and r_decision:
                leverage = r_decision.get("leverage", 3)
                stop_loss = r_decision.get("stop_loss") or (t_sig or {}).get("stop_loss") or (f_sig or {}).get("stop_loss")
                take_profit = r_decision.get("take_profit") or (t_sig or {}).get("take_profit") or (f_sig or {}).get("take_profit")
            else:
                leverage = min(
                    (t_sig or {}).get("leverage", 3) if isinstance((t_sig or {}).get("leverage"), int) else 3,
                    (f_sig or {}).get("leverage", 3) if isinstance((f_sig or {}).get("leverage"), int) else 3,
                    5,  # デフォルト上限
                )
                stop_loss = (t_sig or {}).get("stop_loss") or (f_sig or {}).get("stop_loss")
                take_profit = (t_sig or {}).get("take_profit") or (f_sig or {}).get("take_profit")

            entry_price = (t_sig or {}).get("entry_price") or (f_sig or {}).get("entry_price")

            merged_signals.append({
                "symbol": symbol,
                "action": t_trade_action,
                "confidence": round(avg_conf, 3),
                "entry_price": entry_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "leverage": leverage,
                "reasoning": (
                    f"[合議:IN] T:{t_action}/{t_direction}({t_conf:.2f}), "
                    f"F:{f_action}/{f_direction}({f_conf:.2f}), "
                    f"R:{r_verdict}({r_decision.get('reasoning', '') if r_decision else ''})"
                ),
            })
            consensus_log.append(f"{symbol}: {t_trade_action.upper()} (T+F一致, R:{r_verdict}, conf:{avg_conf:.2f})")
            merge_stats["full_consensus_entry"] += 1

        elif (t_trade_action in ("long", "short") and f_trade_action == "hold") or \
             (f_trade_action in ("long", "short") and t_trade_action == "hold"):
            # 部分合議: 片方が方向性あり + 片方が中立 (矛盾ではない)
            if t_trade_action in ("long", "short"):
                lead_agent, lead_action, lead_conf = "T", t_trade_action, t_conf
                lead_sig = t_sig
                passive_conf = f_conf
            else:
                lead_agent, lead_action, lead_conf = "F", f_trade_action, f_conf
                lead_sig = f_sig
                passive_conf = t_conf

            # --- 4Hトレンドフィルター: F単独longを下降トレンド時にブロック ---
            if lead_agent == "F" and lead_action == "long" and market_data is not None:
                trend = _get_4h_trend_filter(market_data, symbol)
                if trend["bearish"]:
                    reasoning = (
                        f"[合議:4Hフィルター] F単独longを下降トレンドでブロック。"
                        f"{trend['reason']}。"
                        f"T:{t_action}/{t_direction}({t_conf:.2f}), "
                        f"F:{f_action}/{f_direction}({f_conf:.2f})"
                    )
                    merged_signals.append(_build_hold_signal(symbol, reasoning))
                    consensus_log.append(
                        f"{symbol}: HOLD (4Hフィルター: F単独long→ブロック, {trend['reason']})"
                    )
                    merge_stats["r_reject_hold"] += 1
                    logger.info("4H trend filter blocked F-solo long: symbol=%s, %s", symbol, trend["reason"])
                    continue

            # 部分合議: lead agentのconfidenceをそのまま使用
            # 安全性はレバレッジ上限3xで担保する
            discounted_conf = round(lead_conf, 3)

            if r_verdict == "modify" and r_decision:
                leverage = min(r_decision.get("leverage", 3), 3)  # 部分合議は常に上限3x
                stop_loss = r_decision.get("stop_loss") or (lead_sig or {}).get("stop_loss")
                take_profit = r_decision.get("take_profit") or (lead_sig or {}).get("take_profit")
            else:
                leverage = min(
                    (lead_sig or {}).get("leverage", 3) if isinstance((lead_sig or {}).get("leverage"), int) else 3,
                    3,  # 部分合議はレバレッジ上限3x
                )
                stop_loss = (lead_sig or {}).get("stop_loss")
                take_profit = (lead_sig or {}).get("take_profit")

            entry_price = (lead_sig or {}).get("entry_price")

            merged_signals.append({
                "symbol": symbol,
                "action": lead_action,
                "confidence": discounted_conf,
                "entry_price": entry_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "leverage": leverage,
                "reasoning": (
                    f"[合議:部分IN({lead_agent}主導)] T:{t_action}/{t_direction}({t_conf:.2f}), "
                    f"F:{f_action}/{f_direction}({f_conf:.2f}), "
                    f"R:{r_verdict}({r_decision.get('reasoning', '') if r_decision else ''})"
                ),
            })
            consensus_log.append(
                f"{symbol}: {lead_action.upper()} (部分合議:{lead_agent}主導, R:{r_verdict}, conf:{discounted_conf:.2f})"
            )
            merge_stats["partial_consensus_entry"] += 1

        elif t_trade_action in ("long", "short") and f_trade_action in ("long", "short") \
                and t_trade_action != f_trade_action:
            # 真の矛盾: T=long vs F=short (またはその逆) → hold
            reasoning = (
                f"[合議:矛盾] T:{t_action}/{t_direction}({t_conf:.2f}), "
                f"F:{f_action}/{f_direction}({f_conf:.2f}), "
                f"R:{r_verdict}"
            )
            merged_signals.append(_build_hold_signal(symbol, reasoning))
            consensus_log.append(f"{symbol}: HOLD (T⇔F矛盾: T={t_trade_action}, F={f_trade_action})")
            merge_stats["conflict_hold"] += 1

        else:
            # 両方hold → hold
            reasoning = (
                f"[合議:様子見] T:{t_action}/{t_direction}({t_conf:.2f}), "
                f"F:{f_action}/{f_direction}({f_conf:.2f}), "
                f"R:{r_verdict}"
            )
            merged_signals.append(_build_hold_signal(symbol, reasoning))
            consensus_log.append(f"{symbol}: HOLD (T/F両方hold)")
            merge_stats["both_hold"] += 1

    # action_type 判定
    has_trade = any(s["action"] in ("long", "short", "close") and s["confidence"] >= min_confidence
                    for s in merged_signals)
    has_close = any(s["action"] == "close" for s in merged_signals)
    action_type = "trade" if (has_trade or has_close) else "hold"

    # OODA構築
    t_view = technician_output.get("market_view", "")
    f_view = flow_output.get("market_view", "")
    r_assess = risk_output.get("risk_assessment", "")

    ooda = {
        "observe": f"[T] {t_view} [F] {f_view}",
        "orient": f"合議結果: {'; '.join(consensus_log)}. [R] {r_assess}",
        "decide": "; ".join(
            f"{s['symbol']}:{s['action']}({s['confidence']:.2f})"
            for s in merged_signals
        ),
    }

    # market_summary
    summaries = []
    for s in merged_signals:
        summaries.append(f"{s['symbol']}:{s['action']}(conf={s['confidence']:.2f})")
    market_summary = f"合議制判断: {', '.join(summaries)}"

    # journal_entry
    journal_parts = [
        f"=== 合議制サイクル ===",
        f"T(Technician): {t_view[:200]}",
        f"F(Flow): {f_view[:200]}",
        f"R(Risk): {r_assess[:200]}",
        f"合議: {'; '.join(consensus_log)}",
    ]

    result = {
        "ooda": ooda,
        "action_type": action_type,
        "signals": merged_signals,
        "market_summary": market_summary,
        "journal_entry": "\n".join(journal_parts),
        "self_assessment": f"合議制: T/F/R 3エージェントで判断。{'; '.join(consensus_log)}",
    }

    logger.info(
        "Merge complete: action_type=%s, signals=%s, stats=%s",
        action_type,
        [(s["symbol"], s["action"], s["confidence"]) for s in merged_signals],
        merge_stats,
    )

    return result
