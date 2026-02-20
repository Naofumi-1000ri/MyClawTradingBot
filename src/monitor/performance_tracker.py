"""Rubber戦略パフォーマンス追跡・分析モジュール。

brain_consensus.py が記録する state/rubber_signal_log.json と
state/trade_history.json を結合し、ゾーン別・vol_ratio帯別の
勝率・損益ファクター・平均損益を集計してレポートを生成する。

特にpenetration zoneでのエントリーとvol_ratioの関連性を検証する。

主な機能:
  - analyze_performance(): ゾーン/vol_ratio帯/パターン別のパフォーマンス集計
  - get_report_text(): モニタリング用テキストサマリー生成
  - run_analysis(): 定期レポート実行 (monitor.pyから呼び出し)

入力ファイル:
  - state/rubber_signal_log.json  (brain_consensus._log_rubber_signal が記録)
  - state/trade_history.json      (state_manager.record_trade が記録)

出力ファイル:
  - state/performance_report.json (分析結果JSON)
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from src.utils.config_loader import get_state_dir
from src.utils.file_lock import atomic_write_json, read_json
from src.utils.logger import setup_logger

logger = setup_logger("performance_tracker")


def _get_state_dir() -> Path:
    return get_state_dir()


def _read_rubber_signal_log() -> list[dict]:
    """state/rubber_signal_log.json を読み込む。"""
    path = _get_state_dir() / "rubber_signal_log.json"
    try:
        data = read_json(path)
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.warning("Failed to read rubber_signal_log: %s", e)
        return []


def _read_trade_history() -> list[dict]:
    """state/trade_history.json を読み込む。"""
    path = _get_state_dir() / "trade_history.json"
    try:
        data = read_json(path)
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.warning("Failed to read trade_history: %s", e)
        return []


def _parse_dt(s: str) -> datetime | None:
    """ISO文字列をdatetimeに変換。失敗時はNone。"""
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _vol_ratio_bucket(ratio: float | None) -> str:
    """vol_ratioを強度帯バケットに分類。"""
    if ratio is None:
        return "unknown"
    if ratio < 3.0:
        return "<3x"
    if ratio < 5.0:
        return "3-5x"
    if ratio < 7.0:
        return "5-7x"
    if ratio < 10.0:
        return "7-10x"
    return "10x+"


def _empty_stats() -> dict:
    return {"trades": 0, "wins": 0, "total_pnl": 0.0, "gross_profit": 0.0, "gross_loss": 0.0}


def _add_trade(stats: dict, pnl: float) -> None:
    stats["trades"] += 1
    stats["total_pnl"] += pnl
    if pnl > 0:
        stats["wins"] += 1
        stats["gross_profit"] += pnl
    else:
        stats["gross_loss"] += abs(pnl)


def _finalize(stats: dict) -> dict:
    """集計結果を確定: 勝率・平均損益・損益ファクターを計算。"""
    t = stats["trades"]
    stats["win_rate"] = round(stats["wins"] / t * 100, 1) if t > 0 else 0.0
    stats["avg_pnl"] = round(stats["total_pnl"] / t, 4) if t > 0 else 0.0
    stats["total_pnl"] = round(stats["total_pnl"], 4)
    pf_denom = stats["gross_loss"]
    stats["pf"] = round(stats["gross_profit"] / pf_denom, 2) if pf_denom > 0 else None
    # 内部計算用フィールドは削除
    stats.pop("gross_profit", None)
    stats.pop("gross_loss", None)
    return stats


def _match_trade_to_signal(
    trade: dict,
    signal_logs: list[dict],
    match_window_sec: int = 7200,
) -> dict | None:
    """trade_historyの1エントリを最近のrubber_signal_logと時刻近接で結合。

    結合条件:
      - 同シンボル
      - シグナルの timestamp <= トレードの recorded_at (エントリー → クローズの順)
      - 差が match_window_sec 以内 (デフォルト2時間)

    エントリーシグナルからクローズまでのタイムラグ (最大: ETH B momentum = 50分、
    BTC/SOL = TP/SL待ち) を考慮して2時間窓を設定。

    Args:
        trade: trade_history の1エントリ (closed_at/recorded_at を使用)
        signal_logs: rubber_signal_log のリスト
        match_window_sec: 結合許容秒数 (デフォルト2時間=7200s)

    Returns:
        最も近いシグナルlogエントリ、なければNone
    """
    symbol = trade.get("symbol", "")
    # クローズ時刻 (closed_at優先、なければrecorded_at)
    close_dt = _parse_dt(trade.get("closed_at") or trade.get("recorded_at", ""))
    if close_dt is None:
        return None

    best_signal: dict | None = None
    best_diff = float("inf")

    for sig in signal_logs:
        if sig.get("symbol") != symbol:
            continue
        sig_dt = _parse_dt(sig.get("timestamp", ""))
        if sig_dt is None:
            continue
        # シグナルはクローズより前であること
        diff = (close_dt - sig_dt).total_seconds()
        if 0 <= diff < match_window_sec and diff < best_diff:
            best_diff = diff
            best_signal = sig

    return best_signal


def analyze_performance() -> dict:
    """rubber_signal_logとtrade_historyを結合してパフォーマンスを分析。

    Returns:
        {
          "by_zone": {zone: {trades, wins, win_rate, total_pnl, avg_pnl, pf}},
          "by_vol_bucket": {bucket: {trades, wins, win_rate, total_pnl, avg_pnl, pf}},
          "by_symbol": {symbol: {trades, wins, win_rate, total_pnl, avg_pnl, pf}},
          "by_pattern": {pattern: {trades, wins, win_rate, total_pnl, avg_pnl, pf}},
          "penetration_analysis": {
              "penetration_trades": int,
              "penetration_win_rate": float,
              "penetration_avg_pnl": float,
              "penetration_pf": float | None,
              "non_penetration_trades": int,
              "non_penetration_win_rate": float,
              "penetration_vol_buckets": {bucket: stats},
          },
          "total": {trades, wins, win_rate, total_pnl, avg_pnl, pf},
          "matched_trades": int,
          "unmatched_trades": int,
          "analyzed_at": str,
        }
    """
    signal_logs = _read_rubber_signal_log()
    trade_history = _read_trade_history()

    if not trade_history:
        logger.info("No trade history to analyze")
        return {
            "total": {"trades": 0},
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
        }

    by_zone: dict[str, dict] = defaultdict(_empty_stats)
    by_vol_bucket: dict[str, dict] = defaultdict(_empty_stats)
    by_symbol: dict[str, dict] = defaultdict(_empty_stats)
    by_pattern: dict[str, dict] = defaultdict(_empty_stats)
    penetration_vol_buckets: dict[str, dict] = defaultdict(_empty_stats)
    total_stats = _empty_stats()

    matched_count = 0
    unmatched_count = 0

    for trade in trade_history:
        pnl = float(trade.get("pnl", 0) or 0)
        symbol = trade.get("symbol", "unknown")

        _add_trade(total_stats, pnl)
        _add_trade(by_symbol[symbol], pnl)

        signal = _match_trade_to_signal(trade, signal_logs)

        if signal:
            matched_count += 1
            zone = signal.get("zone") or "unknown"
            pattern = signal.get("pattern") or "unknown"
            vol_ratio = signal.get("vol_ratio")
            bucket = _vol_ratio_bucket(vol_ratio)

            _add_trade(by_zone[zone], pnl)
            _add_trade(by_vol_bucket[bucket], pnl)
            _add_trade(by_pattern[pattern], pnl)

            # penetration zone × vol_ratio の詳細追跡
            if zone == "penetration":
                _add_trade(penetration_vol_buckets[bucket], pnl)
        else:
            unmatched_count += 1
            _add_trade(by_zone["unmatched"], pnl)

    # 集計ファイナライズ
    result_by_zone = {k: _finalize(v) for k, v in by_zone.items()}
    result_by_vol = {k: _finalize(v) for k, v in by_vol_bucket.items()}
    result_by_symbol = {k: _finalize(v) for k, v in by_symbol.items()}
    result_by_pattern = {k: _finalize(v) for k, v in by_pattern.items()}
    _finalize(total_stats)

    # penetration vs non-penetration 比較
    penet_stats = result_by_zone.get("penetration", {"trades": 0, "win_rate": 0.0, "avg_pnl": 0.0, "pf": None})
    non_penet_trades = 0
    non_penet_wins = 0
    for zone_name, stats in result_by_zone.items():
        if zone_name not in ("penetration", "unmatched"):
            non_penet_trades += stats["trades"]
            non_penet_wins += stats["wins"]
    non_penet_win_rate = (
        round(non_penet_wins / non_penet_trades * 100, 1) if non_penet_trades > 0 else 0.0
    )

    penetration_analysis = {
        "penetration_trades": penet_stats.get("trades", 0),
        "penetration_win_rate": penet_stats.get("win_rate", 0.0),
        "penetration_avg_pnl": penet_stats.get("avg_pnl", 0.0),
        "penetration_pf": penet_stats.get("pf"),
        "non_penetration_trades": non_penet_trades,
        "non_penetration_win_rate": non_penet_win_rate,
        "penetration_vol_buckets": {k: _finalize(v) for k, v in penetration_vol_buckets.items()},
    }

    return {
        "by_zone": result_by_zone,
        "by_vol_bucket": result_by_vol,
        "by_symbol": result_by_symbol,
        "by_pattern": result_by_pattern,
        "penetration_analysis": penetration_analysis,
        "total": total_stats,
        "matched_trades": matched_count,
        "unmatched_trades": unmatched_count,
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
    }


def get_report_text(analysis: dict | None = None) -> str:
    """パフォーマンス分析結果を人間可読テキストに変換。

    Args:
        analysis: analyze_performance()の戻り値。Noneなら内部で実行。

    Returns:
        マルチラインテキストサマリー
    """
    if analysis is None:
        analysis = analyze_performance()

    total = analysis.get("total", {})
    lines = [
        "=== Rubber戦略 パフォーマンスサマリー ===",
        (
            f"総トレード: {total.get('trades', 0)}  "
            f"勝率: {total.get('win_rate', 0):.1f}%  "
            f"累計損益: {total.get('total_pnl', 0):.4f}  "
            f"PF: {total.get('pf') or 'N/A'}"
        ),
        "",
    ]

    # ゾーン別
    by_zone = analysis.get("by_zone", {})
    if by_zone:
        lines.append("--- ゾーン別 ---")
        for zone in sorted(by_zone.keys()):
            s = by_zone[zone]
            pf_str = f"  PF={s['pf']}" if s.get("pf") else ""
            lines.append(
                f"  {zone:22s}: {s['trades']:3d}件  "
                f"勝率{s['win_rate']:5.1f}%  "
                f"avg_pnl={s['avg_pnl']:+.4f}{pf_str}"
            )
        lines.append("")

    # vol_ratio帯別
    by_vol = analysis.get("by_vol_bucket", {})
    if by_vol:
        lines.append("--- vol_ratio帯別 ---")
        for bucket in ["<3x", "3-5x", "5-7x", "7-10x", "10x+", "unknown"]:
            if bucket not in by_vol:
                continue
            s = by_vol[bucket]
            pf_str = f"  PF={s['pf']}" if s.get("pf") else ""
            lines.append(
                f"  {bucket:8s}: {s['trades']:3d}件  "
                f"勝率{s['win_rate']:5.1f}%  "
                f"avg_pnl={s['avg_pnl']:+.4f}{pf_str}"
            )
        lines.append("")

    # penetration zone × vol_ratio 相関
    pa = analysis.get("penetration_analysis", {})
    penet_n = pa.get("penetration_trades", 0)
    non_penet_n = pa.get("non_penetration_trades", 0)
    if penet_n > 0 or non_penet_n > 0:
        lines.append("--- penetration zone 有効性検証 ---")
        lines.append(
            f"  penetration     : {penet_n:3d}件  "
            f"勝率{pa.get('penetration_win_rate', 0):5.1f}%  "
            f"avg_pnl={pa.get('penetration_avg_pnl', 0):+.4f}  "
            f"PF={pa.get('penetration_pf') or 'N/A'}"
        )
        lines.append(
            f"  non-penetration : {non_penet_n:3d}件  "
            f"勝率{pa.get('non_penetration_win_rate', 0):5.1f}%"
        )
        pvb = pa.get("penetration_vol_buckets", {})
        if pvb:
            lines.append("  [penetration zone × vol_ratio]")
            for bucket in ["3-5x", "5-7x", "7-10x", "10x+"]:
                if bucket not in pvb:
                    continue
                s = pvb[bucket]
                lines.append(
                    f"    {bucket:8s}: {s['trades']:3d}件  "
                    f"勝率{s['win_rate']:5.1f}%  "
                    f"avg_pnl={s['avg_pnl']:+.4f}"
                )
        lines.append("")

    # パターン別
    by_pattern = analysis.get("by_pattern", {})
    if by_pattern:
        lines.append("--- パターン別 ---")
        for pat in sorted(by_pattern.keys()):
            s = by_pattern[pat]
            pf_str = f"  PF={s['pf']}" if s.get("pf") else ""
            lines.append(
                f"  {pat:22s}: {s['trades']:3d}件  "
                f"勝率{s['win_rate']:5.1f}%  "
                f"avg_pnl={s['avg_pnl']:+.4f}{pf_str}"
            )
        lines.append("")

    # シンボル別
    by_symbol = analysis.get("by_symbol", {})
    if by_symbol:
        lines.append("--- シンボル別 ---")
        for sym in sorted(by_symbol.keys()):
            s = by_symbol[sym]
            pf_str = f"  PF={s['pf']}" if s.get("pf") else ""
            lines.append(
                f"  {sym:6s}: {s['trades']:3d}件  "
                f"勝率{s['win_rate']:5.1f}%  "
                f"avg_pnl={s['avg_pnl']:+.4f}{pf_str}"
            )
        lines.append("")

    matched = analysis.get("matched_trades", 0)
    unmatched = analysis.get("unmatched_trades", 0)
    lines.append(f"(signal紐付き: {matched}件, 未紐付き: {unmatched}件)")
    lines.append(f"分析時刻: {analysis.get('analyzed_at', '')}")

    return "\n".join(lines)


def run_analysis(save_report: bool = True) -> dict:
    """パフォーマンス分析を実行してレポートをログ出力・保存。

    monitor.pyのrun_monitor()から定期的に呼び出す (12サイクルごと = 1時間ごと)。

    Args:
        save_report: Trueならstate/performance_report.jsonに保存

    Returns:
        analyze_performance()の結果dict
    """
    logger.info("Running performance analysis...")
    analysis = analyze_performance()

    total = analysis.get("total", {})
    n_trades = total.get("trades", 0)

    if n_trades == 0:
        logger.info("No trades to analyze yet (trade_history.json empty or missing)")
        return analysis

    # テキストレポートをログ出力
    report_text = get_report_text(analysis)
    for line in report_text.split("\n"):
        if line.strip():
            logger.info(line)

    # JSONレポートを保存
    if save_report:
        report_path = _get_state_dir() / "performance_report.json"
        try:
            atomic_write_json(report_path, analysis)
            logger.info("Performance report saved: %s", report_path)
        except Exception as e:
            logger.warning("Failed to save performance report: %s", e)

    return analysis
