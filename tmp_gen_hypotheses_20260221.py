"""
2026-02-21 新規仮説生成スクリプト

根拠データ:
- 425スナップショット (2026-02-18〜20) の実データ分析
- 現在の戦略の静観率: 99.8% (スパイクなし)
- BTC bull spike 2x+ on BULL candle: WR_long=40%, WR_short=33%
- ETH ask_max急増2倍以上 -> 3cyc後の下落(-0.2%): 24%
- BTC consecutive 3+ bear candles -> continued: 23%
- SOL price 5cycle分布: p10=-0.75%, p90=+0.82%
- BTC funding >5e-6 -> price drop: 26%
- ETH negative funding -> drop: 25%
- SOL imbalance<0.5 short WR: 29%
"""
import sys
sys.path.insert(0, '/home/claw/myClaw')

from src.hypothesis.manager import create_hypothesis, get_by_status, _load_all

# 現在のactiveな仮説数確認
raw = get_by_status('raw')
shadow = get_by_status('shadow')
print(f"Current raw: {len(raw)}, shadow: {len(shadow)}")

new_hypotheses = []

# ====================================================
# カテゴリ1: ゴム戦略を補完するトレンドフォロー仮説
# (静観多発の根本対策: vol_threshold未満でもエントリー)
# ====================================================

# Hyp-A: BTC 連続陰線3本 + EMA dead + funding高い → SHORT補完
new_hypotheses.append({
    "description": "BTC EMA9<EMA21 (DEADクロス) かつ連続15m陰線3本以上 かつ funding_rate>+3e-6 (ロング過熱) のとき、3サイクル (15分) 以内に-0.2%以上下落する。根拠: トレンドフォロー + ロング清算圧力の複合。vol_thresholdに依存しない補完シグナル。連続陰線WR_continue=23%にfunding条件を追加して精度向上を狙う。",
    "trigger": {
        "conditions": [
            {"field": "ema_cross", "symbol": "BTC", "op": "==", "value": "dead"},
            {"field": "consecutive_bear_candles", "symbol": "BTC", "op": ">=", "value": 3},
            {"field": "funding_rate", "symbol": "BTC", "op": ">=", "value": 3e-6}
        ],
        "logic": "AND"
    },
    "prediction": {"symbol": "BTC", "direction": "short", "horizon_cycles": 3, "expected_move_pct": 0.2}
})

# Hyp-B: ETH ask_wall_max大口 + EMA dead + 4H下降 → SHORT
# 根拠: ask壁急増は大口売り参加者の動向。
new_hypotheses.append({
    "description": "ETH ask_wall_maxが500ETH以上 かつ EMA9<EMA21 (DEADクロス) かつ 4H価格変化<0 のとき、3サイクル以内に-0.2%以上下落する。根拠: 大口ask壁存在 + EMA下降トレンド = 売り圧力持続のシグナル。ETH ask_max surge後の下落率24%を改善。過去に2000ETH超の大型壁を5回観測。",
    "trigger": {
        "conditions": [
            {"field": "orderbook.ask_wall_max", "symbol": "ETH", "op": ">=", "value": 500},
            {"field": "ema_cross", "symbol": "ETH", "op": "==", "value": "dead"},
            {"field": "price_change_4h", "symbol": "ETH", "op": "<", "value": 0}
        ],
        "logic": "AND"
    },
    "prediction": {"symbol": "ETH", "direction": "short", "horizon_cycles": 3, "expected_move_pct": 0.2}
})

# Hyp-C: BTC bull spike (vol 2x+) + EMA golden → LONG補完
# 根拠: BTC bull spike 2x+ -> WR_long=40%
new_hypotheses.append({
    "description": "BTC 15m出来高が前サイクル平均比2.0倍以上かつ陽線 (consecutive_bull>=1) かつ EMA9>EMA21 (GOLDENクロス) のとき、3サイクル (15分) 以内に+0.2%以上上昇する。根拠: 実データ(425snap)でbull spike 2x+後のWR_long=40%を確認。ゴールデンクロスとの重複で誤シグナル削減。vol_thresholdに依存しない上昇補完戦略。",
    "trigger": {
        "conditions": [
            {"field": "volume_ratio", "symbol": "BTC", "op": ">=", "value": 2.0},
            {"field": "consecutive_bull_candles", "symbol": "BTC", "op": ">=", "value": 1},
            {"field": "ema_cross", "symbol": "BTC", "op": "==", "value": "golden"}
        ],
        "logic": "AND"
    },
    "prediction": {"symbol": "BTC", "direction": "long", "horizon_cycles": 3, "expected_move_pct": 0.2}
})

# ====================================================
# カテゴリ2: SOL固有パターン
# ====================================================

# Hyp-D: SOL EMA dead + imbalance<0.5 (ask優勢) + funding neutral → SHORT
new_hypotheses.append({
    "description": "SOL EMA9<EMA21 (DEADクロス) かつ orderbook imbalance<=0.5 (ask壁がbid壁の2倍以上) かつ funding_rate>=-2e-5 (スクイーズなし) のとき、3サイクル以内に-0.3%以上下落する。根拠: ask優勢の板構造(SOL imbalance<0.5 short基礎WR=29%) + EMAデッドクロス = 売り圧持続。funding中立のため反転リスク低。",
    "trigger": {
        "conditions": [
            {"field": "ema_cross", "symbol": "SOL", "op": "==", "value": "dead"},
            {"field": "orderbook.imbalance", "symbol": "SOL", "op": "<=", "value": 0.5},
            {"field": "funding_rate", "symbol": "SOL", "op": ">=", "value": -2e-5}
        ],
        "logic": "AND"
    },
    "prediction": {"symbol": "SOL", "direction": "short", "horizon_cycles": 3, "expected_move_pct": 0.3}
})

# Hyp-E: SOL 連続陰線3本 + 4H下降 + funding neutral → SHORT
new_hypotheses.append({
    "description": "SOL 15m連続陰線3本以上 かつ 4H価格変化<=-0.5% (4Hダウントレンド) かつ funding_rate>=-3e-5 (スクイーズなし) のとき、2サイクル以内に-0.2%以上下落する。根拠: 4Hトレンドに沿った短期モメンタム継続。SOLのp10=-0.75%から短期下落余地あり。vol_thresholdに依存しない補完SHORT。",
    "trigger": {
        "conditions": [
            {"field": "consecutive_bear_candles", "symbol": "SOL", "op": ">=", "value": 3},
            {"field": "price_change_4h", "symbol": "SOL", "op": "<=", "value": -0.5},
            {"field": "funding_rate", "symbol": "SOL", "op": ">=", "value": -3e-5}
        ],
        "logic": "AND"
    },
    "prediction": {"symbol": "SOL", "direction": "short", "horizon_cycles": 2, "expected_move_pct": 0.2}
})

# ====================================================
# カテゴリ3: クロス銘柄パターン
# ====================================================

# Hyp-F: BTC + ETH 両方EMA dead 同期 → ETH SHORT
new_hypotheses.append({
    "description": "BTC EMA9<EMA21 (DEADクロス) かつ ETH EMA9<EMA21 (DEADクロス) かつ ETH price_change_15m<=-0.1% のとき、ETHが3サイクル以内に-0.2%以上下落する。根拠: BTC/ETH同期デッドクロスは全市場の下落圧力を示す強いシグナル。単独より複合条件で精度向上。実運用でBTC先行→ETH追随パターン複数回確認。",
    "trigger": {
        "conditions": [
            {"field": "ema_cross", "symbol": "BTC", "op": "==", "value": "dead"},
            {"field": "ema_cross", "symbol": "ETH", "op": "==", "value": "dead"},
            {"field": "price_change_15m", "symbol": "ETH", "op": "<=", "value": -0.1}
        ],
        "logic": "AND"
    },
    "prediction": {"symbol": "ETH", "direction": "short", "horizon_cycles": 3, "expected_move_pct": 0.2}
})

# Hyp-G: BTC funding高い + SOL EMA dead → SOL SHORT
new_hypotheses.append({
    "description": "BTC funding_rate>=+5e-6 (ロング過熱) かつ SOL EMA9<EMA21 (DEADクロス) かつ SOL price_change_15m<=-0.1% のとき、SOLが3サイクル以内に-0.3%以上下落する。根拠: BTC funding高水準時はロング清算圧力が全体に波及。SOLのEMAデッドクロスが既にトレンド確認済みの場合の相乗効果。BTC funding>5e-6 WR=26%への補完。",
    "trigger": {
        "conditions": [
            {"field": "funding_rate", "symbol": "BTC", "op": ">=", "value": 5e-6},
            {"field": "ema_cross", "symbol": "SOL", "op": "==", "value": "dead"},
            {"field": "price_change_15m", "symbol": "SOL", "op": "<=", "value": -0.1}
        ],
        "logic": "AND"
    },
    "prediction": {"symbol": "SOL", "direction": "short", "horizon_cycles": 3, "expected_move_pct": 0.3}
})

# ====================================================
# カテゴリ4: MACDモメンタム仮説
# ====================================================

# Hyp-H: BTC MACD expanding + dead cross → BTC SHORT
new_hypotheses.append({
    "description": "BTC EMA9<EMA21 (DEADクロス) かつ MACD-histogram<=-50 かつ macd_direction==expanding (ベアモメンタム加速中) のとき、4サイクル (20分) 以内に-0.15%以上下落する。根拠: MACD拡大はトレンドの加速を示す。vol thresholdに依存しないモメンタム系シグナル。EMA dead cross + MACD expanding = 高確率下落継続。",
    "trigger": {
        "conditions": [
            {"field": "ema_cross", "symbol": "BTC", "op": "==", "value": "dead"},
            {"field": "macd_histogram", "symbol": "BTC", "op": "<=", "value": -50},
            {"field": "macd_direction", "symbol": "BTC", "op": "==", "value": "expanding"}
        ],
        "logic": "AND"
    },
    "prediction": {"symbol": "BTC", "direction": "short", "horizon_cycles": 4, "expected_move_pct": 0.15}
})

# Hyp-I: ETH MACD expanding + dead cross + 4H下降 → ETH SHORT強い
new_hypotheses.append({
    "description": "ETH EMA9<EMA21 (DEADクロス) かつ MACD-histogram<=0 かつ macd_direction==expanding (ベアモメンタム加速) かつ price_change_4h<=-0.3% のとき、4サイクル以内に-0.25%以上下落する。根拠: MACD expanding + 4H下降 = 強いダウントレンド継続シグナル。ETHのreversal_h4_filter_pct=55%と相補的。55%未満でも4H下降中はこのシグナルで補完可能。",
    "trigger": {
        "conditions": [
            {"field": "ema_cross", "symbol": "ETH", "op": "==", "value": "dead"},
            {"field": "macd_histogram", "symbol": "ETH", "op": "<=", "value": 0},
            {"field": "macd_direction", "symbol": "ETH", "op": "==", "value": "expanding"},
            {"field": "price_change_4h", "symbol": "ETH", "op": "<=", "value": -0.3}
        ],
        "logic": "AND"
    },
    "prediction": {"symbol": "ETH", "direction": "short", "horizon_cycles": 4, "expected_move_pct": 0.25}
})

# ====================================================
# カテゴリ5: Orderbook動態 (新パターン)
# ====================================================

# Hyp-J: BTC bid_total高 + ask_total低 + EMA golden → BTC LONG
new_hypotheses.append({
    "description": "BTC orderbook.bid_total>=20.0 (大口bid壁) かつ orderbook.ask_total<=2.0 (ask壁が薄い) かつ EMA9>EMA21 (GOLDENクロス) のとき、4サイクル (20分) 以内に+0.15%以上上昇する。根拠: 実データでBTC bid dominant (imbalance>500) が33件観測。bid厚+ask薄+GCの複合。BTC板は操作されにくく信頼性高い。",
    "trigger": {
        "conditions": [
            {"field": "orderbook.bid_total", "symbol": "BTC", "op": ">=", "value": 20.0},
            {"field": "orderbook.ask_total", "symbol": "BTC", "op": "<=", "value": 2.0},
            {"field": "ema_cross", "symbol": "BTC", "op": "==", "value": "golden"}
        ],
        "logic": "AND"
    },
    "prediction": {"symbol": "BTC", "direction": "long", "horizon_cycles": 4, "expected_move_pct": 0.15}
})

# Hyp-K: SOL bid_max大 + imbalance>2 + funding neutral + EMA golden → SOL LONG
# deep_reversal廃止後の代替LONG戦略
new_hypotheses.append({
    "description": "SOL orderbook.bid_max>=200 (大口bid壁) かつ orderbook.imbalance>=2.0 (bid圧倒優勢) かつ funding_rate>=-1e-5 (スクイーズなし) かつ EMA9>EMA21 (GOLDENクロス) のとき、4サイクル以内に+0.3%以上上昇する。根拠: deep_reversal廃止後の代替LONG戦略。板優位+EMA上昇の複合条件でWR50%以上を狙う。4条件ANDで誤シグナル大幅削減。",
    "trigger": {
        "conditions": [
            {"field": "orderbook.bid_max", "symbol": "SOL", "op": ">=", "value": 200.0},
            {"field": "orderbook.imbalance", "symbol": "SOL", "op": ">=", "value": 2.0},
            {"field": "funding_rate", "symbol": "SOL", "op": ">=", "value": -1e-5},
            {"field": "ema_cross", "symbol": "SOL", "op": "==", "value": "golden"}
        ],
        "logic": "AND"
    },
    "prediction": {"symbol": "SOL", "direction": "long", "horizon_cycles": 4, "expected_move_pct": 0.3}
})

# ====================================================
# カテゴリ6: ゴム戦略の感度向上検証仮説
# ====================================================

# Hyp-L: BTC vol_ratio 3.5x-5.0x BEAR + EMA dead → SHORT (低閾値検証)
new_hypotheses.append({
    "description": "BTC 15m出来高が平均の3.5x以上5.0x未満 (中程度スパイク) かつ BEAR足 かつ EMA9<EMA21 (DEADクロス) のとき、4サイクル以内に-0.15%以上下落する。根拠: 現在vol_threshold=5.0xだがサンプルが希少(1件/425snap)。3.5-5xゾーンに機会が埋もれていないかshadowで検証。EMA条件で品質担保。",
    "trigger": {
        "conditions": [
            {"field": "volume_ratio", "symbol": "BTC", "op": ">=", "value": 3.5},
            {"field": "volume_ratio", "symbol": "BTC", "op": "<", "value": 5.0},
            {"field": "consecutive_bear_candles", "symbol": "BTC", "op": ">=", "value": 1},
            {"field": "ema_cross", "symbol": "BTC", "op": "==", "value": "dead"}
        ],
        "logic": "AND"
    },
    "prediction": {"symbol": "BTC", "direction": "short", "horizon_cycles": 4, "expected_move_pct": 0.15}
})

# Hyp-M: ETH vol_ratio 2.5-3.0x BEAR + EMA dead + funding negative → SHORT
new_hypotheses.append({
    "description": "ETH 15m出来高が平均の2.5x以上3.0x未満 かつ BEAR足 かつ EMA9<EMA21 かつ funding_rate<=-1e-6 (ETHネガティブfunding) のとき、3サイクル以内に-0.2%以上下落する。根拠: momentum_threshold引き上げ(3.0→4.0x)で失われる2.5-3.0xゾーンの機会をfunding条件で補完。ETH negative funding時は売り圧持続しやすい(基礎WR=25%)。",
    "trigger": {
        "conditions": [
            {"field": "volume_ratio", "symbol": "ETH", "op": ">=", "value": 2.5},
            {"field": "volume_ratio", "symbol": "ETH", "op": "<", "value": 3.0},
            {"field": "consecutive_bear_candles", "symbol": "ETH", "op": ">=", "value": 1},
            {"field": "ema_cross", "symbol": "ETH", "op": "==", "value": "dead"},
            {"field": "funding_rate", "symbol": "ETH", "op": "<=", "value": -1e-6}
        ],
        "logic": "AND"
    },
    "prediction": {"symbol": "ETH", "direction": "short", "horizon_cycles": 3, "expected_move_pct": 0.2}
})

# ====================================================
# カテゴリ7: FFTスペクトルベース仮説
# ====================================================

# Hyp-N: ETH spectral entropy低い (周期的市場) + EMA dead → SHORT精度向上
new_hypotheses.append({
    "description": "ETH fft64_spectral_entropy<=0.80 (秩序的・周期的市場) かつ EMA9<EMA21 (DEADクロス) かつ price_change_1h<=-0.2% のとき、3サイクル以内に-0.2%以上下落する。根拠: 低スペクトルエントロピーは市場が周期的パターンに従っている状態。この時、EMAトレンドシグナルの精度が向上する。新規の実験的仮説として検証。",
    "trigger": {
        "conditions": [
            {"field": "fft64_spectral_entropy", "symbol": "ETH", "op": "<=", "value": 0.80},
            {"field": "ema_cross", "symbol": "ETH", "op": "==", "value": "dead"},
            {"field": "price_change_1h", "symbol": "ETH", "op": "<=", "value": -0.2}
        ],
        "logic": "AND"
    },
    "prediction": {"symbol": "ETH", "direction": "short", "horizon_cycles": 3, "expected_move_pct": 0.2}
})

# 仮説を登録
created = []
for hyp_data in new_hypotheses:
    result = create_hypothesis(
        description=hyp_data['description'],
        trigger=hyp_data['trigger'],
        prediction=hyp_data['prediction'],
        source="coder_20260221_pattern_analysis"
    )
    if result:
        created.append(result['id'])
        print(f"  Created: {result['id']}")
    else:
        print(f"  FAILED (limit?): {hyp_data['description'][:60]}")

print(f"\nTotal created: {len(created)}")
print(f"IDs: {created}")

# 最終状態確認
all_hyps = _load_all()
by_s = {}
for h in all_hyps:
    s = h['status']
    by_s[s] = by_s.get(s, 0) + 1
print(f"Final status counts: {by_s}")
