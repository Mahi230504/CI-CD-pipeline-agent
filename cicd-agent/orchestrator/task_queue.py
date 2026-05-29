"""
Async task queue — serializes incoming webhook events.

Uses asyncio.Queue to ensure only one pipeline run executes at a time.
This is required on the Gemini free tier (rate limits prevent parallelism).

Features:
- Max queue depth: 10 (rejects new events if exceeded — returns 429)
- Worker coroutine: started on server boot via FastAPI lifespan
- Graceful drain: on SIGTERM, finishes current task then stops
- Dead letter queue: failed tasks written to logs/dead_letter.jsonl
- queue_depth() → int: exposed for /status endpoint
"""

from __future__ import annotations

import asyncio
import logging

from audit.logger import get_audit_logger
from config.constants import MAX_QUEUE_DEPTH
from metrics import queue_depth
from models.cd import ReleaseSuccessEvent
from models.events import WorkflowFailureEvent
from orchestrator.event_store import get_event_store

# A queue item is either a CI failure (persisted, replayable on restart) or a
# CD release success (in-memory only — re-deploying a stale SHA after a crash
# would be unsafe; the next release fires its own event).
QueueEvent = WorkflowFailureEvent | ReleaseSuccessEvent

logger = logging.getLogger(__name__)

_WORKER_POLL_TIMEOUT_SECONDS = 1.0
_STOP_TIMEOUT_SECONDS = 30.0


class TaskQueue:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[QueueEvent] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None
        self._running: bool = False
        self._processed: int = 0
        self._failed: int = 0

    @property
    def depth(self) -> int:
        return self._queue.qsize()

    @property
    def processed(self) -> int:
        return self._processed

    @property
    def failed(self) -> int:
        return self._failed

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._worker_task = asyncio.create_task(self._worker())
        logger.info("task_queue worker started")

    async def stop(self) -> None:
        self._running = False
        if self._worker_task is None:
            return
        try:
            await asyncio.wait_for(self._worker_task, timeout=_STOP_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning("task_queue worker did not stop within %ds — cancelling", _STOP_TIMEOUT_SECONDS)
            self._worker_task.cancel()
            try:
                await self._worker_task
            except (asyncio.CancelledError, Exception):
                pass
        except Exception as e:
            logger.warning("task_queue worker stopped with error: %s", e)
        finally:
            self._worker_task = None
            logger.info(
                "task_queue stopped: processed=%d failed=%d",
                self._processed,
                self._failed,
            )

    async def enqueue(self, event: QueueEvent) -> bool:
        if self._queue.qsize() >= MAX_QUEUE_DEPTH:
            logger.warning(
                "task_queue full (%d/%d) — rejecting run %d",
                self._queue.qsize(),
                MAX_QUEUE_DEPTH,
                event.run_id,
            )
            return False
        # CI failure events get persisted to the durable store so a crash
        # between accept and pickup doesn't lose them. CD release events
        # are in-memory only: re-deploying after a crash could ship a
        # stale SHA, and the next release fires its own webhook anyway.
        if isinstance(event, WorkflowFailureEvent):
            try:
                inserted = get_event_store().enqueue(event)
                if not inserted:
                    logger.info(
                        "task_queue: run %d already persisted — skipping duplicate enqueue",
                        event.run_id,
                    )
                    return True  # idempotent
            except RuntimeError:
                # event_store not initialised — happens in unit tests that don't
                # go through the FastAPI lifespan. Fall through to in-memory only.
                pass
        await self._queue.put(event)
        queue_depth.set(self._queue.qsize())
        logger.info(
            "Enqueued %s run %d. Queue depth: %d",
            "release" if isinstance(event, ReleaseSuccessEvent) else "failure",
            event.run_id,
            self._queue.qsize(),
        )
        return True

    async def replay_unfinished(self) -> int:
        """On startup, re-load events that were persisted but never marked done.
        Returns the count replayed."""
        try:
            store = get_event_store()
        except RuntimeError:
            return 0
        replayed = 0
        for event in store.list_unfinished():
            await self._queue.put(event)
            replayed += 1
        if replayed:
            queue_depth.set(self._queue.qsize())
            logger.info("task_queue: replayed %d unfinished event(s)", replayed)
        return replayed

    async def _worker(self) -> None:
        # Imported lazily so unit tests that don't exercise the pipeline
        # don't pay the import cost (and don't drag in MCP transport deps).
        from orchestrator.cd_pipeline import run_cd_pipeline
        from orchestrator.pipeline import run_pipeline

        logger.info("task_queue worker loop entered")
        while self._running or not self._queue.empty():
            try:
                event = await asyncio.wait_for(
                    self._queue.get(), timeout=_WORKER_POLL_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                logger.info("task_queue worker cancelled")
                raise
            queue_depth.set(self._queue.qsize())

            is_release = isinstance(event, ReleaseSuccessEvent)
            try:
                if not is_release:
                    try:
                        get_event_store().mark_processing(event.run_id)
                    except RuntimeError:
                        pass
                if is_release:
                    await run_cd_pipeline(event)
                else:
                    await run_pipeline(event)
                self._processed += 1
            except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                raise
            except BaseException as e:
                # Catch BaseExceptionGroup too (anyio task-group failures in MCP).
                # Without this, an ExceptionGroup escapes the worker and kills the
                # whole process. Log, dead-letter, keep the worker alive.
                self._failed += 1
                logger.error(
                    "task_queue: pipeline error for %s run %d (%s): %s",
                    "release" if is_release else "failure",
                    event.run_id,
                    type(e).__name__,
                    e,
                    exc_info=True,
                )
                err_for_dl = e if isinstance(e, Exception) else Exception(repr(e))
                await self._dead_letter(event, err_for_dl)
            finally:
                # CI events are durably tracked; mark done so they don't replay
                # on restart. CD events were never persisted, so nothing to clear.
                if not is_release:
                    try:
                        get_event_store().mark_done(event.run_id)
                    except RuntimeError:
                        pass
                self._queue.task_done()

        logger.info("task_queue worker stopped")

    async def _dead_letter(
        self,
        event: QueueEvent,
        error: Exception,
    ) -> None:
        try:
            get_audit_logger().log_dead_letter(
                run_id=event.run_id,
                reason=str(error),
                raw_event=event.log_context,
            )
        except Exception as e:
            logger.error("dead letter recording failed for run %d: %s", event.run_id, e)


_task_queue: TaskQueue | None = None


def init_task_queue() -> None:
    global _task_queue
    _task_queue = TaskQueue()
    logger.info("task_queue initialised")


def get_task_queue() -> TaskQueue:
    if _task_queue is None:
        raise RuntimeError(
            "task_queue not initialised — call init_task_queue() at startup"
        )
    return _task_queue


async def enqueue_event(event: QueueEvent) -> bool:
    return await get_task_queue().enqueue(event)
