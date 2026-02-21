"""ETH専用: ゴムバンドモデル。

BTC の RubberWall とは逆のロジック。
vol_ratio の強度帯で挙動が反転する ETH 固有の特性を利用。

Pattern A (reversal):  高閾値 BEAR spike → LONG (平均回帰)
  - vol_ratio >= 7.0 の大スパイクはオーバーシュート → 戻る (旧 6.0 から引き上げ)
  - 30日BT (n=27): pos<40%でLONG_wr=70%, pos>=40%でLONG_wr=25%
    → 4H下位40%未満でのみLONG許可 (4Hレンジ高値圏での逆張りは不利)
  - TP = 固定 0.5%, SL = min(IN足low - 0.05%pad, entry * (1 - 0.25%))
  - SL最小距離を0.25%に拡大 (旧0.1%: ノイズでSLが頻発した問題を修正)

Pattern B (momentum):  中閾値 BEAR spike + 上位ゾーン → SHORT
  - vol_ratio 4.0-7.0 かつ 4Hレンジ position >= 40%
    (旧: 3.0x/40%。実運用20件WR=45%/PF=0.77。SHORTのみPF黒字だがエントリー条件が甘すぎた)
  - 30日BT: mid(40-55%) + vol>=3.0x → SHORT_wr=88% (n=8, 最良ゾーン)
  - TP = 時間カット 15bar (75分, 旧10bar=50分: 短期ノイズ吸収のため延長)
  - SL = IN足high + 0.05%pad, 最小距離0.35% (旧0.30%→0.35%: 2/20実例+0.56%ヒット対策)

Pattern C (quiet_long):  スパイクなし + 低出来高 + GOLDEN + 4H低位 → LONG
  - スパイク不要: Rubber戦略が機能しない「静かな市場」でも発火する代替戦略
  - 条件: EMA9 > EMA21 (GOLDEN), 4H pos < 50% (中位以下), 直近5本/100本出来高比 < 0.60
  - 4H EMA補助 (quiet_long_use_4h_ema=true): 5m DEAD でも 4H EMA9 > EMA21 なら発火許可
  - 30日BT (n=22): WR=68.2% (TP達成), SL hit=13.6%, Timeout=18.2%, EV=+0.179%
  - TP = 0.4%, SL = 0.6%, タイムアウト = 10bar (50分)
  - 頻度: 約1.5件/日 (h4_max_pct緩和35%→50%により頻度増加)
  - 静観・フォールバック状態を打破する主要な代替戦略
  - 2026-02-21 h4_max_pct緩和: 35%→50%
    理由: 35%フィルターはETHが底値圏にいない時に発火機会ゼロになる問題
    BTでは30-50%ゾーンでも十分なWRを確認。pos=46%が典型的な実運用値で機会損失大きい

2026-02-21 最適化 (実運用20件分析):
  - ETH LONG: 13件 6勝 PnL=-$0.72。下降トレンド中の逆張りが損失主因
  - ETH SHORT: 7件 3勝 PnL=+$0.24 (黒字)

2026-02-21 フィルター方向修正 (30日BT 27件分析):
  - reversal_h4_filter_pct: フィルターの向きを反転
    旧: pos < filter → SKIP (= pos >= 40%でLONG) → WR=25%
    新: pos >= filter → SKIP (= pos < 40%でLONG) → WR=70%, PF=2.21
    → reversal_h4_max_pct (上限) として機能するよう変更
    → settings.yaml の reversal_h4_filter_pct=40 を上限値として使用

2026-02-21 Pattern C追加 (静観脱却):
  - 30日BT n=22: WR=68.2%, EV=+0.179%, SL hit=13.6%, Timeout=18.2%
  - 低出来高時 (直近5本/100本 < 60%) に発火: 約1.0件/日
  - スパイク不要のため、vol>=7.0xが0件の「静かな市場」でもトレード可能

2026-02-21 ETHトレード詳細分析 (実運用21件クローズ):
  - Pattern A: exit_bars=12 追加 (60分タイムアウト)
    逆張りLONGが長時間保有されてSLヒットするリスクを時間制限で軽減
    30日BT平均決着は8本(40分)以内 → 12本で十分な余裕
  - Pattern B: SL最小距離 0.30% → 0.35%
    2/20 17:22実例: +0.56%上昇でSLヒット。0.35%でも一部カバーできないが確率的に改善
  - Pattern C: quiet_long_use_4h_ema=true 追加
    5m EMAがDEADでも4H EMA GOLDENなら発火許可 (4Hトレンドは5mより信頼性高い)
    confidence=0.72に下げて保守的に設定
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
    # フィルター方向修正 (2026-02-21):
    # 30日BT n=27: pos<40%でLONG_wr=70%, pos>=40%でLONG_wr=25%
    # → pos >= this → SKIP (上限フィルター: 4H高値圏での逆張りLONGを禁止)
    "reversal_h4_max_pct": 40,      # pos >= 40% → Pattern A SKIP。低位ゾーンでのみLONG許可
    # Pattern B: momentum
    "momentum_threshold": 3.0,      # 30日BT: mid(40-55%)+3.0xでSHORT_wr=88%(n=8)。3.0x維持
    "momentum_zone_min": 40,        # 30日BT: 4H pos >= 40%でのSHORT有効。40%維持
    "momentum_cut_bars": 15,        # 旧10bar(50分)→15bar(75分): 短期ノイズ吸収のため延長
    "momentum_sl_pad_pct": 0.0005,  # 0.05% pad above candle high
    "momentum_sl_min_dist": 0.0035, # 旧0.30%→0.35%: 2/20実例+0.56%SLヒット対策 (ノイズ耐性向上)
    # Pattern C: quiet_long (静観脱却)
    # 30日BT n=22: WR=68.2%, EV=+0.179%, 約1.5件/日 (緩和後)
    # スパイク不要。低出来高の静かな市場でGOLDEN+底値圏のLONG
    "quiet_long_enabled": True,          # Pattern C 有効/無効フラグ
    "quiet_long_h4_max_pct": 50,         # 4H pos < 50% (中位以下: 旧35%→50%。pos=46%など典型値で機会損失防止)
    "quiet_long_vol_ratio_max": 0.60,    # 直近5本/100本平均 < 0.60 (低出来高確認)
    "quiet_long_vol_short_window": 5,    # 直近N本の出来高平均 (分子)
    "quiet_long_vol_long_window": 100,   # 比較対象の出来高平均 (分母)
    "quiet_long_tp_pct": 0.004,          # TP 0.4% (30日BT最適)
    "quiet_long_sl_pct": 0.006,          # SL 0.6%
    "quiet_long_cut_bars": 10,           # 10bar (50分) タイムアウト
    "quiet_long_use_4h_ema": True,       # 5m DEAD でも 4H EMA GOLDEN なら発火許可
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

        スキャン順序:
          1. Pattern A/B: BEARスパイク検知 (従来通り)
          2. Pattern C: スパイクなし + 低出来高 + GOLDEN + 4H底値圏 → quiet_long

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
            next_cache = self._build_next_cache(idx)
            # BEARでない → Pattern A/B はスキップ。Pattern C を確認
            if self.cfg.get("quiet_long_enabled", True):
                sig_c = self._pattern_c_quiet_long(idx, candle)
                if sig_c:
                    return sig_c, next_cache
            return None, next_cache

        # --- Fast path: キャッシュ閾値で判定 ---
        if cache and cache.get("next_target_t") == candle["t"]:
            threshold_vol = cache["threshold_vol"]
            if candle["v"] < threshold_vol:
                next_cache = self._build_next_cache(idx)
                # スパイク閾値未満 → Pattern C を確認
                if self.cfg.get("quiet_long_enabled", True):
                    sig_c = self._pattern_c_quiet_long(idx, candle)
                    if sig_c:
                        return sig_c, next_cache
                return None, next_cache
            ratio = self._vol_ratio_single(idx)
        else:
            ratio = self._vol_ratio_single(idx)
            if ratio < momentum_thr:
                next_cache = self._build_next_cache(idx)
                # スパイク閾値未満 → Pattern C を確認
                if self.cfg.get("quiet_long_enabled", True):
                    sig_c = self._pattern_c_quiet_long(idx, candle)
                    if sig_c:
                        return sig_c, next_cache
                return None, next_cache

        # --- パターン判定 (スパイクあり) ---
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

        SL = min(candle low - 0.05%pad, entry * (1 - 0.25%))  ← 最小距離0.25%で頻発SL修正
        TP = 固定 0.5%
        exit_bars = 12 (60分タイムアウト): SL/TP未決着時に強制クローズ (長期ドリフトリスク軽減)
        4Hフィルター (上限): pos >= reversal_h4_max_pct → SKIP
          30日BT n=27: pos<40% → WR=70%, pos>=40% → WR=25%
          4H高値圏 (pos>=40%) での逆張りLONGは不利。低位ゾーンのみ許可。
        """
        h4_window = self.cfg["h4_window"]
        # 後方互換: 旧キー名 reversal_h4_filter_pct も読めるようにする
        h4_max_pct = self.cfg.get("reversal_h4_max_pct",
                                   self.cfg.get("reversal_h4_filter_pct", 40))

        # --- 4Hトレンドフィルター (上限) ---
        h4_low, h4_high = self._h4_range(idx - 1, h4_window)
        h4_pos = self._range_position(candle["c"], h4_low, h4_high)

        if h4_pos >= h4_max_pct:
            logger.info(
                "Pattern A: SKIP (4H pos=%.1f%% >= max=%d%%, 4H=[%.2f-%.2f], "
                "4H高値圏 → 逆張りLONG不利。30日BT: pos>=40%%でWR=25%%)",
                h4_pos, h4_max_pct, h4_low, h4_high,
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
            "exit_mode": "time_cut",    # 旧tp_sl→time_cut: exit_bars=12 (60分) タイムアウト追加
            "exit_bars": 12,             # 60分でSL/TP未決着時タイムアウト (BTで平均8本で決着)
            "vol_ratio": round(ratio, 1),
            "spike_time": candle["t"],
        }
        logger.info(
            "Signal: long ETH @ %.2f, TP=%.2f, SL=%.2f (reversal, sl_dist=%.2f%%, 12bar cut)",
            entry, tp_price, sl_price, sl_dist * 100,
        )
        return signal, self._build_next_cache(idx)

    def _pattern_b_momentum(
        self, idx: int, candle: dict, ratio: float
    ) -> tuple[dict | None, dict]:
        """Pattern B: 中閾値 BEAR spike + 上位ゾーン → SHORT momentum。

        SL = IN足high + 0.05% pad (スパイク否定ライン), 最小距離0.35% (旧0.30%→0.35%)
        TP = 時間カット 15bar (75分後にclose決済, 旧10bar=50分から延長)
        4Hゾーン: position >= 40% (旧50%→40%: 50%では機会ゼロにより緩和)
        注意: 30日BTでupper(40-100%)+vol>=5x の勝率は31%と低い。モニタリング要。
        2/20実例: +0.56%上昇でSLヒット。0.35%でも一部は防げないが耐性向上。
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
        sl_min_dist = self.cfg.get("momentum_sl_min_dist", 0.003)
        cut_bars = self.cfg["momentum_cut_bars"]
        entry = candle["c"]

        # SL = candle high に pad を加えた値
        # 最低SL距離 0.30% を保証 (旧0.20%→0.30%: ノイズ耐性向上)
        sl_from_candle = round(candle["h"] * (1 + sl_pad), 2)
        sl_from_min = round(entry * (1 + sl_min_dist), 2)
        sl_price = max(sl_from_candle, sl_from_min)
        sl_dist = (sl_price - entry) / entry

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
                f"→ SHORT {cut_bars}bar cut, SL=candle_high+{sl_dist*100:.2f}%"
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
            "Signal: short ETH @ %.2f, SL=%.2f (sl_dist=%.2f%%), exit=%dbar (momentum)",
            entry, sl_price, sl_dist * 100, cut_bars,
        )
        return signal, next_cache

    def _pattern_c_quiet_long(self, idx: int, candle: dict) -> dict | None:
        """Pattern C: 低出来高 + GOLDEN クロス + 4H底値圏 → LONG (quiet_long)。

        スパイクが出ない静かな市場で機能する代替戦略。
        30日BT (n=22): WR=68.2%, EV=+0.179%, 約1件/日

        条件:
          1. EMA9 > EMA21 (GOLDEN クロス: 短期上昇トレンド)
             ※ quiet_long_use_4h_ema=true 時: 5m DEAD でも 4H EMA GOLDEN なら許可
          2. 4H range pos < quiet_long_h4_max_pct (底値圏: デフォルト35%)
          3. 直近N本/長期M本 出来高比 < quiet_long_vol_ratio_max (低出来高: デフォルト0.60)
        """
        h4_window = self.cfg["h4_window"]
        h4_max_pct = self.cfg.get("quiet_long_h4_max_pct", 35)
        vol_ratio_max = self.cfg.get("quiet_long_vol_ratio_max", 0.60)
        short_w = self.cfg.get("quiet_long_vol_short_window", 5)
        long_w = self.cfg.get("quiet_long_vol_long_window", 100)
        tp_pct = self.cfg.get("quiet_long_tp_pct", 0.004)
        sl_pct = self.cfg.get("quiet_long_sl_pct", 0.006)
        cut_bars = self.cfg.get("quiet_long_cut_bars", 10)
        use_4h_ema = self.cfg.get("quiet_long_use_4h_ema", False)

        # EMA計算ユーティリティ (直近N本のみ使用)
        def _ema(prices: list[float], period: int) -> float:
            k = 2.0 / (period + 1)
            e = prices[0]
            for p in prices[1:]:
                e = p * k + e * (1 - k)
            return e

        # 1. EMA クロス確認 (GOLDEN: EMA9 > EMA21)
        if idx < 21:
            return None
        closes = [c["c"] for c in self.candles[max(0, idx - 30):idx + 1]]
        if len(closes) < 22:
            return None

        ema9 = _ema(closes, 9)
        ema21 = _ema(closes, 21)
        ema_golden_5m = ema9 > ema21

        # 4H EMA 補助チェック (use_4h_ema=true の場合: 5m DEAD でも 4H GOLDEN なら許可)
        ema_golden_4h = False
        if use_4h_ema and not ema_golden_5m:
            h4_low, h4_high = self._h4_range(idx - 1, h4_window)
            # 4H足は self.candles (5m) の h4_window * 12 本分に相当
            # ここでは 5m candles から 4H相当の EMA を近似計算
            # 5m足の h4_window*12 本 = 48H * 12 = 576本を使用
            h4_equiv_bars = h4_window * 12  # 48H * 12本/H = 576本
            h4_start = max(0, idx - h4_equiv_bars + 1)
            h4_closes = [c["c"] for c in self.candles[h4_start:idx + 1]]
            if len(h4_closes) >= 50:
                # 4H相当の EMA を 5m足での等価ピリオドで計算 (9H=9*12=108本, 21H=21*12=252本)
                ema9_4h_equiv = _ema(h4_closes[-min(len(h4_closes), 300):], 108)
                ema21_4h_equiv = _ema(h4_closes[-min(len(h4_closes), 300):], 252)
                ema_golden_4h = ema9_4h_equiv > ema21_4h_equiv
                if ema_golden_4h:
                    logger.info(
                        "Pattern C: 5m DEAD だが 4H EMA GOLDEN "
                        "(ema9_4h=%.2f > ema21_4h=%.2f) → 許可 (confidence低め)",
                        ema9_4h_equiv, ema21_4h_equiv,
                    )

        if not ema_golden_5m and not ema_golden_4h:
            return None

        # 2. 4H range position (底値圏チェック)
        h4_low, h4_high = self._h4_range(idx - 1, h4_window)
        entry = candle["c"]
        pos = self._range_position(entry, h4_low, h4_high)
        if pos >= h4_max_pct:
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

        # 4H EMAのみの場合はconfidenceを下げて保守的に
        confidence = 0.75 if ema_golden_5m else 0.72
        ema_source = "5m" if ema_golden_5m else "4H"

        logger.info(
            "Pattern C (quiet_long): ema_src=%s ema9=%.2f>ema21=%.2f, pos=%.1f%% < %d%%, "
            "vol_ratio(5/100)=%.2f < %.2f → LONG TP %.1f%% SL %.1f%%",
            ema_source, ema9, ema21, pos, h4_max_pct, vol_ratio, vol_ratio_max,
            tp_pct * 100, sl_pct * 100,
        )

        signal = {
            "symbol": "ETH",
            "action": "long",
            "direction": "long",
            "confidence": confidence,
            "entry_price": round(entry, 2),
            "take_profit": tp_price,
            "stop_loss": sl_price,
            "leverage": 3,
            "reasoning": (
                f"EthRubberBand C: quiet_long ({ema_source} GOLDEN), "
                f"ema9={ema9:.2f}>ema21={ema21:.2f}, "
                f"4H_pos={pos:.1f}%, vol_ratio(5/100)={vol_ratio:.2f}, "
                f"→ LONG TP {tp_pct*100:.1f}% SL {sl_pct*100:.1f}% {cut_bars}bar cut"
            ),
            "zone": "quiet_bottom",
            "pattern": "C_quiet_long",
            "exit_mode": "time_cut",
            "exit_bars": cut_bars,
            "range_position": round(pos, 1),
            "vol_ratio": round(vol_ratio, 2),
            "spike_time": candle["t"],
        }
        logger.info("Signal: long ETH @ %.2f, TP=%.2f, SL=%.2f (quiet_long %s, exit=%dbar)",
                    entry, tp_price, sl_price, ema_source, cut_bars)
        return signal

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
