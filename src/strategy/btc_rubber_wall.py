"""BTC専用: ゴムの壁モデル。

出来高スパイク (BEAR candle) + 4Hレンジ内の位置で
エントリー方向と TP を動的決定する。

最適化: 毎サイクル全足スキャンせず、次の足の閾値volumeを事前計算。
キャッシュヒット時は O(1) で判定完了。

ゾーン:
  貫通    (-20% ~ 0%):   LONG   TP 0.3%  SL 0.6% (30日分 vol>=5x: LONG_wr=55%, PF=2.33)
  レンジ上  (20% ~):       SHORT  TP 0.3%  (30日分 vol>=5x: SHORT_wr=59%, PF=2.06)
  深突破   (~ -20%):      SKIP   (LONG/SHORT 双方 30-40%。エッジ不明確のためSKIP)
  底付近   (0% ~ 20%):    SKIP   (SHORT_wr=55%だが現価格がレンジ底近傍でリスク高)

2026-02-21 vol_threshold 7.0→5.0 変更:
  - 30日分バックテスト: vol>=5.0 BEAR spike のうち upper ゾーンで 59%勝率
  - vol>=7.0 は3日分で2回のみ → 機会損失が過大
  - vol>=5.0 は30日で68回 → 適切な頻度 (1日2-3回)

2026-02-21 penetration ゾーン LONG 変更:
  - 30日BT n=22: LONG_wr=55%, SHORT_wr=32%, avg_ret=+0.230%
  - TP 0.3%/SL 0.6% シミュレーション: W=14/L=3 PF=2.33
  - BEARスパイク + レンジ下抜け = 売り過剰 → 反転LONGが有効
  - 旧設定(SHORT)は逆方向で損失原因だった

2026-02-21 deep_reversal SKIP 変更:
  - 旧LONG: 40%勝率でエッジ不明確。penetrationと合わせてSKIPに変更
"""

from __future__ import annotations

from src.strategy.base import BaseStrategy
from src.utils.logger import setup_logger

logger = setup_logger("btc_rubber_wall")

# デフォルト設定
_DEFAULT_CONFIG = {
    "vol_threshold": 5.0,   # 旧7.0→5.0: 30日バックテストで upper 59%勝率、機会損失削減
    "h4_window": 48,
    "vol_window": 288,
    "zones": {
        # penetration: LONG変更 (旧SHORT)
        # 30日BT BEAR spike>=5x: LONG_wr=55%, TP0.3%/SL0.6%でPF=2.33
        # BEARスパイク + 4Hレンジ下抜けは売り過剰 → 反転LONG
        "penetration": {"range": [-20, 0], "direction": "long", "tp_pct": 0.003, "sl_pct": 0.006},
        # upper_range: SHORT維持
        # 30日BT BEAR spike>=5x: SHORT_wr=59%, avg=-0.321%
        "upper_range": {"range": [20, 999], "direction": "short", "tp_pct": 0.003, "sl_pct": 0.006},
        # deep_reversal: SKIP (旧LONG: 30-40%勝率でエッジ不明確)
        # "deep_reversal": {"range": [-999, -20], "direction": "long", "tp_pct": 0.003},
        # bottom (0~20): skip (SHORT_wr=55%だが現位置がレンジ底近傍でリスク高)
    },
}


class BtcRubberWall(BaseStrategy):
    """BTC専用: 出来高スパイク + 4Hレンジ位置 → ゴムの壁モデル。"""

    def __init__(self, candles: list[dict], config: dict | None = None):
        super().__init__(candles, config)
        # デフォルトにユーザー設定をマージ
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

        キャッシュあり → 閾値比較のみ O(1)
        キャッシュなし → 対象足のみ計算 O(window)
        スパイク検知時のみゾーン分析を実行。

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

            # スパイク検知 — ログ用に実際の ratio を計算
            ratio = self._vol_ratio_single(idx)
            logger.info("Cache hit SPIKE: vol=%.1f >= threshold=%.1f, ratio=%.1f",
                        candle["v"], threshold_vol, ratio)
        else:
            # --- Slow path: 対象足だけ計算 O(window) ---
            ratio = self._vol_ratio_single(idx)

            if ratio < vol_threshold or not is_bear:
                next_cache = self._build_next_cache(idx)
                return None, next_cache

            logger.info("BEAR spike detected: vol_ratio=%.1f, change=%.2f%%",
                        ratio, (candle["c"] - candle["o"]) / candle["o"] * 100)

        # --- スパイク確定: ゾーン分析 (到達は稀) ---
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
        tp_pct = matched_cfg["tp_pct"]
        # sl_pct が明示指定されていれば使用、なければ tp_pct * 2 をフォールバック
        sl_pct = matched_cfg.get("sl_pct", tp_pct * 2)
        entry_price = candle["c"]

        # TP/SL 計算
        if direction == "short":
            tp_price = entry_price * (1 - tp_pct)
            sl_price = entry_price * (1 + sl_pct)
        else:  # long
            tp_price = entry_price * (1 + tp_pct)
            sl_price = entry_price * (1 - sl_pct)

        signal = {
            "symbol": "BTC",
            "action": direction,
            "direction": direction,
            "confidence": 0.85,
            "entry_price": round(entry_price, 2),
            "take_profit": round(tp_price, 2),
            "stop_loss": round(sl_price, 2),
            "leverage": 3,
            "reasoning": (
                f"RubberWall: {matched_zone} zone (pos={pos:.1f}%), "
                f"vol_ratio={ratio:.1f}x, "
                f"4H=[{h4_low:.2f}-{h4_high:.2f}], "
                f"→ {direction} TP {tp_pct*100:.1f}% SL {sl_pct*100:.1f}%"
            ),
            "zone": matched_zone,
            "range_position": round(pos, 1),
            "vol_ratio": round(ratio, 1),
            "spike_time": candle["t"],
        }

        logger.info("Signal: %s %s @ %.2f, TP=%.2f, SL=%.2f (zone=%s)",
                     direction, "BTC", entry_price, tp_price, sl_price, matched_zone)
        return signal, next_cache

    def _vol_ratio_single(self, idx: int) -> float:
        """単一足の出来高比率を計算。O(window)。"""
        window = self.cfg["vol_window"]
        start = max(0, idx - window + 1)
        chunk = self.candles[start : idx + 1]
        avg = sum(c["v"] for c in chunk) / len(chunk) if chunk else 0
        return self.candles[idx]["v"] / avg if avg > 0 else 0.0

    def _build_next_cache(self, current_idx: int) -> dict:
        """次の足の閾値volumeを事前計算。

        数式:
          ratio = V / avg >= threshold
          V / ((sum_prev + V) / N) >= threshold
          V * (N - threshold) >= threshold * sum_prev
          V >= threshold * sum_prev / (N - threshold)
        """
        vol_window = self.cfg["vol_window"]
        vol_threshold = self.cfg["vol_threshold"]

        # 次の対象足 = current_idx + 1
        # その window: [next_idx - vol_window + 1 .. next_idx] (N本)
        # 既知: [next_idx - vol_window + 1 .. current_idx] (N-1本)
        # 未知: next_idx (これが閾値比較対象)
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

        # 次の対象足のタイムスタンプ
        if next_idx < len(self.candles):
            next_t = self.candles[next_idx]["t"]
        else:
            next_t = self.candles[current_idx]["t"] + 300_000  # 5min ms

        return {
            "next_target_t": next_t,
            "threshold_vol": round(threshold_vol, 4),
        }
