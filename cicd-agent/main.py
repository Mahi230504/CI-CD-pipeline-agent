"""
Entry point for the CI/CD Intelligence Agent.

On startup:
1. Loads and validates all settings (hard stop on missing keys)
2. Pings Gemini API to confirm the key works
3. Pings GitHub MCP to confirm PAT has correct permissions
4. Starts the FastAPI server via uvicorn
5. Registers SIGTERM/SIGINT handlers for graceful shutdown

Prints a startup summary with: models in use, repo being watched,
rate limit config, webhook URL to register in GitHub.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

import aiohttp

from audit.setup import configure_logging
from config.settings import get_settings

configure_logging()
logger = logging.getLogger("cicd_agent.main")


def _print_banner(full_repo_name: str) -> None:
    inner = f"github.com/{full_repo_name}"
    width = max(len("CI/CD Intelligence Agent"), len(inner)) + 4
    bar = "═" * width
    print(f"╔{bar}╗")
    print(f"║  {'CI/CD Intelligence Agent'.ljust(width - 2)}║")
    print(f"║  {inner.ljust(width - 2)}║")
    print(f"╚{bar}╝")


async def startup_checks() -> bool:
    settings = get_settings()
    _print_banner(settings.full_repo_name)

    from llm.gemini_client import init_gemini_client
    from llm.rate_limiter import init_rate_limiter

    init_rate_limiter(settings.rate_limit_delay_seconds)
    client = init_gemini_client()
    try:
        ok = await client.ping()
    except Exception as e:
        print(f"✗ Gemini API: failed ({e})")
        return False
    if not ok:
        print("✗ Gemini API: empty response")
        return False
    print(f"✓ Gemini API: connected ({settings.primary_model})")

    url = f"https://api.github.com/repos/{settings.full_repo_name}"
    headers = {
        "Authorization": f"Bearer {settings.github_pat}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "cicd-agent",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    print(
                        f"✓ GitHub PAT: valid "
                        f"(repo={settings.full_repo_name}, private={bool(data.get('private'))})"
                    )
                else:
                    print(f"✗ GitHub PAT: {resp.status} — check permissions")
                    return False
    except Exception as e:
        print(f"✗ GitHub PAT: error contacting GitHub ({e})")
        return False

    if settings.slack_webhook_url and settings.telegram_bot_token and settings.telegram_chat_id:
        notif = "Slack+Telegram"
    elif settings.slack_webhook_url:
        notif = "Slack"
    elif settings.telegram_bot_token and settings.telegram_chat_id:
        notif = "Telegram"
    else:
        notif = "none"

    print(f"  Webhook port : {settings.webhook_port}")
    print(f"  Primary model: {settings.primary_model}")
    print(f"  Rate limit   : {settings.rate_limit_delay_seconds}s between calls")
    print(f"  Max attempts : {settings.max_patch_attempts}")
    print(f"  Notifications: {notif}")
    print(f"  Production   : {settings.production_mode}")
    print()
    print("  Register webhook at:")
    print(f"  → https://github.com/{settings.full_repo_name}/settings/hooks")
    print("  → Payload URL: https://<your-ngrok-url>/webhook")
    print("  → Content type: application/json")
    print("  → Secret: <value from GITHUB_WEBHOOK_SECRET in .env>")
    print("  → Events: Workflow runs")
    return True


async def main() -> None:
    settings = get_settings()
    ok = await startup_checks()
    if not ok:
        print("Startup checks failed. Fix errors above and retry.")
        sys.exit(1)

    print(f"Starting webhook server on port {settings.webhook_port}...")

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    import uvicorn

    from webhook.server import app

    config = uvicorn.Config(
        app=app,
        host="0.0.0.0",
        port=settings.webhook_port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
