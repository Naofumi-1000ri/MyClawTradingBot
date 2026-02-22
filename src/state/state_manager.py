"""State management for myClaw: positions, trades, daily P&L."""

from datetime import datetime, timezone
from pathlib import Path

from src.utils.config_loader import get_state_dir
from src.utils.file_lock import atomic_write_json, read_json
from src.utils.logger import setup_logger
from src.utils.safe_parse import parse_leverage, safe_float

logger = setup_logger("state_manager")

MAX_TRADE_HISTORY = 500


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

    @staticmethod
    def _sum_unrealized_from_positions(positions: list) -> float:
        total = 0.0
        for p in positions:
            if not isinstance(p, dict):
                continue
            total += safe_float(p.get("unrealized_pnl", 0), label="unrealized_pnl")
        return total

    def sync_positions(self, client) -> list:
        """Sync positions from Hyperliquid API via HLClient and save to state."""
        try:
            positions = client.get_positions()
            self.save_positions(positions)
            logger.info("Synced %d positions from API", len(positions))
            # ポジション同期後、daily_pnl.unrealizedとの整合を補正
            self.reconcile_daily_unrealized(positions)
            # ポジションがなくなった銘柄の meta を削除
            active_symbols = {p["symbol"] for p in positions}

            # Rubber meta (ETH/SOL用、BTC補完)
            for sym in ("BTC", "ETH", "SOL"):
                meta_path = self.state_dir / f"{sym.lower()}_rubber_meta.json"
                if sym not in active_symbols and meta_path.exists():
                    try:
                        meta_path.unlink()
                        logger.info("%s rubber meta cleared (no active position)", sym)
                    except Exception as e:
                        logger.warning("Failed to clear %s rubber meta: %s", sym, e)

            # Wave Rider meta (BTC/HYPE)
            wr_meta_files = {
                "BTC": "btc_wave_rider_meta.json",
                "HYPE": "hype_wave_rider_meta.json",
            }
            for sym, fname in wr_meta_files.items():
                meta_path = self.state_dir / fname
                if sym not in active_symbols and meta_path.exists():
                    try:
                        meta_path.unlink()
                        logger.info("%s wave_rider meta cleared (no active position)", sym)
                    except Exception as e:
                        logger.warning("Failed to clear %s wave_rider meta: %s", sym, e)

            # Reversion pending (BTC WR → REV 橋渡しファイル)
            rev_pending = self.state_dir / "btc_wr_rev_pending.json"
            if "BTC" not in active_symbols and rev_pending.exists():
                try:
                    rev_pending.unlink()
                    logger.info("BTC reversion pending cleared (no active position)")
                except Exception as e:
                    logger.warning("Failed to clear reversion pending: %s", e)
            return positions
        except Exception as e:
            logger.error("Failed to sync positions: %s", e)
            return self.get_positions()

    # -- Trade History --

    def record_trade(self, trade: dict) -> None:
        """Append a trade to trade_history.json (max 500 entries)."""
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

    def update_daily_pnl(
        self,
        equity: float,
        realized_pnl: float = 0,
        api_unrealized_pnl: float | None = None,
    ) -> dict:
        """Update daily P&L. Resets if date changed."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        pnl = self.get_daily_pnl()

        if pnl.get("date") != today:
            pnl = {
                "date": today,
                "realized_pnl": 0,
                "unrealized_pnl": 0,
                "start_of_day_equity": equity,
                "equity": equity,
                "peak_equity": equity,
            }

        pnl["realized_pnl"] = float(pnl.get("realized_pnl", 0)) + realized_pnl

        # unrealized_pnl:
        # 1) API由来値があればそれを優先
        # 2) なければ equity差分から算出
        start_equity = float(pnl.get("start_of_day_equity", equity))
        if api_unrealized_pnl is not None:
            pnl["unrealized_pnl"] = float(api_unrealized_pnl)
        else:
            pnl["unrealized_pnl"] = equity - start_equity - float(pnl.get("realized_pnl", 0))
        pnl["equity"] = equity
        # peak_equity は実現ベース (start + realized) で追跡。
        # 含み益で膨らんだequityをpeakにすると、決済後に偽ドローダウンが発生する。
        realized_equity = start_equity + float(pnl.get("realized_pnl", 0))
        pnl["peak_equity"] = max(float(pnl.get("peak_equity", 0)), realized_equity)

        atomic_write_json(self.state_dir / "daily_pnl.json", pnl)
        logger.info("Daily P&L updated: realized=%.2f unrealized=%.2f equity=%.2f (start=%.2f)",
                    pnl["realized_pnl"], pnl["unrealized_pnl"], equity, start_equity)
        return pnl

    def reconcile_daily_unrealized(self, positions: list | None = None, tolerance_usd: float = 1.0) -> dict:
        """Keep daily_pnl.unrealized_pnl consistent with synced positions."""
        if positions is None:
            positions = self.get_positions()
        pnl = self.get_daily_pnl()
        if not pnl:
            return {}

        pos_upnl = self._sum_unrealized_from_positions(positions)
        current_upnl = float(pnl.get("unrealized_pnl", 0) or 0)
        if abs(pos_upnl - current_upnl) < tolerance_usd:
            return pnl

        # 補正: positions準拠に揃える
        pnl["unrealized_pnl"] = pos_upnl
        start = float(pnl.get("start_of_day_equity", pnl.get("equity", 0)) or 0)
        realized = float(pnl.get("realized_pnl", 0) or 0)
        pnl["equity"] = start + realized + pos_upnl
        atomic_write_json(self.state_dir / "daily_pnl.json", pnl)
        logger.warning(
            "Reconciled daily unrealized: %.2f -> %.2f (positions=%d)",
            current_upnl, pos_upnl, len(positions),
        )
        return pnl
