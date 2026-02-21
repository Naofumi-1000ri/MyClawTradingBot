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

    @staticmethod
    def confidence_to_leverage(confidence: float, base_leverage: int = 3) -> int:
        """Confidence-Adaptive Position Sizing (CAPS): confidence に応じてレバレッジを削減。

        低確信度シグナル（quiet系パターン）に対して小さいレバレッジを返す。
        高確信度シグナル（スパイク系）はbase_leverageをそのまま使用。

        マッピング:
          confidence >= 0.80: base_leverage (3x) - スパイク系 (Pattern A/B/main)
          confidence >= 0.74: base_leverage - 1 (2x) - 中確信度 quiet (5m GOLDEN)
          confidence < 0.74:  max(1, base_leverage - 2) (1x) - 低確信度 (4H EMAのみ等)

        Args:
            confidence: シグナルの確信度 (0.0 - 1.0)
            base_leverage: 標準レバレッジ (デフォルト3)

        Returns:
            適用するレバレッジ整数値 (最小1)
        """
        if confidence >= 0.80:
            return base_leverage
        elif confidence >= 0.74:
            return max(1, base_leverage - 1)
        else:
            return max(1, base_leverage - 2)

    def _rsi(self, idx: int, period: int = 14) -> float | None:
        """RSI (Relative Strength Index) を計算。

        Args:
            idx: 対象足のインデックス
            period: RSI計算窓 (デフォルト14本)

        Returns:
            RSI値 (0-100)。データ不足時はNone。
        """
        needed = period + 1
        start = max(0, idx - needed * 2 + 1)  # 余裕をもって取得
        closes = [self.candles[i]["c"] for i in range(start, idx + 1)]
        if len(closes) < needed:
            return None

        gains = []
        losses = []
        for i in range(1, len(closes)):
            delta = closes[i] - closes[i - 1]
            if delta > 0:
                gains.append(delta)
                losses.append(0.0)
            else:
                gains.append(0.0)
                losses.append(abs(delta))

        if len(gains) < period:
            return None

        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period

        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def _price_momentum(self, idx: int, window: int = 6) -> float:
        """直近N本の価格変化率の合計 (方向モメンタム)。

        プラスなら上昇モメンタム、マイナスなら下落モメンタム。
        スパイクなし環境でトレンド方向を確認するために使用。

        Args:
            idx: 対象足のインデックス
            window: 計算窓 (デフォルト6本=30分)

        Returns:
            直近window本の終値変化率の合計 (%)
        """
        start = max(0, idx - window)
        if start >= idx:
            return 0.0
        base = self.candles[start]["c"]
        current = self.candles[idx]["c"]
        if base <= 0:
            return 0.0
        return (current - base) / base * 100.0

    def _bb_squeeze(
        self,
        idx: int,
        window: int = 20,
        mult: float = 2.0,
        squeeze_ratio: float = 0.6,
    ) -> bool:
        """ボリンジャーバンドのスクイーズ (収縮) を検出。

        直近windowの BB幅 が 長期windowのBB幅平均に対して
        squeeze_ratio 以下なら「スクイーズ状態」と判定。
        スクイーズ = 価格レンジが収縮 = ブレイクアウト前の静寂。

        Args:
            idx: 対象足のインデックス
            window: BB計算窓 (デフォルト20本)
            mult: バンド幅の標準偏差倍率 (デフォルト2.0)
            squeeze_ratio: スクイーズ判定比率 (デフォルト0.6: 60%以下)

        Returns:
            True = スクイーズ中 (低ボラ・コンソリデーション)
        """
        needed = window * 2
        if idx < needed:
            return False

        def _bb_width(candles_slice: list[dict]) -> float:
            closes = [c["c"] for c in candles_slice]
            mean = sum(closes) / len(closes)
            variance = sum((x - mean) ** 2 for x in closes) / len(closes)
            std = variance ** 0.5
            upper = mean + mult * std
            lower = mean - mult * std
            return (upper - lower) / mean if mean > 0 else 0.0

        # 現在のBB幅
        current_slice = self.candles[max(0, idx - window + 1):idx + 1]
        if len(current_slice) < window:
            return False
        current_width = _bb_width(current_slice)

        # 長期の平均BB幅 (window本前の同窓)
        past_start = max(0, idx - window * 2 + 1)
        past_end = max(0, idx - window + 1)
        past_slice = self.candles[past_start:past_end + window]
        if len(past_slice) < window:
            return False
        past_width = _bb_width(past_slice)

        if past_width <= 0:
            return False
        return (current_width / past_width) <= squeeze_ratio

    def _candle_body_ratio(self, idx: int, window: int = 3) -> float:
        """直近N本の平均ボディ/レンジ比率。

        0.0 = 全部ドジ足 (方向性なし)
        1.0 = 全部ロングボディ足 (方向性あり)

        スパイクなし環境でのエントリー前に方向性のある値動きかを確認。
        ドジ足が多い = ノイズ状態 → quiet系パターン発火を抑制。

        Args:
            idx: 対象足のインデックス
            window: 計算窓 (デフォルト3本=15分)

        Returns:
            平均ボディ比率 (0.0 - 1.0)
        """
        start = max(0, idx - window + 1)
        chunk = self.candles[start:idx + 1]
        if not chunk:
            return 0.5
        ratios = []
        for c in chunk:
            body = abs(c["c"] - c["o"])
            rng = c["h"] - c["l"]
            ratios.append(body / rng if rng > 0 else 0.0)
        return sum(ratios) / len(ratios)

    def scan(self) -> dict | None:
        """サブクラスで実装。シグナルまたはNoneを返す。"""
        raise NotImplementedError
