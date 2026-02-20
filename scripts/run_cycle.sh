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

# 2.5 Data health validation (Collect -> Validate -> Repair/Fallback)
echo "[$TIMESTAMP] [Observe] Validating data health..."
if ! python3 -m src.collector.data_health_check; then
    echo "[$TIMESTAMP] [SAFE] Data health check failed - switching to close-only mode"
    python3 - <<'PY'
import json
from pathlib import Path

positions_path = Path("state/positions.json")
signals_path = Path("signals/signals.json")

signals = []
try:
    positions = json.loads(positions_path.read_text())
except Exception:
    positions = []

for p in positions:
    sym = p.get("symbol")
    if not sym:
        continue
    signals.append({
        "symbol": sym,
        "action": "close",
        "confidence": 1.0,
        "entry_price": None,
        "stop_loss": None,
        "take_profit": None,
        "leverage": int(float(p.get("leverage", 1) or 1)),
        "reasoning": "data health degraded -> force close-only",
    })

payload = {
    "action_type": "trade" if signals else "hold",
    "signals": signals,
    "market_summary": "data_health_check failed (close-only fallback)",
    "ooda": {"observe": "", "orient": "", "decide": "close-only fallback"},
}
signals_path.write_text(json.dumps(payload, ensure_ascii=False))
PY
    if [ -s "signals/signals.json" ]; then
        echo "[$TIMESTAMP] [SAFE] Executing close-only actions..."
        EXECUTOR_MODE=close_only python3 -m src.executor.trade_executor || echo "Close-only execution had errors"
    fi
    echo "[$TIMESTAMP] Running monitor (safe mode)..."
    python3 -m src.monitor.monitor || echo "Monitor had errors"
    echo "[$TIMESTAMP] === OODA cycle complete (safe close-only) ==="
    exit 0
fi

# data_health policy: set executor mode for this cycle
EXECUTOR_MODE=$(python3 - <<'PY'
import json
from pathlib import Path
p = Path("state/data_health.json")
mode = "all"
if p.exists():
    try:
        d = json.loads(p.read_text())
        mode = d.get("execution_mode", "all")
    except Exception:
        pass
print(mode)
PY
)
export EXECUTOR_MODE
echo "[$TIMESTAMP] Data health execution mode: $EXECUTOR_MODE"

# Update adaptive size regime for this cycle
python3 -m src.risk.size_regime || echo "Size regime update failed (non-critical)"

# 3. Orient + Decide (3エージェント合議制)
echo "[$TIMESTAMP] [Orient+Decide] Running AI brain (consensus)..."
python3 -m src.brain.brain_consensus || { echo "Brain consensus failed"; exit 1; }

# 4. Act: OODA処理 (失敗時1回リトライ)
echo "[$TIMESTAMP] [Act] Processing OODA output..."
if ! python3 -m src.brain.ooda_processor; then
    echo "[$TIMESTAMP] [WARN] OODA processing failed - retrying in 5s..."
    sleep 5
    if ! python3 -m src.brain.ooda_processor; then
        echo "[$TIMESTAMP] [WARN] OODA processing failed (2nd attempt) - clearing signals to prevent stale trades"
        python3 -c "
from src.monitor.telegram_notifier import send_message
send_message('*WARNING: ooda_processor 2回連続失敗*\nsignals.json をholdに設定しました。ログを確認してください。')
" 2>/dev/null || true
        echo '{"action_type":"hold","signals":[],"market_summary":"ooda_processor failed (2 attempts)","ooda":{"observe":"","orient":"","decide":""}}' > signals/signals.json
    fi
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
