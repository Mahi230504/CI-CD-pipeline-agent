"""Tests for the persistent webhook outbox (orchestrator/event_store.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from models.events import WorkflowFailureEvent
from orchestrator.event_store import EventStore


def _event(run_id: int) -> WorkflowFailureEvent:
    return WorkflowFailureEvent(
        run_id=run_id,
        repo_owner="acme",
        repo_name="widgets",
        workflow_name="CI",
        branch="main",
        head_sha="abc1234567890def",
        html_url=f"https://github.com/acme/widgets/actions/runs/{run_id}",
        sender_login="user",
    )


@pytest.fixture
def store(tmp_path: Path) -> EventStore:
    return EventStore(tmp_path / "queue.sqlite3")


def test_enqueue_persists_event(store: EventStore) -> None:
    assert store.enqueue(_event(1)) is True
    unfinished = list(store.list_unfinished())
    assert len(unfinished) == 1
    assert unfinished[0].run_id == 1


def test_duplicate_enqueue_is_idempotent(store: EventStore) -> None:
    assert store.enqueue(_event(7)) is True
    assert store.enqueue(_event(7)) is False  # second insert is a no-op
    assert len(list(store.list_unfinished())) == 1


def test_mark_done_excludes_from_unfinished(store: EventStore) -> None:
    store.enqueue(_event(1))
    store.enqueue(_event(2))
    store.mark_done(1)
    unfinished = list(store.list_unfinished())
    assert [e.run_id for e in unfinished] == [2]


def test_mark_processing_still_unfinished(store: EventStore) -> None:
    """Events in `processing` state must replay on restart — we crashed mid-pipeline."""
    store.enqueue(_event(42))
    store.mark_processing(42)
    unfinished = list(store.list_unfinished())
    assert [e.run_id for e in unfinished] == [42]


def test_round_trip_preserves_event_fields(store: EventStore) -> None:
    original = _event(100)
    store.enqueue(original)
    [replayed] = list(store.list_unfinished())
    assert replayed.run_id == original.run_id
    assert replayed.repo_owner == original.repo_owner
    assert replayed.repo_name == original.repo_name
    assert replayed.workflow_name == original.workflow_name
    assert replayed.branch == original.branch
    assert replayed.head_sha == original.head_sha
    assert replayed.html_url == original.html_url
    assert replayed.sender_login == original.sender_login


def test_persistence_across_instances(tmp_path: Path) -> None:
    """A new EventStore against the same file sees the prior store's events."""
    path = tmp_path / "queue.sqlite3"
    s1 = EventStore(path)
    s1.enqueue(_event(11))

    s2 = EventStore(path)
    unfinished = list(s2.list_unfinished())
    assert [e.run_id for e in unfinished] == [11]
