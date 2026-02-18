#!/bin/bash
# run_reviewer.sh - Reviewer エージェント (監査役)
# 1時間ごとに呼ばれ、Alphaのパフォーマンスを評価し、
# review.json (Alphaへのフィードバック) と
# improvement_queue.json (Coderへのタスク) を出力する。
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
echo "[$TIMESTAMP] === Reviewer: starting hourly review ==="

# コンテキスト構築: 各種状態ファイルを1つのJSONにまとめる
CONTEXT=$(python3 -c "
import json
from pathlib import Path

ctx = {}

# OODA log (直近50件に圧縮)
try:
    with open('state/ooda_log.json') as f:
        log = json.load(f)
    ctx['ooda_log_recent'] = log[-50:] if isinstance(log, list) else []
    ctx['ooda_log_total'] = len(log) if isinstance(log, list) else 0
except: ctx['ooda_log_recent'] = []

# Trade history
try:
    with open('state/trade_history.json') as f:
        ctx['trade_history'] = json.load(f)
except: ctx['trade_history'] = []

# Daily PnL
try:
    with open('state/daily_pnl.json') as f:
        ctx['daily_pnl'] = json.load(f)
except: ctx['daily_pnl'] = {}

# Positions
try:
    with open('state/positions.json') as f:
        ctx['positions'] = json.load(f)
except: ctx['positions'] = []

# Latest signals
try:
    with open('signals/signals.json') as f:
        ctx['latest_signals'] = json.load(f)
except: ctx['latest_signals'] = {}

# Previous review (if any)
try:
    with open('state/review.json') as f:
        ctx['previous_review'] = json.load(f)
except: ctx['previous_review'] = None

print(json.dumps(ctx, ensure_ascii=False))
")

if [ -z "$CONTEXT" ] || [ "$CONTEXT" = "{}" ]; then
    echo "[$TIMESTAMP] No data to review. Skipping."
    exit 0
fi

SYSTEM_PROMPT=$(cat src/brain/prompts/reviewer_prompt.md)
SCHEMA=$(cat src/brain/schemas/review_schema.json)

# claude -p で Reviewer を実行
RAW=$(echo "$CONTEXT" | claude -p \
    --output-format json \
    --model sonnet \
    --append-system-prompt "$SYSTEM_PROMPT" \
    "以下のデータを監査し、review_schema.json に従ってJSON出力せよ。
データが少ない場合も、現時点で分かる範囲で評価せよ。")

# JSON抽出 (claude -p の result ラッパーを処理)
REVIEW=$(python3 -c "
import json, sys, re
raw = json.loads(sys.stdin.read())
content = raw.get('result', '') if isinstance(raw, dict) and 'result' in raw else json.dumps(raw)
content = re.sub(r'^\`\`\`json\s*', '', content.strip())
content = re.sub(r'\`\`\`\s*$', '', content.strip())
parsed = json.loads(content)
print(json.dumps(parsed, ensure_ascii=False, indent=2))
" <<< "$RAW")

if [ -z "$REVIEW" ]; then
    echo "[$TIMESTAMP] Reviewer output was empty"
    exit 1
fi

# review.json 保存 (Alphaが次サイクルで読む)
echo "$REVIEW" | python3 -c "
import json, sys
from datetime import datetime, timezone
review = json.load(sys.stdin)
review['reviewed_at'] = datetime.now(timezone.utc).isoformat()
with open('state/review.json', 'w') as f:
    json.dump(review, f, ensure_ascii=False, indent=2)
print('Review saved to state/review.json')

# improvement_items があれば improvement_queue.json に追加
items = review.get('improvement_items', [])
if items:
    import os
    queue_path = 'state/improvement_queue.json'
    try:
        with open(queue_path) as f:
            queue = json.load(f)
    except:
        queue = {'items': []}

    for item in items:
        item['created_at'] = datetime.now(timezone.utc).isoformat()
        item['status'] = 'pending'
        queue['items'].append(item)

    with open(queue_path, 'w') as f:
        json.dump(queue, f, ensure_ascii=False, indent=2)
    print(f'Added {len(items)} items to improvement_queue.json')

# critical alert があれば Telegram通知
alerts = [a for a in review.get('risk_alerts', []) if a.get('severity') == 'critical']
if alerts:
    try:
        from src.monitor.telegram_notifier import send_message
        msg = 'REVIEWER ALERT:\\n' + '\\n'.join(a['message'] for a in alerts)
        send_message(msg)
    except: pass

score = review.get('performance_score', '?')
print(f'Performance score: {score}/100')
"

echo "[$TIMESTAMP] === Reviewer: complete ==="
