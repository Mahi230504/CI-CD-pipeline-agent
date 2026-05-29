"""
Project-wide constants and enumerations.

Contains:
- ErrorType: classifies what kind of CI failure occurred
- TaskState: the state machine states for an agent task
- PipelineStep: the discrete steps the orchestrator advances through
- ErrorCategory: high-level routing classification (code vs. infra vs. flaky)
- BLOCKED_FILE_PATTERNS: files the code patcher must never touch
- INFRA_ERROR_KEYWORDS: keywords that mark a failure as infra noise, not a code bug
- LOG_SLICE_WINDOW / MAX_LOG_CHARS: log slicing bounds for Gemini context
- FLAKINESS_THRESHOLD / FLAKINESS_LOOKBACK: flakiness detector parameters
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class ErrorType(StrEnum):
    TEST_FAILURE = "test_failure"
    BUILD_ERROR = "build_error"
    LINT_ERROR = "lint_error"
    NETWORK = "network"
    INFRA = "infra"
    DEPENDENCY = "dependency"
    CONFIG = "config"
    FLAKY = "flaky"
    UNKNOWN = "unknown"

    @classmethod
    def coerce(cls, value: object) -> "ErrorType":
        if not isinstance(value, str) or not value.strip():
            return cls.UNKNOWN
        try:
            return cls(value.strip().lower())
        except ValueError:
            return cls.UNKNOWN


class TaskState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    DIAGNOSING = "diagnosing"
    PATCHING = "patching"
    OPTIMIZING = "optimizing"
    NOTIFYING = "notifying"
    DONE = "done"
    FAILED = "failed"
    ESCALATED = "escalated"
    TIMED_OUT = "timed_out"


class PipelineStep(StrEnum):
    DEDUP_CHECK = "dedup_check"
    FLAKINESS_CHECK = "flakiness_check"
    LOG_ANALYSIS = "log_analysis"
    CONFIDENCE_GATE = "confidence_gate"
    ATTEMPT_GATE = "attempt_gate"
    CODE_PATCH = "code_patch"
    YAML_OPTIMIZE = "yaml_optimize"
    NOTIFY = "notify"
    # ── CD pipeline steps (Phase 3) ──────────────────────────────────
    # The CD half runs in a separate pipeline triggered by a successful
    # release.yml workflow_run — but the audit logger keys on this enum,
    # so the steps live in the same namespace.
    DEPLOY_GUARD = "deploy_guard"
    DEPLOY = "deploy"
    HEALTH_CHECK = "health_check"
    ROLLBACK = "rollback"


class ErrorCategory(StrEnum):
    CODE_BUG = "code_bug"
    INFRA_NOISE = "infra_noise"
    FLAKY_TEST = "flaky_test"
    CONFIG_ISSUE = "config_issue"


BLOCKED_FILE_PATTERNS: Final[list[str]] = [
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.crt",
    "*.cer",
    "*secret*",
    "*password*",
    "*credential*",
    "*token*",
    "secrets.*",
    "*.tfvars",
    ".github/workflows/*.yml",
    "Dockerfile",
    "docker-compose*.yml",
]

LOG_SLICE_WINDOW: Final[int] = 30
MAX_LOG_CHARS: Final[int] = 32_000
# A run is "flaky" only when the workflow is normally green AND today's failure
# is the odd one out. Threshold is the pass rate of recent *decisive* runs
# (skipped/cancelled excluded). 0.85 means: "if at least 85% of recent runs
# passed and now we fail, it's suspicious and likely transient."
FLAKINESS_THRESHOLD: Final[float] = 0.85
FLAKINESS_LOOKBACK: Final[int] = 5

INFRA_ERROR_KEYWORDS: Final[list[str]] = [
    "no space left on device",
    "disk full",
    "out of memory",
    "oom-killed",
    "killed by signal",
    "docker pull failed",
    "manifest unknown",
    "i/o timeout",
    "network timeout",
    "network is unreachable",
    "connection reset by peer",
    "connection timed out",
    "could not resolve host",
    "rate limit exceeded",
    "429 too many requests",
    "the runner has received a shutdown signal",
    "the runner was shut down",
    "the operation was canceled",
    "econnreset",
    "etimedout",
    "enospc",
]

IGNORED_ACTOR_PATTERNS: Final[list[str]] = [
    "dependabot",
    "dependabot[bot]",
    "github-actions",
    "github-actions[bot]",
    "renovate",
    "renovate[bot]",
]

PATCH_BRANCH_PREFIX: Final[str] = "agent/fix"
OPTIMIZE_BRANCH_PREFIX: Final[str] = "agent/optimize"
# Single rolling branch — all auto-fixes land here as separate commits until the
# associated PR is merged or closed. Replaces per-run agent/fix-{run_id} branches.
ROLLING_PATCH_BRANCH: Final[str] = "agent/fixes"
# All agent-created branches share this prefix. The router drops workflow_run
# events from these branches so the agent does not self-trigger on CI for its
# own PRs.
AGENT_BRANCH_PREFIX: Final[str] = "agent/"
# Commit-message prefix the demo workflow filters on to avoid CI re-running on
# agent's own commits.
AGENT_FIX_COMMIT_TAG: Final[str] = "[agent-fix]"
MAX_QUEUE_DEPTH: Final[int] = 10
DEAD_LETTER_FILE: Final[str] = "dead_letter.jsonl"
