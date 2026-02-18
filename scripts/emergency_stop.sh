#!/bin/bash
set -euo pipefail
# 緊急全ポジションクローズ + Kill Switch有効化
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

echo "=== EMERGENCY STOP ==="
python3 -c "
from src.risk.kill_switch import activate
from src.utils.config_loader import load_settings, get_hyperliquid_url
import os

# Kill Switch有効化
activate('Manual emergency stop')
print('Kill switch activated')

# ポジションクローズ
try:
    from hyperliquid.info import Info
    from hyperliquid.exchange import Exchange
    from eth_account import Account

    settings = load_settings()
    base_url = get_hyperliquid_url(settings)
    private_key = os.environ.get('HYPERLIQUID_PRIVATE_KEY', '')
    if not private_key:
        print('WARNING: No private key, cannot close positions')
        exit(0)

    account = Account.from_key(private_key)
    info = Info(base_url, skip_ws=True)
    exchange = Exchange(account, base_url)

    user_state = info.user_state(account.address)
    positions = user_state.get('assetPositions', [])
    for pos in positions:
        p = pos.get('position', {})
        coin = p.get('coin', '')
        szi = float(p.get('szi', '0'))
        if szi != 0 and coin:
            print(f'Closing {coin}: size={szi}')
            exchange.market_close(coin)
            print(f'Closed {coin}')
except Exception as e:
    print(f'Error closing positions: {e}')

print('=== EMERGENCY STOP COMPLETE ===')
"
