"""ETH専用: ゴムバンドモデル。

BTC の RubberWall とは逆のロジック。
vol_ratio の強度帯で挙動が反転する ETH 固有の特性を利用。

Pattern A (reversal):  高閾値 BEAR spike → LONG (平均回帰)
  - vol_ratio >= 7.0 の大スパイクはオーバーシュート → 戻る (旧 6.0 から引き上げ)
  - 4Hレンジ下位40%では抑制 (4Hダウントレンド中の逆張りを回避)
  - TP = 固定 0.5%, SL = max(IN足low - 0.05%pad, entry * (1 - 0.25%))
  - SL最小距離を0.25%に拡大 (旧0.1%: ノイズでSLが頻発した問題を修正)

Pattern B (momentum):  中閾値 BEAR spike + 上位ゾーン → SHORT
  - vol_ratio 3.0-7.0 かつ 4Hレンジ position >= 30%
  - TP = 時間カット 10bar (50分), SL = IN足high + 0.05%pad
  - 56% win, PF 4.70, avg +0.47%/trade
"""

from __future__ import annotations

from src.strategy.base import BaseStrategy
from src.utils.logger import setup_logger

logger = setup_logger("eth_rubber_band")

_DEFAULT_CONFIG = {
    # Pattern A: reversal
    "reversal_threshold": 7.0,      # 旧6.0→7.0: より強い出来高スパイクのみ反転狙い
    "reversal_tp_pct": 0.005,       # 旧0.4%→0.5%: SL拡大に合わせてR:R維持
    "reversal_sl_pad_pct": 0.0005,  # 0.05% pad below candle low
    "reversal_sl_min_dist": 0.0025, # 旧0.1%→0.25%: ノイズ耐性確保 (直近SL頻発問題修正)
    "reversal_h4_filter_pct": 40,   # 4Hレンジ下位40%では反転LONG抑制 (ダウントレンド中回避)
    # Pattern B: momentum
    "momentum_threshold": 3.0,      # lower bound
    "momentum_zone_min": 30,        # 4H position >= 30%
    "momentum_cut_bars": 10,        # 50分で時間カット
    "momentum_sl_pad_pct": 0.0005,  # 0.05% pad above candle high
    # shared
    "h4_window": 48,
    "vol_window": 288,
}


class EthRubberBand(BaseStrategy):
    """ETH専用: 2パターン ゴムバンドモデル。"""

    def __init__(self, candles: list[dict], config: dict | None = None):
        super().__init__(candles, config)
        merged = dict(_DEFAULT_CONFIG)
        if config:
            merged.update(config)
        self.cfg = merged

    def scan(self, cache: dict | None = None) -> tuple[dict | None, dict]:
        """直近確定足をチェック。

        キャッシュあり → 閾値比較のみ O(1)
        キャッシュなし → 対象足のみ計算 O(window)

        Returns:
            (signal_or_None, next_cache) タプル
        """
        if len(self.candles) < self.cfg["h4_window"] + 10:
            logger.warning("Insufficient candles: %d", len(self.candles))
            return None, {}

        h4_window = self.cfg["h4_window"]
        reversal_thr = self.cfg["reversal_threshold"]
        momentum_thr = self.cfg["momentum_threshold"]

        idx = len(self.candles) - 2
        if idx < h4_window:
            return None, {}

        candle = self.candles[idx]
        is_bear = candle["c"] < candle["o"]

        if not is_bear:
            return None, self._build_next_cache(idx)

        # --- Fast path: キャッシュ閾値で判定 ---
        if cache and cache.get("next_target_t") == candle["t"]:
            threshold_vol = cache["threshold_vol"]
            if candle["v"] < threshold_vol:
                return None, self._build_next_cache(idx)
            ratio = self._vol_ratio_single(idx)
        else:
            ratio = self._vol_ratio_single(idx)
            if ratio < momentum_thr:
                return None, self._build_next_cache(idx)

        # --- パターン判定 ---
        # reversal_threshold 以上 → Pattern A (reversal LONG)
        # momentum_threshold 以上 & reversal_threshold 未満 → Pattern B (momentum SHORT)
        if ratio >= reversal_thr:
            return self._pattern_a_reversal(idx, candle, ratio)
        else:
            return self._pattern_b_momentum(idx, candle, ratio)

    def _pattern_a_reversal(
        self, idx: int, candle: dict, ratio: float
    ) -> tuple[dict | None, dict]:
        """Pattern A: 高閾値 BEAR spike → LONG reversal。

        SL = max(candle low - 0.05%pad, entry * (1 - 0.25%))  ← 最小距離0.25%で頻発SL修正
        TP = 固定 0.5%
        4Hフィルター: 4Hレンジ下位40%では抑制 (ダウントレンド継続中の逆張り回避)
        """
        h4_window = self.cfg["h4_window"]
        h4_filter_pct = self.cfg["reversal_h4_filter_pct"]

        # --- 4Hトレンドフィルター ---
        h4_low, h4_high = self._h4_range(idx - 1, h4_window)
        h4_pos = self._range_position(candle["c"], h4_low, h4_high)

        if h4_pos < h4_filter_pct:
            logger.info(
                "Pattern A: SKIP (4H pos=%.1f%% < filter=%d%%, 4H=[%.2f-%.2f], "
                "4H下位ゾーン → ダウントレンド継続リスク高)",
                h4_pos, h4_filter_pct, h4_low, h4_high,
            )
            return None, self._build_next_cache(idx)

        tp_pct = self.cfg["reversal_tp_pct"]
        sl_pad = self.cfg["reversal_sl_pad_pct"]
        sl_min_dist = self.cfg["reversal_sl_min_dist"]
        entry = candle["c"]

        # SL = candle low に pad を加えた値
        sl_from_candle = round(candle["l"] * (1 - sl_pad), 2)
        sl_from_min = round(entry * (1 - sl_min_dist), 2)
        # 最低 SL 距離を保証 (0.25%): candle low SL と最低距離 SL の低い方を採用
        sl_price = min(sl_from_candle, sl_from_min)
        sl_dist = (entry - sl_price) / entry

        tp_price = round(entry * (1 + tp_pct), 2)

        logger.info(
            "Pattern A (reversal): vol_ratio=%.1f, change=%.2f%%, "
            "SL=%.2f (candle_low=%.2f, min_dist=%.2f, sl_dist=%.2f%%), "
            "4H pos=%.1f%%",
            ratio, (candle["c"] - candle["o"]) / candle["o"] * 100,
            sl_price, sl_from_candle, sl_from_min, sl_dist * 100, h4_pos,
        )

        signal = {
            "symbol": "ETH",
            "action": "long",
            "direction": "long",
            "confidence": 0.85,
            "entry_price": round(entry, 2),
            "take_profit": tp_price,
            "stop_loss": sl_price,
            "leverage": 3,
            "reasoning": (
                f"EthRubberBand A: reversal, vol_ratio={ratio:.1f}x, "
                f"4H_pos={h4_pos:.1f}%, "
                f"BEAR spike → LONG TP {tp_pct*100:.1f}% SL={sl_dist*100:.2f}%"
            ),
            "zone": "reversal",
            "pattern": "A_reversal",
            "exit_mode": "tp_sl",
            "vol_ratio": round(ratio, 1),
            "spike_time": candle["t"],
        }
        logger.info(
            "Signal: long ETH @ %.2f, TP=%.2f, SL=%.2f (reversal, sl_dist=%.2f%%)",
            entry, tp_price, sl_price, sl_dist * 100,
        )
        return signal, self._build_next_cache(idx)

    def _pattern_b_momentum(
        self, idx: int, candle: dict, ratio: float
    ) -> tuple[dict | None, dict]:
        """Pattern B: 中閾値 BEAR spike + 上位ゾーン → SHORT momentum。

        SL = IN足high + 0.05% pad (スパイク否定ライン)
        TP = 時間カット 10bar (50分後にclose決済)
        """
        h4_window = self.cfg["h4_window"]
        zone_min = self.cfg["momentum_zone_min"]

        h4_low, h4_high = self._h4_range(idx - 1, h4_window)
        pos = self._range_position(candle["c"], h4_low, h4_high)

        logger.info(
            "Pattern B check: vol_ratio=%.1f, 4H pos=%.1f%% (need >= %d%%)",
            ratio, pos, zone_min,
        )

        next_cache = self._build_next_cache(idx)

        if pos < zone_min:
            logger.info("Position %.1f%% < %d%%, skip", pos, zone_min)
            return None, next_cache

        sl_pad = self.cfg["momentum_sl_pad_pct"]
        cut_bars = self.cfg["momentum_cut_bars"]
        entry = candle["c"]

        # SL = candle high に pad を加えた値
        sl_price = round(candle["h"] * (1 + sl_pad), 2)
        sl_dist = (sl_price - entry) / entry
        if sl_dist < 0.001:
            sl_price = round(entry * (1 + 0.001), 2)

        # TP は時間カットなので設定しない (brain 側で管理)
        # trade_executor の R:R チェックを通すため形式的に遠い値を設定
        tp_price = round(entry * (1 - 0.01), 2)

        signal = {
            "symbol": "ETH",
            "action": "short",
            "direction": "short",
            "confidence": 0.80,
            "entry_price": round(entry, 2),
            "take_profit": tp_price,
            "stop_loss": sl_price,
            "leverage": 3,
            "reasoning": (
                f"EthRubberBand B: momentum, vol_ratio={ratio:.1f}x, "
                f"pos={pos:.1f}%, 4H=[{h4_low:.2f}-{h4_high:.2f}], "
                f"→ SHORT {cut_bars}bar cut, SL=candle_high"
            ),
            "zone": "momentum",
            "pattern": "B_momentum",
            "exit_mode": "time_cut",
            "exit_bars": cut_bars,
            "range_position": round(pos, 1),
            "vol_ratio": round(ratio, 1),
            "spike_time": candle["t"],
        }
        logger.info(
            "Signal: short ETH @ %.2f, SL=%.2f, exit=%dbar (momentum, candle-SL)",
            entry, sl_price, cut_bars,
        )
        return signal, next_cache

    def _vol_ratio_single(self, idx: int) -> float:
        """単一足の出来高比率。O(window)。"""
        window = self.cfg["vol_window"]
        start = max(0, idx - window + 1)
        chunk = self.candles[start : idx + 1]
        avg = sum(c["v"] for c in chunk) / len(chunk) if chunk else 0
        return self.candles[idx]["v"] / avg if avg > 0 else 0.0

    def _build_next_cache(self, current_idx: int) -> dict:
        """次の足の閾値volumeを事前計算。

        momentum_threshold (最低ライン) を基準に閾値を算出。
        これを下回れば Pattern A/B どちらもありえない。
        """
        vol_window = self.cfg["vol_window"]
        threshold = self.cfg["momentum_threshold"]

        next_idx = current_idx + 1
        start = max(0, next_idx - vol_window + 1)
        end = min(current_idx + 1, len(self.candles))
        sum_known = sum(self.candles[i]["v"] for i in range(start, end))
        n_known = end - start
        n_total = n_known + 1

        denom = n_total - threshold
        if denom <= 0:
            threshold_vol = float("inf")
        else:
            threshold_vol = threshold * sum_known / denom

        if next_idx < len(self.candles):
            next_t = self.candles[next_idx]["t"]
        else:
            next_t = self.candles[current_idx]["t"] + 300_000

        return {
            "next_target_t": next_t,
            "threshold_vol": round(threshold_vol, 4),
        }
