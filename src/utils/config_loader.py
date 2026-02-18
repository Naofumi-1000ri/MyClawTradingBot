"""Configuration loader for myClaw."""

import os
from pathlib import Path

import yaml


def get_project_root() -> Path:
    """Return the project root directory."""
    return Path(__file__).resolve().parent.parent.parent


def load_yaml(filepath: Path) -> dict:
    """Load a YAML file and return as dict."""
    with open(filepath, "r") as f:
        return yaml.safe_load(f) or {}


def load_settings() -> dict:
    """Load global settings from config/settings.yaml."""
    root = get_project_root()
    return load_yaml(root / "config" / "settings.yaml")


def load_risk_params() -> dict:
    """Load risk parameters from config/risk_params.yaml."""
    root = get_project_root()
    return load_yaml(root / "config" / "risk_params.yaml")


def get_hyperliquid_url(settings: dict | None = None) -> str:
    """Return the appropriate Hyperliquid API URL based on environment."""
    if settings is None:
        settings = load_settings()
    env = settings.get("environment", "testnet")
    urls = settings.get("hyperliquid", {})
    if env == "mainnet":
        return urls.get("mainnet_url", "https://api.hyperliquid.xyz")
    return urls.get("testnet_url", "https://api.hyperliquid-testnet.xyz")


def resolve_path(relative_path: str, settings: dict | None = None) -> Path:
    """Resolve a relative path from settings to an absolute path."""
    root = get_project_root()
    return root / relative_path


def get_data_dir(settings: dict | None = None) -> Path:
    """Return the data directory path, creating it if needed."""
    if settings is None:
        settings = load_settings()
    path = resolve_path(settings.get("paths", {}).get("data_dir", "data"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_signals_dir(settings: dict | None = None) -> Path:
    """Return the signals directory path, creating it if needed."""
    if settings is None:
        settings = load_settings()
    path = resolve_path(settings.get("paths", {}).get("signals_dir", "signals"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_state_dir(settings: dict | None = None) -> Path:
    """Return the state directory path, creating it if needed."""
    if settings is None:
        settings = load_settings()
    path = resolve_path(settings.get("paths", {}).get("state_dir", "state"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_logs_dir(settings: dict | None = None) -> Path:
    """Return the logs directory path, creating it if needed."""
    if settings is None:
        settings = load_settings()
    path = resolve_path(settings.get("paths", {}).get("logs_dir", "logs"))
    path.mkdir(parents=True, exist_ok=True)
    return path
