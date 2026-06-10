"""Slice-3 tests: ChatTaskEvent wire parsing + EditProposal (pure, no env)."""

from __future__ import annotations

import pytest

from models.chat import ChatTaskEvent, EditProposal


def test_from_stream_fields_full() -> None:
    evt = ChatTaskEvent.from_stream_fields(
        {
            "tenant_id": "tenant_1",
            "conversation_id": "cnv_5",
            "turn_id": "turn_9",
            "run_id": "run_3",
            "message": "add a low-stock endpoint",
            "autonomy": "auto",
            "kind": "chat",
        }
    )
    assert evt.conversation_id == "cnv_5"
    assert evt.run_id == "run_3"
    assert evt.autonomy == "auto"
    assert evt.kind == "chat"
    assert evt.log_context["turn_id"] == "turn_9"


def test_null_run_id_becomes_none() -> None:
    # The demo encodes None as the JSON literal "null".
    evt = ChatTaskEvent.from_stream_fields(
        {
            "tenant_id": "tenant_1",
            "conversation_id": "cnv_5",
            "turn_id": "turn_9",
            "run_id": "null",
            "message": "approve",
            "autonomy": "manual",
            "kind": "approve",
        }
    )
    assert evt.run_id is None
    assert evt.kind == "approve"


def test_defaults_when_optional_missing() -> None:
    evt = ChatTaskEvent.from_stream_fields(
        {"tenant_id": "tenant_1", "conversation_id": "cnv_1", "turn_id": "turn_1", "message": "hi"}
    )
    assert evt.autonomy == "manual"
    assert evt.kind == "chat"
    assert evt.run_id is None


def test_missing_required_raises() -> None:
    with pytest.raises(ValueError):
        ChatTaskEvent.from_stream_fields({"tenant_id": "tenant_1", "turn_id": "turn_1"})


def test_edit_proposal_actionable() -> None:
    p = EditProposal(file_contents={"a.py": "x = 1\n"}, diff="d", summary="s")
    assert p.is_actionable is True
    assert p.files == ["a.py"]
    assert EditProposal().is_actionable is False
    assert EditProposal(
        file_contents={"a.py": "x"}, cannot_reason="blocked"
    ).is_actionable is False
