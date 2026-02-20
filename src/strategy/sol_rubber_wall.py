"""SOL専用: ゴムの壁モデル (BTC型モメンタム)。

BTC RubberWall と同じ方向性 (BEAR spike → SHORT) だが、
SOL 固有のボラティリティに合わせた広いTP/SL設定。

分析結果 (90日バックテスト):
  - SOLはBTC型 (BEAR spike後、価格は反発せずさらに下がる)
  - SHORT favorable/adverse ratio = 1.68 (LONG = 0.59)
  - Best: BEAR 5x → SHORT TP=1.0%/SL=0.5% PF=1.41, Net=+5.38%

2026-02-21 最適化 (実運用データに基づく):
  - SLヒット原因: SOL 5分足ノイズ幅 0.3-1% に対しSL 0.5%が狭すぎた
  - vol_threshold 5.0x → 6.0x (シグナル品質向上)
  - penetration SL 0.5% → 0.8%、TP 1.0% → 1.5% (R:R=1.875維持)
  - upper_range SL 0.4% → 0.6%、TP 0.8% → 1.2%
  - funding_rate < -2e-5 時のSHORT禁止 (ショートスクイーズ回避)

ゾーン:
  貫通    (-20% ~ 0%):    SHORT  TP 1.5%  SL 0.8% (最強ゾーン: 62-71% win)
  レンジ上 (20% ~):        SHORT  TP 1.2%  SL 0.6%
  深突破   (~ -20%):       LONG   TP 0.8%  SL 0.5% (反転。7x+ threshold)
  底付近   (0% ~ 20%):     SKIP
"""

from __future__ import annotations

from src.strategy.base import BaseStrategy
from src.utils.logger import setup_logger

logger = setup_logger("sol_rubber_wall")

_DEFAULT_CONFIG = {
    "vol_threshold": 6.0,       # メインSHORT: 6x (実運用で5xはノイズシグナルが多く損失)
    "deep_threshold": 7.0,      # deep_below LONG: 7x+ のみ
    "h4_window": 48,
    "vol_window": 288,
    # funding_rate がこの閾値より低い場合、SHORT新規エントリー禁止 (スクイーズ回避)
    "funding_short_block_threshold": -2e-5,
    "zones": {
        # penetration: SL 0.5%→0.8% (5分足ノイズ耐性強化), TP 1.0%→1.5% (R:R≈1.875)
        "penetration": {"range": [-20, 0], "direction": "short", "tp_pct": 0.015, "sl_pct": 0.008},
        # upper_range: SL 0.4%→0.6%, TP 0.8%→1.2%
        "upper_range": {"range": [20, 999], "direction": "short", "tp_pct": 0.012, "sl_pct": 0.006},
        # deep_reversal: LONG は据え置き (SL 0.5% はLONG時は反対方向なので問題なし)
        "deep_reversal": {"range": [-999, -20], "direction": "long", "tp_pct": 0.008, "sl_pct": 0.005},
        # bottom (0~20): skip
    },
}


class SolRubberWall(BaseStrategy):
    """SOL専用: 出来高スパイク + 4Hレンジ位置 → ゴムの壁モデル (BTC型)。"""

    def __init__(self, candles: list[dict], config: dict | None = None):
        super().__init__(candles, config)
        merged = dict(_DEFAULT_CONFIG)
        if config:
            for k, v in config.items():
                if k == "zones" and isinstance(v, dict):
                    merged["zones"] = {**_DEFAULT_CONFIG["zones"], **v}
                else:
                    merged[k] = v
        self.cfg = merged

    def scan(self, cache: dict | None = None) -> tuple[dict | None, dict]:
        """直近確定足をチェック。BEARスパイク検知時にシグナルを返す。

        BTC RubberWall と同じキャッシュ方式。
        deep_reversal ゾーンのみ高閾値 (7x) を適用。

        Returns:
            (signal_or_None, next_cache) タプル
        """
        if len(self.candles) < self.cfg["h4_window"] + 10:
            logger.warning("Insufficient candles: %d (need >= %d)",
                           len(self.candles), self.cfg["h4_window"] + 10)
            return None, {}

        vol_threshold = self.cfg["vol_threshold"]
        h4_window = self.cfg["h4_window"]

        idx = len(self.candles) - 2
        if idx < h4_window:
            return None, {}

        candle = self.candles[idx]
        is_bear = candle["c"] < candle["o"]

        # --- Fast path: キャッシュ閾値で O(1) 判定 ---
        if cache and cache.get("next_target_t") == candle["t"]:
            threshold_vol = cache["threshold_vol"]

            if candle["v"] < threshold_vol or not is_bear:
                next_cache = self._build_next_cache(idx)
                return None, next_cache

            ratio = self._vol_ratio_single(idx)
            logger.info("Cache hit SPIKE: vol=%.1f >= threshold=%.1f, ratio=%.1f",
                        candle["v"], threshold_vol, ratio)
        else:
            ratio = self._vol_ratio_single(idx)

            if ratio < vol_threshold or not is_bear:
                next_cache = self._build_next_cache(idx)
                return None, next_cache

            logger.info("BEAR spike detected: vol_ratio=%.1f, change=%.2f%%",
                        ratio, (candle["c"] - candle["o"]) / candle["o"] * 100)

        # --- スパイク確定: ゾーン分析 ---
        h4_low, h4_high = self._h4_range(idx - 1, h4_window)
        pos = self._range_position(candle["c"], h4_low, h4_high)

        logger.info("4H range: low=%.2f, high=%.2f, close=%.2f, position=%.1f%%",
                     h4_low, h4_high, candle["c"], pos)

        zones = self.cfg["zones"]
        matched_zone = None
        matched_cfg = None
        for zone_name, zcfg in zones.items():
            lo, hi = zcfg["range"]
            if lo <= pos < hi:
                matched_zone = zone_name
                matched_cfg = zcfg
                break

        next_cache = self._build_next_cache(idx)

        if matched_zone is None:
            logger.info("Position %.1f%% falls in skip zone (bottom 0-20%%)", pos)
            return None, next_cache

        direction = matched_cfg["direction"]

        # deep_reversal (LONG) は高閾値を要求
        deep_thr = self.cfg["deep_threshold"]
        if matched_zone == "deep_reversal" and ratio < deep_thr:
            logger.info("deep_reversal: ratio %.1f < deep_threshold %.1f, skip",
                        ratio, deep_thr)
            return None, next_cache

        # funding rate フィルター: 極端なネガティブfundingでのSHORTはスクイーズリスク高
        if direction == "short":
            funding_rate = self.cfg.get("current_funding_rate", 0.0)
            block_threshold = self.cfg.get("funding_short_block_threshold", -2e-5)
            if funding_rate < block_threshold:
                logger.info(
                    "SHORT blocked: funding_rate=%.2e < threshold=%.2e (squeeze risk)",
                    funding_rate, block_threshold
                )
                return None, next_cache

        tp_pct = matched_cfg["tp_pct"]
        sl_pct = matched_cfg["sl_pct"]
        entry_price = candle["c"]

        # TP/SL 計算
        if direction == "short":
            tp_price = entry_price * (1 - tp_pct)
            sl_price = entry_price * (1 + sl_pct)
        else:  # long
            tp_price = entry_price * (1 + tp_pct)
            sl_price = entry_price * (1 - sl_pct)

        signal = {
            "symbol": "SOL",
            "action": direction,
            "direction": direction,
            "confidence": 0.80,
            "entry_price": round(entry_price, 4),
            "take_profit": round(tp_price, 4),
            "stop_loss": round(sl_price, 4),
            "leverage": 3,
            "reasoning": (
                f"SolRubberWall: {matched_zone} zone (pos={pos:.1f}%), "
                f"vol_ratio={ratio:.1f}x, "
                f"4H=[{h4_low:.4f}-{h4_high:.4f}], "
                f"→ {direction} TP {tp_pct*100:.1f}% SL {sl_pct*100:.1f}%"
            ),
            "zone": matched_zone,
            "pattern": f"wall_{matched_zone}",
            "exit_mode": "tp_sl",
            "range_position": round(pos, 1),
            "vol_ratio": round(ratio, 1),
            "spike_time": candle["t"],
        }

        logger.info("Signal: %s %s @ %.4f, TP=%.4f, SL=%.4f (zone=%s)",
                     direction, "SOL", entry_price, tp_price, sl_price, matched_zone)
        return signal, next_cache

    def _vol_ratio_single(self, idx: int) -> float:
        """単一足の出来高比率を計算。O(window)。"""
        window = self.cfg["vol_window"]
        start = max(0, idx - window + 1)
        chunk = self.candles[start : idx + 1]
        avg = sum(c["v"] for c in chunk) / len(chunk) if chunk else 0
        return self.candles[idx]["v"] / avg if avg > 0 else 0.0

    def _build_next_cache(self, current_idx: int) -> dict:
        """次の足の閾値volumeを事前計算。"""
        vol_window = self.cfg["vol_window"]
        vol_threshold = self.cfg["vol_threshold"]

        next_idx = current_idx + 1
        start = max(0, next_idx - vol_window + 1)
        end = min(current_idx + 1, len(self.candles))
        sum_known = sum(self.candles[i]["v"] for i in range(start, end))
        n_known = end - start
        n_total = n_known + 1

        denominator = n_total - vol_threshold
        if denominator <= 0:
            threshold_vol = float("inf")
        else:
            threshold_vol = vol_threshold * sum_known / denominator

        if next_idx < len(self.candles):
            next_t = self.candles[next_idx]["t"]
        else:
            next_t = self.candles[current_idx]["t"] + 300_000

        return {
            "next_target_t": next_t,
            "threshold_vol": round(threshold_vol, 4),
        }
