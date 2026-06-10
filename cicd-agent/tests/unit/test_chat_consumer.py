"""Slice-3 tests: the Redis chat consumer.

fakeredis + a fake orchestrator + a SimpleNamespace settings — no real Redis,
no real .env, no HTTP. Drives _handle_entry directly (the per-entry path) so we
don't spin the infinite read loop.
"""

from __future__ import annotations

from types import SimpleNamespace

import fakeredis.aioredis as fakeredis_aio
import pytest_asyncio

from models.chat import ChatTaskEvent
from orchestrator.chat_consumer import ChatConsumer


class FakeOrchestrator:
    def __init__(self) -> None:
        self.handled: list[ChatTaskEvent] = []
        self.client = SimpleNamespace()

    async def handle_turn(self, event: ChatTaskEvent) -> None:
        self.handled.append(event)


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        redis_url="redis://unused",
        chat_tasks_stream="agent:tasks",
        chat_consumer_group="agent-chat",
        chat_consumer_name="agent-1",
        session_timeout_seconds=5,
    )


@pytest_asyncio.fixture
async def consumer():
    orch = FakeOrchestrator()
    c = ChatConsumer(orchestrator=orch, settings=_settings())
    c._redis = fakeredis_aio.FakeRedis(decode_responses=True)
    await c._ensure_group()
    yield c, orch
    await c._redis.aclose()


async def test_valid_entry_dispatched_and_acked(consumer) -> None:
    c, orch = consumer
    fields = {
        "tenant_id": "tenant_1",
        "conversation_id": "cnv_1",
        "turn_id": "turn_1",
        "run_id": "run_1",
        "message": "add endpoint",
        "autonomy": "auto",
        "kind": "chat",
    }
    entry_id = await c._redis.xadd("agent:tasks", fields)
    # Read into the group so the entry is pending, then handle it.
    await c._redis.xreadgroup(
        groupname="agent-chat", consumername="agent-1", streams={"agent:tasks": ">"}, count=10
    )
    await c._handle_entry(entry_id, fields)

    assert len(orch.handled) == 1
    assert orch.handled[0].turn_id == "turn_1"
    # Acked → no longer pending.
    pending = await c._redis.xpending("agent:tasks", "agent-chat")
    assert pending["pending"] == 0


async def test_malformed_entry_dropped(consumer) -> None:
    c, orch = consumer
    bad = {"tenant_id": "tenant_1", "turn_id": "turn_1"}  # missing conversation_id + message
    entry_id = await c._redis.xadd("agent:tasks", bad)
    await c._redis.xreadgroup(
        groupname="agent-chat", consumername="agent-1", streams={"agent:tasks": ">"}, count=10
    )
    await c._handle_entry(entry_id, bad)
    assert orch.handled == []  # never dispatched
    pending = await c._redis.xpending("agent:tasks", "agent-chat")
    assert pending["pending"] == 0  # acked-and-skipped
