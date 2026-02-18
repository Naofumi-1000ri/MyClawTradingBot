"""Logging setup for myClaw."""

import logging
import sys
from pathlib import Path

from src.utils.config_loader import get_logs_dir


def setup_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """Set up a logger with file and console handlers.

    Args:
        name: Logger name (used as log filename).
        level: Logging level.

    Returns:
        Configured logger instance.
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(level)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    logger.addHandler(console)

    # File handler
    logs_dir = get_logs_dir()
    file_handler = logging.FileHandler(logs_dir / f"{name}.log")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger
