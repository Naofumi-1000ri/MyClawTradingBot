"""スパイクベース戦略の共通基底クラス。

candle parse、出来高比率計算、4Hレンジ計算、レンジ内位置計算を提供。
BtcRubberWall や将来の AltReversal が継承して使う。

ATRボラティリティ感度調整 (Volatility-Adaptive Sensitivity):
  高ボラ時: vol_threshold を引き上げてfalse positive (誤検知) を削減。
  低ボラ時: vol_threshold を引き下げて機会損失 (見逃し) を削減。
"""

from __future__ import annotations


class BaseStrategy:
    """スパイクベース戦略の基底クラス。"""

    def __init__(self, candles: list[dict], config: dict | None = None):
        self.candles = self._parse(candles)
        self.config = config or {}

    @staticmethod
    def _parse(raw: list[dict]) -> list[dict]:
        """candle dict の値を float に変換。"""
        parsed = []
        for c in raw:
            parsed.append({
                "t": c.get("t", 0),
                "o": float(c.get("o", 0)),
                "c": float(c.get("c", 0)),
                "h": float(c.get("h", 0)),
                "l": float(c.get("l", 0)),
                "v": float(c.get("v", 0)),
            })
        return parsed

    def _vol_ratio(self, window: int = 288) -> list[float]:
        """各足の出来高比率 (window本の平均比) を計算。

        Args:
            window: 平均出来高の計算窓 (デフォルト288本 = 5m×288 = 24h)

        Returns:
            各足の vol / avg_vol のリスト (candlesと同じ長さ)
        """
        n = len(self.candles)
        ratios = [0.0] * n
        for i in range(n):
            start = max(0, i - window + 1)
            chunk = self.candles[start : i + 1]
            avg = sum(c["v"] for c in chunk) / len(chunk) if chunk else 0
            ratios[i] = self.candles[i]["v"] / avg if avg > 0 else 0.0
        return ratios

    def _h4_range(self, idx: int, h4_window: int = 48) -> tuple[float, float]:
        """指定idx時点の直近4H high/low を返す。

        Args:
            idx: 対象足のインデックス
            h4_window: 4Hレンジ計算窓 (デフォルト48本 = 5m×48 = 4h)

        Returns:
            (h4_low, h4_high) タプル
        """
        start = max(0, idx - h4_window + 1)
        chunk = self.candles[start : idx + 1]
        if not chunk:
            c = self.candles[idx]
            return (c["l"], c["h"])
        h4_low = min(c["l"] for c in chunk)
        h4_high = max(c["h"] for c in chunk)
        return (h4_low, h4_high)

    @staticmethod
    def _range_position(close: float, h4_low: float, h4_high: float) -> float:
        """4Hレンジ内の位置 (%) を返す。

        0 = 底, 100 = 天, マイナス = 下抜け, 100超 = 上抜け。
        """
        span = h4_high - h4_low
        if span <= 0:
            return 50.0
        return (close - h4_low) / span * 100.0

    def _atr_volatility_multiplier(
        self,
        idx: int,
        short_window: int = 24,
        long_window: int = 288,
        high_vol_threshold: float = 1.5,
        low_vol_threshold: float = 0.7,
        high_vol_factor: float = 1.20,
        low_vol_factor: float = 0.85,
    ) -> tuple[float, str]:
        """ATR比率に基づく出来高閾値の動的感度調整乗数を計算。

        市場ボラティリティに応じて vol_threshold の乗数を返す:
          - 高ボラ (ATR_short/ATR_long > high_vol_threshold):
              乗数 = high_vol_factor (デフォルト 1.20: 閾値+20%)
              → 平均出来高の上昇による誤検知 (false positive) を抑制
          - 低ボラ (ATR_short/ATR_long < low_vol_threshold):
              乗数 = low_vol_factor (デフォルト 0.85: 閾値-15%)
              → 静かな市場でのシグナル見逃しを削減
          - 通常 (low_vol ≤ ratio ≤ high_vol):
              乗数 = 1.0 (変更なし)

        ATR計算: candle の (high - low) の単純移動平均。
        True Range の full計算ではなく高速な近似値を使用。

        Args:
            idx: 対象足のインデックス
            short_window: 短期ATR計算窓 (デフォルト24本 = 5m×24 = 2h)
            long_window: 長期ATR計算窓 (デフォルト288本 = 5m×288 = 24h)
            high_vol_threshold: 高ボラ判定比率 (デフォルト 1.5)
            low_vol_threshold: 低ボラ判定比率 (デフォルト 0.7)
            high_vol_factor: 高ボラ時の乗数 (デフォルト 1.20)
            low_vol_factor: 低ボラ時の乗数 (デフォルト 0.85)

        Returns:
            (multiplier, regime_label) タプル
              multiplier: vol_threshold に掛ける乗数 (float)
              regime_label: "high_vol" / "low_vol" / "normal" (ログ用)
        """
        n = len(self.candles)
        if idx < short_window or n <= short_window:
            return 1.0, "normal"

        # 短期ATR (直近short_window本)
        short_start = max(0, idx - short_window + 1)
        short_chunk = self.candles[short_start : idx + 1]
        short_atr = (
            sum(c["h"] - c["l"] for c in short_chunk) / len(short_chunk)
            if short_chunk else 0.0
        )

        # 長期ATR (直近long_window本)
        long_start = max(0, idx - long_window + 1)
        long_chunk = self.candles[long_start : idx + 1]
        long_atr = (
            sum(c["h"] - c["l"] for c in long_chunk) / len(long_chunk)
            if long_chunk else 0.0
        )

        if long_atr <= 0 or short_atr <= 0:
            return 1.0, "normal"

        atr_ratio = short_atr / long_atr

        if atr_ratio > high_vol_threshold:
            return high_vol_factor, "high_vol"
        elif atr_ratio < low_vol_threshold:
            return low_vol_factor, "low_vol"
        else:
            return 1.0, "normal"

    def scan(self) -> dict | None:
        """サブクラスで実装。シグナルまたはNoneを返す。"""
        raise NotImplementedError
