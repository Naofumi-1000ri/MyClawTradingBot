# src/api/ — HLClient API リファレンス

Hyperliquid DEX APIラッパー。SDK の地雷を封印したクラス。

**全てのHyperliquid APIアクセスは HLClient 経由で行うこと。SDK直接importは禁止。**

---

## 初期化

```python
from src.api.hl_client import HLClient

# 通常 (読み書き)
client = HLClient(settings=None)  # settings省略時は load_settings() で自動読込

# 読み取り専用 (トレード系メソッド使用不可)
client = HLClient(read_only=True)
```

| 引数 | 型 | デフォルト | 説明 |
|------|------|-----------|------|
| `settings` | `dict \| None` | `None` | 設定dict。Noneなら `load_settings()` |
| `read_only` | `bool` | `False` | Trueでトレード系メソッドを無効化 |

`read_only=True` の場合、`place_market_order`, `close_position`, `cancel_order` を呼ぶと `RuntimeError` が発生する。

---

## Read メソッド (Info)

### get_equity() → float

口座残高を返す。**equity計算はこの1メソッドのみ。他で実装するな。**

```python
equity = client.get_equity()  # 508.0
```

| 戻り値 | 説明 |
|--------|------|
| `float` | Portfolio Margin: `spot_usdc + sum(perps unrealized PnL)`。Standard: `perps accountValue`。失敗時 `0.0`。 |

### get_positions() → list[dict]

現在のポジション一覧を正規化して返す。

```python
positions = client.get_positions()
# [{"symbol": "BTC", "side": "long", "size": 0.001,
#   "entry_price": 96000.0, "leverage": 3,
#   "opened_at": None, "unrealized_pnl": 50.0, "mid_price": 97000.0}]
```

| フィールド | 型 | 説明 |
|-----------|------|------|
| `symbol` | `str` | 銘柄名 (BTC, ETH, SOL等) |
| `side` | `str` | "long" or "short" |
| `size` | `float` | ポジションサイズ (絶対値) |
| `entry_price` | `float` | エントリー価格 |
| `leverage` | `int` | レバレッジ |
| `opened_at` | `None` | (APIでは取得不可) |
| `unrealized_pnl` | `float` | 含み損益 |
| `mid_price` | `float` | 現在の中値 |

### get_mid_prices() → dict[str, float]

全銘柄の中値。

```python
prices = client.get_mid_prices()
# {"BTC": 97000.0, "ETH": 2700.0, "SOL": 160.0, ...}
```

### get_candles(coin, interval, count) → list[dict]

ローソク足データ。

```python
candles = client.get_candles("BTC", interval="5m", count=336)
# [{"t": 1700000000000, "o": 97000, "c": 97050, "h": 97100, "l": 96950, "v": 150}, ...]
```

| 引数 | 型 | デフォルト | 説明 |
|------|------|-----------|------|
| `coin` | `str` | (必須) | 銘柄名 |
| `interval` | `str` | `"15m"` | `"5m"`, `"15m"`, `"1h"`, `"4h"` |
| `count` | `int \| None` | `None` | 取得本数。Noneならintervalごとのデフォルト値 |

デフォルト本数: 5m=336, 15m=96, 1h=48, 4h=50

### get_orderbook(coin, depth) → dict

L2オーダーブック。

```python
book = client.get_orderbook("BTC", depth=5)
# {"bids": [{"px": "97000", "sz": "10"}], "asks": [{"px": "97001", "sz": "10"}]}
```

| 引数 | 型 | デフォルト | 説明 |
|------|------|-----------|------|
| `coin` | `str` | (必須) | 銘柄名 |
| `depth` | `int` | `5` | 板の深さ |

**注意: px, sz は文字列。**

### get_funding_rates() → dict[str, float]

全銘柄のファンディングレート。

```python
rates = client.get_funding_rates()
# {"BTC": 0.0001, "ETH": -0.0001, "SOL": 0.0}
```

### get_user_state() → dict

生のユーザーステートをパススルー。通常は使わない。

### get_open_orders() → list[dict]

オープンオーダー一覧をパススルー。

---

## Trade メソッド (Exchange)

`read_only=True` の場合、全て `RuntimeError` を送出する。

### place_market_order(coin, side, size, leverage) → dict

成行注文。**内部でleverage設定→注文の順序を保証。**

```python
result = client.place_market_order("BTC", "long", 0.001, leverage=3)
# {"success": True, "status": "filled", "fill_price": 97000.0,
#  "raw_response": {...}, "error": None}
```

| 引数 | 型 | 説明 |
|------|------|------|
| `coin` | `str` | 銘柄名 |
| `side` | `str` | `"long"` or `"short"` |
| `size` | `float` | 注文数量 |
| `leverage` | `int` | レバレッジ |

| 戻り値フィールド | 型 | 説明 |
|-----------------|------|------|
| `success` | `bool` | filled or partial なら True |
| `status` | `str` | `"filled"`, `"partial"`, `"failed"`, `"error"` |
| `fill_price` | `float` | 約定価格。未約定時 0.0 |
| `raw_response` | `dict \| None` | SDK生レスポンス |
| `error` | `str \| None` | エラーメッセージ |

### close_position(coin) → dict

ポジションクローズ。

```python
result = client.close_position("BTC")
# {"success": True, "status": "closed", "fill_price": 97100.0, ...}
```

| status | 意味 |
|--------|------|
| `"closed"` | 正常クローズ |
| `"no_position"` | ポジションなし (SDK が None を返した場合) |
| `"failed"` | クローズ失敗 |

### cancel_order(coin, oid) → dict

注文キャンセル。

```python
result = client.cancel_order("BTC", 12345)
# {"success": True, "status": "cancelled", ...}
```

| 引数 | 型 | 説明 |
|------|------|------|
| `coin` | `str` | 銘柄名 |
| `oid` | `int` | オーダーID |

---

## 既知の地雷と対処法

### 1. equity 計算 (3回再発 → 封印済み)

**地雷**: `marginSummary.accountValue` ≠ 口座残高。Portfolio Margin口座では担保がspot側にあり、perps側は ~$20。

**対処**: `get_equity()` で `spot_usdc + sum(unrealized PnL)` を計算。**この1メソッド以外でequityを計算するな。**

### 2. szi 符号付き文字列

**地雷**: ポジションの `szi` は符号付き文字列 ("0.001" = long, "-0.001" = short)。

**対処**: `get_positions()` 内で `safe_float()` + `abs()` で正規化済み。

### 3. leverage の多態

**地雷**: `leverage` フィールドが `{"value": 3}` (dict) またはスカラー `3` で返る。

**対処**: `parse_leverage()` で統一的にint変換。

### 4. API値が全て STRING

**地雷**: Hyperliquid APIは数値を全て文字列で返す ("97000" not 97000)。

**対処**: 全メソッドで `safe_float()` を使用して変換。

### 5. leverage 設定→注文の順序

**地雷**: leverage未設定でmarket_openするとデフォルト20xで約定する。

**対処**: `place_market_order()` で必ず `update_leverage()` → `market_open()` の順序を保証。

### 6. market_close() の None 返却

**地雷**: ポジションなし時に `market_close()` が None を返す (SDK bug)。

**対処**: `close_position()` で None チェック → `"no_position"` ステータスを返却。

### 7. cancel() の位置引数

**地雷**: `exchange.cancel()` は位置引数のみ受付。kwargs 不可。

**対処**: `cancel_order()` で `self.exchange.cancel(coin, oid)` と位置引数で呼び出し。

### 8. オーダーブック構造

**地雷**: `l2_snapshot()` の `levels[0]`=bids, `levels[1]`=asks。構造が壊れていることがある。

**対処**: `get_orderbook()` で levels の型・長さを検証してから使用。

### 9. meta_and_asset_ctxs() の戻り値

**地雷**: list を返す (tuple ではない)。

**対処**: `get_funding_rates()` で `isinstance(result, (list, tuple))` + `len(result) < 2` をチェック。

### 10. candle 時間範囲

**地雷**: 時間範囲の計算ミスでデータ欠損。

**対処**: `get_candles()` でバッファ1本分を追加 (`- interval_ms`)。

---

## テストでのモック差し替えパターン

### 基本パターン

```python
from unittest.mock import MagicMock, patch

mock_client = MagicMock()
mock_client.get_equity.return_value = 500.0
mock_client.get_positions.return_value = []
mock_client.get_mid_prices.return_value = {"BTC": 97000.0}
mock_client.address = "0xMOCK"
mock_client._main_address = "0xMAIN"

# HLClient コンストラクタをモック (returnするオブジェクトを指定)
with patch("src.executor.trade_executor.HLClient", return_value=mock_client):
    from src.executor.trade_executor import TradeExecutor
    executor = TradeExecutor()
```

### conftest.py の fixture を使う

```python
def test_something(mock_hl_client):
    mock_hl_client.get_equity.return_value = 1000.0  # 上書きも可
    # ...
```

### 注文結果のモック

```python
# 約定成功
mock_client.place_market_order.return_value = {
    "success": True, "status": "filled", "fill_price": 97000.0,
    "raw_response": {}, "error": None,
}

# 約定失敗
mock_client.place_market_order.return_value = {
    "success": False, "status": "failed", "fill_price": 0.0,
    "raw_response": {}, "error": "insufficient margin",
}

# クローズ
mock_client.close_position.return_value = {
    "success": True, "status": "closed", "fill_price": 97100.0,
    "raw_response": {},
}
```

### ポジションありのモック

```python
mock_client.get_positions.return_value = [{
    "symbol": "BTC", "side": "long", "size": 0.001,
    "entry_price": 96000.0, "leverage": 3,
    "opened_at": "2026-01-01T00:00:00",
    "unrealized_pnl": 50.0, "mid_price": 97000.0,
}]
```
