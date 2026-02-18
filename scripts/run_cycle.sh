#!/bin/bash
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# Load runtime env (secrets / API keys)
if [ -f "config/runtime.env" ]; then
    set -a
    source config/runtime.env
    set +a
fi

# Activate venv
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
echo "[$TIMESTAMP] === Starting myClaw OODA cycle ==="

# 1. Kill Switch チェック
python3 -c "
from src.risk.kill_switch import is_active
if is_active():
    print('Kill switch is ACTIVE. Skipping cycle.')
    exit(1)
" || { echo "Kill switch active or check failed. Aborting."; exit 1; }

# 2. Observe
echo "[$TIMESTAMP] [Observe] Collecting market data..."
python3 -m src.collector.data_collector || { echo "Data collection failed"; exit 1; }

# 3. Orient + Decide
echo "[$TIMESTAMP] [Orient+Decide] Running AI brain..."
bash src/brain/brain.sh || { echo "Brain failed"; exit 1; }

# 4. Act: OODA処理
echo "[$TIMESTAMP] [Act] Processing OODA output..."
if ! python3 -m src.brain.ooda_processor; then
    echo "[$TIMESTAMP] [WARN] OODA processing failed - clearing signals to prevent stale trades"
    echo '{"action_type":"hold","signals":[],"market_summary":"ooda_processor failed","ooda":{"observe":"","orient":"","decide":""}}'  > signals/signals.json
fi

# 5. Act: action_type に応じて分岐
ACTION_TYPE=$(python3 -c "
import json
try:
    with open('signals/signals.json') as f:
        print(json.load(f).get('action_type', 'hold'))
except: print('hold')
")
echo "[$TIMESTAMP] Action type: $ACTION_TYPE"

case "$ACTION_TYPE" in
    trade)
        echo "[$TIMESTAMP] [Act] Executing trades..."
        python3 -m src.executor.trade_executor || echo "Trade execution had errors"
        ;;
    self_improve)
        echo "[$TIMESTAMP] [Act] Self-improving..."
        bash src/brain/self_improve.sh || echo "Self-improve had errors"
        ;;
    *)
        echo "[$TIMESTAMP] [Act] No execution needed ($ACTION_TYPE)"
        ;;
esac

# 6. Monitor
echo "[$TIMESTAMP] Running monitor..."
python3 -m src.monitor.monitor || echo "Monitor had errors"

echo "[$TIMESTAMP] === OODA cycle complete ==="
