"""
Notifier agent — sends the full pipeline report to Slack and/or Telegram.

Sends a single formatted message containing:
- Whether the failure was flaky or real
- Diagnosis summary (error type, file, line)
- Patch PR link (if created)
- Optimization PR link + estimated time saved (if applicable)
- Total time taken by the agent pipeline

Supports both Slack (via incoming webhook + Block Kit) and Telegram (bot API).
If both are configured, sends to both. If neither is configured, logs only.

Uses LIGHT_MODEL (gemini-2.5-flash-lite) for message formatting.
Runs async — never blocks the pipeline. Retries once on failure.
"""

from __future__ import annotations

import asyncio
import json
import logging

import aiohttp

from config.prompts import NOTIFIER_SYSTEM_PROMPT
from config.settings import Settings, get_settings
from llm.gemini_client import get_gemini_client
from llm.rate_limiter import (
    DailyLimitReachedError,
    GeminiError,
    GeminiRateLimitError,
)
from models.task import NotificationPayload

logger = logging.getLogger(__name__)


_HTTP_TIMEOUT_SECONDS = 10.0
_RETRY_DELAY_SECONDS = 0.5


async def send(payload: NotificationPayload) -> bool:
    logger.info(
        "notifier: run=%d summary=%s",
        payload.run_id,
        payload.summary_line,
    )
    settings = get_settings()

    context = {
        "run_id": payload.run_id,
        "repo": payload.repo_full_name,
        "branch": payload.branch,
        "summary": payload.summary_line,
        "is_flaky": payload.is_flaky,
        "flakiness_reason": payload.flakiness_reason,
        "error_type": str(payload.diagnosis.error_type) if payload.diagnosis else None,
        "explanation": payload.diagnosis.explanation if payload.diagnosis else None,
        "patch_success": payload.patch_result.success if payload.patch_result else None,
        "patch_pr_url": payload.patch_result.pr_url if payload.patch_result else None,
        "optimization_pr_url": payload.optimization_result.pr_url if payload.optimization_result else None,
        "time_saved": payload.optimization_result.savings_display if payload.optimization_result else None,
        "pipeline_duration_seconds": round(payload.pipeline_duration_seconds, 1),
        "escalated": payload.escalated,
        "escalation_reason": payload.escalation_reason,
    }

    message = payload.summary_line
    try:
        message = await get_gemini_client().generate(
            prompt=json.dumps(context),
            system_prompt=NOTIFIER_SYSTEM_PROMPT,
            agent="notifier",
            use_light_model=True,
            temperature=0.3,
        )
    except (GeminiError, GeminiRateLimitError, DailyLimitReachedError) as e:
        logger.warning("notifier: gemini formatting failed, using summary_line: %s", e)
    except Exception as e:
        logger.warning("notifier: unexpected gemini error, using summary_line: %s", e)

    coros = []
    if settings.slack_webhook_url:
        coros.append(_send_slack(message, settings))
    if settings.telegram_bot_token and settings.telegram_chat_id:
        coros.append(_send_telegram(message, settings))

    if not coros:
        logger.warning("notifier: no channels configured, skipping send")
        return False

    results = await asyncio.gather(*coros, return_exceptions=True)
    sent_count = sum(1 for r in results if r is True)
    logger.info("notifier: delivered to %d/%d channels", sent_count, len(results))
    return sent_count > 0


async def _send_slack(message: str, settings: Settings) -> bool:
    body = {"text": message}
    for attempt in (1, 2):
        try:
            timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(settings.slack_webhook_url, json=body) as resp:
                    if resp.status == 200:
                        logger.info("notifier/slack: ok (attempt %d)", attempt)
                        return True
                    logger.warning(
                        "notifier/slack: status=%d attempt=%d",
                        resp.status,
                        attempt,
                    )
        except Exception as e:
            logger.warning("notifier/slack: error attempt %d: %s", attempt, e)
        if attempt == 1:
            await asyncio.sleep(_RETRY_DELAY_SECONDS)
    return False


async def _send_telegram(message: str, settings: Settings) -> bool:
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    body = {"chat_id": settings.telegram_chat_id, "text": message}
    for attempt in (1, 2):
        try:
            timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=body) as resp:
                    data = await resp.json(content_type=None)
                    if isinstance(data, dict) and data.get("ok") is True:
                        logger.info("notifier/telegram: ok (attempt %d)", attempt)
                        return True
                    description = (
                        data.get("description") if isinstance(data, dict) else "no description"
                    )
                    logger.warning(
                        "notifier/telegram: response=%s attempt=%d",
                        description,
                        attempt,
                    )
        except Exception as e:
            logger.warning("notifier/telegram: error attempt %d: %s", attempt, e)
        if attempt == 1:
            await asyncio.sleep(_RETRY_DELAY_SECONDS)
    return False
