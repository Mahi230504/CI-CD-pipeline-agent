"""
Dataclasses for the CD half of the pipeline.

ReleaseSuccessEvent  — normalized form of a successful release workflow_run
DeployVerdict        — LLM-judged go/no-go for promoting a merged change
DeployResult         — outcome of one deploy attempt (forward or rollback)
HealthReport         — post-deploy health probe summary

Mirrors the conventions in models/run.py: frozen dataclasses, UTC timestamps
via field(default_factory=_now), no I/O in __init__ or properties.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from models.events import WebhookPayload


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DeployRisk(StrEnum):
    """Coarse risk band the deploy guard assigns to a candidate release.

    Used by the CD orchestrator and the notifier; not enforced as a gate by
    itself — `DeployVerdict.approve` is the actual go/no-go signal.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @classmethod
    def coerce(cls, value: object) -> "DeployRisk":
        if not isinstance(value, str) or not value.strip():
            return cls.MEDIUM
        try:
            return cls(value.strip().lower())
        except ValueError:
            return cls.MEDIUM


@dataclass(frozen=True)
class ReleaseSuccessEvent:
    """A successful run of the release workflow on `main`.

    Produced by webhook/router.py when GitHub fires `workflow_run` with
    name == release_workflow_name and conclusion == "success". This is the
    trigger the CD pipeline waits for — the image is already in GHCR by
    the time the agent sees it.
    """

    run_id: int
    repo_owner: str
    repo_name: str
    workflow_name: str
    branch: str
    head_sha: str
    html_url: str
    sender_login: str
    # If GitHub attaches a merge_commit_sha / pull_request reference on the
    # workflow_run payload (it does for runs triggered by a merge), router.py
    # plumbs them through so the deploy guard can fetch the PR diff.
    pr_number: int | None = None
    received_at: datetime = field(default_factory=_now)

    @property
    def full_repo(self) -> str:
        return f"{self.repo_owner}/{self.repo_name}"

    @property
    def short_sha(self) -> str:
        # Must match the image tag release.yml pushes, which uses
        # `git rev-parse --short` (7-char default). An 8-char tag would not
        # exist in GHCR and the deployer's `docker compose pull` would fail.
        return self.head_sha[:7]

    @property
    def log_context(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "repo": self.full_repo,
            "branch": self.branch,
            "sha": self.short_sha,
            "pr": self.pr_number,
        }

    @classmethod
    def from_payload(cls, payload: "WebhookPayload") -> "ReleaseSuccessEvent":
        """Normalise a successful release workflow_run into the CD event.

        Mirrors `WorkflowFailureEvent.from_payload` so the router can pick
        the matching constructor by event class. `pr_number` is left None
        here — release workflow_run payloads typically don't carry
        `pull_requests`; the CD pipeline resolves PR via
        `rest_api.get_pulls_for_commit(head_sha)` at runtime.
        """
        return cls(
            run_id=payload.run_id,
            repo_owner=payload.repo_owner,
            repo_name=payload.repo_name,
            workflow_name=payload.run_name,
            branch=payload.branch,
            head_sha=payload.head_sha,
            html_url=payload.html_url,
            sender_login=payload.sender_login,
            pr_number=None,
        )


@dataclass(frozen=True)
class DeployVerdict:
    """Output of the deploy guard agent.

    `approve` is the only field the orchestrator branches on; the rest is for
    audit, notifications, and the SSE reasoning panel. `risk` is a hint, not
    a gate — a HIGH-risk verdict with approve=True still ships (with louder
    notifications); a LOW-risk verdict with approve=False still blocks.
    """

    approve: bool
    risk: DeployRisk
    reason: str
    # Free-form bullets the LLM identified — e.g. "touches stock_movements
    # consumer", "alembic migration present". Surfaced in the dashboard.
    concerns: tuple[str, ...] = ()
    confidence: float = 0.0
    raw_response: str = ""
    judged_at: datetime = field(default_factory=_now)

    @property
    def is_high_confidence(self) -> bool:
        return self.confidence >= 0.6


@dataclass(frozen=True)
class DeployResult:
    """Outcome of one `docker compose pull && up -d` on the target host.

    `prev_tag` records what was running before this attempt — captured so
    a follow-up rollback can re-deploy it without re-querying the host.
    Empty `prev_tag` means we couldn't read the previous tag (first deploy,
    or the host's .env didn't have IMAGE_TAG set yet).
    """

    success: bool
    # Full image reference written to the host's .env — e.g.
    # "ghcr.io/mahi230504/inventory-flow:abc1234". Named `image_tag` for
    # call-site brevity; semantically it's a full `<repo>:<tag>` ref.
    image_tag: str
    prev_tag: str = ""
    # Truncated stdout/stderr from the ssh session — bounded by the deployer
    # before construction so this dataclass stays small enough to log.
    output: str = ""
    error_message: str | None = None
    duration_seconds: float = 0.0
    deployed_at: datetime = field(default_factory=_now)

    @property
    def short_tag(self) -> str:
        return self.image_tag[:12]


@dataclass(frozen=True)
class HealthReport:
    """Post-deploy probe outcome.

    `healthy` is true only when /health returned 200 within the timeout AND
    /version reported `expected_sha` (or its short form). A 200 from /health
    on its own is not enough — that would also pass for the OLD image, which
    would mask a bad pull / failed `docker compose up`.
    """

    healthy: bool
    expected_sha: str
    observed_sha: str | None
    latency_ms: int
    attempts: int
    error_message: str | None = None
    probed_at: datetime = field(default_factory=_now)

    @property
    def sha_matches(self) -> bool:
        if not self.observed_sha:
            return False
        # Tolerate either the full SHA or the conventional 7-12 char prefix
        # — the demo /version endpoint may return either depending on how
        # release.yml stamps it.
        return (
            self.observed_sha == self.expected_sha
            or self.expected_sha.startswith(self.observed_sha)
            or self.observed_sha.startswith(self.expected_sha)
        )
