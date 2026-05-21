"""
Dataclasses representing a GitHub Actions workflow run and its analysis.

WorkflowRun     — the raw run event from GitHub
JobLog          — a single job's log, raw and sliced
Diagnosis       — the structured output of the log analyst agent
PatchResult     — the result of a code patch attempt including PR URL
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

from config.constants import ErrorType


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class WorkflowRun:
    run_id: int
    name: str
    repo_owner: str
    repo_name: str
    branch: str
    head_sha: str
    html_url: str
    created_at: datetime = field(default_factory=_now)

    @property
    def full_repo(self) -> str:
        return f"{self.repo_owner}/{self.repo_name}"

    @property
    def short_sha(self) -> str:
        return self.head_sha[:8]


@dataclass
class JobLog:
    job_id: int
    job_name: str
    raw_log: str
    sliced_log: str | None = None
    error_line_number: int | None = None
    error_step_name: str | None = None
    log_size_chars: int = 0

    def __post_init__(self) -> None:
        self.log_size_chars = len(self.raw_log)


@dataclass(frozen=True)
class Diagnosis:
    error_type: ErrorType
    file: str | None
    line_number: int | None
    explanation: str
    confidence: float
    is_patchable: bool
    raw_response: str
    diagnosed_at: datetime = field(default_factory=_now)

    @property
    def is_high_confidence(self) -> bool:
        return self.confidence >= 0.6

    @property
    def error_hash(self) -> str:
        key = f"{self.file or 'unknown'}:{self.line_number}:{self.error_type}"
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class PatchResult:
    branch_name: str
    success: bool
    attempt_number: int
    pr_url: str | None = None
    pr_number: int | None = None
    diff: str | None = None
    error_message: str | None = None
    patched_at: datetime = field(default_factory=_now)
