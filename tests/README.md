# tests/ — myClaw テスト基盤

## クイックスタート

```bash
# 回帰テスト (70件, ~1秒)
make test

# 新戦略プレチェック (8件)
make pretest-strategy STRATEGY=src.strategy.btc_rubber_wall
```

---

## テスト一覧

### test_strategy.py (20件) — 戦略ロジック

純粋ロジックテスト。モック不要。`candle_factory` で生成したデータを使用。

| クラス | テスト | 目的 |
|--------|--------|------|
| **BTC RubberWall** | | |
| `TestBtcBearSpikePenetrationLong` | `test_penetration_long` | vol≥5x, 4H range下部 → LONG |
| `TestBtcBearSpikeUpperShort` | `test_upper_short` | vol≥5x, 4H range上部 → SHORT |
| `TestBtcNoSpikeHold` | `test_no_spike` | vol<5x → None (見送り) |
| `TestBtcBottomRequiresVol7x` | `test_bottom_below_7x` | vol=6.5x → None (7x必須) |
| | `test_bottom_above_7x` | vol=7.5x → LONG |
| `TestBtcQuietLong` | `test_quiet_long` | EMA golden + pos≥65% + low vol → LONG |
| **ETH RubberBand** | | |
| `TestEthReversalLong` | `test_reversal_long` | vol≥7x, pos<40% → LONG (Pattern A) |
| `TestEthMomentumShort` | `test_momentum_short` | vol≥3x, pos≥40% → SHORT (Pattern B) |
| `TestEthMomentumSkipLowVol` | `test_low_vol_skip` | Pattern B + 低ボラ → None |
| `TestEthNoSpikeHold` | `test_no_spike` | vol<3x → None |
| `TestEthQuietLong` | `test_quiet_long` | pos<45%, low vol, EMA golden → LONG (Pattern C) |
| **SOL RubberWall** | | |
| `TestSolBearSpikeShort` | `test_bear_spike_short` | vol≥5x → SHORT |
| `TestSolFundingBlocksShort` | `test_funding_blocks` | funding=-6e-5 → None (short抑制) |
| `TestSolQuietShort` | `test_quiet_short` | pos≥70%, RSI>55 → SHORT (Pattern E) |
| `TestSolNoSpikeHold` | `test_no_spike` | vol<5x → None |
| **BaseStrategy** | | |
| `TestVolRatioCalculation` | `test_vol_ratio_window` | 288本ウィンドウで正しい比率 |
| | `test_range_position` | 4H range positionが0-100 |
| | `test_confidence_to_leverage` | CAPS変換 (confidence→leverage) |
| `TestSignalFormat` | `test_btc_signal_format` | BTCシグナルの必須フィールド |
| | `test_eth_signal_format` | ETHシグナルの必須フィールド |
| | `test_sol_signal_format` | SOLシグナルの必須フィールド |

### test_brain.py (9件) — brain_consensus 内部関数

`_signals_to_merged()` と `_get_fallback_adjusted_settings()` を直接テスト。`main()` は呼ばない。

| クラス | テスト | 目的 |
|--------|--------|------|
| `TestSignalFormat` | `test_required_fields` | マージ結果の必須フィールド |
| `TestHoldWhenNoSpike` | `test_fallback_output` | 全戦略None → action_type="hold" |
| `TestTradeWhenSignalExists` | `test_signals_to_merged_trade` | シグナルあり → action_type="trade" |
| `TestFallbackPhase1Thresholds` | `test_phase1_adjustment` | 6サイクル → vol_threshold -10% |
| `TestFallbackPhase2Thresholds` | `test_phase2_adjustment` | 12サイクル → vol_threshold -20% |
| `TestFallbackResetOnTrade` | `test_no_adjustment_below_threshold` | 閾値未満 → 調整なし |
| `TestMultipleSymbolSignals` | `test_btc_eth_simultaneous` | BTC+ETH同時シグナル |
| `TestExitPriorityOverEntry` | `test_close_in_signals` | close含む → action_type="trade" |
| `TestHoldPositionForActive` | `test_hold_position_only_is_hold` | hold_positionのみ → action_type="hold" |

### test_data_collector.py (6件) — データ収集

`HLClient` と `StateManager` をモック。

| クラス | テスト | 目的 |
|--------|--------|------|
| `TestCollectOutputFormat` | `test_output_keys` | timestamp, symbols, account_equity |
| `TestCollectSymbolKeys` | `test_symbol_data_keys` | mid_price, orderbook, funding_rate |
| `TestCollectEquityIncluded` | `test_equity_positive` | account_equity > 0 |
| `TestCollectFallbackOnFailure` | `test_candle_failure_uses_prev` | candle取得失敗 → fallback |
| `TestCollectPositionSync` | `test_sync_called` | sync_positions() 呼び出し |
| `TestCollectWritesJson` | `test_atomic_write_called` | atomic_write_json() 呼び出し |

### test_executor.py (9件) — 注文実行

`HLClient`, `StateManager`, `RiskManager`, `read_json` をモック。

| クラス | テスト | 目的 |
|--------|--------|------|
| `TestHoldSkip` | `test_hold_returns_none` | action="hold" → None |
| | `test_hold_position_returns_none` | action="hold_position" → None |
| `TestLowConfidenceSkip` | `test_low_confidence` | confidence < 0.7 → None |
| `TestRiskRejected` | `test_risk_manager_rejects` | validate_signal=False → rejected |
| `TestGateEquityDrift` | `test_equity_drift_rejects` | drift > 20% → rejected |
| `TestGateDailyLoss` | `test_daily_loss_rejects` | loss ≥ 2% → rejected |
| `TestGateCooldown` | `test_cooldown_rejects` | 10分以内 → rejected |
| `TestOpenPosition` | `test_place_market_order_called` | place_market_order 呼び出し確認 |
| `TestCloseRecordsPnl` | `test_close_records` | close → PnL計算 + record_trade |

### test_integration.py (4件) — 統合テスト

data→brain→executor の流れ。`HLClient` のみモック。tmpdir でファイルI/O隔離。

| クラス | テスト | 目的 |
|--------|--------|------|
| `TestFullCycleHold` | `test_hold_no_execute` | hold → 実行されない |
| `TestFullCycleWithSignal` | `test_signal_propagation` | signals.json にシグナル出力 |
| `TestKillSwitchBlocks` | `test_kill_switch_blocks_execution` | kill_switch=True → 空リスト |
| `TestDataHealthBlocksEntry` | `test_low_data_health` | score < 80 → rejected |

### test_hl_client.py (21件) — HLClient APIラッパー (既存)

`Info`, `Exchange` SDK をモック。

### test_strategy_precheck.py (8件) — 新戦略ゲート

`--strategy-module` オプションで指定した戦略を自動検証。

| カテゴリ | テスト | 判定基準 |
|---------|--------|---------|
| フォーマット | `test_required_fields` | symbol, action, confidence, tp, sl, entry_price |
| | `test_valid_actions` | ∈ {long, short, hold, close, hold_position} |
| | `test_confidence_bounds` | 0 ≤ confidence ≤ 1 |
| | `test_tp_sl_direction` | long: tp>entry, sl<entry / short: 逆 |
| リスク準拠 | `test_max_leverage` | ≤ 10 |
| | `test_no_oversized` | size > 0 かつ合理的 |
| 品質 | `test_profit_factor` | PF > 0.5 |
| | `test_win_rate` | 勝率 > 10% |

---

## テストの追加方法

### 戦略テスト (test_strategy.py)

```python
from tests.helpers.candle_factory import make_candles, inject_spike
from src.strategy.btc_rubber_wall import BtcRubberWall

class TestNewPattern:
    def test_my_pattern(self):
        candles = make_candles(n=300, base_price=97000.0)
        inject_spike(candles, idx=299, vol_multiplier=8.0, bear=True)
        s = BtcRubberWall(candles)
        signal, cache = s.scan(cache=None)
        assert signal is not None
        assert signal["action"] == "long"
```

### Brain テスト (test_brain.py)

```python
from src.brain.brain_consensus import _signals_to_merged

class TestMyBrainLogic:
    def test_something(self):
        signals = [{"symbol": "BTC", "action": "long", ...}]
        merged = _signals_to_merged(signals)
        assert merged["action_type"] == "trade"
```

### Executor テスト (test_executor.py)

```python
from tests.test_executor import _make_executor, _make_signal

class TestMyGate:
    def test_gate_blocks(self):
        executor, mock_client, mock_state = _make_executor(equity=500.0)
        # ... setup mocks ...
        result = executor.execute_signal(_make_signal())
        assert result["status"] == "rejected"
```

### 統合テスト (test_integration.py)

```python
from tests.test_integration import _setup_integration, _run_brain

class TestMyIntegration:
    def test_scenario(self, tmp_path):
        dirs = _setup_integration(tmp_path)
        all_failed, signals = _run_brain(dirs)
        assert signals is not None
```

---

## conftest.py — 共有 fixture 一覧

| Fixture | 型 | 用途 |
|---------|------|------|
| `stable_candles` | `list[dict]` | 300本の安定キャンドル (vol≈100, trend=0) |
| `uptrend_candles` | `list[dict]` | 300本の上昇トレンド (EMA9>EMA21) |
| `mock_settings` | `dict` | テスト用 settings.yaml 相当 |
| `mock_risk_params` | `dict` | テスト用 risk_params.yaml 相当 |
| `isolated_dirs` | `dict` | tmpdir に data/signals/state ディレクトリ作成 |
| `mock_hl_client` | `MagicMock` | 全メソッド設定済みの HLClient モック |
| `strategy_module` | `type` | `--strategy-module` で指定した戦略クラス |

定数 (fixture ではないが import して使う):
```python
from tests.conftest import MOCK_SETTINGS, MOCK_RISK_PARAMS
```

---

## helpers — ユーティリティ

### candle_factory.py

テスト用 OHLCV キャンドル生成。seed指定で再現性あり。

```python
from tests.helpers.candle_factory import (
    make_candles,       # N本の安定キャンドル列
    inject_spike,       # 指定位置にスパイク足を注入
    make_uptrend_candles,  # 上昇トレンドキャンドル
    make_low_vol_candles,  # 指定範囲を低出来高に変更
)
```

| 関数 | 引数 | 説明 |
|------|------|------|
| `make_candles(n, base_price, base_volume, trend, volatility, seed)` | n=300, base_price=100.0, seed=42 | メインの生成関数 |
| `inject_spike(candles, idx, vol_multiplier, bear, price_change_pct)` | vol_multiplier=8.0, bear=True | in-placeでスパイク注入 |
| `make_uptrend_candles(n, base_price, base_volume, seed)` | trend=0.0005 | EMA9>EMA21 になるトレンド |
| `make_low_vol_candles(candles, start_idx, end_idx, vol_ratio)` | vol_ratio=0.3 | 出来高を平均の30%に |

### backtest_runner.py

`test_strategy_precheck.py` で使用する簡易バックテスト。

```python
from tests.helpers.backtest_runner import run_backtest

result = run_backtest(BtcRubberWall, candles, window=300)
# result = {"trades": [...], "pf": 1.5, "win_rate": 0.45, "total": 20}
```

| 関数 | 引数 | 戻り値 |
|------|------|--------|
| `run_backtest(strategy_class, candles, config, window)` | window=300 | `{"trades", "pf", "win_rate", "total"}` |

---

## モックの書き方パターン

### HLClient モック (基本)

```python
from unittest.mock import MagicMock

mock_client = MagicMock()
mock_client.get_equity.return_value = 500.0
mock_client.get_positions.return_value = []
mock_client.get_mid_prices.return_value = {"BTC": 97000.0, "ETH": 2700.0}
mock_client.get_candles.return_value = make_candles(n=336, base_price=97000.0)
mock_client.get_orderbook.return_value = {
    "bids": [{"px": "97000", "sz": "1"}],
    "asks": [{"px": "97001", "sz": "1"}],
}
mock_client.get_funding_rates.return_value = {"BTC": 0.0001}
mock_client.place_market_order.return_value = {
    "success": True, "status": "filled", "fill_price": 97000.0,
    "raw_response": {}, "error": None,
}
```

または conftest.py の `mock_hl_client` fixture を使う。

### HLClient をパッチする場所

```python
with patch("src.executor.trade_executor.HLClient", return_value=mock_client):
    from src.executor.trade_executor import TradeExecutor
    executor = TradeExecutor()
```

### 遅延 import されるクラスのパッチ

`StateManager` と `RiskManager` は関数内で遅延importされるため、**ソース元をパッチする**:

```python
# NG: AttributeError になる
with patch("src.collector.data_collector.StateManager", ...):  # ❌

# OK: importされる元をパッチ
with patch("src.state.state_manager.StateManager", ...):       # ✅
with patch("src.risk.risk_manager.RiskManager", ...):          # ✅
```
