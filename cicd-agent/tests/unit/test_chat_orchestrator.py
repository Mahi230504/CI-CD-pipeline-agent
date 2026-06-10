"""Slice-3 tests: the ChatOrchestrator turn lifecycle (skeleton).

Uses a fake ConsoleApiClient (no HTTP) and stubs event_publisher.publish_safe
so nothing touches the real .env / backend (dotenv-isolation hazard).
"""

from __future__ import annotations

import pytest

from agents import chat_orchestrator as co_module
from agents.chat_orchestrator import ChatOrchestrator
from models.chat import ChatTaskEvent


class FakeClient:
    def __init__(self, fail_on_message: bool = False) -> None:
        self.turn_patches: list[dict] = []
        self.messages: list[dict] = []
        self.fail_on_message = fail_on_message

    async def patch_turn(self, turn_id: str, **fields) -> bool:
        self.turn_patches.append({"turn_id": turn_id, **fields})
        return True

    async def patch_run(self, run_id: str, **fields) -> bool:
        return True

    async def post_message(self, conversation_id: str, **kw) -> bool:
        if self.fail_on_message:
            raise RuntimeError("boom")
        self.messages.append({"conversation_id": conversation_id, **kw})
        return True

    async def get_repo(self):
        return {"owner": "o", "name": "n"}


def _event(kind: str = "chat") -> ChatTaskEvent:
    return ChatTaskEvent(
        tenant_id="tenant_1",
        conversation_id="cnv_1",
        turn_id="turn_1",
        message="add endpoint",
        autonomy="auto",
        kind=kind,
        run_id="run_1",
    )


@pytest.fixture(autouse=True)
def _stub_publish(monkeypatch) -> list:
    """Capture emitted events instead of POSTing them anywhere."""
    captured: list[tuple] = []

    async def fake_publish_safe(stage, message, *, level="info", metadata=None):
        captured.append((stage, level, message))

    monkeypatch.setattr(co_module.event_publisher, "publish_safe", fake_publish_safe)
    return captured


async def test_chat_turn_runs_then_done(_stub_publish) -> None:
    client = FakeClient()
    await ChatOrchestrator(client=client).handle_turn(_event("chat"))

    statuses = [p.get("status") for p in client.turn_patches]
    assert statuses[0] == "running"
    assert statuses[-1] == "done"
    assert any(m["kind"] == "status" for m in client.messages)
    assert any(stage == "chat_received" for stage, _l, _m in _stub_publish)


async def test_approve_marks_done(_stub_publish) -> None:
    client = FakeClient()
    await ChatOrchestrator(client=client).handle_turn(_event("approve"))
    assert client.turn_patches[-1]["status"] == "done"
    assert any(stage == "chat_approved" for stage, _l, _m in _stub_publish)


async def test_reject_marks_rejected(_stub_publish) -> None:
    client = FakeClient()
    await ChatOrchestrator(client=client).handle_turn(_event("reject"))
    assert client.turn_patches[-1]["status"] == "rejected"


async def test_exception_fails_turn(_stub_publish) -> None:
    client = FakeClient(fail_on_message=True)  # post_message raises mid-turn
    await ChatOrchestrator(client=client).handle_turn(_event("chat"))
    # handle_turn never raises; the turn ends FAILED with an error recorded.
    last = client.turn_patches[-1]
    assert last["status"] == "failed"
    assert "error" in last
    assert any(stage == "chat_error" for stage, _l, _m in _stub_publish)
