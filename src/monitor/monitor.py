"""Monitoring module for myClaw."""

import time
from datetime import datetime, timezone
from pathlib import Path

from src.utils.config_loader import get_signals_dir, get_state_dir
from src.utils.file_lock import read_json
from src.utils.logger import setup_logger
from src.monitor.telegram_notifier import send_message

logger = setup_logger("monitor")

# Alert thresholds
SIGNAL_STALE_SECONDS = 600  # 10 minutes


def _read_safe(path: Path) -> dict:
    """Read JSON file, returning empty dict on error."""
    try:
        return read_json(path)
    except (FileNotFoundError, Exception) as e:
        logger.debug("Could not read %s: %s", path, e)
        return {}


def run_monitor() -> None:
    """Run one monitoring cycle."""
    signals_dir = get_signals_dir()
    state_dir = get_state_dir()
    alerts = []

    # 1. Check signals freshness
    signals_path = signals_dir / "signals.json"
    if signals_path.exists():
        mtime = signals_path.stat().st_mtime
        age = time.time() - mtime
        if age > SIGNAL_STALE_SECONDS:
            msg = f"Signals stale: last updated {int(age)}s ago"
            logger.warning(msg)
            alerts.append(msg)
        else:
            logger.info("Signals OK (age: %ds)", int(age))
    else:
        logger.info("No signals file yet")

    # 2. Check positions (positions.json is a list)
    positions_path = state_dir / "positions.json"
    positions = []
    if positions_path.exists():
        try:
            data = read_json(positions_path)
            positions = data if isinstance(data, list) else []
        except Exception:
            pass
    if positions:
        logger.info("Active positions: %d", len(positions))
        for p in positions:
            symbol = p.get("symbol", "?")
            side = p.get("side", "?")
            unrealized = p.get("unrealized_pnl", 0)
            logger.info("  %s %s unrealizedPnL=%.2f", symbol, side, float(unrealized))
    else:
        logger.info("No active positions")

    # 3. Check daily P&L
    daily_pnl = _read_safe(state_dir / "daily_pnl.json")
    if daily_pnl:
        realized = float(daily_pnl.get("realized_pnl", 0))
        unrealized = float(daily_pnl.get("unrealized_pnl", 0))
        total = realized + unrealized
        logger.info("Daily P&L: realized=%.2f unrealized=%.2f total=%.2f", realized, unrealized, total)
        if total < 0:
            alerts.append(f"Daily P&L negative: {total:.2f}")

    # 4. Check kill switch
    ks = _read_safe(state_dir / "kill_switch.json")
    if ks.get("enabled"):
        reason = ks.get("reason", "unknown")
        msg = f"KILL SWITCH ACTIVE: {reason}"
        logger.warning(msg)
        alerts.append(msg)

    # 5. Send alerts if any
    if alerts:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        text = f"*myClaw Alert* ({now})\n" + "\n".join(f"- {a}" for a in alerts)
        send_message(text)

    logger.info("Monitor cycle complete")


if __name__ == "__main__":
    run_monitor()
