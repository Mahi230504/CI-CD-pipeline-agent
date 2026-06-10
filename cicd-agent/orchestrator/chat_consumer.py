"""Redis consumer for chat turns.

Pulls ChatTaskEvents off the demo backend's ``agent:tasks`` stream (a consumer
group, so it's restart-safe via the group offset and horizontally scalable) and
runs each through the ChatOrchestrator under a hard per-turn timeout — a wedged
turn can never pin the worker, mirroring the pipeline's degrade-don't-hang rule.

Chat turns deliberately do NOT go through orchestrator/task_queue.py: that queue
is keyed on integer GitHub run ids and persists to the event_store outbox, both
of which chat avoids (chat durability lives in the demo's `turns` table). The
global LLM rate limiter still serializes Gemini calls across chat + CI/CD work,
so running this alongside the webhook worker doesn't blow the rate budget.
"""

from __future__ import annotations

import asyncio
import logging

from redis.asyncio import Redis, from_url

from agents.chat_orchestrator import ChatOrchestrator
from config.settings import Settings, get_settings
from models.chat import ChatTaskEvent

logger = logging.getLogger("cicd_agent.chat_consumer")

_BLOCK_MS = 5000
_BATCH = 4


class ChatConsumer:
    def __init__(
        self,
        orchestrator: ChatOrchestrator | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._orchestrator = orchestrator or ChatOrchestrator()
        self._redis: Redis | None = None
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._redis = from_url(self._settings.redis_url, decode_responses=True)
        await self._ensure_group()
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "chat_consumer started (stream=%s group=%s)",
            self._settings.chat_tasks_stream,
            self._settings.chat_consumer_group,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._redis is not None:
            try:
                await self._redis.aclose()  # type: ignore[attr-defined]
            except Exception:
                pass
            self._redis = None
        logger.info("chat_consumer stopped")

    async def _ensure_group(self) -> None:
        assert self._redis is not None
        try:
            await self._redis.xgroup_create(
                name=self._settings.chat_tasks_stream,
                groupname=self._settings.chat_consumer_group,
                id="0",
                mkstream=True,
            )
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                raise

    async def _loop(self) -> None:
        assert self._redis is not None
        s = self._settings
        while self._running:
            try:
                messages = await self._redis.xreadgroup(
                    groupname=s.chat_consumer_group,
                    consumername=s.chat_consumer_name,
                    streams={s.chat_tasks_stream: ">"},
                    count=_BATCH,
                    block=_BLOCK_MS,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("chat_consumer xreadgroup error: %s", e)
                await asyncio.sleep(1.0)
                continue
            if not messages:
                continue
            for _stream, entries in messages:
                for entry_id, fields in entries:
                    await self._handle_entry(entry_id, fields)

    async def _handle_entry(self, entry_id: str, fields: dict[str, str]) -> None:
        s = self._settings
        try:
            event = ChatTaskEvent.from_stream_fields(fields)
        except ValueError as e:
            logger.warning("chat_consumer: dropping malformed entry %s: %s", entry_id, e)
            await self._ack(entry_id)
            return
        try:
            async with asyncio.timeout(s.session_timeout_seconds):
                await self._orchestrator.handle_turn(event)
        except asyncio.TimeoutError:
            logger.error("chat turn %s exceeded %ds — abandoning", event.turn_id, s.session_timeout_seconds)
            try:
                await self._orchestrator.client.patch_turn(
                    event.turn_id, status="failed", error="timed out"
                )
            except Exception:
                pass
        except asyncio.CancelledError:
            raise
        finally:
            # Always ack: the turn is durable in the DB; leaving it unacked would
            # reprocess on restart and double-open PRs.
            await self._ack(entry_id)

    async def _ack(self, entry_id: str) -> None:
        assert self._redis is not None
        try:
            await self._redis.xack(  # type: ignore[no-untyped-call]
                self._settings.chat_tasks_stream, self._settings.chat_consumer_group, entry_id
            )
        except Exception as e:
            logger.warning("chat_consumer ack failed for %s: %s", entry_id, e)


_consumer: ChatConsumer | None = None


def get_chat_consumer() -> ChatConsumer | None:
    return _consumer


async def start_chat_consumer() -> ChatConsumer | None:
    """Start the consumer if chat is enabled + Redis configured. Returns the
    consumer (or None when disabled). Safe to call once at startup."""
    global _consumer
    settings = get_settings()
    if not settings.chat_enabled:
        logger.info("chat disabled (CHAT_ENABLED unset) — consumer not started")
        return None
    _consumer = ChatConsumer(settings=settings)
    await _consumer.start()
    return _consumer


async def stop_chat_consumer() -> None:
    global _consumer
    if _consumer is not None:
        await _consumer.stop()
        _consumer = None
