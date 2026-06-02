"""
CI verifier — closes the fix→verify loop.

After the code patcher opens (or appends to) the fix PR, we don't know whether
the change actually resolves the failure until the SAME check that failed runs
again. This module watches the fix PR's own CI run and returns a verdict:

    True  — the patch's CI run completed `success` (the fix works)
    False — it completed `failure` (still broken); the new failing log is
            returned so the patcher can retry with real feedback
    None  — could not confirm (no run found, non-decisive conclusion, or the
            wait budget expired). Callers must NOT treat None as success.

It is intentionally general: it keys on the workflow that failed (`event
.workflow_name`) and the fix branch, never on any specific error type. The CI
status is read via the same `list_workflow_runs` call the flakiness detector
already relies on — no new MCP surface, no local toolchain.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from config.constants import ROLLING_PATCH_BRANCH
from config.settings import Settings
from github.log_fetcher import fetch_job_logs
from github.mcp_client import GitHubMCPClient
from models.events import WorkflowFailureEvent
from models.run import PatchResult

logger = logging.getLogger("cicd_agent.ci_verifier")

# How long to wait for the patch's CI run to even APPEAR before giving up on
# finding it (kept well under the overall timeout so a never-scheduled run
# doesn't burn the whole budget). Re-derived from the overall timeout so a
# small configured timeout still leaves room to actually poll.
_MIN_DISCOVERY_SECONDS = 45


@dataclass(frozen=True)
class VerifyResult:
    verified: bool | None
    detail: str
    failing_log: str | None = None


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _select_run(runs: list[dict], head_sha: str | None, since: datetime, workflow_name: str) -> dict | None:
    """Find the fix branch's CI run for this patch.

    Prefer an exact head_sha match (precise). Otherwise fall back to the most
    recent run on the fix branch created at/after `since` — this covers GitHub
    associating a pull_request run with a sha other than the branch tip.
    """
    branch_runs = [
        r
        for r in runs
        if isinstance(r, dict)
        and r.get("head_branch") == ROLLING_PATCH_BRANCH
        and (not workflow_name or r.get("name") == workflow_name)
    ]
    if head_sha:
        for r in branch_runs:
            if r.get("head_sha") == head_sha:
                return r
    # Fallback: newest branch run that started after we pushed.
    fresh = [r for r in branch_runs if (_parse_iso(r.get("created_at")) or since) >= since]
    if not fresh:
        return None
    return max(fresh, key=lambda r: r.get("created_at", ""))


async def _failing_log_for_run(run_id: int, mcp_client: GitHubMCPClient) -> str | None:
    """Pull a compact failing-log excerpt from a completed-failure run."""
    try:
        job_logs = await fetch_job_logs(run_id, mcp_client)
    except Exception as e:  # never let log fetch break verification
        logger.info("ci_verifier: could not fetch logs for run %s: %s", run_id, e)
        return None
    target = next((jl for jl in job_logs if jl.sliced_log), None)
    if target is not None:
        return target.sliced_log
    return job_logs[0].raw_log[:8000] if job_logs else None


async def verify_patch_ci(
    patch_result: PatchResult,
    event: WorkflowFailureEvent,
    mcp_client: GitHubMCPClient,
    settings: Settings,
) -> VerifyResult:
    """Block until the fix PR's CI reaches a verdict, the run can't be found,
    or the configured timeout expires. Never raises."""
    workflow_name = event.workflow_name or ""
    head_sha = patch_result.head_sha
    since = datetime.now(timezone.utc)
    deadline = time.monotonic() + max(1, settings.patch_verify_timeout_seconds)
    discovery_deadline = time.monotonic() + min(
        _MIN_DISCOVERY_SECONDS, max(1, settings.patch_verify_timeout_seconds)
    )
    poll = max(1.0, settings.patch_verify_poll_interval_seconds)

    seen_run = False
    while time.monotonic() < deadline:
        try:
            runs = await mcp_client.list_workflow_runs(workflow_name, per_page=20)
        except Exception as e:
            logger.info("ci_verifier: list runs failed (will retry): %s", e)
            runs = []

        run = _select_run(runs, head_sha, since, workflow_name)
        if run is not None:
            seen_run = True
            status = run.get("status")
            run_id = run.get("id")
            if status == "completed":
                conclusion = run.get("conclusion")
                if conclusion == "success":
                    return VerifyResult(True, f"CI passed (run {run_id})")
                if conclusion == "failure":
                    failing_log = (
                        await _failing_log_for_run(run_id, mcp_client)
                        if isinstance(run_id, int)
                        else None
                    )
                    return VerifyResult(False, f"CI failed (run {run_id})", failing_log)
                # cancelled / skipped / neutral / timed_out → no clean verdict.
                return VerifyResult(None, f"CI ended '{conclusion}' — inconclusive (run {run_id})")
            # queued / in_progress → keep waiting.
        elif not seen_run and time.monotonic() >= discovery_deadline:
            return VerifyResult(None, "no CI run found for the fix branch — unverified")

        await asyncio.sleep(poll)

    return VerifyResult(None, f"CI verdict not reached within {settings.patch_verify_timeout_seconds}s")
