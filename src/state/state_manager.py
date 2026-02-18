"""State management for myClaw: positions, trades, daily P&L."""

from datetime import datetime, timezone
from pathlib import Path

from src.utils.config_loader import get_state_dir
from src.utils.file_lock import atomic_write_json, read_json
from src.utils.logger import setup_logger

logger = setup_logger("state_manager")

MAX_TRADE_HISTORY = 100


class StateManager:
    """Manages positions, trade history, and daily P&L state files."""

    def __init__(self):
        self.state_dir = get_state_dir()

    # -- Kill Switch --

    def get_kill_switch_status(self) -> dict:
        try:
            return read_json(self.state_dir / "kill_switch.json")
        except FileNotFoundError:
            return {"enabled": False, "reason": "", "triggered_at": ""}

    # -- Positions --

    def get_positions(self) -> list:
        try:
            data = read_json(self.state_dir / "positions.json")
            return data if isinstance(data, list) else []
        except FileNotFoundError:
            return []

    def save_positions(self, positions: list) -> None:
        atomic_write_json(self.state_dir / "positions.json", positions)

    def sync_positions(self, info, address: str) -> list:
        """Sync positions from Hyperliquid API and save to state."""
        try:
            user_state = info.user_state(address)
            mids = info.all_mids()
            positions = []
            for pos in user_state.get("assetPositions", []):
                p = pos.get("position", {})
                szi = float(p.get("szi", 0))
                if szi == 0:
                    continue
                coin = p.get("coin", "")
                mid = float(mids.get(coin, 0))
                entry = float(p.get("entryPx", 0))
                upnl = float(p.get("unrealizedPnl", 0))
                positions.append({
                    "symbol": coin,
                    "side": "long" if szi > 0 else "short",
                    "size": abs(szi),
                    "entry_price": entry,
                    "leverage": int(float(p.get("leverage", {}).get("value", 1))) if isinstance(p.get("leverage"), dict) else 1,
                    "opened_at": None,
                    "unrealized_pnl": upnl,
                    "mid_price": mid,
                })
            self.save_positions(positions)
            logger.info("Synced %d positions from API", len(positions))
            return positions
        except Exception as e:
            logger.error("Failed to sync positions: %s", e)
            return self.get_positions()

    # -- Trade History --

    def record_trade(self, trade: dict) -> None:
        """Append a trade to trade_history.json (max 100 entries)."""
        path = self.state_dir / "trade_history.json"
        try:
            history = read_json(path)
            if not isinstance(history, list):
                history = []
        except FileNotFoundError:
            history = []

        trade["recorded_at"] = datetime.now(timezone.utc).isoformat()
        history.append(trade)
        history = history[-MAX_TRADE_HISTORY:]
        atomic_write_json(path, history)
        logger.info("Trade recorded: %s %s %s", trade.get("symbol"), trade.get("side"), trade.get("size"))

    # -- Daily P&L --

    def get_daily_pnl(self) -> dict:
        try:
            return read_json(self.state_dir / "daily_pnl.json")
        except FileNotFoundError:
            return {"date": "", "realized_pnl": 0, "unrealized_pnl": 0, "equity": 0, "peak_equity": 0}

    def update_daily_pnl(self, equity: float, realized_pnl: float = 0) -> dict:
        """Update daily P&L. Resets if date changed."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        pnl = self.get_daily_pnl()

        if pnl.get("date") != today:
            pnl = {"date": today, "realized_pnl": 0, "unrealized_pnl": 0, "equity": equity, "peak_equity": equity}

        pnl["realized_pnl"] = float(pnl.get("realized_pnl", 0)) + realized_pnl
        pnl["equity"] = equity
        pnl["peak_equity"] = max(float(pnl.get("peak_equity", 0)), equity)
        
        atomic_write_json(self.state_dir / "daily_pnl.json", pnl)
        logger.info("Daily P&L updated: realized=%.2f equity=%.2f", pnl["realized_pnl"], equity)
        return pnl
