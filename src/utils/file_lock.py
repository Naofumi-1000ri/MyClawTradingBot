"""Atomic JSON file operations with file locking."""

import fcntl
import json
import tempfile
from pathlib import Path


def atomic_write_json(filepath: Path, data: dict) -> None:
    """Write JSON data atomically using temp file + rename.

    Uses fcntl.flock for advisory locking and writes to a temp file
    first, then renames to prevent partial reads.

    Args:
        filepath: Target JSON file path.
        data: Dictionary to serialize as JSON.
    """
    filepath = Path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)

    # Write to temp file in same directory, then atomic rename
    fd, tmp_path = tempfile.mkstemp(
        dir=filepath.parent, suffix=".tmp", prefix=".myclaw_"
    )
    try:
        with open(fd, "w") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            json.dump(data, f, indent=2, default=str)
            f.flush()
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        Path(tmp_path).rename(filepath)
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise


def read_json(filepath: Path) -> dict:
    """Read a JSON file with shared lock.

    Args:
        filepath: JSON file to read.

    Returns:
        Parsed dictionary.

    Raises:
        FileNotFoundError: If file doesn't exist.
    """
    filepath = Path(filepath)
    with open(filepath, "r") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_SH)
        data = json.load(f)
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return data
