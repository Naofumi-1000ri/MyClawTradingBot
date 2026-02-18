"""Thin async wrapper around claude -p CLI."""

import asyncio
import json
import time
from pathlib import Path

from src.utils.config_loader import get_project_root, get_state_dir
from src.utils.logger import setup_logger

logger = setup_logger("claude_cli")

SESSION_RESET_SECONDS = 86400  # 24h


class ClaudeCLI:
    """Call claude -p as async subprocess. Session continuity via --resume."""

    def __init__(self, model: str = "sonnet"):
        self.model = model
        self.project_root = get_project_root()
        self.session_id: str | None = None
        self.session_ts: float = 0
        self._load_session()

    def _session_file(self) -> Path:
        return get_state_dir() / "gateway_session.json"

    def _load_session(self):
        try:
            with open(self._session_file()) as f:
                data = json.load(f)
            age = time.time() - data.get("timestamp", 0)
            if age < SESSION_RESET_SECONDS:
                self.session_id = data["session_id"]
                self.session_ts = data["timestamp"]
                logger.info("Resumed session %s (age: %ds)", self.session_id, int(age))
        except (FileNotFoundError, KeyError, json.JSONDecodeError):
            pass

    def _save_session(self, session_id: str):
        self.session_id = session_id
        self.session_ts = time.time()
        path = self._session_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump({"session_id": session_id, "timestamp": self.session_ts}, f)

    async def ask(
        self,
        prompt: str,
        system_prompt: str | None = None,
        output_json: bool = False,
    ) -> str:
        """Send a prompt to claude -p and return the response.

        Args:
            prompt: The user prompt.
            system_prompt: Optional system prompt to append.
            output_json: If True, request JSON output format.

        Returns:
            Claude's response as string (or JSON string).
        """
        cmd = ["claude", "-p", "--model", self.model]

        if output_json:
            cmd.extend(["--output-format", "json"])

        if self.session_id:
            cmd.extend(["--resume", self.session_id])

        if system_prompt:
            cmd.extend(["--append-system-prompt", system_prompt])

        cmd.append(prompt)

        logger.info("Calling claude -p (model=%s, session=%s)", self.model, self.session_id or "new")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.project_root),
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            err = stderr.decode().strip()
            logger.error("claude -p failed (rc=%d): %s", proc.returncode, err)
            raise RuntimeError(f"claude -p failed: {err}")

        response = stdout.decode().strip()

        # Try to extract session ID from stderr for --resume
        for line in stderr.decode().splitlines():
            if "session:" in line.lower() or "id:" in line.lower():
                parts = line.strip().split()
                if parts:
                    self._save_session(parts[-1])
                    break

        logger.info("claude -p responded (%d chars)", len(response))
        return response
