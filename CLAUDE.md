# CLAUDE.md

Claude Code (claude.ai/code) への指示書。

---

## ⚠️ 重要: ここがVPS本番環境

**このマシンが本番VPS。`/home/claw/myClaw/` が正本。**

- ここで編集したコードがそのまま本番で動く
- systemd (`myclaw.service`) で daemon.sh が常時稼働中
- `/opt/myclaw` は存在しない。古いドキュメントに残っていたら無視

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

Hyperliquid DEX 上の自律型暗号通貨トレーディングシステム。
ゴム理論 (出来高スパイク検知) によるルールベース戦略。

```
daemon.sh (while true, 5分ループ)
  ├─ 毎サイクル: run_cycle.sh
  │   ├─ data_collector.py → market_data.json + アーカイブ蓄積
  │   ├─ brain_consensus.py (ゴム戦略)
  │   │   ├─ BTC RubberWall: 出来高スパイク → BULL/BEAR判定 → long/short
  │   │   ├─ ETH RubberBand: 2パターン (反転LONG + モメンタムSHORT)
  │   │   └─ SOL RubberWall: BTC型 (BEAR spike → SHORT, deep反転LONG)
  │   ├─ ooda_processor.py → journal/git commit
  │   └─ trade_executor.py → Hyperliquid注文
  ├─ 12サイクルごと: run_reviewer.sh (Reviewer AI)
  └─ on-demand: run_coder.sh (Coder AI)
```

## ⚠️ HLClient APIラッパー（厳守）

**Hyperliquid APIへのアクセスは必ず src/api/hl_client.py の HLClient を経由すること。**

- hyperliquid SDK (Info, Exchange, Account) をビジネスロジックで直接importするな。
- 新しいAPI操作が必要な場合は HLClient にメソッドを追加すること。
- テストでは HLClient をモック差し替えして使ってよい。
- equity計算は HLClient.get_equity() の1箇所のみ。他に書くな。コピペ厳禁。
- 理由: Portfolio Margin口座のequity計算は3回バグ再発した地雷。ラッパーに封印済み。

## ⚠️ 既知の地雷: equity 計算 (3回再発 → HLClientに封印済み)

**この口座は統合口座 (Portfolio Margin)。equity 計算を触る時は必ず読め。**

- `marginSummary.accountValue` ≠ 口座残高 (担保はspot側にある、perps側は ~$20)
- **正しい equity = spot_usdc + sum(perps unrealized PnL)** ≈ $508
- equity 計算は `src/api/hl_client.py:HLClient.get_equity()` の **1箇所のみ**
- 詳細: `reference/docs/api_reference.md` §6.4

## 運用コマンド (VPS上で実行)

```bash
# systemd管理 (本番運用) — sudoパスワードは claw
echo claw | sudo -S systemctl start myclaw     # 起動
echo claw | sudo -S systemctl stop myclaw      # 停止
echo claw | sudo -S systemctl restart myclaw   # 再起動
echo claw | sudo -S systemctl status myclaw    # 状態確認
journalctl -u myclaw -f                        # ログ追従

# 手動実行 (デバッグ用)
cd ~/myClaw && source .venv/bin/activate
make cycle      # 1サイクル手動実行
make daemon     # foregroundでdaemon起動
make logs       # ログ確認
make stop       # 緊急停止
make status     # 状態表示
```

## 重要ファイル

| ファイル | 役割 |
|---------|------|
| `src/brain/brain_consensus.py` | ゴム戦略オーケストレーター (BTC+ETH+SOL) |
| `src/strategy/btc_rubber_wall.py` | BTC出来高スパイク検知 + キャッシュ最適化 |
| `src/strategy/eth_rubber_band.py` | ETH 2パターン (反転/モメンタム) |
| `src/strategy/sol_rubber_wall.py` | SOL BTC型ゴムの壁 (SHORT主体, 広TP/SL) |
| `src/strategy/base.py` | 戦略基底クラス (vol_ratio, h4_range等) |
| `src/api/hl_client.py` | Hyperliquid APIラッパー (**equity計算はここだけ**) |
| `src/collector/data_collector.py` | データ収集 (HLClient経由) |
| `src/executor/trade_executor.py` | 注文執行 (HLClient経由) |
| `config/settings.yaml` | 環境/銘柄/戦略パラメータ |
| `config/risk_params.yaml` | リスク制限値 (**AI変更禁止**) |
| `config/runtime.env` | 秘密鍵/APIキー (gitignore済み) |
| `state/kill_switch.json` | 緊急停止フラグ |
| `reference/docs/api_reference.md` | Hyperliquid API仕様 + インシデント記録 |
| `reference/docs/trend_waveform_analysis.md` | BTC波形トレンド分析 (Tooth Sharpness Theory) |
| `reference/docs/frequency_session_analysis.md` | FFT・セッション分析 (US Wave Rider, Mirror Theory等) |
| `reference/docs/issues.md` | 未解決の問題・要検討事項 |

## セキュリティ制約

- `config/runtime.env` はgitignore済み (秘密鍵含む)
