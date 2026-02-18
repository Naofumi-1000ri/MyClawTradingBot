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

# セキュリティ: タイムスタンプが10分以内のシグナルのみ実行
SIGNAL_AGE=$(python3 -c "
import json, time, os
try:
    mtime = os.path.getmtime('signals/signals.json')
    age = time.time() - mtime
    print(int(age))
except:
    print(9999)
")
if [ "$SIGNAL_AGE" -gt 600 ]; then
    echo "[self_improve] Signal too old (${SIGNAL_AGE}s). Skipping for safety."
    exit 0
fi

# セキュリティ: タスク長制限 (2000文字)
TASK_LEN=${#TASK}
if [ "$TASK_LEN" -gt 2000 ]; then
    echo "[self_improve] Task too long (${TASK_LEN} chars). Skipping for safety."
    exit 0
fi

# セキュリティ: 危険パターン検出
DANGEROUS=$(echo "$TASK" | grep -iE "rm -rf|curl |wget |/etc/|~/.ssh|base64 |eval |exec(" || true)
if [ -n "$DANGEROUS" ]; then
    echo "[self_improve] Dangerous pattern detected in task. Skipping."
    exit 1
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

# セキュリティ: 禁止ファイルが変更されていないかチェック (run_coder.sh と同じガードレール)
GIT_DIFF=$(git diff --name-only HEAD~1 HEAD 2>/dev/null || echo "")
if [ -n "$GIT_DIFF" ]; then
    FORBIDDEN=$(echo "$GIT_DIFF" | grep -E "^(src/risk/|src/executor/trade_executor\.py|scripts/daemon\.sh|scripts/emergency_stop\.sh|config/secrets|config/runtime\.env|config/risk_params\.yaml|\.git/)" || true)
    if [ -n "$FORBIDDEN" ]; then
        echo "[self_improve] SECURITY: Forbidden files modified! Reverting."
        echo "Forbidden: $FORBIDDEN"
        git revert --no-edit HEAD 2>/dev/null || git reset --hard HEAD~1 2>/dev/null || true
        python3 -c "
from src.monitor.telegram_notifier import send_message
send_message('SECURITY: self_improve modified forbidden files. Reverted.\n$FORBIDDEN')
" 2>/dev/null || true
        exit 1
    fi
fi

echo "=== Self-Improve: complete ==="
