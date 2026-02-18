"""Minimal autonomous agent gateway.

Thin event loop around claude -p. Receives events from Telegram, cron,
and webhooks, routes them to Claude, executes the response.

Usage:
    python3 -m src.gateway.server
"""

import asyncio
import json
import os
import signal
import sys
from datetime import datetime, timezone

from src.gateway.claude_cli import ClaudeCLI
from src.utils.config_loader import get_project_root, load_yaml
from src.utils.logger import setup_logger

logger = setup_logger("gateway")


def load_gateway_config() -> dict:
    root = get_project_root()
    return load_yaml(root / "config" / "gateway.yaml")


# ──────────────────────────── Telegram ────────────────────────────

async def start_telegram(claude: ClaudeCLI, config: dict):
    """Run Telegram bot. Bridges messages to/from claude -p."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", config.get("telegram", {}).get("bot_token", ""))
    if not token:
        logger.info("Telegram not configured, skipping")
        return

    try:
        from telegram import Update
        from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
    except ImportError:
        logger.warning("python-telegram-bot not installed. pip install 'python-telegram-bot>=21'")
        return

    allowed = set(config.get("telegram", {}).get("allowed_users", []))

    async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if allowed and user_id not in allowed:
            logger.warning("Unauthorized user: %d", user_id)
            return

        text = update.message.text
        logger.info("Telegram [%d]: %s", user_id, text[:100])

        try:
            response = await claude.ask(text)
            # Split long messages (Telegram limit 4096)
            for i in range(0, len(response), 4000):
                await update.message.reply_text(response[i:i+4000])
        except Exception as e:
            logger.exception("Error handling Telegram message")
            await update.message.reply_text(f"Error: {e}")

    async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if allowed and user_id not in allowed:
            return
        root = get_project_root()
        status_parts = [f"myClaw Gateway - {datetime.now(timezone.utc).strftime('%H:%M UTC')}"]

        # Kill switch
        ks_path = root / "state" / "kill_switch.json"
        if ks_path.exists():
            with open(ks_path) as f:
                ks = json.load(f)
            status_parts.append(f"Kill Switch: {'ON' if ks.get('enabled') else 'OFF'}")

        # Positions
        pos_path = root / "state" / "positions.json"
        if pos_path.exists():
            with open(pos_path) as f:
                positions = json.load(f)
            status_parts.append(f"Positions: {len(positions) if isinstance(positions, list) else 0}")

        # Daily PnL
        pnl_path = root / "state" / "daily_pnl.json"
        if pnl_path.exists():
            with open(pnl_path) as f:
                pnl = json.load(f)
            realized = float(pnl.get("realized_pnl", 0))
            unrealized = float(pnl.get("unrealized_pnl", 0))
            status_parts.append(f"PnL: {realized+unrealized:+.2f} (R:{realized:+.2f} U:{unrealized:+.2f})")

        await update.message.reply_text("\n".join(status_parts))

    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Telegram bot starting")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    # Keep running until cancelled
    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


# ──────────────────────────── Scheduler ────────────────────────────

async def start_scheduler(claude: ClaudeCLI, config: dict):
    """Simple cron-like scheduler using asyncio."""
    jobs = config.get("scheduler", {}).get("jobs", [])
    if not jobs:
        logger.info("No scheduled jobs configured")
        return

    async def run_job(job: dict):
        name = job["name"]
        interval = job.get("interval_minutes", 5)
        prompt = job["prompt"]
        logger.info("Scheduler: job '%s' every %dm", name, interval)

        while True:
            await asyncio.sleep(interval * 60)
            logger.info("Scheduler: running '%s'", name)
            try:
                # If job has a "script" field, run it instead of claude
                script = job.get("script")
                if script:
                    proc = await asyncio.create_subprocess_shell(
                        script,
                        cwd=str(get_project_root()),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout, stderr = await proc.communicate()
                    logger.info("Script '%s' done (rc=%d)", name, proc.returncode)
                else:
                    response = await claude.ask(prompt)
                    logger.info("Job '%s' response: %s", name, response[:200])
            except Exception:
                logger.exception("Scheduler job '%s' failed", name)

    tasks = [asyncio.create_task(run_job(job)) for job in jobs]
    await asyncio.gather(*tasks)


# ──────────────────────────── Webhook ────────────────────────────

async def start_webhook(claude: ClaudeCLI, config: dict):
    """Simple HTTP webhook server."""
    wh_config = config.get("webhook", {})
    if not wh_config.get("enabled", False):
        logger.info("Webhook server disabled")
        return

    try:
        from aiohttp import web
    except ImportError:
        logger.warning("aiohttp not installed. pip install aiohttp")
        return

    port = wh_config.get("port", 8080)
    secret = wh_config.get("secret", "")

    async def handle_post(request: web.Request) -> web.Response:
        # Simple auth via shared secret
        if secret and request.headers.get("X-Webhook-Secret") != secret:
            return web.Response(status=403, text="Forbidden")

        body = await request.json()
        prompt = body.get("prompt", "")
        if not prompt:
            return web.Response(status=400, text="Missing 'prompt' field")

        logger.info("Webhook received: %s", prompt[:100])
        try:
            response = await claude.ask(prompt)
            return web.json_response({"response": response})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    app = web.Application()
    app.router.add_post("/ask", handle_post)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    logger.info("Webhook server listening on 127.0.0.1:%d", port)

    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


# ──────────────────────────── Main ────────────────────────────

async def main():
    config = load_gateway_config()
    model = config.get("claude", {}).get("model", "sonnet")
    claude = ClaudeCLI(model=model)

    logger.info("myClaw Gateway starting (model=%s)", model)

    # Graceful shutdown
    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def shutdown_handler():
        logger.info("Shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown_handler)

    # Launch all services concurrently
    tasks = [
        asyncio.create_task(start_telegram(claude, config)),
        asyncio.create_task(start_scheduler(claude, config)),
        asyncio.create_task(start_webhook(claude, config)),
    ]

    # Wait for shutdown signal
    await stop_event.wait()
    logger.info("Shutting down...")

    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("Gateway stopped")


if __name__ == "__main__":
    asyncio.run(main())
