# myClaw アーキテクチャ

## これは何か

Claude Code `-p` (Max 20xサブスクリプション) をバックエンドにした自律型AIエージェント。
APIキー不要。claude CLIがインストール・認証済み。

## 動作モデル — OODA Loop

```
5分ごとのOODAサイクル:

  [Observe]  data_collector.py -> market_data.json
       |
  [Orient]   build_context.py -> context.json (市場+状態+履歴)
       |
  [Decide]   claude -p (Sonnet) -> signals.json (action_type選択)
       |
  [Act]      action_typeに応じて分岐:
              trade           -> trade_executor.py -> Hyperliquid注文
              hold            -> 何もしない
              journal         -> journal/*.md に学びを記録 -> git commit
              adjust_strategy -> strategy_proposals.json -> git commit
              research        -> research_queue.json

  毎回: ooda_processor.py が環境チェック -> 不足があればTelegramで人間に要求
```

コンポーネント間通信は **全てJSONファイル経由**。
エージェントは **指示を待たず、自分で判断して行動し、足りないものは人間に要求する**。

## ディレクトリ構成

```
~/myClaw/
├── config/
│   ├── settings.yaml          # 環境(testnet/mainnet)、シンボル、AI設定
│   ├── risk_params.yaml       # リスク制限値
│   └── gateway.yaml           # Gateway設定 (Telegram, Cron, Webhook)
│
├── src/
│   ├── collector/
│   │   └── data_collector.py  # Hyperliquid API -> data/market_data.json
│   ├── brain/
│   │   ├── brain.sh           # claude -p 呼出し (Sonnet)。セッション継続対応
│   │   ├── build_context.py   # market_data + state -> context.json (トークン圧縮)
│   │   ├── schemas/
│   │   │   └── signal_schema.json  # AIの出力JSON Schema
│   │   └── prompts/
│   │       └── system_prompt.md    # トレード分析プロンプト (ここを変えればAIの判断が変わる)
│   ├── executor/
│   │   └── trade_executor.py  # signals.json -> Hyperliquid注文API
│   ├── risk/
│   │   ├── risk_manager.py    # ポジション制限・損失制限の検証
│   │   └── kill_switch.py     # 緊急停止フラグ (state/kill_switch.json)
│   ├── state/
│   │   └── state_manager.py   # ポジション・P&L・取引履歴管理
│   ├── monitor/
│   │   ├── monitor.py         # 状態チェック + アラート
│   │   └── telegram_notifier.py
│   ├── gateway/
│   │   ├── claude_cli.py      # claude -p 非同期ラッパー (--resume対応)
│   │   └── server.py          # 常駐デーモン (Telegram + Cron + Webhook)
│   └── utils/
│       ├── config_loader.py   # YAML設定読込、パス解決
│       ├── file_lock.py       # atomic JSON read/write (fcntl.flock)
│       ├── crypto.py          # GPG秘密鍵復号
│       └── logger.py          # ロガー (logs/*.log)
│
├── scripts/
│   ├── run_cycle.sh           # 1サイクル実行 (collect->brain->execute->monitor)
│   └── emergency_stop.sh      # 全ポジション決済 + Kill Switch有効化
│
├── deploy/
│   ├── myclaw-gateway.service # systemd (常駐Gateway)
│   ├── myclaw.service         # systemd (バッチサイクル)
│   └── myclaw.timer           # systemd (5分間隔)
│
├── data/                      # ランタイム: 市場データ
│   ├── market_data.json       # collector出力
│   └── context.json           # build_context出力 (claude -pへの入力)
├── signals/
│   └── signals.json           # brain出力 (AIのトレード判断)
├── state/                     # ランタイム: 状態
│   ├── positions.json         # 現在ポジション (list)
│   ├── trade_history.json     # 取引履歴 (list, max 100)
│   ├── daily_pnl.json         # 日次P&L
│   ├── kill_switch.json       # Kill Switchフラグ
│   └── brain_session.txt      # claude -p セッションID
└── logs/                      # ログファイル
```

## JSONデータフロー

```
Hyperliquid API
      |
      v
data/market_data.json --> data/context.json --> claude -p --> signals/signals.json
                              ^                                      |
                              |                                      v
                     state/positions.json              trade_executor.py
                     state/daily_pnl.json                    |
                     state/trade_history.json                v
                              ^                     Hyperliquid 注文API
                              |                              |
                              +------------------------------+
```

## 主要JSONフォーマット

### signals/signals.json (AIの出力)

```json
{
  "signals": [
    {
      "symbol": "BTC",
      "action": "long / short / close / hold",
      "confidence": 0.85,
      "entry_price": 67500,
      "stop_loss": 66000,
      "take_profit": 70000,
      "leverage": 3,
      "reasoning": "判断理由テキスト"
    }
  ],
  "market_summary": "市場概況テキスト"
}
```

### state/positions.json

```json
[
  {
    "symbol": "BTC",
    "side": "long",
    "size": 0.1,
    "entry_price": 67000,
    "leverage": 3,
    "opened_at": "2026-02-18T06:30:00Z",
    "unrealized_pnl": 150
  }
]
```

### state/kill_switch.json

```json
{"enabled": false, "reason": null, "triggered_at": null}
```

## リスク制限 (config/risk_params.yaml)

- 1ポジション: 最大エクイティ10%
- 総エクスポージャー: 30%以下
- 同時ポジション: 3つまで
- レバレッジ: 最大10x
- 日次損失5%でKill Switch自動発動
- 最大ドローダウン15%でKill Switch自動発動
- confidence < 0.7 のシグナルは自動スキップ

## 環境

- Python 3.12 (venv: .venv/)
- Claude Code 2.1.45 (認証済み、Max 20x)
- Hyperliquid SDK 0.22.0
- 現在: testnet (config/settings.yaml の environment で切替)

## クイックコマンド

```bash
cd ~/myClaw && source .venv/bin/activate

# 個別実行
make collect          # データ取得
make brain            # AI分析
make execute          # 注文執行
make cycle            # フルサイクル

# 運用
make gateway          # 常駐Gateway起動 (Telegram+Cron)
make stop             # 緊急停止
make status           # 状態確認
```

## 秘密鍵

Hyperliquid秘密鍵は未設定。設定方法:

```bash
export HYPERLIQUID_PRIVATE_KEY=0x...
# または config/secrets.env.gpg にGPG暗号化して保存
```

推奨: Hyperliquid API Wallet (出金権限なし) を使用。

## 何を変えるとどうなるか

| 変更対象 | 影響 |
|---------|------|
| `config/settings.yaml` の symbols | 監視対象通貨が変わる |
| `config/settings.yaml` の environment | testnet <-> mainnet 切替 |
| `config/risk_params.yaml` | リスク制限値が変わる |
| `src/brain/prompts/system_prompt.md` | AIのトレード判断基準が変わる |
| `src/brain/schemas/signal_schema.json` | AIの出力フォーマットが変わる |
| `config/gateway.yaml` | Cron間隔、Telegram設定が変わる |

## 成長の記録

- `journal/` - 日付別のトレーディングジャーナル (OODA思考、自己評価)
- `state/ooda_log.json` - 全OODAサイクルのログ (最大500件)
- `state/strategy_proposals.json` - AIによる戦略変更提案
- `state/research_queue.json` - AIによる調査キュー
- `state/requests.json` - 人間への要求 (APIキー、権限、設定等)

これらはgit commit + pushされ、GitHubでエージェントの成長を追跡できる。

## 動作確認済み (2026-02-18)

- data_collector: BTC $67,514 / ETH $1,995 / SOL $85.27 取得OK
- brain.sh: claude -p (Sonnet) が市場分析しsignals.json生成OK
- セッション継続: --resume でコンテキスト維持OK
