"""
CD pipeline — orchestrates the five Phase 3 modules into a single run.

Triggered by a successful `release.yml` workflow_run. The sequence:

  1. Publish "release detected" event for the dashboard.
  2. Resolve the merged PR for `head_sha` and pull its diff + files.
  3. deploy_guard.judge → DeployVerdict (approve/block + risk).
  4. If blocked → publish, notify, exit.
  5. deployer.deploy → DeployResult (captures prev_tag).
  6. If deploy fails → publish, notify, exit (nothing to roll back to).
  7. health_monitor.check → HealthReport.
  8. If unhealthy AND auto_rollback_enabled AND prev_tag → rollback +
     re-check health.
  9. Publish final outcome, notify.

Wrapped in asyncio.timeout(session_timeout_seconds) so a wedged deploy
can't pin the worker indefinitely. Every step is audited via the same
audit_step context manager used by the CI pipeline; outcomes are surfaced
both to the dashboard (via event_publisher) and to Slack/Telegram (via
notifier).

The pipeline NEVER raises out of run_cd_pipeline — the worker treats an
exception as a dead-letter, which we don't want for normal deploy
failures. All errors are converted into a state + a final notification.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from agents import deploy_guard, deployer, event_publisher, health_monitor, rollback
from audit.context import clear_run_context, set_run_context
from audit.logger import audit_step, get_audit_logger
from config.constants import PipelineStep
from config.settings import Settings, get_settings
from github import rest_api
from metrics import active_runs, record_pipeline
from models.cd import DeployResult, DeployVerdict, HealthReport, ReleaseSuccessEvent

logger = logging.getLogger("cicd_agent.cd_pipeline")


# ── State container ──────────────────────────────────────────────────────


@dataclass
class CDTaskState:
    """Mutable state passed between CD pipeline phases.

    Lighter than the AgentTask used by the CI pipeline because the CD flow
    has fewer branches — every successful release goes through the same
    five gates, so a flat dataclass is enough for audit + notify.
    """

    event: ReleaseSuccessEvent
    pr_number: int | None = None
    pr_title: str = ""
    pr_body: str = ""
    files_changed: list[str] = field(default_factory=list)
    diff_text: str = ""
    verdict: DeployVerdict | None = None
    deploy_result: DeployResult | None = None
    health_report: HealthReport | None = None
    rollback_result: DeployResult | None = None
    rollback_health: HealthReport | None = None
    outcome: str = "unknown"
    error_message: str | None = None
    started_at: float = field(default_factory=time.monotonic)

    @property
    def duration_seconds(self) -> float:
        return time.monotonic() - self.started_at


# ── Helpers ──────────────────────────────────────────────────────────────


async def _publish(state: CDTaskState, stage: str, message: str, *, level: str = "info") -> None:
    """Thin wrapper that adds the run_id + sha to every event's metadata."""
    await event_publisher.publish_safe(
        stage,
        message,
        level=level,  # type: ignore[arg-type]
        metadata={
            "run_id": state.event.run_id,
            "sha": state.event.short_sha,
            "pr": state.pr_number,
        },
    )


async def _resolve_pr(state: CDTaskState) -> None:
    """Populate pr_number, pr_title, pr_body, files_changed, diff_text.

    Tolerates a missing PR (push-to-main without an associated PR): the
    deploy guard still runs against a minimal payload that just describes
    the commit. The synthetic payload includes the head SHA so the LLM
    still has something to anchor on.
    """
    pulls = await rest_api.get_pulls_for_commit(state.event.head_sha)
    merged = [p for p in pulls if isinstance(p.get("merged_at"), str)]
    chosen = merged[0] if merged else (pulls[0] if pulls else None)
    if chosen is None:
        logger.info(
            "cd_pipeline: no PR found for sha %s — guard runs on commit metadata only",
            state.event.short_sha,
        )
        return

    state.pr_number = int(chosen.get("number")) if chosen.get("number") else None
    state.pr_title = str(chosen.get("title") or "").strip()
    state.pr_body = str(chosen.get("body") or "").strip()

    if state.pr_number:
        diff = await rest_api.get_pr_diff(state.pr_number)
        files = await rest_api.get_pr_files(state.pr_number)
        state.diff_text = diff or ""
        state.files_changed = files


def _synthesise_diff_for_commit_only(state: CDTaskState) -> str:
    """Fallback diff text when no PR is found.

    The deploy guard refuses to judge on an empty diff. For push-to-main
    sequences without a PR we still want the guard to run — even if it just
    says "no diff available, treat as medium risk". A short marker text
    keeps the guard's contract happy and the LLM has the SHA to query
    history if it wants.
    """
    return (
        f"--- (no diff available)\n"
        f"+++ (no diff available)\n"
        f"@@ commit {state.event.head_sha} @@\n"
        f"# release.yml workflow run {state.event.run_id} succeeded on branch "
        f"{state.event.branch} but the merged PR could not be located. "
        f"No file-level changes are available for review.\n"
    )


def _format_notification(state: CDTaskState, settings: Settings) -> str:
    """Build the plain-text notification body. No LLM call — the CD flow is
    structured enough that a hand-written summary reads naturally. Keeps
    cost low and behaviour predictable when the LLM provider is degraded."""
    e = state.event
    lines = [
        f"[CD] {e.full_repo} @ {e.short_sha} → {state.outcome}",
        f"workflow_run: {e.html_url}",
    ]
    if state.pr_number:
        lines.append(f"PR: #{state.pr_number} — {state.pr_title}")
    if state.verdict is not None:
        v = state.verdict
        lines.append(
            f"deploy_guard: {'approve' if v.approve else 'block'} "
            f"({v.risk}, conf {v.confidence:.2f}) — {v.reason}"
        )
    if state.deploy_result is not None:
        d = state.deploy_result
        lines.append(
            f"deploy: {'ok' if d.success else 'failed'} → {d.image_tag}"
            + (f" — {d.error_message}" if d.error_message else "")
        )
    if state.health_report is not None:
        h = state.health_report
        lines.append(
            f"health: {'ok' if h.healthy else 'unhealthy'} "
            f"(attempts={h.attempts}, latency={h.latency_ms}ms)"
            + (f" — {h.error_message}" if h.error_message else "")
        )
    if state.rollback_result is not None:
        r = state.rollback_result
        lines.append(
            f"rollback: {'ok' if r.success else 'failed'} → {r.image_tag}"
        )
    if state.rollback_health is not None:
        rh = state.rollback_health
        lines.append(
            f"post-rollback health: {'ok' if rh.healthy else 'still unhealthy'}"
        )
    if state.error_message:
        lines.append(f"error: {state.error_message}")
    lines.append(f"duration: {state.duration_seconds:.1f}s")
    return "\n".join(lines)


async def _notify(state: CDTaskState) -> None:
    """Send the CD report to Slack/Telegram if configured. No-op otherwise.

    We do NOT route through agents/notifier.send here because that function
    is tightly bound to the CI NotificationPayload shape and uses an LLM
    formatter. The CD path's flat string is short and human-readable
    already; a direct send is simpler and one less failure surface.
    """
    settings = get_settings()
    if not settings.has_notifications:
        logger.info("cd_pipeline: no notification channels configured")
        return

    text = _format_notification(state, settings)
    timeout = aiohttp.ClientTimeout(total=10)
    coros = []
    if settings.slack_webhook_url:
        coros.append(_send_slack(text, settings, timeout))
    if settings.telegram_bot_token and settings.telegram_chat_id:
        coros.append(_send_telegram(text, settings, timeout))
    if coros:
        results = await asyncio.gather(*coros, return_exceptions=True)
        sent = sum(1 for r in results if r is True)
        logger.info("cd_pipeline: notified %d/%d channels", sent, len(results))


async def _send_slack(message: str, settings: Settings, timeout: aiohttp.ClientTimeout) -> bool:
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(settings.slack_webhook_url, json={"text": message}) as resp:
                return resp.status == 200
    except Exception as e:
        logger.warning("cd_pipeline/slack: %s", e)
        return False


async def _send_telegram(message: str, settings: Settings, timeout: aiohttp.ClientTimeout) -> bool:
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    body = {"chat_id": settings.telegram_chat_id, "text": message}
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=body) as resp:
                data = await resp.json(content_type=None)
                return isinstance(data, dict) and data.get("ok") is True
    except Exception as e:
        logger.warning("cd_pipeline/telegram: %s", e)
        return False


# ── Main entry point ─────────────────────────────────────────────────────


async def run_cd_pipeline(event: ReleaseSuccessEvent) -> None:
    """Run the CD pipeline end-to-end for a successful release event.

    Never raises — every failure is captured in state.outcome /
    state.error_message and surfaced via _notify.
    """
    settings = get_settings()
    state = CDTaskState(event=event)
    set_run_context(run_id=event.run_id, phase="cd_start")
    active_runs.inc()
    logger.info(
        "cd_pipeline: starting — repo=%s sha=%s branch=%s",
        event.full_repo,
        event.short_sha,
        event.branch,
    )

    try:
        async with asyncio.timeout(settings.session_timeout_seconds):
            await _execute(state, settings)
    except asyncio.TimeoutError:
        state.outcome = "timed_out"
        state.error_message = (
            f"CD pipeline exceeded session timeout ({settings.session_timeout_seconds}s)"
        )
        logger.error("cd_pipeline: timeout for run %d", event.run_id)
        try:
            get_audit_logger().log_step(
                event.run_id,
                "cd_timeout",
                "timed_out",
                duration_ms=int(state.duration_seconds * 1000),
            )
        except Exception as e:
            logger.warning("cd_pipeline: audit on timeout failed: %s", e)
        await _publish(state, "cd_done", "timed out", level="error")
        await _notify(state)
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        raise
    except BaseException as e:
        state.outcome = "failed"
        state.error_message = f"{type(e).__name__}: {e}"
        logger.error(
            "cd_pipeline: fatal error for run %d (%s): %s",
            event.run_id,
            type(e).__name__,
            e,
            exc_info=True,
        )
        try:
            err_for_audit = e if isinstance(e, Exception) else Exception(repr(e))
            get_audit_logger().log_error(event.run_id, "cd_fatal", err_for_audit)
        except Exception as audit_err:
            logger.warning("cd_pipeline: audit after fatal failed: %s", audit_err)
        await _publish(state, "cd_done", state.error_message, level="error")
        await _notify(state)
    finally:
        record_pipeline(f"cd_{state.outcome}", state.duration_seconds)
        active_runs.dec()
        logger.info(
            "cd_pipeline: finished — outcome=%s duration=%.1fs",
            state.outcome,
            state.duration_seconds,
        )
        clear_run_context()


async def _execute(state: CDTaskState, settings: Settings) -> None:
    """The phase-by-phase work, factored out so the outer try/except wrapping
    only deals with timeouts and fatals."""
    event = state.event
    await _publish(state, "cd_start", f"release detected: {event.short_sha}")

    # ── PR resolution (best-effort) ──────────────────────────────────────
    async with audit_step(event.run_id, "cd_resolve_pr"):
        try:
            await _resolve_pr(state)
        except Exception as e:
            logger.warning("cd_pipeline: PR resolution failed: %s", e)
            state.diff_text = ""

    await _publish(
        state,
        "deploy_guard",
        f"judging {len(state.files_changed)} file(s) for promotion",
    )

    # ── Step 1: Deploy guard ─────────────────────────────────────────────
    async with audit_step(event.run_id, PipelineStep.DEPLOY_GUARD):
        diff_input = state.diff_text or _synthesise_diff_for_commit_only(state)
        state.verdict = await deploy_guard.judge(
            pr_number=state.pr_number or 0,
            pr_title=state.pr_title or f"release of {event.short_sha}",
            pr_body=state.pr_body,
            files_changed=state.files_changed,
            diff_summary=diff_input,
            head_sha=event.head_sha,
            recent_deploys=[],  # TODO: pull from audit log once stable
        )

    set_run_context(phase="deploy_guard")

    if not state.verdict.approve:
        state.outcome = "blocked"
        await _publish(
            state,
            "deploy_guard",
            f"BLOCKED: {state.verdict.reason}",
            level="warn",
        )
        await _publish(state, "cd_done", "promotion blocked by guard")
        await _notify(state)
        return

    await _publish(
        state,
        "deploy_guard",
        f"APPROVED ({state.verdict.risk}, conf {state.verdict.confidence:.2f})",
        level="success",
    )

    # ── Step 2: Build image ref + deploy ─────────────────────────────────
    try:
        image_ref = deployer.build_image_ref(event.short_sha)
    except ValueError as e:
        state.outcome = "failed"
        state.error_message = str(e)
        await _publish(state, "deploy", f"could not build image ref: {e}", level="error")
        await _notify(state)
        return

    await _publish(state, "deploy", f"deploying {image_ref}")

    async with audit_step(event.run_id, PipelineStep.DEPLOY):
        state.deploy_result = await deployer.deploy(image_ref)

    if not state.deploy_result.success:
        state.outcome = "deploy_failed"
        state.error_message = state.deploy_result.error_message
        await _publish(
            state,
            "deploy",
            f"deploy failed: {state.deploy_result.error_message}",
            level="error",
        )
        await _publish(state, "cd_done", "deploy failed — no rollback (nothing changed)")
        await _notify(state)
        return

    await _publish(
        state,
        "deploy",
        f"image flipped to {state.deploy_result.short_tag} "
        f"(prev: {state.deploy_result.prev_tag or 'none'})",
        level="success",
    )

    # ── Step 3: Health check ─────────────────────────────────────────────
    await _publish(state, "health_check", "polling /health and /version")

    async with audit_step(event.run_id, PipelineStep.HEALTH_CHECK):
        state.health_report = await health_monitor.check(event.short_sha)

    if state.health_report.healthy:
        state.outcome = "deployed"
        await _publish(
            state,
            "health_check",
            f"healthy on attempt {state.health_report.attempts} "
            f"(latency {state.health_report.latency_ms}ms)",
            level="success",
        )
        await _publish(state, "cd_done", "deploy verified healthy")
        await _notify(state)
        return

    # ── Step 4: Rollback ─────────────────────────────────────────────────
    await _publish(
        state,
        "health_check",
        f"UNHEALTHY: {state.health_report.error_message}",
        level="error",
    )

    if not settings.auto_rollback_enabled:
        state.outcome = "unhealthy_no_rollback"
        await _publish(
            state,
            "rollback",
            "auto-rollback disabled — leaving bad image live for debugging",
            level="warn",
        )
        await _notify(state)
        return

    prev_tag = state.deploy_result.prev_tag
    if not prev_tag:
        state.outcome = "unhealthy_no_prev_tag"
        await _publish(
            state,
            "rollback",
            "no prev_tag captured — cannot roll back",
            level="error",
        )
        await _notify(state)
        return

    await _publish(state, "rollback", f"rolling back to {prev_tag}", level="warn")

    async with audit_step(event.run_id, PipelineStep.ROLLBACK):
        state.rollback_result = await rollback.rollback_to(prev_tag)

    if not state.rollback_result.success:
        state.outcome = "rollback_failed"
        state.error_message = state.rollback_result.error_message
        await _publish(
            state,
            "rollback",
            f"rollback FAILED: {state.rollback_result.error_message}",
            level="error",
        )
        await _publish(state, "cd_done", "BAD IMAGE STILL LIVE — manual intervention required")
        await _notify(state)
        return

    # Re-check health after rollback. If we got here, the previous tag was
    # known good (it was running before our deploy) so health should come
    # back green. If it doesn't, something deeper is wrong.
    await _publish(state, "rollback", "rollback applied — re-checking health")
    async with audit_step(event.run_id, "cd_post_rollback_health"):
        # Strip the `repo:` prefix to get just the tag portion for the health
        # check. If prev_tag is malformed we fall back to the bare value.
        observed_tag = (
            prev_tag.split(":", 1)[1] if ":" in prev_tag else prev_tag
        )
        state.rollback_health = await health_monitor.check(observed_tag)

    if state.rollback_health.healthy:
        state.outcome = "rolled_back"
        await _publish(
            state,
            "cd_done",
            "rollback successful — previous image is live",
            level="success",
        )
    else:
        state.outcome = "rollback_health_failed"
        state.error_message = state.rollback_health.error_message
        await _publish(
            state,
            "cd_done",
            f"rollback applied but health still failing: "
            f"{state.rollback_health.error_message}",
            level="error",
        )

    await _notify(state)


def _outcome_label(state: CDTaskState) -> str:
    """Exposed for tests / metrics: the public outcome string we record."""
    return f"cd_{state.outcome}"
