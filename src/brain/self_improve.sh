#!/bin/bash
# self_improve.sh - Phase 2: フルエージェントモードでコード改善を実行
#
# Phase 1 (brain.sh) が action_type=self_improve を出力した場合に呼ばれる。
# claude -p を制約なし（JSON出力制約なし）で呼び、ファイル編集・テスト・commitを行う。
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

# signals.json から self_improve_task を取得
TASK=$(python3 -c "
import json
with open('signals/signals.json') as f:
    data = json.load(f)
task = data.get('self_improve_task', '')
ooda = data.get('ooda', {})
assessment = data.get('self_assessment', '')
print(f'''## 改善タスク
{task}

## OODAコンテキスト
- Observe: {ooda.get('observe', '')}
- Orient: {ooda.get('orient', '')}
- Decide: {ooda.get('decide', '')}

## Self Assessment
{assessment}''')
")

if [ -z "$TASK" ]; then
    echo "No self_improve_task found in signals.json"
    exit 0
fi

echo "=== Self-Improve: executing task ==="
echo "$TASK"
echo "==================================="

# claude -p をフルエージェントモードで実行
# --output-format json なし = ファイル編集・bash実行が可能
claude -p \
    --model sonnet \
    --append-system-prompt "$(cat <<'SYSPROMPT'
あなたはmyClawの自己改善エージェントである。
プロジェクトルートは現在のディレクトリ。

## ルール
1. 指定されたタスクを実装せよ。
2. ファイルを編集したら、python3 -c "import py_compile; py_compile.compile('対象ファイル', doraise=True)" で構文チェック。
3. 変更が正しく動くことを簡単に確認せよ。
4. 完了したら git add + git commit せよ。コミットメッセージは "self-improve: タスク概要" の形式。
5. 安全制約 (risk_manager.py, kill_switch.py) のリスク制限を緩める変更は絶対にするな。
6. 変更内容をjournal/に記録せよ。
SYSPROMPT
)" \
    "$TASK"

echo "=== Self-Improve: complete ==="
