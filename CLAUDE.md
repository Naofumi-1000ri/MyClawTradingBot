# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## ⚠️ 重要: このコードの正本はVPS上にある

**ソースコードの正本: VPS `claw` の `~/myClaw/`**

- ローカルMacにコピーが存在する場合でも、**必ずVPS上のコードを編集すること**
- VPSへの接続: `ssh claw`
- ローカルファイルは古い可能性がある。信頼するな。

---

## 日本語で応答すること

## 記憶に頼るな、ドキュメントに残せ

- コンテキストは失われる前提で行動する
- 作業前に `.claude/commands/` のスキルファイルを確認してから着手

## トレード運用方針

- トレード戦略・判断はユーザーに聞かず、自律的に判断・実行すること
- ユーザーに確認を求めるのは入出金など環境面のみ

---

## システム概要

Claude Code `-p` (Max 20x) をバックエンドにした自律型暗号通貨トレーディングエージェント。

```
daemon.sh (while true, 5分ループ)
  ├─ 毎サイクル: run_cycle.sh (Alpha AI)
  │   ├─ data_collector.py → market_data.json + equity取得
  │   ├─ chart_generator.py → 15m/1H/4H チャート画像 (9枚)
  │   ├─ brain.sh → claude -p --allowedTools Read → signals.json
  │   ├─ ooda_processor.py → journal/git commit
  │   └─ trade_executor.py → Hyperliquid注文
  ├─ 12サイクルごと: run_reviewer.sh (Reviewer AI)
  └─ on-demand: run_coder.sh (Coder AI, 禁止リスト付き)
```

## クイックコマンド (VPS上で実行)

```bash
cd ~/myClaw && source .venv/bin/activate

make cycle      # 1サイクル実行
make daemon     # while ループ起動
make logs       # ステータス確認
make stop       # 緊急停止
bash scripts/logs.sh tail  # リアルタイムログ
```

## 重要ファイル

| ファイル | 役割 |
|---------|------|
| `src/brain/prompts/system_prompt.md` | AIのトレード判断基準 |
| `config/settings.yaml` | environment/symbols設定 |
| `config/risk_params.yaml` | リスク制限値 (**AI変更禁止**) |
| `config/runtime.env` | 秘密鍵/APIキー (gitignore済み) |
| `state/kill_switch.json` | 緊急停止フラグ |

## セキュリティ制約

- `src/risk/`, `src/executor/trade_executor.py` はAI自律変更禁止
- `config/risk_params.yaml` はAI自律変更禁止
- `config/runtime.env` はgitignore済み (秘密鍵含む)
