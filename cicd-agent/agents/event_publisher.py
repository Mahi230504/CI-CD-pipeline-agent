"""
Event publisher — pushes the agent's reasoning steps to the demo backend
so the dashboard can render them live over SSE.

Wire shape (matches app/api/agent_events.py on the demo side):
  POST {BACKEND_BASE_URL}/internal/agent-event
  Header: X-Agent-Token: {AGENT_SHARED_SECRET}
  Body:   {"stage": str, "level": "info"|"warn"|"error"|"success",
           "message": str, "metadata": {...}, "timestamp": iso8601?}

Design choices:
- Fire-and-forget: this module NEVER raises. A backend that's down should not
  cascade into a failed deploy. Failures are logged at WARN and dropped.
- Bounded latency: one HTTP attempt with a tight timeout (no retries). If
  the operator notices missing events, the audit log is the source of truth.
- No-op when unconfigured: empty BACKEND_BASE_URL or empty AGENT_SHARED_SECRET
  silently skips the POST. This lets the CD pipeline run in dev / tests
  without needing a live backend.

Used by every CD module (deploy_guard, deployer, health_monitor, rollback)
to surface progress to the UI.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Literal

import aiohttp

from config.settings import get_settings

logger = logging.getLogger("cicd_agent.event_publisher")

EventLevel = Literal["info", "warn", "error", "success"]

# A single short timeout — events are nice-to-have, never a blocking
# dependency. If the backend is slow, drop the event rather than wait.
_HTTP_TIMEOUT_SECONDS = 3.0

# Hard cap on the per-event message size. The backend's schema allows 2000
# characters; we clip a little under that to stay safely under any
# transport-level limits the client might impose.
_MAX_MESSAGE_CHARS = 1800


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(s: str, limit: int = _MAX_MESSAGE_CHARS) -> str:
    if len(s) <= limit:
        return s
    # Leave a marker so logs make sense when truncated text gets surfaced.
    return s[: limit - 14] + "...[truncated]"


async def publish(
    stage: str,
    message: str,
    *,
    level: EventLevel = "info",
    metadata: dict[str, Any] | None = None,
    timestamp: str | None = None,
) -> bool:
    """Send one reasoning event to the demo backend.

    Returns True if the backend accepted the event (HTTP 2xx), False on any
    failure including misconfiguration. Never raises.

    `stage` is a coarse label the frontend uses for grouping/colour — keep
    it stable across a single pipeline phase (e.g. `"deploy_guard"`,
    `"deploy"`, `"health_check"`). `metadata` is arbitrary JSON; keep it
    small (PR numbers, image tags, latency numbers — not full diffs).
    """
    settings = get_settings()
    url = settings.agent_events_url
    token = settings.agent_shared_secret

    if not url or not token:
        # No-op rather than warn — this is the expected state in tests and
        # in a CI-only deployment. The CD code path itself is guarded by
        # cd_enabled, so reaching here without a URL only happens for
        # legitimate non-CD callers.
        return False

    body = {
        "stage": stage,
        "level": level,
        "message": _clip(message),
        "metadata": metadata or {},
        "timestamp": timestamp or _now_iso(),
    }
    headers = {"X-Agent-Token": token, "Content-Type": "application/json"}

    try:
        timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=body) as resp:
                if resp.status // 100 == 2:
                    return True
                # Read a small slice of the error body for the log so
                # auth/config bugs are diagnosable without a full trace.
                try:
                    detail = (await resp.text())[:200]
                except Exception:
                    detail = "<unreadable>"
                logger.warning(
                    "event_publisher: stage=%s status=%d detail=%s",
                    stage,
                    resp.status,
                    detail,
                )
                return False
    except asyncio.TimeoutError:
        logger.warning("event_publisher: stage=%s timed out after %.1fs", stage, _HTTP_TIMEOUT_SECONDS)
        return False
    except aiohttp.ClientError as e:
        logger.warning("event_publisher: stage=%s client error: %s", stage, e)
        return False
    except Exception as e:
        # Defensive: an unexpected exception here must not poison the
        # caller. Log loudly and move on.
        logger.warning("event_publisher: stage=%s unexpected error: %s", stage, e)
        return False


async def publish_safe(
    stage: str,
    message: str,
    *,
    level: EventLevel = "info",
    metadata: dict[str, Any] | None = None,
) -> None:
    """Same as `publish` but discards the return value.

    Convenience for the orchestrator, where most call sites don't branch on
    delivery success. Keeps the call sites short.
    """
    await publish(stage, message, level=level, metadata=metadata)
