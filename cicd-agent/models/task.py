"""
Dataclasses for tracking agent task state through the pipeline.

AgentTask           — the full lifecycle of one pipeline run through the agent
OptimizationResult  — the output of the YAML optimizer agent
NotificationPayload — the structured data sent to Slack/Telegram
FlakinessVerdict    — the output of the flakiness detector agent
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from config.constants import ErrorCategory, PipelineStep, TaskState
from models.events import WorkflowFailureEvent
from models.run import Diagnosis, PatchResult


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class FlakinessVerdict:
    is_flaky: bool
    reason: str
    pass_rate: float
    error_category: ErrorCategory

    @property
    def should_patch(self) -> bool:
        return not self.is_flaky and self.error_category == ErrorCategory.CODE_BUG


@dataclass(frozen=True)
class OptimizationResult:
    original_yaml: str
    optimized_yaml: str
    jobs_parallelized: tuple[str, ...]
    cache_steps_added: tuple[str, ...]
    estimated_savings_seconds: int
    pr_url: str | None = None
    pr_number: int | None = None
    explanation: str = ""
    optimized_at: datetime = field(default_factory=_now)

    @property
    def has_improvements(self) -> bool:
        return bool(
            self.jobs_parallelized
            or self.cache_steps_added
            or self.estimated_savings_seconds > 0
        )

    @property
    def savings_display(self) -> str:
        s = self.estimated_savings_seconds
        if s >= 60:
            return f"{s // 60}m {s % 60}s"
        return f"{s}s"


@dataclass(frozen=True)
class NotificationPayload:
    run_id: int
    repo_full_name: str
    branch: str
    html_url: str
    is_flaky: bool
    flakiness_reason: str | None
    diagnosis: Diagnosis | None
    patch_result: PatchResult | None
    optimization_result: OptimizationResult | None
    pipeline_duration_seconds: float
    escalated: bool = False
    escalation_reason: str | None = None

    @property
    def summary_line(self) -> str:
        if self.is_flaky:
            return f"[FLAKY] {self.repo_full_name} #{self.run_id} on {self.branch} — skipped patching"
        if self.escalated:
            return f"[ESCALATED] {self.repo_full_name} #{self.run_id} — {self.escalation_reason}"
        pr = self.patch_result
        if pr and pr.success:
            # attempt_number == 0 signals the dedup path: we commented on an
            # already-open PR rather than opening/patching anew.
            if pr.attempt_number == 0:
                return (
                    f"[DUPLICATE] {self.repo_full_name} #{self.run_id} — "
                    f"same error already addressed by PR: {pr.pr_url}"
                )
            # Honest labelling: only a green CI run earns "[FIXED]". A PR that
            # opened but didn't pass CI is explicitly flagged for review rather
            # than reported as a fix (which it isn't).
            if pr.verified is True:
                return f"[FIXED] {self.repo_full_name} #{self.run_id} — CI passed — PR: {pr.pr_url}"
            if pr.verified is False:
                return (
                    f"[PATCH NEEDS REVIEW] {self.repo_full_name} #{self.run_id} — "
                    f"PR opened but its CI is still failing — PR: {pr.pr_url}"
                )
            # verified is None: opened but CI not confirmed (verification off,
            # no CI run found, or timed out). Don't over-claim.
            return (
                f"[PATCH OPENED] {self.repo_full_name} #{self.run_id} — "
                f"CI not confirmed — PR: {pr.pr_url}"
            )
        return f"[FAILED] {self.repo_full_name} #{self.run_id} on {self.branch} — patch unsuccessful"


@dataclass
class AgentTask:
    run_id: int
    task_id: str = ""
    state: TaskState = TaskState.IDLE
    event: WorkflowFailureEvent | None = None
    flakiness_verdict: FlakinessVerdict | None = None
    diagnosis: Diagnosis | None = None
    patch_result: PatchResult | None = None
    optimization_result: OptimizationResult | None = None
    notification_sent: bool = False
    escalated: bool = False
    escalation_reason: str | None = None
    error_message: str | None = None
    steps_completed: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)

    def __post_init__(self) -> None:
        if not self.task_id:
            self.task_id = str(uuid4())

    def mark_step_done(self, step: PipelineStep) -> None:
        self.steps_completed.append(step.value)
        self.updated_at = _now()

    def set_state(self, new_state: TaskState) -> None:
        self.state = new_state
        self.updated_at = _now()

    def escalate(self, reason: str) -> None:
        self.escalated = True
        self.escalation_reason = reason
        self.set_state(TaskState.ESCALATED)

    @property
    def duration_seconds(self) -> float:
        return (self.updated_at - self.created_at).total_seconds()

    @property
    def to_notification_payload(self) -> NotificationPayload:
        event = self.event
        return NotificationPayload(
            run_id=self.run_id,
            repo_full_name=event.full_repo if event else "",
            branch=event.branch if event else "",
            html_url=event.html_url if event else "",
            is_flaky=bool(self.flakiness_verdict and self.flakiness_verdict.is_flaky),
            flakiness_reason=self.flakiness_verdict.reason if self.flakiness_verdict else None,
            diagnosis=self.diagnosis,
            patch_result=self.patch_result,
            optimization_result=self.optimization_result,
            pipeline_duration_seconds=self.duration_seconds,
            escalated=self.escalated,
            escalation_reason=self.escalation_reason,
        )
