"""
Persistent webhook outbox — events are written here before the in-memory queue
sees them. On startup, any event still in state='queued' or 'processing' is
re-enqueued so a server restart between dequeue and pipeline completion
doesn't silently drop work.

Schema (one table):
    queued_events
      id           INTEGER PRIMARY KEY AUTOINCREMENT
      run_id       INTEGER NOT NULL
      payload      TEXT     NOT NULL  -- json-encoded WorkflowFailureEvent fields
      state        TEXT     NOT NULL  -- queued | processing | done
      enqueued_at  TEXT     NOT NULL  (iso8601)
      started_at   TEXT
      finished_at  TEXT
      UNIQUE(run_id)                  -- dedup on run_id across the lifetime of the store

Backed by sqlite3 (stdlib). No new deps. Synchronous I/O — the calls are tiny
and happen at boundary moments (enqueue / start / finish), not in hot loops.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from models.events import WorkflowFailureEvent

logger = logging.getLogger("cicd_agent.event_store")

_RETAIN_DONE_DAYS = 7


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event_to_payload(event: WorkflowFailureEvent) -> str:
    return json.dumps(
        {
            "run_id": event.run_id,
            "repo_owner": event.repo_owner,
            "repo_name": event.repo_name,
            "workflow_name": event.workflow_name,
            "branch": event.branch,
            "head_sha": event.head_sha,
            "html_url": event.html_url,
            "sender_login": event.sender_login,
        }
    )


def _payload_to_event(payload: str) -> WorkflowFailureEvent | None:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    try:
        return WorkflowFailureEvent(
            run_id=int(data["run_id"]),
            repo_owner=str(data["repo_owner"]),
            repo_name=str(data["repo_name"]),
            workflow_name=str(data["workflow_name"]),
            branch=str(data["branch"]),
            head_sha=str(data["head_sha"]),
            html_url=str(data["html_url"]),
            sender_login=str(data.get("sender_login", "")),
        )
    except (KeyError, TypeError, ValueError):
        return None


class EventStore:
    def __init__(self, path: Path | str = "queue.sqlite3") -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS queued_events (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id       INTEGER NOT NULL UNIQUE,
                    payload      TEXT    NOT NULL,
                    state        TEXT    NOT NULL,
                    enqueued_at  TEXT    NOT NULL,
                    started_at   TEXT,
                    finished_at  TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS ix_state ON queued_events(state)")

    def enqueue(self, event: WorkflowFailureEvent) -> bool:
        """Persist an event in `queued` state. Returns False if the run_id
        is already present (no-op so retries are idempotent)."""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO queued_events
                    (run_id, payload, state, enqueued_at)
                VALUES (?, ?, 'queued', ?)
                """,
                (event.run_id, _event_to_payload(event), _now_iso()),
            )
            return cur.rowcount > 0

    def mark_processing(self, run_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE queued_events
                   SET state='processing', started_at=?
                 WHERE run_id=? AND state IN ('queued','processing')
                """,
                (_now_iso(), run_id),
            )

    def mark_done(self, run_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE queued_events
                   SET state='done', finished_at=?
                 WHERE run_id=?
                """,
                (_now_iso(), run_id),
            )

    def list_unfinished(self) -> Iterable[WorkflowFailureEvent]:
        """All events not yet marked done — used on startup to replay survivors."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT payload FROM queued_events
                 WHERE state IN ('queued','processing')
                 ORDER BY id ASC
                """
            ).fetchall()
        for (payload,) in rows:
            ev = _payload_to_event(payload)
            if ev is not None:
                yield ev

    def cleanup_done(self, retention_days: int = _RETAIN_DONE_DAYS) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM queued_events WHERE state='done' AND finished_at < ?",
                (cutoff,),
            )
            return cur.rowcount or 0


_store: EventStore | None = None


def init_event_store(path: Path | str) -> EventStore:
    global _store
    _store = EventStore(path)
    logger.info("event_store initialised at %s", _store._path)
    return _store


def get_event_store() -> EventStore:
    if _store is None:
        raise RuntimeError("event_store not initialised — call init_event_store() at startup")
    return _store


def reset_for_testing() -> None:
    global _store
    _store = None
