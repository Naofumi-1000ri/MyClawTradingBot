"""GPG-encrypted secrets management."""

import os
import subprocess
from pathlib import Path

from src.utils.config_loader import get_project_root


def decrypt_secrets(passphrase: str | None = None) -> dict[str, str]:
    """Decrypt secrets.env.gpg and return as key-value dict.

    Args:
        passphrase: GPG passphrase. If None, reads from
                    MYCLAW_GPG_PASSPHRASE environment variable.

    Returns:
        Dictionary of secret key-value pairs.

    Raises:
        RuntimeError: If decryption fails.
        FileNotFoundError: If encrypted secrets file not found.
    """
    if passphrase is None:
        passphrase = os.environ.get("MYCLAW_GPG_PASSPHRASE")
    if not passphrase:
        raise RuntimeError("No GPG passphrase provided. Set MYCLAW_GPG_PASSPHRASE.")

    gpg_file = get_project_root() / "config" / "secrets.env.gpg"
    if not gpg_file.exists():
        raise FileNotFoundError(f"Encrypted secrets not found: {gpg_file}")

    result = subprocess.run(
        [
            "gpg", "--quiet", "--batch", "--yes",
            "--passphrase-fd", "0",
            "--decrypt", str(gpg_file),
        ],
        input=passphrase.encode(),
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"GPG decryption failed: {result.stderr.decode()}")

    secrets = {}
    for line in result.stdout.decode().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            secrets[key.strip()] = value.strip()
    return secrets


def get_hyperliquid_key(passphrase: str | None = None) -> str:
    """Get the Hyperliquid private key from encrypted secrets.

    Args:
        passphrase: GPG passphrase (or from env var).

    Returns:
        Hyperliquid private key string.
    """
    secrets = decrypt_secrets(passphrase)
    key = secrets.get("HYPERLIQUID_PRIVATE_KEY")
    if not key:
        raise RuntimeError("HYPERLIQUID_PRIVATE_KEY not found in secrets")
    return key
