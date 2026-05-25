"""
Flakiness detector agent.

Before the log analyst runs, this agent checks if the failing job is
genuinely broken or just intermittently flaky.

Logic:
1. Fetch last 5 runs of the same workflow via run_history.py
2. Compute pass rate — if >= 0.4 (passed 2+ of last 5), mark as flaky
3. Separately: scan the current log for known infra error keywords
   (network timeout, rate limit, docker pull, runner: no space left)
   These are never code bugs and should never trigger a patch.

Returns: FlakinessVerdict(is_flaky, reason, pass_rate, error_category)

If is_flaky=True, the orchestrator skips patching entirely.
Uses ZERO Gemini calls — this is a GitHub API + keyword check only.
"""

from __future__ import annotations

import logging

from config.constants import (
    FLAKINESS_LOOKBACK,
    FLAKINESS_THRESHOLD,
    IGNORED_ACTOR_PATTERNS,
    INFRA_ERROR_KEYWORDS,
    ErrorCategory,
)
from github.log_fetcher import has_infra_error
from github.mcp_client import GitHubMCPClient
from github.run_history import compute_pass_rate, get_last_n_runs, had_success_at_sha
from models.events import WorkflowFailureEvent
from models.run import JobLog
from models.task import FlakinessVerdict

logger = logging.getLogger(__name__)


def _first_infra_keyword(log_text: str) -> str:
    lower = log_text.lower()
    return next((k for k in INFRA_ERROR_KEYWORDS if k in lower), "unknown infra error")


async def check(
    event: WorkflowFailureEvent,
    job_logs: list[JobLog],
    mcp_client: GitHubMCPClient,
) -> FlakinessVerdict:
    try:
        logger.info(
            "flakiness_check: run=%d sender=%s jobs=%d",
            event.run_id,
            event.sender_login,
            len(job_logs),
        )

        for job_log in job_logs:
            if has_infra_error(job_log.raw_log):
                keyword = _first_infra_keyword(job_log.raw_log)
                logger.info(
                    "flakiness_check: infra match %r in job %d",
                    keyword,
                    job_log.job_id,
                )
                return FlakinessVerdict(
                    is_flaky=True,
                    reason=f"Infrastructure error detected: {keyword}",
                    pass_rate=0.0,
                    error_category=ErrorCategory.INFRA_NOISE,
                )

        sender_lower = event.sender_login.lower()
        for pattern in IGNORED_ACTOR_PATTERNS:
            if sender_lower == pattern.lower():
                logger.info("flakiness_check: ignored actor %s", event.sender_login)
                return FlakinessVerdict(
                    is_flaky=True,
                    reason=f"Ignored actor: {event.sender_login}",
                    pass_rate=1.0,
                    error_category=ErrorCategory.INFRA_NOISE,
                )

        runs = await get_last_n_runs(event.workflow_name, FLAKINESS_LOOKBACK, mcp_client)

        # Strongest signal: the SAME head_sha succeeded in a previous run.
        # Code didn't change, outcome differs → genuine flakiness.
        if had_success_at_sha(runs, event.head_sha):
            logger.info("flakiness_check: same head_sha %s passed previously → flaky",
                        event.head_sha[:8])
            return FlakinessVerdict(
                is_flaky=True,
                reason=f"Same commit {event.head_sha[:8]} passed in a prior run",
                pass_rate=1.0,
                error_category=ErrorCategory.FLAKY_TEST,
            )

        pass_rate = compute_pass_rate(runs)
        # Today's failure looks flaky only if the workflow is otherwise reliably
        # green. Below the threshold it's either a real regression or part of an
        # ongoing broken streak — either way, the agent should try to patch.
        if pass_rate >= FLAKINESS_THRESHOLD:
            logger.info("flakiness_check: pass_rate=%.2f ≥ %.2f → flaky", pass_rate,
                        FLAKINESS_THRESHOLD)
            return FlakinessVerdict(
                is_flaky=True,
                reason=(
                    f"Workflow normally green ({pass_rate:.0%} pass rate over recent "
                    f"decisive runs); today's failure looks transient"
                ),
                pass_rate=pass_rate,
                error_category=ErrorCategory.FLAKY_TEST,
            )

        logger.info("flakiness_check: pass_rate=%.2f < %.2f → real failure", pass_rate,
                    FLAKINESS_THRESHOLD)
        return FlakinessVerdict(
            is_flaky=False,
            reason=f"Recent pass rate {pass_rate:.0%} — treating as real failure",
            pass_rate=pass_rate,
            error_category=ErrorCategory.CODE_BUG,
        )
    except Exception as e:
        logger.error("flakiness_check failed for run %d: %s", event.run_id, e)
        return FlakinessVerdict(
            is_flaky=False,
            reason=f"Flakiness check failed: {e} — treating as real failure",
            pass_rate=0.0,
            error_category=ErrorCategory.CODE_BUG,
        )
