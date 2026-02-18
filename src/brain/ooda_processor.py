"""Process OODA loop output: journal, strategy updates, git commits."""

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from src.utils.config_loader import get_project_root, get_state_dir
from src.utils.file_lock import atomic_write_json, read_json
from src.utils.logger import setup_logger

logger = setup_logger("ooda")


def process_ooda_output(output: dict) -> None:
    """Process the full OODA output from brain.

    Handles journal entries, strategy updates, and git commits.
    Trade signals are handled by trade_executor separately.
    """
    action_type = output.get("action_type", "hold")

    # Always save OODA thinking to log
    _save_ooda_log(output)

    # Check for environment issues and create requests
    _check_environment_needs()

    if action_type == "journal":
        _process_journal(output)
    elif action_type == "adjust_strategy":
        _process_strategy_update(output)
    elif action_type == "research":
        _process_research(output)

    # Git commit if there were meaningful changes
    if action_type in ("journal", "adjust_strategy"):
        _git_commit(output)


def _save_ooda_log(output: dict) -> None:
    """Append OODA thinking to state/ooda_log.json."""
    state_dir = get_state_dir()
    log_path = state_dir / "ooda_log.json"

    entries = []
    if log_path.exists():
        try:
            entries = read_json(log_path)
            if not isinstance(entries, list):
                entries = []
        except Exception:
            entries = []

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action_type": output.get("action_type"),
        "ooda": output.get("ooda"),
        "market_summary": output.get("market_summary"),
        "self_assessment": output.get("self_assessment"),
    }

    if output.get("journal_entry"):
        entry["journal_entry"] = output["journal_entry"]
    if output.get("strategy_update"):
        entry["strategy_update"] = output["strategy_update"]
    if output.get("research_topic"):
        entry["research_topic"] = output["research_topic"]

    entries.append(entry)
    # Keep last 500 entries
    if len(entries) > 500:
        entries = entries[-500:]

    atomic_write_json(log_path, entries)
    logger.info("OODA log saved (%s)", output.get("action_type"))


def _process_journal(output: dict) -> None:
    """Save journal entry to journal/ directory."""
    root = get_project_root()
    journal_dir = root / "journal"
    journal_dir.mkdir(exist_ok=True)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    journal_file = journal_dir / f"{today}.md"

    timestamp = datetime.now(timezone.utc).strftime("%H:%M UTC")
    entry = output.get("journal_entry", "")
    ooda = output.get("ooda", {})
    assessment = output.get("self_assessment", "")

    content = f"\n## {timestamp}\n\n"
    content += f"**Observe**: {ooda.get('observe', '')}\n\n"
    content += f"**Orient**: {ooda.get('orient', '')}\n\n"
    content += f"**Decide**: {ooda.get('decide', '')}\n\n"
    if entry:
        content += f"{entry}\n\n"
    if assessment:
        content += f"**Self-assessment**: {assessment}\n\n"
    content += "---\n"

    # Append to today's journal
    is_new = not journal_file.exists() or journal_file.stat().st_size == 0
    with open(journal_file, "a") as f:
        if is_new:
            f.write(f"# Trading Journal - {today}\n")
        f.write(content)

    logger.info("Journal entry written: %s", journal_file)


def _process_strategy_update(output: dict) -> None:
    """Save strategy update proposal to state/strategy_proposals.json."""
    state_dir = get_state_dir()
    proposals_path = state_dir / "strategy_proposals.json"

    proposals = []
    if proposals_path.exists():
        try:
            proposals = read_json(proposals_path)
            if not isinstance(proposals, list):
                proposals = []
        except Exception:
            proposals = []

    update = output.get("strategy_update", {})
    proposals.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "description": update.get("description", ""),
        "changes": update.get("changes", {}),
        "reasoning": output.get("ooda", {}).get("decide", ""),
        "applied": False,
    })

    atomic_write_json(proposals_path, proposals)
    logger.info("Strategy update proposed: %s", update.get("description", ""))


def _process_research(output: dict) -> None:
    """Log research topic to state/research_queue.json."""
    state_dir = get_state_dir()
    queue_path = state_dir / "research_queue.json"

    queue = []
    if queue_path.exists():
        try:
            queue = read_json(queue_path)
            if not isinstance(queue, list):
                queue = []
        except Exception:
            queue = []

    queue.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "topic": output.get("research_topic", ""),
        "context": output.get("ooda", {}).get("orient", ""),
        "done": False,
    })

    atomic_write_json(queue_path, queue)
    logger.info("Research topic queued: %s", output.get("research_topic", ""))


def _check_environment_needs() -> None:
    """Check if the agent needs something from the human and create requests."""
    import os
    root = get_project_root()
    state_dir = get_state_dir()
    requests_path = state_dir / "requests.json"

    requests = []

    # Check private key
    has_key = bool(os.environ.get("HYPERLIQUID_PRIVATE_KEY"))
    if not has_key:
        gpg_file = root / "config" / "secrets.env.gpg"
        if not gpg_file.exists():
            requests.append({
                "type": "need_api_key",
                "message": "HYPERLIQUID_PRIVATE_KEYが未設定です。トレード執行にはTestnet秘密鍵が必要です。",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

    # Check git remote
    try:
        result = subprocess.run(
            ["git", "remote", "-v"], cwd=str(root),
            capture_output=True, timeout=5,
        )
        if not result.stdout.strip():
            requests.append({
                "type": "need_setup",
                "message": "Gitリモートリポジトリが未設定です。成長記録をGitHubにpushするにはgit remote addが必要です。",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
    except Exception:
        pass

    # Check Telegram
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        requests.append({
            "type": "need_setup",
            "message": "TELEGRAM_BOT_TOKENが未設定です。人間との通信にはTelegram Botが必要です。",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    if requests:
        atomic_write_json(requests_path, requests)
        logger.warning("Agent has %d pending requests for human", len(requests))

        # Send via Telegram if available
        try:
            from src.monitor.telegram_notifier import send_message
            for req in requests:
                send_message(f"*myClaw Request*\n{req['message']}")
        except Exception:
            pass


def _git_commit(output: dict) -> None:
    """Commit changes to git with OODA context as commit message."""
    root = get_project_root()
    action_type = output.get("action_type", "")
    ooda = output.get("ooda", {})

    # Build commit message
    summary = ooda.get("decide", action_type)[:72]
    body = ""
    if output.get("journal_entry"):
        body += f"Journal: {output['journal_entry'][:200]}\n\n"
    if (output.get("strategy_update") or {}).get("description"):
        body += f"Strategy: {output['strategy_update']['description'][:200]}\n\n"
    if output.get("self_assessment"):
        body += f"Assessment: {output['self_assessment'][:200]}\n\n"

    msg = f"ooda({action_type}): {summary}"
    if body:
        msg += f"\n\n{body}"

    try:
        # Add relevant files
        subprocess.run(
            ["git", "add", "journal/", "state/ooda_log.json",
             "state/strategy_proposals.json", "state/research_queue.json"],
            cwd=str(root), capture_output=True, timeout=10,
        )
        # Commit (allow empty in case nothing changed)
        result = subprocess.run(
            ["git", "commit", "-m", msg, "--allow-empty"],
            cwd=str(root), capture_output=True, timeout=10,
        )
        if result.returncode == 0:
            logger.info("Git commit: %s", summary)
            # Push if remote exists
            subprocess.run(
                ["git", "push"],
                cwd=str(root), capture_output=True, timeout=30,
            )
        else:
            logger.debug("Git commit skipped: %s", result.stderr.decode()[:100])
    except Exception:
        logger.warning("Git commit failed")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        with open(sys.argv[1]) as f:
            data = json.load(f)
        process_ooda_output(data)
    else:
        # Read from signals/signals.json
        root = get_project_root()
        signals_path = root / "signals" / "signals.json"
        data = read_json(signals_path)
        process_ooda_output(data)
