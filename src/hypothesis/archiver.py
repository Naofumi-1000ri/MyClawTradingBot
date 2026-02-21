"""Market data archiver: 毎サイクルのmarket_dataスナップショットを保存。

バックテスト用の履歴データを蓄積する。
保存先: data/history/{YYYY-MM-DD}/{HHMMSS}.json.gz
"""

import gzip
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.utils.config_loader import get_data_dir, get_project_root, load_settings
from src.utils.file_lock import read_json
from src.utils.logger import setup_logger

logger = setup_logger("archiver")

HISTORY_DIR = get_project_root() / "data" / "history"


def archive_market_data(settings: dict | None = None) -> Path | None:
    """現在のmarket_data.jsonをgzip圧縮してアーカイブ保存。

    Returns:
        保存先パス、または失敗時None。
    """
    if settings is None:
        settings = load_settings()

    data_dir = get_data_dir(settings)
    market_data_path = data_dir / "market_data.json"

    if not market_data_path.exists():
        logger.warning("No market_data.json to archive")
        return None

    try:
        data = read_json(market_data_path)
    except Exception as e:
        logger.error("Failed to read market_data.json: %s", e)
        return None

    now = datetime.now(timezone.utc)
    day_dir = HISTORY_DIR / now.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)

    filename = now.strftime("%H%M%S") + ".json.gz"
    archive_path = day_dir / filename

    try:
        json_bytes = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with gzip.open(archive_path, "wb") as f:
            f.write(json_bytes)
        logger.info("Archived market_data: %s (%d bytes)", archive_path, archive_path.stat().st_size)
        return archive_path
    except Exception as e:
        logger.error("Failed to archive market_data: %s", e)
        return None


def load_history(days: int = 7) -> list[dict]:
    """過去N日分のアーカイブを時系列順に読み込む。

    Args:
        days: 遡る日数

    Returns:
        market_data dictのリスト (古い順)
    """
    snapshots = []
    now = datetime.now(timezone.utc)

    for d in range(days, -1, -1):
        day = now - timedelta(days=d)
        day_dir = HISTORY_DIR / day.strftime("%Y-%m-%d")
        if not day_dir.exists():
            continue

        for gz_path in sorted(day_dir.glob("*.json.gz")):
            try:
                with gzip.open(gz_path, "rb") as f:
                    data = json.loads(f.read().decode("utf-8"))
                snapshots.append(data)
            except Exception as e:
                logger.warning("Failed to load %s: %s", gz_path, e)

    logger.info("Loaded %d historical snapshots from %d days", len(snapshots), days)
    return snapshots


def rotate_old(settings: dict | None = None) -> int:
    """古いアーカイブを削除。

    Returns:
        削除した日数。
    """
    if settings is None:
        settings = load_settings()

    max_days = settings.get("hypothesis", {}).get("archive_days", 7)
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_days)
    removed = 0

    if not HISTORY_DIR.exists():
        return 0

    for day_dir in sorted(HISTORY_DIR.iterdir()):
        if not day_dir.is_dir():
            continue
        try:
            dir_date = datetime.strptime(day_dir.name, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if dir_date < cutoff:
                shutil.rmtree(day_dir)
                logger.info("Rotated old archive: %s", day_dir.name)
                removed += 1
        except ValueError:
            continue

    return removed


if __name__ == "__main__":
    # テスト実行
    path = archive_market_data()
    if path:
        print(f"Archived: {path}")
    history = load_history(days=1)
    print(f"History snapshots: {len(history)}")
    removed = rotate_old()
    print(f"Rotated: {removed} old dirs")
