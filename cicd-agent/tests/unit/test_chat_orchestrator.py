"""Slice-4 functional tests: the ChatOrchestrator feature pipeline.

Fakes every collaborator (editor, PR open, verify, guard, merge, deploy, LLM)
plus a fake MCP factory and ConsoleApiClient, and stubs event_publisher — so the
real control flow + the AUTO gate (real autonomy_policy + real pr_risk) run with
no network, no LLM, no .env-driven SSH (deployer.deploy is faked unconditionally).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agents import chat_orchestrator as co
from agents.chat_orchestrator import ChatOrchestrator
from models.chat import ChatTaskEvent, EditProposal


class FakeClient:
    def __init__(self) -> None:
        self.turn_patches: list[dict] = []
        self.run_patches: list[dict] = []
        self.messages: list[dict] = []
        self.turn_state: dict = {}

    async def get_repo(self):
        return {
            "owner": "o", "name": "n", "default_branch": "main",
            "ci_workflow_name": "CI", "live_url": "https://demo.example",
        }

    async def get_turn(self, turn_id):
        return self.turn_state

    async def patch_turn(self, turn_id, **f):
        self.turn_patches.append(f)
        return True

    async def patch_run(self, run_id, **f):
        self.run_patches.append(f)
        return True

    async def post_message(self, conversation_id, **kw):
        self.messages.append(kw)
        return True


class FakeMCP:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _event(kind="chat", autonomy="auto") -> ChatTaskEvent:
    return ChatTaskEvent(
        tenant_id="tenant_1", conversation_id="cnv_1", turn_id="turn_1",
        message="add a low-stock endpoint", autonomy=autonomy, kind=kind, run_id="run_1",
    )


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    """Fake all collaborators; keep autonomy_policy + pr_risk real."""
    events: list[tuple] = []

    async def fake_publish(stage, message, *, level="info", metadata=None):
        events.append((stage, level))

    async def fake_classify_llm(*a, **k):
        return '{"intent":"feature","summary":"add a low-stock endpoint"}'

    class FakeGemini:
        async def generate(self, *a, **k):
            return await fake_classify_llm()

    async def fake_generate_edit(instruction, mcp, *, ref="main"):
        return EditProposal(
            file_contents={"app/api/low_stock.py": "def low_stock():\n    return []\n"},
            diff="--- /dev/null\n+++ b/app/api/low_stock.py\n",
            summary="add a low-stock endpoint",
        )

    async def fake_open_pr(**kw):
        return SimpleNamespace(success=True, pr_number=7, pr_url="http://pr/7",
                               head_sha="abcdef1", error_message=None, branch_name=kw["branch"])

    async def fake_verify(*a, **k):
        return SimpleNamespace(verified=True, detail="CI passed (run 99)", failing_log=None)

    async def fake_judge(**kw):
        return SimpleNamespace(approve=True, confidence=0.9, is_high_confidence=True, reason="safe")

    merge_calls: list[int] = []

    async def fake_merge(pr_number, *, merge_method="squash", sha=None):
        merge_calls.append(pr_number)
        return (True, "mergedsha123")

    async def fake_deploy(image_tag):
        return SimpleNamespace(success=True, error_message=None, image_tag=image_tag)

    monkeypatch.setattr(co.event_publisher, "publish_safe", fake_publish)
    monkeypatch.setattr(co, "get_gemini_client", lambda: FakeGemini())
    monkeypatch.setattr(co.chat_editor, "generate_edit", fake_generate_edit)
    monkeypatch.setattr(co.pr_manager, "open_feature_pr", fake_open_pr)
    monkeypatch.setattr(co.ci_verifier, "verify_patch_ci", fake_verify)
    monkeypatch.setattr(co.deploy_guard, "judge", fake_judge)
    monkeypatch.setattr(co.rest_api, "merge_pull_request", fake_merge)
    monkeypatch.setattr(co.deployer, "deploy", fake_deploy)
    return SimpleNamespace(events=events, merge_calls=merge_calls)


def _orch(client):
    return ChatOrchestrator(client=client, mcp_factory=lambda: FakeMCP())


async def test_auto_ships_end_to_end(_wire) -> None:
    client = FakeClient()
    await _orch(client).handle_turn(_event(autonomy="auto"))

    statuses = [p["status"] for p in client.turn_patches if "status" in p]
    assert "running" in statuses
    assert "merging" in statuses
    assert statuses[-1] == "done"
    assert _wire.merge_calls == [7]  # merged unattended
    # The PR + a diff + a live-url/status message were surfaced.
    kinds = [m.get("kind") for m in client.messages]
    assert "diff" in kinds and ("live_url" in kinds or "status" in kinds)
    # A run row recorded the PR + verification.
    assert any(p.get("pr_number") == 7 for p in client.run_patches)
    assert any(p.get("verified") is True for p in client.run_patches)


async def test_manual_pauses_for_approval(_wire) -> None:
    client = FakeClient()
    await _orch(client).handle_turn(_event(autonomy="manual"))

    statuses = [p["status"] for p in client.turn_patches if "status" in p]
    assert statuses[-1] == "awaiting_approval"
    assert any(p.get("resume_token") for p in client.turn_patches)
    assert _wire.merge_calls == []  # did NOT ship
    assert any(m.get("kind") == "approval" for m in client.messages)


async def test_approve_resumes_and_ships(_wire) -> None:
    client = FakeClient()
    client.turn_state = {"status": "awaiting_approval", "pr_number": 7}
    await _orch(client).handle_turn(_event(kind="approve", autonomy="manual"))
    assert _wire.merge_calls == [7]
    assert [p["status"] for p in client.turn_patches if "status" in p][-1] == "done"


async def test_reject_marks_rejected(_wire) -> None:
    client = FakeClient()
    await _orch(client).handle_turn(_event(kind="reject"))
    assert [p["status"] for p in client.turn_patches if "status" in p][-1] == "rejected"
    assert _wire.merge_calls == []


async def test_blocked_edit_fails_turn(_wire, monkeypatch) -> None:
    async def cannot_edit(instruction, mcp, *, ref="main"):
        return EditProposal(cannot_reason="would touch a protected path")

    monkeypatch.setattr(co.chat_editor, "generate_edit", cannot_edit)
    client = FakeClient()
    await _orch(client).handle_turn(_event(autonomy="auto"))
    assert [p["status"] for p in client.turn_patches if "status" in p][-1] == "failed"
    assert _wire.merge_calls == []
