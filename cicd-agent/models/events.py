"""
Dataclasses for incoming webhook events.

WebhookPayload          — the raw parsed JSON body from GitHub
WorkflowFailureEvent    — a validated, normalized failure event ready for the pipeline
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _require_dict(obj: Any, path: str) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise ValueError(f"{path} must be a JSON object")
    return obj


def _require_field(obj: dict[str, Any], key: str, path: str) -> Any:
    if key not in obj:
        raise ValueError(f"missing required field: {path}")
    value = obj[key]
    if value is None:
        raise ValueError(f"required field is null: {path}")
    return value


@dataclass
class WebhookPayload:
    action: str
    run_id: int
    run_name: str
    conclusion: str
    branch: str
    head_sha: str
    html_url: str
    repo_owner: str
    repo_name: str
    sender_login: str
    sender_type: str
    raw_body: bytes

    @classmethod
    def from_github_payload(cls, body: bytes) -> "WebhookPayload":
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            raise ValueError(f"webhook body is not valid JSON: {e}") from e

        data = _require_dict(data, "body")

        action = _require_field(data, "action", "action")
        wr = _require_dict(_require_field(data, "workflow_run", "workflow_run"), "workflow_run")

        repo_src = wr.get("repository") if isinstance(wr.get("repository"), dict) else data.get("repository")
        repo = _require_dict(repo_src, "repository")
        owner = _require_dict(_require_field(repo, "owner", "repository.owner"), "repository.owner")

        sender_src = data.get("sender") if isinstance(data.get("sender"), dict) else wr.get("sender")
        sender = _require_dict(sender_src, "sender")

        return cls(
            action=str(action),
            run_id=int(_require_field(wr, "id", "workflow_run.id")),
            run_name=str(_require_field(wr, "name", "workflow_run.name")),
            conclusion=str(_require_field(wr, "conclusion", "workflow_run.conclusion")),
            branch=str(_require_field(wr, "head_branch", "workflow_run.head_branch")),
            head_sha=str(_require_field(wr, "head_sha", "workflow_run.head_sha")),
            html_url=str(_require_field(wr, "html_url", "workflow_run.html_url")),
            repo_owner=str(_require_field(owner, "login", "repository.owner.login")),
            repo_name=str(_require_field(repo, "name", "repository.name")),
            sender_login=str(_require_field(sender, "login", "sender.login")),
            sender_type=str(_require_field(sender, "type", "sender.type")),
            raw_body=body,
        )


@dataclass(frozen=True)
class WorkflowFailureEvent:
    run_id: int
    repo_owner: str
    repo_name: str
    workflow_name: str
    branch: str
    head_sha: str
    html_url: str
    sender_login: str
    received_at: datetime = field(default_factory=_now)

    @property
    def full_repo(self) -> str:
        return f"{self.repo_owner}/{self.repo_name}"

    @property
    def short_sha(self) -> str:
        return self.head_sha[:8]

    @property
    def log_context(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "repo": self.full_repo,
            "branch": self.branch,
            "sha": self.short_sha,
        }

    @classmethod
    def from_payload(cls, payload: WebhookPayload) -> "WorkflowFailureEvent":
        return cls(
            run_id=payload.run_id,
            repo_owner=payload.repo_owner,
            repo_name=payload.repo_name,
            workflow_name=payload.run_name,
            branch=payload.branch,
            head_sha=payload.head_sha,
            html_url=payload.html_url,
            sender_login=payload.sender_login,
        )
