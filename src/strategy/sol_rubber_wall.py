"""SOL専用: ゴムの壁モデル (BTC型モメンタム)。

BTC RubberWall と同じ方向性 (BEAR spike → SHORT) だが、
SOL 固有のボラティリティに合わせた広いTP/SL設定。

分析結果 (90日バックテスト):
  - SOLはBTC型 (BEAR spike後、価格は反発せずさらに下がる)
  - SHORT favorable/adverse ratio = 1.68 (LONG = 0.59)
  - Best: BEAR 5x → SHORT TP=1.0%/SL=0.5% PF=1.41, Net=+5.38%

パターン E (quiet_short):  スパイクなし + 低出来高 + 4H高位 + GOLDEN → SHORT (静観脱却)
  - 2026-02-21 バックテスト: 4H pos>75% + GOLDEN + vol5/100<0.50x → WR=40.8%, PF=2.91
    (PF=2.91はTP/SL比が大きいため。n=76件の十分なサンプル)
  - TP=0.6%/SL=0.8%/exit_bars=10 (50分): BT最適パラメータ
  - BEARスパイク不要: 静かな上昇高値圏で価格が重くなるパターンにSHORT
  - funding_rate フィルター: 極端なネガティブfunding時はSHORTブロック (既存ロジック流用)
  - 頻度: 1日2-3回 (SOLのスパイク待ち静観を打破する補助戦略)
  - 2026-02-21 h4_min_pct 75%→70%: 75%は発火機会が少なすぎた。70%に緩和して高値圏を広く設定

2026-02-21 quiet_short追加:
  - 静観率90%以上の問題に対する補助戦略として実装
  - SOL BT根拠: 4H高位(>75%) + GOLDEN + vol<0.50x → n=76 WR=40.8%, PF=2.91

2026-02-21 最適化 (実運用データに基づく):
  - SLヒット原因: SOL 5分足ノイズ幅 0.3-1% に対しSL 0.5%が狭すぎた
  - vol_threshold 5.0x → 6.0x (シグナル品質向上) ※settings.yamlは5.0xで実運用中
  - penetration SL 0.5% → 0.8%、TP 1.0% → 1.5% (R:R=1.875維持)
  - upper_range SL 0.4% → 0.6%、TP 0.8% → 1.2%
  - funding_rate < -2e-5 時のSHORT禁止 (ショートスクイーズ回避)

2026-02-21 追加調整 (静観・フォールバック多発対策):
  - funding_short_block_threshold: -2e-5 → -5e-5 (閾値緩和)
    理由: -2e-5 は中程度のネガティブfundingでもSHORTブロックしすぎ。
    -5e-5 (=極端なスクイーズリスク域) のみブロックに変更してSOL機会損失を削減。

2026-02-21 ゾーン精度改善:
  - upper_range 下限 20%→40%: pos=20-40%のグレーゾーン (ボトム近傍) を排除
    理由: pos=39.9%でのSHORT (vol=8.0x) がSLヒット。ボトム寄りでのSHORTは逆行リスク高い

2026-02-21 深部ゾーン見直し (実運用28件分析):
  - SOL LONG (deep_reversal): 14件中2勝 WR=14% PnL=-$1.08 → 機能不全
    理由: deep_reversal はバックテストでLONG=0.59 (SHORT=1.68) と元々不利。
    実運用でも反証が積み重なった。deep_reversalを無効化 (SKIP) してSHORT専念。
  - SOL vol_threshold: settings.yaml で 5.0→6.5 に引き上げ。
    28件中SHORT 5/14勝。6.5x以上の高品質スパイクに絞る。

ゾーン (2026-02-21 改訂):
  貫通    (-20% ~ 0%):    SHORT  TP 1.5%  SL 0.8% (最強ゾーン: 62-71% win)
  レンジ上 (40% ~):        SHORT  TP 1.2%  SL 0.6% (旧20%→40%: グレーゾーン排除済み)
  深突破   (~ -20%):       SKIP   (旧LONG: 実運用14件2勝14%で無効化。SHORT専念)
  底付近   (0% ~ 40%):     SKIP  (0-20%: ボトム, 20-40%: グレーゾーン)
"""

from __future__ import annotations

from src.strategy.base import BaseStrategy
from src.utils.logger import setup_logger

logger = setup_logger("sol_rubber_wall")

_DEFAULT_CONFIG = {
    "vol_threshold": 5.0,       # メインSHORT: 5x (settings.yamlで5.0が指定済み。コードのデフォルトも整合)
    "deep_threshold": 7.0,      # deep_below LONG: 7x+ のみ
    "h4_window": 48,
    "vol_window": 288,
    # funding_rate がこの閾値より低い場合、SHORT新規エントリー禁止 (スクイーズ回避)
    # -2e-5 → -5e-5 に緩和: 中程度のネガティブfundingでのブロック過多によるSOL機会損失を削減
    "funding_short_block_threshold": -5e-5,
    # Pattern E: quiet_short (静観脱却)
    # 2026-02-21 BT: 4H pos>75% + GOLDEN + vol5/100<0.50x → n=76 WR=40.8%, PF=2.91
    "quiet_short_enabled": True,
    "quiet_short_h4_min_pct": 70,        # 4H pos >= 70% (旧75%→70%: 高値圏をより広く設定、発火機会を拡大)
    "quiet_short_vol_ratio_max": 0.50,   # 直近5本/100本 < 0.50 (低出来高)
    "quiet_short_vol_short_window": 5,   # 直近N本平均 (分子)
    "quiet_short_vol_long_window": 100,  # 比較対象M本平均 (分母)
    "quiet_short_tp_pct": 0.006,         # TP 0.6% (BT最適)
    "quiet_short_sl_pct": 0.008,         # SL 0.8% (SOLのボラに合わせた広めSL)
    "quiet_short_exit_bars": 10,         # 10bar=50分タイムアウト
    "zones": {
        # penetration: SL 0.5%→0.8% (5分足ノイズ耐性強化), TP 1.0%→1.5% (R:R≈1.875)
        "penetration": {"range": [-20, 0], "direction": "short", "tp_pct": 0.015, "sl_pct": 0.008},
        # upper_range: SL 0.4%→0.6%, TP 0.8%→1.2%
        # 下限 20%→40%: 4H 20-40% のグレーゾーン (ボトム近傍) でのSHORTは逆行リスク高い
        "upper_range": {"range": [40, 999], "direction": "short", "tp_pct": 0.012, "sl_pct": 0.006},
        # deep_reversal: SKIP (旧LONG。実運用14件2勝14% WR、PnL=-$1.08。機能不全のため無効化)
        # 将来: 50件以上の追加データで再評価する
        # "deep_reversal": {"range": [-999, -20], "direction": "long", "tp_pct": 0.008, "sl_pct": 0.005},
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

        base_vol_threshold = self.cfg["vol_threshold"]
        h4_window = self.cfg["h4_window"]

        idx = len(self.candles) - 2
        if idx < h4_window:
            return None, {}

        # ATRボラティリティ感度調整: 高ボラ時は閾値引き上げ、低ボラ時は引き下げ
        vol_multiplier, vol_regime = self._atr_volatility_multiplier(idx)
        vol_threshold = base_vol_threshold * vol_multiplier
        if vol_regime != "normal":
            logger.info(
                "VAS: regime=%s, multiplier=%.2f → vol_threshold %.1f→%.1f",
                vol_regime, vol_multiplier, base_vol_threshold, vol_threshold,
            )

        candle = self.candles[idx]
        is_bear = candle["c"] < candle["o"]

        # --- Fast path: キャッシュ閾値で O(1) 判定 ---
        if cache and cache.get("next_target_t") == candle["t"]:
            threshold_vol = cache["threshold_vol"]

            if candle["v"] < threshold_vol or not is_bear:
                next_cache = self._build_next_cache(idx)
                # スパイクなし → Pattern E (quiet_short) を確認
                if self.cfg.get("quiet_short_enabled", True):
                    sig_e = self._pattern_e_quiet_short(idx, candle)
                    if sig_e:
                        return sig_e, next_cache
                return None, next_cache

            ratio = self._vol_ratio_single(idx)
            # VAS調整後の閾値で再チェック
            if ratio < vol_threshold:
                logger.info("Cache SPIKE but VAS-adjusted threshold=%.1f (regime=%s) filters out ratio=%.1f",
                            vol_threshold, vol_regime, ratio)
                next_cache = self._build_next_cache(idx)
                if self.cfg.get("quiet_short_enabled", True):
                    sig_e = self._pattern_e_quiet_short(idx, candle)
                    if sig_e:
                        return sig_e, next_cache
                return None, next_cache
            logger.info("Cache hit SPIKE: vol=%.1f >= threshold=%.1f, ratio=%.1f (regime=%s)",
                        candle["v"], threshold_vol, ratio, vol_regime)
        else:
            ratio = self._vol_ratio_single(idx)

            if ratio < vol_threshold or not is_bear:
                next_cache = self._build_next_cache(idx)
                # スパイクなし → Pattern E (quiet_short) を確認
                if self.cfg.get("quiet_short_enabled", True):
                    sig_e = self._pattern_e_quiet_short(idx, candle)
                    if sig_e:
                        return sig_e, next_cache
                return None, next_cache

            logger.info("BEAR spike detected: vol_ratio=%.1f, change=%.2f%% (regime=%s)",
                        ratio, (candle["c"] - candle["o"]) / candle["o"] * 100, vol_regime)

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

        vas_note = f" [VAS:{vol_regime}x{vol_multiplier:.2f}]" if vol_regime != "normal" else ""
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
                f"vol_ratio={ratio:.1f}x (thr={vol_threshold:.1f}{vas_note}), "
                f"4H=[{h4_low:.4f}-{h4_high:.4f}], "
                f"→ {direction} TP {tp_pct*100:.1f}% SL {sl_pct*100:.1f}%"
            ),
            "zone": matched_zone,
            "pattern": f"wall_{matched_zone}",
            "exit_mode": "tp_sl",
            "range_position": round(pos, 1),
            "vol_ratio": round(ratio, 1),
            "vol_regime": vol_regime,
            "spike_time": candle["t"],
        }

        logger.info("Signal: %s %s @ %.4f, TP=%.4f, SL=%.4f (zone=%s)",
                     direction, "SOL", entry_price, tp_price, sl_price, matched_zone)
        return signal, next_cache

    def _pattern_e_quiet_short(self, idx: int, candle: dict) -> dict | None:
        """Pattern E: 低出来高 + GOLDEN クロス + 4H高位 → SHORT (quiet_short)。

        スパイクなしの静かな市場で4H高値圏に達した時のショート戦略。
        2026-02-21 BT: 4H pos>75% + GOLDEN + vol5/100<0.50x → n=76 WR=40.8%, PF=2.91

        追加フィルター (2026-02-21 強化):
          5. RSI > 55 (高値圏での疲労確認: RSI中立以上でないと意味なし)
          6. 価格モメンタム < 0.2% (急騰中のSHORTは危険: 上昇モメンタムが弱いこと)
          7. BBスクイーズ OR ボディ品質 >= 0.25 (コンソリ/方向性足を優先)

        条件:
          1. EMA9 > EMA21 (GOLDEN: 上昇トレンド確認 = 高値圏に到達している状態)
          2. 4H range pos >= quiet_short_h4_min_pct (高位ゾーン: デフォルト70%)
          3. 直近N本/長期M本 出来高比 < quiet_short_vol_ratio_max (低出来高: デフォルト0.50)
          4. funding_rate フィルター (極端なネガティブfunding時はSHORT禁止)
          5. RSI14 > 55 (疲労確認: 高値圏でRSIが中立以上)
          6. 直近6本の価格モメンタム < 0.20% (急騰中のSHORT禁止)
          7. ボディ品質 >= 0.25 OR BBスクイーズ (ドジ足ノイズ状態を除外)
        """
        h4_window = self.cfg["h4_window"]
        h4_min_pct = self.cfg.get("quiet_short_h4_min_pct", 75)
        vol_ratio_max = self.cfg.get("quiet_short_vol_ratio_max", 0.50)
        short_w = self.cfg.get("quiet_short_vol_short_window", 5)
        long_w = self.cfg.get("quiet_short_vol_long_window", 100)
        tp_pct = self.cfg.get("quiet_short_tp_pct", 0.006)
        sl_pct = self.cfg.get("quiet_short_sl_pct", 0.008)
        exit_bars = self.cfg.get("quiet_short_exit_bars", 10)

        # funding rate フィルター (スパイク版と同じロジック)
        funding_rate = self.cfg.get("current_funding_rate", 0.0)
        block_threshold = self.cfg.get("funding_short_block_threshold", -5e-5)
        if funding_rate < block_threshold:
            logger.info(
                "Pattern E: SHORT blocked: funding_rate=%.2e < threshold=%.2e",
                funding_rate, block_threshold,
            )
            return None

        # EMA計算
        def _ema(prices: list[float], period: int) -> float:
            k = 2.0 / (period + 1)
            e = prices[0]
            for p in prices[1:]:
                e = p * k + e * (1 - k)
            return e

        # 1. EMA GOLDEN クロス確認
        if idx < 21:
            return None
        closes = [c["c"] for c in self.candles[max(0, idx - 30):idx + 1]]
        if len(closes) < 22:
            return None

        ema9 = _ema(closes, 9)
        ema21 = _ema(closes, 21)
        if ema9 <= ema21:
            return None

        # 2. 4H range position (高位ゾーン確認: pos >= h4_min_pct)
        h4_low, h4_high = self._h4_range(idx - 1, h4_window)
        entry = candle["c"]
        pos = self._range_position(entry, h4_low, h4_high)
        if pos < h4_min_pct:
            return None

        # 3. 低出来高チェック
        short_start = max(0, idx - short_w + 1)
        short_vols = [self.candles[j]["v"] for j in range(short_start, idx + 1)]
        long_start = max(0, idx - long_w + 1)
        long_vols = [self.candles[j]["v"] for j in range(long_start, idx + 1)]
        short_avg = sum(short_vols) / len(short_vols) if short_vols else 0
        long_avg = sum(long_vols) / len(long_vols) if long_vols else 0
        if long_avg <= 0:
            return None
        vol_ratio = short_avg / long_avg
        if vol_ratio >= vol_ratio_max:
            return None

        # 5. RSIフィルター: 高値圏でRSI疲労(>55)を確認 (中立以下はまだ上昇余地あり)
        rsi = self._rsi(idx, period=14)
        if rsi is not None and rsi <= 55.0:
            logger.info(
                "Pattern E: SKIP (RSI=%.1f <= 55, 高値圏でまだRSI低め → SHORT早すぎ)",
                rsi,
            )
            return None

        # 6. 価格モメンタム: 急騰中のSHORT禁止 (上昇モメンタムが強い場合はSKIP)
        momentum = self._price_momentum(idx, window=6)
        if momentum > 0.20:
            logger.info(
                "Pattern E: SKIP (momentum=%.3f%% > 0.20%%, 急騰中のSHORT禁止)",
                momentum,
            )
            return None

        # 7. ボディ品質チェック: ドジ足ノイズ状態を除外
        body_q = self._candle_body_ratio(idx, window=3)
        bb_squeeze = self._bb_squeeze(idx, window=20)
        if body_q < 0.25 and not bb_squeeze:
            logger.info(
                "Pattern E: SKIP (body_ratio=%.2f < 0.25, BBスクイーズなし: ドジ足ノイズ)",
                body_q,
            )
            return None

        tp_price = round(entry * (1 - tp_pct), 4)
        sl_price = round(entry * (1 + sl_pct), 4)

        # CAPS: RSI高め(>65) + 下落モメンタム → confidence上げて2x
        # それ以外は conservative 0.72 → 1x
        has_quality = (rsi is not None and rsi > 65.0) or (momentum < 0.0 and bb_squeeze)
        confidence = 0.75 if has_quality else 0.72
        leverage = self.confidence_to_leverage(confidence)

        rsi_str = f"RSI={rsi:.1f}" if rsi is not None else "RSI=n/a"
        squeeze_str = "BB_squeeze" if bb_squeeze else f"body={body_q:.2f}"
        logger.info(
            "Pattern E (quiet_short): ema9=%.4f>ema21=%.4f, pos=%.1f%% >= %d%%, "
            "vol_ratio(5/100)=%.2f, %s, mom=%.3f%%, %s "
            "→ SHORT TP %.1f%% SL %.1f%% [CAPS: conf=%.2f → %dx]",
            ema9, ema21, pos, h4_min_pct, vol_ratio,
            rsi_str, momentum, squeeze_str,
            tp_pct * 100, sl_pct * 100, confidence, leverage,
        )

        signal = {
            "symbol": "SOL",
            "action": "short",
            "direction": "short",
            "confidence": confidence,
            "entry_price": round(entry, 4),
            "take_profit": tp_price,
            "stop_loss": sl_price,
            "leverage": leverage,
            "reasoning": (
                f"SolRubberWall E: quiet_short, "
                f"ema9={ema9:.4f}>ema21={ema21:.4f}, "
                f"4H_pos={pos:.1f}%, vol_ratio(5/100)={vol_ratio:.2f}, "
                f"{rsi_str}, mom={momentum:.3f}%, {squeeze_str}, "
                f"→ SHORT TP {tp_pct*100:.1f}% SL {sl_pct*100:.1f}% {exit_bars}bar cut "
                f"[CAPS: {leverage}x]"
            ),
            "zone": "quiet_high",
            "pattern": "E_quiet_short",
            "exit_mode": "time_cut",
            "exit_bars": exit_bars,
            "range_position": round(pos, 1),
            "vol_ratio": round(vol_ratio, 2),
            "spike_time": candle["t"],
        }
        logger.info("Signal: short SOL @ %.4f, TP=%.4f, SL=%.4f (quiet_short, exit=%dbar)",
                    entry, tp_price, sl_price, exit_bars)
        return signal

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
