"""
Webhook event router — filters and normalizes GitHub events.

Only passes through:
- Event type: workflow_run
- Action: completed
- Conclusion: failure
- Actor: NOT dependabot, NOT [bot] suffix, NOT fork PRs

Everything else is acknowledged (200) and discarded silently.

Converts the raw GitHub payload into a WorkflowFailureEvent dataclass
and enqueues it via task_queue. Returns 200 immediately — processing is async.
"""

from __future__ import annotations

import logging

from config.constants import AGENT_BRANCH_PREFIX, IGNORED_ACTOR_PATTERNS
from models.events import WebhookPayload, WorkflowFailureEvent
from orchestrator.task_queue import enqueue_event

logger = logging.getLogger("cicd_agent.router")


async def route_webhook(body: bytes, github_event: str) -> tuple[bool, str]:
    if github_event != "workflow_run":
        return False, f"Ignored event type: {github_event}"

    try:
        payload = WebhookPayload.from_github_payload(body)
    except ValueError as e:
        logger.warning("router: malformed payload: %s", e)
        return False, f"Malformed payload: {e}"

    if payload.action != "completed":
        return False, f"Ignored action: {payload.action}"

    if payload.conclusion != "failure":
        return False, f"Ignored conclusion: {payload.conclusion}"

    if payload.branch.startswith(AGENT_BRANCH_PREFIX):
        return False, f"Ignored agent branch: {payload.branch}"

    ignored_lower = {a.lower() for a in IGNORED_ACTOR_PATTERNS}
    if payload.sender_login.lower() in ignored_lower:
        return False, f"Ignored actor: {payload.sender_login}"

    if payload.sender_type == "Bot" and payload.sender_login not in IGNORED_ACTOR_PATTERNS:
        return False, f"Ignored bot: {payload.sender_login}"

    event = WorkflowFailureEvent.from_payload(payload)
    accepted = await enqueue_event(event)
    if not accepted:
        return False, "Queue full — event rejected"
    return True, f"Accepted run {payload.run_id}"
