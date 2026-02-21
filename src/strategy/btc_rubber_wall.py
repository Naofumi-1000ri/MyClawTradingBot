"""BTC専用: ゴムの壁モデル。

出来高スパイク (BEAR candle) + 4Hレンジ内の位置で
エントリー方向と TP を動的決定する。

最適化: 毎サイクル全足スキャンせず、次の足の閾値volumeを事前計算。
キャッシュヒット時は O(1) で判定完了。

ゾーン (スパイクあり):
  貫通    (-20% ~ 0%):   LONG   TP 0.3%  SL 0.6% (30日分 vol>=5x: LONG_wr=55%, PF=2.33)
                          exit_bars=12 (60分タイムアウト。legacyで36時間漂流した教訓)
  レンジ上  (40% ~):       SHORT  TP 0.5%  SL 0.6% (30日分 vol>=5x: SHORT_wr=59%, EV=+0.049%)
                          exit_bars=10 (50分タイムアウト。モメンタム系は長期保有不要)
  深突破   (~ -20%):      SKIP   (LONG/SHORT 双方 30-40%。エッジ不明確のためSKIP)
  底付近   (0% ~ 20%):    SHORT  TP 0.4%  SL 0.6%  vol>=7.0のみ (SHORT_wr=55%。BTCノイズ幅0.3-0.5%に対してSL0.5%は狭すぎた)
                          exit_bars=8 (40分タイムアウト。底付近は短期勝負)
  中間     (20% ~ 40%):   SKIP   (ゾーン下寄りでSHORT期待値が不明確。旧20~40%は廃止)

パターン D (quiet_long):  スパイクなし + 低出来高 + 4H高位 + GOLDEN → LONG (静観脱却)
  - 2026-02-21 バックテスト: 4H pos>70% + GOLDEN + vol5/100<0.3x → 13件中85%上昇
  - 平均上昇幅 +0.32%。TP=0.3%/SL=0.5%/exit_bars=8 (40分) 設定
  - BEARスパイク不要: 静かな上昇トレンド継続中に順張りエントリー
  - vol_ratio_max=0.55 (旧0.40→0.55): 適度な出来高低下時に発火。0.40では発火機会が少なすぎた
  - 4H高位 (pos>=65%): 旧70%→65%。上昇トレンド継続ゾーンをより広く設定
  - 頻度: 1日2-3回 (スパイク待ち静観を打破する補助戦略)

2026-02-21 quiet_long追加:
  - 静観率90%以上の問題に対する補助戦略として実装
  - ETH Pattern C (quiet_long) の成功を参考に BTC 版を導入
  - BTC バックテスト根拠: 4H高位(>70%) + GOLDEN + vol<0.3x → 13件avg+0.32%

2026-02-21 exit_bars追加:
  - penetration LONG: legacy BTCが36時間漂流→SLヒット -$0.24 の教訓
    12bar=60分: 1時間以内に反転しなければ環境変化とみなしクローズ
  - upper_range SHORT: モメンタム系は短命。10bar=50分で期待値実現できなければ手仕舞い
  - bottom SHORT: exit_bars=8で40分タイムアウト (底付近は特に短期判断要)
  - bottom SL 0.5%→0.6%: BTCの5分足ノイズ0.3-0.5%に対して0.5%SLは狭すぎた

2026-02-21 bottomゾーン追加:
  - 旧SKIP (SHORT_wr=55%だが現価格がレンジ底近傍でリスク高) から変更
  - vol>=7.0xの強スパイク限定 (vol>=5xより厳格)。強い売り勢いは底割れ示唆
  - TP 0.4%/SL 0.5% で小さめ設定 (底近傍のため利幅控えめ)
  - 静観多発の改善: bottomゾーンで多くの機会を逃していた問題に対処

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

2026-02-21 upper_range 期待値改善:
  - TP 0.3%→0.5%: 旧EV=-0.069%, 新EV=+0.049% (WR=59%前提)
  - ゾーン開始: pos>=20%→pos>=40%: 中位ゾーン(20-40%)はSHORT期待値不明確なためSKIP
  - 実運用シグナルログ: pos=33%でSHORTが1件発生。このケースをSKIPに変更
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
    # Pattern D: quiet_long (静観脱却)
    # 2026-02-21 BT: 4H pos>70% + GOLDEN + vol5/100<0.3x → n=13 avg+0.32%上昇
    # ETH Pattern C の成功を参考にBTC版を実装
    "quiet_long_enabled": True,
    "quiet_long_h4_min_pct": 65,        # 4H pos >= 65% (旧70%→65%: 上昇トレンド継続ゾーン、発火機会を拡大)
    "quiet_long_vol_ratio_max": 0.55,   # 直近5本/100本 < 0.55 (旧0.40→0.55: 静かな市場を適切に捕捉)
    "quiet_long_vol_short_window": 5,   # 直近N本平均 (分子)
    "quiet_long_vol_long_window": 100,  # 比較対象M本平均 (分母)
    "quiet_long_tp_pct": 0.003,         # TP 0.3% (BT avg+0.32%に対して保守的設定)
    "quiet_long_sl_pct": 0.005,         # SL 0.5% (R:R=0.6。底付近SL0.6%より狭い高位ゾーン設定)
    "quiet_long_exit_bars": 8,          # 8bar=40分タイムアウト (静かな市場は短期決着)
    "zones": {
        # penetration: LONG変更 (旧SHORT)
        # 30日BT BEAR spike>=5x: LONG_wr=55%, TP0.3%/SL0.6%でPF=2.33
        # BEARスパイク + 4Hレンジ下抜けは売り過剰 → 反転LONG
        # exit_bars=12: 60分タイムアウト。legacyで36時間漂流→SLヒット -$0.24 の教訓
        "penetration": {"range": [-20, 0], "direction": "long", "tp_pct": 0.003, "sl_pct": 0.006, "exit_bars": 12},
        # upper_range: SHORT (pos>=40%のみ)
        # 30日BT BEAR spike>=5x: SHORT_wr=59%, avg=-0.321%
        # TP 0.3%→0.5%: 旧EV=-0.069%→新EV=+0.049% (WR=59%前提)
        # ゾーン開始 20%→40%: 中位ゾーン(20-40%)はSHORT期待値不明確なためSKIP
        # exit_bars=10: 50分タイムアウト。モメンタム系は短期間で結果が出なければ撤退
        "upper_range": {"range": [40, 999], "direction": "short", "tp_pct": 0.005, "sl_pct": 0.006, "exit_bars": 10},
        # deep_reversal: SKIP (旧LONG: 30-40%勝率でエッジ不明確)
        # "deep_reversal": {"range": [-999, -20], "direction": "long", "tp_pct": 0.003},
        # bottom (0~20): SHORT (vol>=7.0xの強スパイク限定)
        # 30日BT SHORT_wr=55%。強いBEARスパイクは底割れ → 続落シグナル
        # TP 0.4%/SL 0.6% (底近傍のため利幅控えめ。旧SL=0.5%はBTCノイズ幅0.3-0.5%に狭すぎた)
        # exit_bars=8: 40分タイムアウト。底付近は特に短期勝負
        # vol_min_override=7.0で通常閾値(5.0)より高い品質要求
        "bottom": {"range": [0, 20], "direction": "short", "tp_pct": 0.004, "sl_pct": 0.006, "vol_min_override": 7.0, "exit_bars": 8},
        # middle (20~40): SKIP (上昇中間帯でSHORT期待値不明確)
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
                # スパイクなし → Pattern D (quiet_long) を確認
                if self.cfg.get("quiet_long_enabled", True):
                    sig_d = self._pattern_d_quiet_long(idx, candle)
                    if sig_d:
                        return sig_d, next_cache
                return None, next_cache

            # スパイク検知 — ログ用に実際の ratio を計算
            ratio = self._vol_ratio_single(idx)
            # キャッシュはbase閾値ベースなので、VAS調整後の閾値で再チェック
            if ratio < vol_threshold:
                logger.info("Cache SPIKE but VAS-adjusted threshold=%.1f (regime=%s) filters out ratio=%.1f",
                            vol_threshold, vol_regime, ratio)
                next_cache = self._build_next_cache(idx)
                if self.cfg.get("quiet_long_enabled", True):
                    sig_d = self._pattern_d_quiet_long(idx, candle)
                    if sig_d:
                        return sig_d, next_cache
                return None, next_cache
            logger.info("Cache hit SPIKE: vol=%.1f >= threshold=%.1f, ratio=%.1f (regime=%s)",
                        candle["v"], threshold_vol, ratio, vol_regime)
        else:
            # --- Slow path: 対象足だけ計算 O(window) ---
            ratio = self._vol_ratio_single(idx)

            if ratio < vol_threshold or not is_bear:
                next_cache = self._build_next_cache(idx)
                # スパイクなし → Pattern D (quiet_long) を確認
                if self.cfg.get("quiet_long_enabled", True):
                    sig_d = self._pattern_d_quiet_long(idx, candle)
                    if sig_d:
                        return sig_d, next_cache
                return None, next_cache

            logger.info("BEAR spike detected: vol_ratio=%.1f, change=%.2f%% (regime=%s)",
                        ratio, (candle["c"] - candle["o"]) / candle["o"] * 100, vol_regime)

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
            logger.info("Position %.1f%% falls in skip zone (middle 20-40%%)", pos)
            return None, next_cache

        # ゾーン固有の最低vol閾値チェック (vol_min_override)
        # 例: bottomゾーンは vol>=7.0 のみ (通常5.0より高品質スパイク要求)
        vol_min = matched_cfg.get("vol_min_override")
        if vol_min is not None and ratio < vol_min:
            logger.info(
                "Position %.1f%% (%s zone): vol_ratio=%.1f < zone_min=%.1f, skip",
                pos, matched_zone, ratio, vol_min,
            )
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

        # タイムアウト設定 (ゾーンごと)
        exit_bars = matched_cfg.get("exit_bars")
        exit_mode = "time_cut" if exit_bars is not None else "tp_sl"

        vas_note = f" [VAS:{vol_regime}x{vol_multiplier:.2f}]" if vol_regime != "normal" else ""
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
                f"vol_ratio={ratio:.1f}x (thr={vol_threshold:.1f}{vas_note}), "
                f"4H=[{h4_low:.2f}-{h4_high:.2f}], "
                f"→ {direction} TP {tp_pct*100:.1f}% SL {sl_pct*100:.1f}%"
                + (f" timeout={exit_bars}bar" if exit_bars else "")
            ),
            "zone": matched_zone,
            "range_position": round(pos, 1),
            "vol_ratio": round(ratio, 1),
            "vol_regime": vol_regime,
            "spike_time": candle["t"],
            "exit_mode": exit_mode,
        }
        if exit_bars is not None:
            signal["exit_bars"] = exit_bars

        logger.info("Signal: %s %s @ %.2f, TP=%.2f, SL=%.2f (zone=%s)",
                     direction, "BTC", entry_price, tp_price, sl_price, matched_zone)
        return signal, next_cache

    def _pattern_d_quiet_long(self, idx: int, candle: dict) -> dict | None:
        """Pattern D: 低出来高 + GOLDEN クロス + 4H高位 → LONG (quiet_long)。

        スパイクが出ない静かな市場でトレンド順張りする補助戦略。
        2026-02-21 BT: 4H pos>70% + GOLDEN + vol5/100<0.3x → n=13 avg+0.32%上昇

        条件:
          1. EMA9 > EMA21 (GOLDEN: 上昇トレンド確認)
          2. 4H range pos >= quiet_long_h4_min_pct (高位ゾーン: デフォルト70%)
          3. 直近N本/長期M本 出来高比 < quiet_long_vol_ratio_max (低出来高: デフォルト0.40)
        """
        h4_window = self.cfg["h4_window"]
        h4_min_pct = self.cfg.get("quiet_long_h4_min_pct", 70)
        vol_ratio_max = self.cfg.get("quiet_long_vol_ratio_max", 0.40)
        short_w = self.cfg.get("quiet_long_vol_short_window", 5)
        long_w = self.cfg.get("quiet_long_vol_long_window", 100)
        tp_pct = self.cfg.get("quiet_long_tp_pct", 0.003)
        sl_pct = self.cfg.get("quiet_long_sl_pct", 0.005)
        exit_bars = self.cfg.get("quiet_long_exit_bars", 8)

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

        # 3. 低出来高チェック (直近N本/長期M本 < 閾値)
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

        tp_price = round(entry * (1 + tp_pct), 2)
        sl_price = round(entry * (1 - sl_pct), 2)

        # CAPS: confidence=0.72 (低確信度) → leverage=1x (縮小サイズ)
        confidence = 0.72
        leverage = self.confidence_to_leverage(confidence)

        logger.info(
            "Pattern D (quiet_long): ema9=%.2f>ema21=%.2f, pos=%.1f%% >= %d%%, "
            "vol_ratio(5/100)=%.2f < %.2f → LONG TP %.1f%% SL %.1f%% [CAPS: conf=%.2f → %dx]",
            ema9, ema21, pos, h4_min_pct, vol_ratio, vol_ratio_max,
            tp_pct * 100, sl_pct * 100, confidence, leverage,
        )

        signal = {
            "symbol": "BTC",
            "action": "long",
            "direction": "long",
            "confidence": confidence,
            "entry_price": round(entry, 2),
            "take_profit": tp_price,
            "stop_loss": sl_price,
            "leverage": leverage,
            "reasoning": (
                f"BtcRubberWall D: quiet_long, "
                f"ema9={ema9:.2f}>ema21={ema21:.2f}, "
                f"4H_pos={pos:.1f}%, vol_ratio(5/100)={vol_ratio:.2f}, "
                f"→ LONG TP {tp_pct*100:.1f}% SL {sl_pct*100:.1f}% {exit_bars}bar cut "
                f"[CAPS: {leverage}x]"
            ),
            "zone": "quiet_high",
            "pattern": "D_quiet_long",
            "exit_mode": "time_cut",
            "exit_bars": exit_bars,
            "range_position": round(pos, 1),
            "vol_ratio": round(vol_ratio, 2),
            "spike_time": candle["t"],
        }
        logger.info("Signal: long BTC @ %.2f, TP=%.2f, SL=%.2f (quiet_long, exit=%dbar)",
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
