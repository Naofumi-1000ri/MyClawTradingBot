#!/bin/bash
# run_coder.sh - Coder エージェント (自己改善)
# improvement_queue.json のタスクを1つずつ実行。
#
# ガードレール:
# 1. 変更可能ファイルのホワイトリスト (ALLOWED_PATHS)
# 2. risk/, kill_switch.py, trade_executor.py は変更禁止
# 3. 変更後に構文チェック
# 4. git diff を記録、Telegram通知
# 5. 1回の起動で最大1タスクのみ実行
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd 2>/dev/null || cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
echo "[$TIMESTAMP] === Coder: starting ==="

QUEUE_FILE="state/improvement_queue.json"

# 最初のpendingタスクを取得
TASK=$(python3 -c "
import json
try:
    with open('$QUEUE_FILE') as f:
        q = json.load(f)
    items = q.get('items', []) if isinstance(q, dict) else q
    for i, item in enumerate(items):
        if item.get('status') == 'pending':
            print(json.dumps({'index': i, 'task': item['task'], 'priority': item.get('priority','medium'), 'target_file': item.get('target_file')}, ensure_ascii=False))
            break
except: pass
")

if [ -z "$TASK" ]; then
    echo "[$TIMESTAMP] No pending tasks in queue"
    exit 0
fi

TASK_DESC=$(echo "$TASK" | python3 -c "import json,sys; print(json.load(sys.stdin)['task'])")
TASK_INDEX=$(echo "$TASK" | python3 -c "import json,sys; print(json.load(sys.stdin)['index'])")
echo "[$TIMESTAMP] Task: $TASK_DESC"

# タスクをin_progressに更新
python3 -c "
import json
with open('$QUEUE_FILE') as f:
    q = json.load(f)
q['items'][$TASK_INDEX]['status'] = 'in_progress'
with open('$QUEUE_FILE', 'w') as f:
    json.dump(q, f, ensure_ascii=False, indent=2)
"

# git の現在のHEADを記録 (ロールバック用)
GIT_BEFORE=$(git rev-parse HEAD 2>/dev/null || echo "no-git")

# claude -p をフルエージェントモードで実行 (ガードレール付きプロンプト)
claude -p \
    --model sonnet \
    --append-system-prompt "$(cat <<'SYSPROMPT'
あなたはmyClawのCoderエージェントである。
Reviewerから割り当てられた改善タスクを実行する。

## 厳守ルール

1. 以下のファイルは絶対に変更してはならない:
   - src/risk/risk_manager.py
   - src/risk/kill_switch.py
   - src/executor/trade_executor.py
   - scripts/daemon.sh
   - scripts/emergency_stop.sh
   - config/risk_params.yaml (リスク制限を緩める変更は禁止)

2. 変更可能な対象:
   - src/collector/ (データ収集の改善)
   - src/brain/build_context.py (コンテキスト構築の改善)
   - src/brain/prompts/ (プロンプトの改善)
   - src/brain/schemas/ (スキーマの改善)
   - src/state/ (状態管理の改善)
   - src/monitor/ (モニタリングの改善)
   - src/utils/ (ユーティリティの改善)
   - config/settings.yaml (シンボル追加等)

3. ファイルを変更したら、構文チェックを実行:
   python3 -c "import py_compile; py_compile.compile('対象ファイル', doraise=True)"

4. 変更内容をjournal/に記録せよ。

5. 完了したら git add + git commit。コミットメッセージは "coder: タスク概要" の形式。

6. 1つのタスクだけ実行し、完了したら終了せよ。
SYSPROMPT
)" \
    "$TASK_DESC"

CODER_EXIT=$?

# git diff を確認
GIT_AFTER=$(git rev-parse HEAD 2>/dev/null || echo "no-git")

if [ "$GIT_BEFORE" != "$GIT_AFTER" ] && [ "$GIT_BEFORE" != "no-git" ]; then
    DIFF_SUMMARY=$(git diff --stat "$GIT_BEFORE".."$GIT_AFTER" 2>/dev/null || echo "no diff")
    echo "[$TIMESTAMP] Changes committed:"
    echo "$DIFF_SUMMARY"

    # 禁止ファイルが変更されていないかチェック
    FORBIDDEN=$(git diff --name-only "$GIT_BEFORE".."$GIT_AFTER" 2>/dev/null | grep -E "^(src/risk/|src/executor/trade_executor\.py|scripts/daemon\.sh|scripts/emergency_stop\.sh|config/secrets|config/runtime\.env|config/risk_params\.yaml|\.git/)" || true)
    if [ -n "$FORBIDDEN" ]; then
        echo "[$TIMESTAMP] SECURITY: Forbidden files modified! Reverting."
        echo "Forbidden: $FORBIDDEN"
        git revert --no-edit HEAD 2>/dev/null || git reset --hard HEAD~1 2>/dev/null || true
        python3 -c "
from src.monitor.telegram_notifier import send_message
send_message('SECURITY: Coder modified forbidden files. Reverted.\\n$FORBIDDEN')
" 2>/dev/null || true
        CODER_EXIT=1
    else
        # Telegram通知 (diff summary)
        python3 -c "
from src.monitor.telegram_notifier import send_message
send_message('Coder completed task:\\n$TASK_DESC\\n\\nChanges:\\n$DIFF_SUMMARY')
" 2>/dev/null || true
    fi
fi

# タスクのステータス更新
python3 -c "
import json
from datetime import datetime, timezone
with open('$QUEUE_FILE') as f:
    q = json.load(f)
q['items'][$TASK_INDEX]['status'] = 'done' if $CODER_EXIT == 0 else 'failed'
q['items'][$TASK_INDEX]['completed_at'] = datetime.now(timezone.utc).isoformat()
with open('$QUEUE_FILE', 'w') as f:
    json.dump(q, f, ensure_ascii=False, indent=2)
"

echo "[$TIMESTAMP] === Coder: complete (exit=$CODER_EXIT) ==="
