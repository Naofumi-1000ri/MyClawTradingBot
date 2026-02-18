#!/bin/bash
# daemon.sh - myClaw メインループ
# 3エージェント (Alpha/Reviewer/Coder) を while ループで順次実行。
# 同時に1つの claude -p しか動かないため、2GB RAM でも安全。
#
# systemd で daemon.sh を起動し、Restart=always で保護する。
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

if [ -f "config/runtime.env" ]; then
    set -a
    source config/runtime.env
    set +a
fi

if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate

# Kill switch 初期化
if [ ! -f "state/kill_switch.json" ]; then
    python3 -c "from src.risk.kill_switch import deactivate; deactivate()" 2>/dev/null || true
    echo "[daemon] kill_switch.json initialized"
fi
fi

CYCLE=0
ALPHA_INTERVAL=300        # 5分
REVIEWER_EVERY=12         # 12サイクルごと = 1時間
CONSECUTIVE_FAILURES=0
MAX_FAILURES=3

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

log "=== myClaw daemon starting ==="
log "Alpha: every cycle (${ALPHA_INTERVAL}s)"
log "Reviewer: every ${REVIEWER_EVERY} cycles"
log "Coder: on-demand (when improvement_queue.json exists)"

while true; do
    log "--- Cycle $CYCLE ---"

    # ── 1. Alpha (トレーダー): 毎サイクル ──
    log "[Alpha] Starting OODA cycle..."
    if bash scripts/run_cycle.sh; then
        log "[Alpha] Cycle complete"
        CONSECUTIVE_FAILURES=0
    else
        ((CONSECUTIVE_FAILURES++))
        log "[Alpha] FAILED (consecutive: $CONSECUTIVE_FAILURES)"

        if (( CONSECUTIVE_FAILURES >= MAX_FAILURES )); then
            log "CRITICAL: $MAX_FAILURES consecutive failures. Notifying human."
            python3 -c "
from src.monitor.telegram_notifier import send_message
send_message('CRITICAL: myClaw $MAX_FAILURES consecutive Alpha failures. Investigating.')
" 2>/dev/null || true
            # 失敗しても止まらない。次のサイクルで回復を試みる。
        fi
    fi

    # ── 2. Reviewer (監査役): N サイクルごと ──
    if (( CYCLE > 0 && CYCLE % REVIEWER_EVERY == 0 )); then
        log "[Reviewer] Starting hourly review..."
        bash scripts/run_reviewer.sh || log "[Reviewer] Had errors"
    fi

    # ── 3. Coder (自己改善): improvement_queue があれば ──
    QUEUE_FILE="$PROJECT_ROOT/state/improvement_queue.json"
    if [ -s "$QUEUE_FILE" ]; then
        QUEUE_LEN=$(python3 -c "
import json
try:
    with open('$QUEUE_FILE') as f:
        q = json.load(f)
    items = q.get('items', []) if isinstance(q, dict) else q
    pending = [i for i in items if i.get('status') != 'done']
    print(len(pending))
except: print(0)
" 2>/dev/null)

        if [ "${QUEUE_LEN:-0}" -gt 0 ]; then
            log "[Coder] $QUEUE_LEN pending improvements. Starting..."
            bash scripts/run_coder.sh || log "[Coder] Had errors"
        fi
    fi

    # ── サイクル完了 ──
    ((CYCLE++))
    log "--- Cycle $((CYCLE - 1)) done. Sleeping ${ALPHA_INTERVAL}s ---"
    sleep "$ALPHA_INTERVAL"
done
