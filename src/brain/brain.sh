#!/bin/bash
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

# 1. コンテキスト構築
python3 -m src.brain.build_context

# 2. チャート画像生成
python3 -m src.collector.chart_generator 2>/dev/null || echo "[brain] Chart generation skipped"

# 3. セッション管理
# --allowedTools "Read" でツールを使ったセッションは再開が不安定なため
# セッション有効期間を1時間に短縮
SESSION_FILE="state/brain_session.txt"
SESSION_ARGS=""
if [ -f "$SESSION_FILE" ]; then
    SESSION_ID=$(cat "$SESSION_FILE")
    SESSION_AGE=$(( $(date +%s) - $(stat -c %Y "$SESSION_FILE" 2>/dev/null || stat -f %m "$SESSION_FILE") ))
    if [ "$SESSION_AGE" -lt 3600 ]; then  # 1時間以内のみ再開
        SESSION_ARGS="--resume $SESSION_ID"
    else
        echo "[brain] Session expired (${SESSION_AGE}s), starting fresh"
        rm -f "$SESSION_FILE"
    fi
fi

# 4. チャートファイルリスト (絶対パス)
CHART_LIST=""
for tf in 15m 1h 4h; do
    for f in "$PROJECT_ROOT/data/charts/"*_${tf}.png; do
        [ -f "$f" ] && CHART_LIST="$CHART_LIST $f"
    done
done

# 5. プロンプト
SCHEMA=$(cat src/brain/schemas/signal_schema.json)
SYSTEM_PROMPT=$(cat src/brain/prompts/system_prompt.md)

CHART_INSTRUCTION=""
if [ -n "$CHART_LIST" ]; then
    CHART_INSTRUCTION="
## チャート画像 (Readツールで必ず参照せよ)
各シンボルの 15m/1H/4H チャート画像を Read ツールで開いて視覚分析すること。
EMA9/EMA21クロス、RSI水準、MACDヒストグラム、ボリューム動向を確認せよ。

$(for f in $CHART_LIST; do echo "$f"; done)"
fi

PROMPT="市場データとチャート画像を分析し、JSON Schemaに従ってシグナルを出力せよ。コードブロックなし、純粋なJSONのみ。
${CHART_INSTRUCTION}

JSON Schema:
$SCHEMA"

STDERR_LOG="/tmp/brain_stderr.log"

# 6. claude -p 実行 (最大3回リトライ)
MAX_RETRIES=3
ATTEMPT=0
RAW=""
VALID="fail"

while [ $ATTEMPT -lt $MAX_RETRIES ]; do
    ATTEMPT=$(( ATTEMPT + 1 ))
    echo "[brain] Calling claude -p (attempt $ATTEMPT/$MAX_RETRIES, session=$([ -n "$SESSION_ARGS" ] && echo yes || echo no))..."

    # stdin はシェル変数ではなくファイルから直接渡す (大きな変数のパイプ不安定を回避)
    RAW=$(cat data/context.json | timeout 120 claude -p \
        --output-format json \
        --model sonnet \
        --allowedTools "Read" \
        --append-system-prompt "$SYSTEM_PROMPT" \
        $SESSION_ARGS \
        "$PROMPT" 2>"$STDERR_LOG") || RAW=""

    # 失敗時はstderrを記録
    if [ -s "$STDERR_LOG" ]; then
        echo "[brain] stderr: $(cat $STDERR_LOG | head -3)"
    fi

    # 空チェック + JSONバリデーション
    VALID=$(python3 -c "
import json, sys
try:
    raw = sys.stdin.read().strip()
    if not raw:
        print('empty')
        sys.exit(0)
    parsed = json.loads(raw)
    result = parsed.get('result', '') if isinstance(parsed, dict) else ''
    print('ok' if result.strip() else 'empty_result')
except Exception as e:
    print('invalid:' + str(e)[:50])
" <<< "$RAW" 2>/dev/null)

    echo "[brain] attempt $ATTEMPT result: $VALID"

    if [ "$VALID" = "ok" ]; then
        break
    fi

    # リトライ: セッションリセット
    echo "[brain] Retrying in 5s (clearing session)..."
    SESSION_ARGS=""
    rm -f "$SESSION_FILE"
    sleep 5
done

if [ "$VALID" != "ok" ]; then
    echo "[brain] All $MAX_RETRIES attempts failed (last=$VALID). Aborting."
    exit 1
fi

# 7. result 抽出
SIGNAL=$(python3 -c "
import json, sys, re
raw = json.loads(sys.stdin.read())
content = raw.get('result', '') if isinstance(raw, dict) and 'result' in raw else json.dumps(raw)
content = re.sub(r'^\`\`\`json\s*', '', content.strip())
content = re.sub(r'\`\`\`\s*$', '', content.strip())
parsed = json.loads(content)
print(json.dumps(parsed, ensure_ascii=False, indent=2))
" <<< "$RAW")

# 8. セッションID保存
SESSION_ID=$(python3 -c "
import json, sys
raw = json.loads(sys.stdin.read())
sid = raw.get('session_id', '')
if sid: print(sid)
" <<< "$RAW")
if [ -n "$SESSION_ID" ]; then
    mkdir -p state
    echo "$SESSION_ID" > "$SESSION_FILE"
fi

# 9. signals.json 保存
mkdir -p signals
echo "$SIGNAL" > signals/signals.json

CHART_COUNT=$(echo $CHART_LIST | wc -w)
echo "[brain] Complete. Charts: $CHART_COUNT, Session: ${SESSION_ID:-none}"
