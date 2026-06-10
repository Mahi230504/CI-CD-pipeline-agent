"""Chat domain types for the Agent Console (agent side).

A ``ChatTaskEvent`` is one chat turn handed to the agent worker over the Redis
``agent:tasks`` stream by the demo backend. All ids on the wire are opaque
public strings (``cnv_…``/``turn_…``/``run_…``/``tenant_…``) — the agent passes
them straight back to the demo's /internal/console endpoints, so it never needs
to know they're integers underneath.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ChatIntent(StrEnum):
    FEATURE = "feature"
    BUGFIX = "bugfix"
    DEPLOY = "deploy"
    QUESTION = "question"
    APPROVE = "approve"
    REJECT = "reject"


class TurnStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    MERGING = "merging"
    DEPLOYING = "deploying"
    DONE = "done"
    FAILED = "failed"
    REJECTED = "rejected"


def _clean(value: str | None) -> str | None:
    """The demo enqueues None as the JSON literal ``"null"``; map it (and empty)
    back to None. Every other field is a plain string and passes through."""
    if value is None or value in ("null", ""):
        return None
    return value


@dataclass(frozen=True)
class ChatTaskEvent:
    """One chat turn off the agent-tasks stream. ``kind`` is chat | approve |
    reject (approve/reject resume a paused turn)."""

    tenant_id: str
    conversation_id: str
    turn_id: str
    message: str
    autonomy: str = "manual"
    kind: str = "chat"
    run_id: str | None = None

    @classmethod
    def from_stream_fields(cls, fields: dict[str, str]) -> "ChatTaskEvent":
        """Parse a Redis stream entry (flat string fields) into the event.

        Raises ValueError if a required field is missing so the consumer can
        ack-and-skip a malformed entry rather than crash the worker loop.
        """
        required = ("tenant_id", "conversation_id", "turn_id", "message")
        missing = [k for k in required if not fields.get(k)]
        if missing:
            raise ValueError(f"chat task missing fields: {missing}")
        return cls(
            tenant_id=fields["tenant_id"],
            conversation_id=fields["conversation_id"],
            turn_id=fields["turn_id"],
            message=fields["message"],
            autonomy=_clean(fields.get("autonomy")) or "manual",
            kind=_clean(fields.get("kind")) or "chat",
            run_id=_clean(fields.get("run_id")),
        )

    @property
    def log_context(self) -> dict[str, str | None]:
        return {
            "tenant": self.tenant_id,
            "conversation_id": self.conversation_id,
            "turn_id": self.turn_id,
            "run_id": self.run_id,
            "kind": self.kind,
        }


@dataclass
class EditProposal:
    """The chat editor's output.

    ``file_contents`` maps path → full new file content (the editor returns whole
    files, so we commit them directly — robust for NEW files where there's no
    original to apply hunks against). ``diff`` is a synthesized unified diff for
    DISPLAY only. ``cannot_reason`` is set when the editor declines (out of
    scope, or it would touch a blocked path)."""

    file_contents: dict[str, str] = field(default_factory=dict)
    diff: str = ""
    summary: str = ""
    cannot_reason: str | None = None

    @property
    def files(self) -> list[str]:
        return list(self.file_contents.keys())

    @property
    def is_actionable(self) -> bool:
        return bool(self.file_contents and self.cannot_reason is None)
