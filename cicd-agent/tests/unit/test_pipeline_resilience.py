"""The orchestrator degrades instead of hanging.

``_run_phase`` runs a pipeline phase under a hard timeout and, on expiry,
abandons it and returns the safe fallback so the pipeline keeps moving. This is
the belt-and-suspenders layer behind "never get stuck anywhere, whatever the
reason": even an await we didn't anticipate can't freeze a run.
"""

from __future__ import annotations

import asyncio
import time

from models.events import WorkflowFailureEvent
from orchestrator import pipeline


def _event() -> WorkflowFailureEvent:
    return WorkflowFailureEvent(
        run_id=1,
        repo_owner="o",
        repo_name="r",
        workflow_name="CI",
        branch="main",
        head_sha="ab" * 20,
        html_url="http://x",
        sender_login="u",
    )


async def test_run_phase_returns_fallback_on_timeout(monkeypatch):
    published: list[tuple[str, str, str]] = []

    async def _fake_publish(event, stage, message, *, level="info", meta=None):
        published.append((stage, level, message))

    monkeypatch.setattr(pipeline, "_publish", _fake_publish)

    async def _hang():
        await asyncio.sleep(3600)

    started = time.monotonic()
    result = await pipeline._run_phase(
        _hang(),
        timeout=0.05,
        fallback="FELL_BACK",
        label="diagnosis",
        event=_event(),
        stage="diagnosis",
        timeout_message="root-cause analysis timed out",
    )
    elapsed = time.monotonic() - started

    assert result == "FELL_BACK"
    assert elapsed < 1.0  # bounded by the 0.05s budget, not the 3600s sleep
    assert published and published[0][1] == "warn"  # surfaced a warning


async def test_run_phase_passes_through_result(monkeypatch):
    async def _fake_publish(*args, **kwargs):
        return None

    monkeypatch.setattr(pipeline, "_publish", _fake_publish)

    async def _work():
        return 42

    result = await pipeline._run_phase(
        _work(),
        timeout=5,
        fallback=0,
        label="diagnosis",
        event=_event(),
        stage="diagnosis",
        timeout_message="unused",
    )
    assert result == 42
