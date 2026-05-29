"""Tests for the release-success branch added to webhook/router.py.

Existing CI-failure routing already has coverage in other test files; this
file focuses on the new CD entry path and confirms the CI path still
behaves under the modified router shape.
"""

from __future__ import annotations

import json

import pytest

from config import settings as settings_module
from orchestrator import task_queue
from webhook import router


def _payload(
    *,
    conclusion: str,
    workflow_name: str,
    branch: str = "main",
    sender: str = "alice",
    sender_type: str = "User",
) -> bytes:
    """Build a minimal GitHub workflow_run payload as raw bytes."""
    body = {
        "action": "completed",
        "workflow_run": {
            "id": 999,
            "name": workflow_name,
            "head_branch": branch,
            "head_sha": "abc1234567890" + "a" * 27,
            "status": "completed",
            "conclusion": conclusion,
            "html_url": "https://github.com/x/y/actions/runs/999",
            "repository": {
                "full_name": "x/y",
                "name": "y",
                "owner": {"login": "x"},
            },
            "sender": {"login": sender, "type": sender_type},
        },
        "sender": {"login": sender, "type": sender_type},
        "repository": {
            "full_name": "x/y",
            "name": "y",
            "owner": {"login": "x"},
        },
    }
    return json.dumps(body).encode("utf-8")


class _CapturingQueue:
    """Drop-in for the task queue that records what was enqueued.

    Lets the test inspect that the right event TYPE went on the wire.
    """

    def __init__(self) -> None:
        self.enqueued: list[object] = []

    async def enqueue(self, event: object) -> bool:
        self.enqueued.append(event)
        return True


@pytest.fixture
def capturing_queue(monkeypatch: pytest.MonkeyPatch) -> _CapturingQueue:
    cap = _CapturingQueue()
    monkeypatch.setattr(task_queue, "_task_queue", cap)
    yield cap
    monkeypatch.setattr(task_queue, "_task_queue", None)


@pytest.fixture
def cd_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    settings_module.get_settings.cache_clear()
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake")
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "fake")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "fake")
    monkeypatch.setenv("GITHUB_REPO_OWNER", "x")
    monkeypatch.setenv("GITHUB_REPO_NAME", "y")
    monkeypatch.setenv("CODESPACE_NAME", "test-cs")
    monkeypatch.setenv("BACKEND_BASE_URL", "https://example")
    monkeypatch.setenv("DEPLOY_IMAGE_REPOSITORY", "ghcr.io/x/y")
    monkeypatch.setenv("RELEASE_WORKFLOW_NAME", "release")
    yield
    settings_module.get_settings.cache_clear()


@pytest.fixture
def cd_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    settings_module.get_settings.cache_clear()
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake")
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "fake")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "fake")
    monkeypatch.setenv("GITHUB_REPO_OWNER", "x")
    monkeypatch.setenv("GITHUB_REPO_NAME", "y")
    monkeypatch.delenv("CODESPACE_NAME", raising=False)
    monkeypatch.delenv("BACKEND_BASE_URL", raising=False)
    monkeypatch.delenv("DEPLOY_IMAGE_REPOSITORY", raising=False)
    yield
    settings_module.get_settings.cache_clear()


# ── Release-success path ──────────────────────────────────────────────────


async def test_release_success_enqueues_release_event(
    cd_enabled: None, capturing_queue: _CapturingQueue
) -> None:
    from models.cd import ReleaseSuccessEvent

    body = _payload(conclusion="success", workflow_name="release")
    accepted, reason = await router.route_webhook(body, "workflow_run")

    assert accepted is True
    assert "Accepted release" in reason
    assert len(capturing_queue.enqueued) == 1
    event = capturing_queue.enqueued[0]
    assert isinstance(event, ReleaseSuccessEvent)
    assert event.workflow_name == "release"
    assert event.branch == "main"


async def test_release_success_rejected_when_cd_disabled(
    cd_disabled: None, capturing_queue: _CapturingQueue
) -> None:
    body = _payload(conclusion="success", workflow_name="release")
    accepted, reason = await router.route_webhook(body, "workflow_run")

    assert accepted is False
    assert "CD not configured" in reason
    assert capturing_queue.enqueued == []


async def test_success_on_non_release_workflow_ignored(
    cd_enabled: None, capturing_queue: _CapturingQueue
) -> None:
    """A CI workflow finishing GREEN must not trigger a deploy."""
    body = _payload(conclusion="success", workflow_name="CI")
    accepted, reason = await router.route_webhook(body, "workflow_run")

    assert accepted is False
    assert "Ignored success workflow" in reason
    assert capturing_queue.enqueued == []


async def test_release_on_agent_branch_rejected(
    cd_enabled: None, capturing_queue: _CapturingQueue
) -> None:
    body = _payload(conclusion="success", workflow_name="release", branch="agent/fixes")
    accepted, reason = await router.route_webhook(body, "workflow_run")
    assert accepted is False
    assert "agent branch" in reason
    assert capturing_queue.enqueued == []


# ── CI failure path still works (regression guard) ────────────────────────


async def test_ci_failure_still_enqueues_failure_event(
    cd_enabled: None, capturing_queue: _CapturingQueue
) -> None:
    from models.events import WorkflowFailureEvent

    body = _payload(conclusion="failure", workflow_name="CI")
    accepted, reason = await router.route_webhook(body, "workflow_run")

    assert accepted is True
    assert "Accepted run" in reason
    assert len(capturing_queue.enqueued) == 1
    assert isinstance(capturing_queue.enqueued[0], WorkflowFailureEvent)


async def test_ci_failure_on_agent_branch_ignored(
    cd_enabled: None, capturing_queue: _CapturingQueue
) -> None:
    body = _payload(conclusion="failure", workflow_name="CI", branch="agent/fix-1")
    accepted, reason = await router.route_webhook(body, "workflow_run")
    assert accepted is False
    assert "agent branch" in reason


# ── Defensive coverage ────────────────────────────────────────────────────


async def test_ignored_event_type(cd_enabled: None, capturing_queue: _CapturingQueue) -> None:
    body = _payload(conclusion="success", workflow_name="release")
    accepted, reason = await router.route_webhook(body, "push")
    assert accepted is False
    assert "Ignored event type" in reason


async def test_malformed_payload(cd_enabled: None, capturing_queue: _CapturingQueue) -> None:
    accepted, reason = await router.route_webhook(b"not json", "workflow_run")
    assert accepted is False
    assert "Malformed" in reason


async def test_non_completed_action(cd_enabled: None, capturing_queue: _CapturingQueue) -> None:
    body = json.loads(_payload(conclusion="success", workflow_name="release"))
    body["action"] = "requested"
    raw = json.dumps(body).encode()
    accepted, reason = await router.route_webhook(raw, "workflow_run")
    assert accepted is False
    assert "Ignored action" in reason


async def test_neither_success_nor_failure(
    cd_enabled: None, capturing_queue: _CapturingQueue
) -> None:
    """Cancelled / neutral conclusions are dropped before either branch."""
    body = _payload(conclusion="cancelled", workflow_name="release")
    accepted, reason = await router.route_webhook(body, "workflow_run")
    assert accepted is False
    assert "Ignored conclusion" in reason
