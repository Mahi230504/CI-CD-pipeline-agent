"""
The main CI/CD agent pipeline.

Single entry point: async run(event: WorkflowFailureEvent) → None

Executes the 6-step agent sequence:
1. Deduplication check (run_registry)
2. Flakiness detection (agents/flakiness_detector)
3. Log analysis + confidence gate (agents/log_analyst)
4. Attempt count check + code patching (agents/code_patcher)
5. YAML optimization (agents/yaml_optimizer)
6. Notification (agents/notifier)

Writes an audit log entry at every step boundary.
Wrapped in asyncio.timeout(SESSION_TIMEOUT_SECONDS) — kills runaway pipelines.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from agents import event_publisher
from audit.context import clear_run_context, set_run_context
from audit.logger import audit_step, get_audit_logger
from config.constants import ROLLING_PATCH_BRANCH, PipelineStep, TaskState
from config.settings import Settings, get_settings
from github import rest_api
from github.log_fetcher import fetch_job_logs
from github.mcp_client import GitHubMCPClient
from github.run_history import clear_cache
from metrics import active_runs, record_pipeline
from models.events import WorkflowFailureEvent
from models.run import PatchResult
from models.task import AgentTask, NotificationPayload
from orchestrator.run_registry import get_registry

logger = logging.getLogger("cicd_agent.pipeline")


async def _publish(
    event: WorkflowFailureEvent,
    stage: str,
    message: str,
    *,
    level: str = "info",
    meta: dict[str, Any] | None = None,
) -> None:
    """Push one CI-pipeline reasoning event to the dashboard (fire-and-forget).

    Mirrors orchestrator.cd_pipeline._publish: stamps run_id / repo / branch /
    sha onto every event so the frontend can stitch the full story together.
    Never raises — event_publisher.publish_safe swallows all errors, and the
    call is a silent no-op when the backend URL / token are unset (tests, CI).
    """
    base: dict[str, Any] = {
        "run_id": event.run_id,
        "repo": event.full_repo,
        "branch": event.branch,
        "sha": event.short_sha,
    }
    if meta:
        base.update(meta)
    await event_publisher.publish_safe(stage, message, level=level, metadata=base)  # type: ignore[arg-type]


def _pipeline_outcome(task: AgentTask) -> str:
    """Reduce the task's final state to a single label for metrics."""
    if task.flakiness_verdict is not None and task.flakiness_verdict.is_flaky:
        return "flaky"
    if task.state == TaskState.TIMED_OUT:
        return "timed_out"
    if task.state == TaskState.FAILED:
        return "failed"
    if task.escalated:
        return "escalated"
    # Dedup gate sets attempt_number=0 on patch_result to signal "didn't try, commented".
    if task.patch_result is not None and task.patch_result.attempt_number == 0:
        return "deduped"
    # The happy path sets DONE and then transitions to NOTIFYING for the final
    # send; by the time _pipeline_outcome runs in the `finally`, a non-escalated,
    # non-flaky run that reached the notify step has completed its work.
    if task.state in (TaskState.DONE, TaskState.NOTIFYING):
        return "success"
    return "unknown"


async def run_pipeline(event: WorkflowFailureEvent) -> None:
    settings = get_settings()
    task = AgentTask(run_id=event.run_id, event=event)
    start_time = time.monotonic()

    set_run_context(run_id=event.run_id, phase="start")
    active_runs.inc()
    logger.info(
        "Pipeline starting: repo=%s branch=%s",
        event.full_repo,
        event.branch,
    )

    # Patch verification waits on the fix PR's live CI run, which legitimately
    # takes minutes — so it gets its own budget ON TOP of the base session
    # timeout, or the timeout would kill the pipeline mid-verify.
    verify_budget = (
        settings.patch_verify_timeout_seconds * (settings.patch_verify_max_iterations + 1)
        if settings.patch_verify_enabled
        else 0
    )
    effective_timeout = settings.session_timeout_seconds + verify_budget

    try:
        async with asyncio.timeout(effective_timeout):
            await _execute_pipeline(task, event, settings, start_time)
    except asyncio.TimeoutError:
        task.set_state(TaskState.TIMED_OUT)
        logger.error(
            "Pipeline timed out after %ds for run %d",
            effective_timeout,
            event.run_id,
        )
        await _publish(
            event,
            "timeout",
            f"Pipeline timed out after {effective_timeout}s",
            level="error",
        )
        try:
            get_audit_logger().log_step(
                event.run_id,
                "timeout",
                "timed_out",
                duration_ms=int((time.monotonic() - start_time) * 1000),
            )
        except Exception as e:
            logger.warning("audit log after timeout failed: %s", e)
        try:
            await _send_notification(task, start_time)
        except Exception as e:
            logger.error("notification after timeout failed: %s", e)
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        raise
    except BaseException as e:
        # Catches BaseExceptionGroup raised by anyio task-groups (mcp transport).
        # Previously these escaped and killed the process between code_patch and
        # the rest of the pipeline.
        task.set_state(TaskState.FAILED)
        task.error_message = f"{type(e).__name__}: {e}"
        logger.error(
            "Pipeline fatal error for run %d (%s): %s",
            event.run_id,
            type(e).__name__,
            e,
            exc_info=True,
        )
        # Surface only the exception class to the dashboard — the message can
        # carry log/file fragments we must not leak (CLAUDE.md security rules).
        await _publish(event, "error", f"Pipeline error: {type(e).__name__}", level="error")
        try:
            err_for_audit = e if isinstance(e, Exception) else Exception(repr(e))
            get_audit_logger().log_error(event.run_id, "pipeline_fatal", err_for_audit)
        except Exception as audit_err:
            logger.warning("audit log after fatal failed: %s", audit_err)
        try:
            await _send_notification(task, start_time)
        except Exception as notif_err:
            logger.error("notification after fatal failed: %s", notif_err)
    finally:
        clear_cache()
        outcome = _pipeline_outcome(task)
        duration = time.monotonic() - start_time
        record_pipeline(outcome, duration)
        active_runs.dec()
        logger.info(
            "Pipeline finished: outcome=%s state=%s duration=%.1fs",
            outcome,
            task.state,
            duration,
        )
        done_level = "success" if outcome in {"success", "flaky", "deduped"} else "warn"
        await _publish(
            event,
            "done",
            f"Pipeline finished: {outcome} ({duration:.1f}s)",
            level=done_level,
            meta={"outcome": outcome},
        )
        clear_run_context()


async def _execute_pipeline(
    task: AgentTask,
    event: WorkflowFailureEvent,
    settings: Settings,
    start_time: float,
) -> None:
    registry = get_registry()
    task.set_state(TaskState.RUNNING)
    await _publish(
        event,
        "received",
        f"CI failure detected on {event.full_repo} @ {event.short_sha} ({event.branch})",
        level="warn",
    )

    # ── Step 1: Deduplication ──────────────────────────────────────────────
    async with audit_step(event.run_id, PipelineStep.DEDUP_CHECK):
        if registry.is_duplicate(event.run_id):
            logger.info("Pipeline: run %d already processed — skipping", event.run_id)
            await _publish(
                event, "dedup", f"Run {event.run_id} already processed — skipping"
            )
            task.set_state(TaskState.DONE)
            task.mark_step_done(PipelineStep.DEDUP_CHECK)
            return
        task.mark_step_done(PipelineStep.DEDUP_CHECK)

    async with GitHubMCPClient() as mcp:
        # ── Step 2: Fetch logs ─────────────────────────────────────────────
        async with audit_step(event.run_id, "fetch_logs"):
            await _publish(event, "fetch_logs", "Fetching failed-job logs from GitHub…")
            job_logs = await fetch_job_logs(event.run_id, mcp)
            if not job_logs:
                await _publish(
                    event,
                    "fetch_logs",
                    "No failed-job logs found — escalating to a human",
                    level="warn",
                )
                task.escalate("No failed job logs found")
                registry.mark_run_processed(event.run_id, "escalated")
                await _send_notification(task, start_time)
                return
            await _publish(
                event,
                "fetch_logs",
                f"Pulled logs from {len(job_logs)} failed job(s)",
                level="success",
            )

        # ── Step 3: Flakiness check ────────────────────────────────────────
        from agents.flakiness_detector import check as flakiness_check
        async with audit_step(event.run_id, PipelineStep.FLAKINESS_CHECK):
            task.set_state(TaskState.DIAGNOSING)
            await _publish(
                event, "flakiness", "Checking whether this failure is flaky or genuine…"
            )
            verdict = await flakiness_check(event, job_logs, mcp)
            task.flakiness_verdict = verdict
            task.mark_step_done(PipelineStep.FLAKINESS_CHECK)

        if not verdict.should_patch:
            await _publish(
                event,
                "flakiness",
                f"Classified as flaky — no code fix needed ({verdict.reason})",
                level="success",
                meta={"pass_rate": verdict.pass_rate},
            )
            registry.mark_run_processed(event.run_id, "done")
            task.set_state(TaskState.DONE)
            await _send_notification(task, start_time)
            return

        await _publish(
            event,
            "flakiness",
            "Genuine failure (not flaky) — proceeding to root-cause analysis",
        )

        # ── Step 4: Log analysis ───────────────────────────────────────────
        from agents.log_analyst import diagnose
        async with audit_step(event.run_id, PipelineStep.LOG_ANALYSIS):
            task.set_state(TaskState.DIAGNOSING)
            await _publish(
                event, "diagnosis", "Analysing logs with Gemini to find the root cause…"
            )
            diagnosis = await diagnose(job_logs, event, mcp_client=mcp)
            task.diagnosis = diagnosis
            if diagnosis is not None:
                etype = getattr(diagnosis.error_type, "value", str(diagnosis.error_type))
                await _publish(
                    event,
                    "diagnosis",
                    f"Root cause: {etype} in {diagnosis.file or '?'}:{diagnosis.line_number} "
                    f"(confidence {diagnosis.confidence:.2f})",
                    level="success",
                    meta={
                        "confidence": diagnosis.confidence,
                        "error_type": etype,
                        "file": diagnosis.file,
                    },
                )
            task.mark_step_done(PipelineStep.LOG_ANALYSIS)

        async with audit_step(event.run_id, PipelineStep.CONFIDENCE_GATE):
            if diagnosis is None or not diagnosis.is_high_confidence:
                reason = (
                    "Log analysis returned no diagnosis"
                    if diagnosis is None
                    else f"Low confidence: {diagnosis.confidence:.2f}"
                )
                await _publish(
                    event, "confidence_gate", f"Escalating to a human — {reason}", level="warn"
                )
                task.escalate(reason)
                registry.mark_run_processed(event.run_id, "escalated")
                task.mark_step_done(PipelineStep.CONFIDENCE_GATE)
                await _send_notification(task, start_time)
                return
            await _publish(
                event,
                "confidence_gate",
                f"Confidence {diagnosis.confidence:.2f} clears the bar — attempting an automated fix",
            )
            task.mark_step_done(PipelineStep.CONFIDENCE_GATE)

        # ── Step 5: Attempt gate + dedup + patch ───────────────────────────
        # patch_and_verify opens the fix PR, then watches its CI and retries
        # with feedback while it stays red — so a "fix" is only reported when
        # its CI actually passes.
        from agents.code_patcher import patch_and_verify as do_patch
        async with audit_step(event.run_id, PipelineStep.ATTEMPT_GATE):
            error_hash = diagnosis.error_hash
            set_run_context(error_hash=error_hash)

            # Dedup-by-error-hash: if an open PR is already addressing this exact
            # failure, comment on it instead of opening / appending another.
            existing = registry.get_open_pr(error_hash)
            if existing is not None:
                pr_number, pr_url = existing
                pr_data = await rest_api.get_pull_request(pr_number)
                pr_state = str((pr_data or {}).get("state", "")).lower()
                if pr_data is not None and pr_state == "open":
                    comment = (
                        f"Same error recurred in run {event.run_id}.\n"
                        f"- Branch: `{event.branch}`\n"
                        f"- Commit: `{event.head_sha[:8]}`\n"
                        f"- Diagnosis confidence: {diagnosis.confidence:.2f}\n"
                        f"- Failing run: {event.html_url}"
                    )
                    try:
                        await rest_api.post_issue_comment(pr_number, comment)
                    except Exception as e:
                        logger.warning("dedup: comment failed for PR #%d: %s", pr_number, e)
                    await _publish(
                        event,
                        "code_patch",
                        f"Same error already addressed by open PR #{pr_number} — commented there instead",
                        meta={"pr": pr_number},
                    )
                    task.patch_result = PatchResult(
                        branch_name=ROLLING_PATCH_BRANCH,
                        success=True,
                        attempt_number=0,
                        pr_url=pr_url,
                        pr_number=pr_number,
                        error_message="dedup: commented on existing open PR",
                    )
                    task.set_state(TaskState.DONE)
                    registry.mark_run_processed(event.run_id, "deduped")
                    task.mark_step_done(PipelineStep.ATTEMPT_GATE)
                    await _send_notification(task, start_time)
                    return
                # PR was closed or merged. If merged, the previous fix landed —
                # a same-shape failure now is a NEW occurrence, so reset the
                # attempt counter so the natural cap doesn't block us. If closed
                # without merge (human rejected), keep the count in place.
                was_merged = (
                    pr_data is not None
                    and isinstance(pr_data.get("merged_at"), str)
                    and pr_data.get("merged_at") is not None
                )
                registry.clear_open_pr(error_hash, reset_attempts=was_merged)

            attempt_count = registry.get_attempt_count(error_hash)
            if registry.is_escalated(error_hash) or attempt_count >= settings.max_patch_attempts:
                await _publish(
                    event,
                    "attempt_gate",
                    f"Max patch attempts reached "
                    f"({attempt_count}/{settings.max_patch_attempts}) — escalating to a human",
                    level="warn",
                )
                task.escalate(
                    f"Max patch attempts reached "
                    f"({attempt_count}/{settings.max_patch_attempts})"
                )
                registry.mark_run_processed(event.run_id, "escalated")
                task.mark_step_done(PipelineStep.ATTEMPT_GATE)
                await _send_notification(task, start_time)
                return
            task.mark_step_done(PipelineStep.ATTEMPT_GATE)

        async with audit_step(event.run_id, PipelineStep.CODE_PATCH):
            task.set_state(TaskState.PATCHING)
            await _publish(
                event,
                "code_patch",
                f"Generating a fix and opening a pull request (attempt {attempt_count + 1})…",
            )
            registry.increment_attempt(error_hash)
            # Hand the patcher the failing log so it can read the failing test(s)
            # and satisfy every assertion (including boundary cases), not just the
            # first one pytest happened to print.
            target_log = next((jl for jl in job_logs if jl.sliced_log), None)
            failing_log = (
                target_log.sliced_log
                if target_log is not None
                else (job_logs[0].raw_log[:8000] if job_logs else None)
            )
            patch_result = await do_patch(
                diagnosis=diagnosis,
                event=event,
                attempt_number=attempt_count + 1,
                mcp_client=mcp,
                failing_log=failing_log,
            )
            task.patch_result = patch_result
            if patch_result.success and isinstance(patch_result.pr_number, int):
                registry.record_open_pr(
                    error_hash,
                    patch_result.pr_number,
                    patch_result.pr_url,
                )
            elif not patch_result.success and attempt_count + 1 >= settings.max_patch_attempts:
                # Only escalate once the natural attempt budget is exhausted.
                # A single failed attempt should leave the door open for the
                # next webhook to try again — otherwise one transient gemini /
                # diff-apply hiccup permanently blocks the hash.
                registry.mark_escalated(error_hash)
            if patch_result.success:
                if patch_result.verified is True:
                    pr_msg = f"Fix PR #{patch_result.pr_number} verified — its CI passed"
                    pr_level = "success"
                elif patch_result.verified is False:
                    pr_msg = (
                        f"Fix PR #{patch_result.pr_number} opened but its CI is still "
                        f"failing — needs human review"
                    )
                    pr_level = "warn"
                else:
                    pr_msg = f"Fix PR #{patch_result.pr_number} opened (CI not confirmed)"
                    pr_level = "info"
                await _publish(
                    event,
                    "code_patch",
                    pr_msg,
                    level=pr_level,
                    meta={
                        "pr": patch_result.pr_number,
                        "pr_url": patch_result.pr_url,
                        "verified": patch_result.verified,
                    },
                )
            else:
                await _publish(
                    event,
                    "code_patch",
                    f"Patch attempt failed — {patch_result.error_message or 'unknown error'}",
                    level="error",
                )
            task.mark_step_done(PipelineStep.CODE_PATCH)

        # ── Step 6: YAML optimization ──────────────────────────────────────
        from agents.yaml_optimizer import optimize
        async with audit_step(event.run_id, PipelineStep.YAML_OPTIMIZE):
            task.set_state(TaskState.OPTIMIZING)
            await _publish(
                event, "yaml_optimize", "Optimising the workflow YAML for faster CI runs…"
            )
            opt_result = await optimize(event, mcp)
            task.optimization_result = opt_result
            if opt_result is not None and opt_result.pr_number:
                await _publish(
                    event,
                    "yaml_optimize",
                    f"Workflow optimization PR opened: #{opt_result.pr_number} "
                    f"(saves ~{opt_result.savings_display})",
                    level="success",
                    meta={"pr": opt_result.pr_number, "pr_url": opt_result.pr_url},
                )
            else:
                await _publish(
                    event, "yaml_optimize", "No worthwhile workflow optimizations found"
                )
            task.mark_step_done(PipelineStep.YAML_OPTIMIZE)

        # ── Step 7: Notify + finish ────────────────────────────────────────
        registry.mark_run_processed(event.run_id, task.state.value)
        task.set_state(TaskState.DONE)
        await _send_notification(task, start_time)


async def _send_notification(task: AgentTask, start_time: float) -> None:
    from agents.notifier import send as notify

    async with audit_step(task.run_id, PipelineStep.NOTIFY):
        task.set_state(TaskState.NOTIFYING)
        base = task.to_notification_payload
        payload = NotificationPayload(
            run_id=base.run_id,
            repo_full_name=base.repo_full_name,
            branch=base.branch,
            html_url=base.html_url,
            is_flaky=base.is_flaky,
            flakiness_reason=base.flakiness_reason,
            diagnosis=base.diagnosis,
            patch_result=base.patch_result,
            optimization_result=base.optimization_result,
            pipeline_duration_seconds=time.monotonic() - start_time,
            escalated=base.escalated,
            escalation_reason=base.escalation_reason,
        )
        task.notification_sent = await notify(payload)
        if task.event is not None:
            await _publish(
                task.event,
                "notify",
                "Notification sent" if task.notification_sent else "Notification could not be sent",
                level="info" if task.notification_sent else "warn",
            )
        task.mark_step_done(PipelineStep.NOTIFY)
