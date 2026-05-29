"""
Webhook event router — filters and normalizes GitHub events.

Two routing paths share one entry point:

  CI (failure):
    Event:       workflow_run
    Action:      completed
    Conclusion:  failure
    Branch:      NOT under agent/
    Actor:       NOT dependabot / bot / fork
    Effect:      → WorkflowFailureEvent → task_queue (CI pipeline)

  CD (release success):
    Event:       workflow_run
    Action:      completed
    Conclusion:  success
    Workflow:    name == settings.release_workflow_name (default "release")
    Branch:      the repo's release branch (typically `main`)
    Gate:        settings.cd_enabled (codespace + backend + image repo all set)
    Effect:      → ReleaseSuccessEvent → task_queue (CD pipeline)

Anything that matches neither path is acknowledged (200) and discarded.
Conversion to a dataclass + enqueue is the only side effect.
"""

from __future__ import annotations

import logging

from config.constants import AGENT_BRANCH_PREFIX, IGNORED_ACTOR_PATTERNS
from config.settings import get_settings
from models.cd import ReleaseSuccessEvent
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

    # ── CD path: successful run of the release workflow ──────────────
    if payload.conclusion == "success":
        return await _maybe_route_release(payload)

    if payload.conclusion != "failure":
        return False, f"Ignored conclusion: {payload.conclusion}"

    # ── CI path: failure of any non-agent workflow ───────────────────
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


async def _maybe_route_release(payload: WebhookPayload) -> tuple[bool, str]:
    """Decide whether a SUCCESS workflow_run is a release we should deploy.

    Releases are gated three ways:
      1. The workflow name must match settings.release_workflow_name.
         (CI workflows pass too — we don't want to deploy every green CI run.)
      2. settings.cd_enabled must be True (= codespace + backend + image
         repo configured). Avoids attempting deploys against a half-set env.
      3. The branch must not be an agent branch — defensive, agent branches
         shouldn't run release.yml in the first place but a misconfig
         shouldn't silently fire a deploy.
    """
    settings = get_settings()

    if payload.run_name != settings.release_workflow_name:
        return False, f"Ignored success workflow: {payload.run_name}"

    if payload.branch.startswith(AGENT_BRANCH_PREFIX):
        return False, f"Ignored release on agent branch: {payload.branch}"

    if not settings.cd_enabled:
        logger.info(
            "router: release event for run %d dropped — CD not configured "
            "(set CODESPACE_NAME, BACKEND_BASE_URL, DEPLOY_IMAGE_REPOSITORY to enable)",
            payload.run_id,
        )
        return False, "CD not configured"

    event = ReleaseSuccessEvent.from_payload(payload)
    accepted = await enqueue_event(event)
    if not accepted:
        return False, "Queue full — release event rejected"
    logger.info(
        "router: release accepted — run=%d sha=%s branch=%s",
        event.run_id,
        event.short_sha,
        event.branch,
    )
    return True, f"Accepted release run {payload.run_id}"
