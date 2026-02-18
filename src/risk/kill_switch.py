"""Kill switch module for myClaw emergency stop."""

from datetime import datetime, timezone

from src.utils.config_loader import get_state_dir
from src.utils.file_lock import atomic_write_json, read_json
from src.utils.logger import setup_logger

logger = setup_logger("kill_switch")

_KS_FILENAME = "kill_switch.json"


def _ks_path():
    return get_state_dir() / _KS_FILENAME


def is_active() -> bool:
    """Return True if kill switch is currently enabled.

    Fail-safe: if file is missing, auto-create it as disabled and return False.
    This prevents bypass via file deletion while allowing first-run.
    """
    ks_path = _ks_path()
    try:
        data = read_json(ks_path)
        return data.get("enabled", False)
    except FileNotFoundError:
        # フェイルセーフ: ファイル不存在 = 状態不明 = 停止
        # 初回起動時は daemon.sh が deactivate() を呼んで初期化する
        logger.warning("kill_switch.json not found - defaulting to ACTIVE (failsafe)")
        return True


def activate(reason: str) -> None:
    """Enable the kill switch with given reason."""
    data = {
        "enabled": True,
        "reason": reason,
        "triggered_at": datetime.now(timezone.utc).isoformat(),
    }
    atomic_write_json(_ks_path(), data)
    logger.critical("Kill switch ACTIVATED: %s", reason)


def deactivate() -> None:
    """Disable the kill switch."""
    data = {
        "enabled": False,
        "reason": "",
        "triggered_at": "",
        "deactivated_at": datetime.now(timezone.utc).isoformat(),
    }
    atomic_write_json(_ks_path(), data)
    logger.info("Kill switch deactivated")


def get_status() -> dict:
    """Return current kill switch status."""
    try:
        return read_json(_ks_path())
    except FileNotFoundError:
        return {"enabled": False, "reason": "", "triggered_at": ""}
