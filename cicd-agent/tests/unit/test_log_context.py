"""Tests for the contextvars-based logging context and the JSON formatter."""

from __future__ import annotations

import json
import logging

import pytest

from audit.context import clear_run_context, get_run_context, set_run_context
from audit.setup import ContextFilter, JsonFormatter


@pytest.fixture(autouse=True)
def _isolate_context() -> None:
    clear_run_context()
    yield
    clear_run_context()


def test_context_defaults_to_none() -> None:
    ctx = get_run_context()
    assert ctx.run_id is None
    assert ctx.error_hash is None
    assert ctx.phase is None


def test_set_and_get_context() -> None:
    set_run_context(run_id=42, phase="code_patch", error_hash="abc123def456")
    ctx = get_run_context()
    assert ctx.run_id == 42
    assert ctx.phase == "code_patch"
    assert ctx.error_hash == "abc123def456"


def test_set_context_does_not_clear_other_fields() -> None:
    set_run_context(run_id=42, phase="code_patch")
    set_run_context(phase="notify")
    ctx = get_run_context()
    assert ctx.run_id == 42  # unchanged
    assert ctx.phase == "notify"


def test_set_context_empty_string_clears_field() -> None:
    set_run_context(phase="code_patch")
    set_run_context(phase="")  # sentinel: explicit clear
    assert get_run_context().phase is None


def test_clear_context() -> None:
    set_run_context(run_id=42, phase="x", error_hash="y")
    clear_run_context()
    ctx = get_run_context()
    assert ctx.run_id is None
    assert ctx.phase is None
    assert ctx.error_hash is None


def _make_record(level: int = logging.INFO, msg: str = "hello") -> logging.LogRecord:
    return logging.LogRecord(
        name="test", level=level, pathname=__file__, lineno=1,
        msg=msg, args=(), exc_info=None,
    )


def test_context_filter_attaches_attributes() -> None:
    set_run_context(run_id=99, phase="diagnose", error_hash="deadbeef")
    record = _make_record()
    ContextFilter().filter(record)
    assert record.run_id == 99
    assert record.phase == "diagnose"
    assert record.error_hash == "deadbeef"
    assert record.ctxprefix == " [run=99 phase=diagnose err=deadbeef]"


def test_context_filter_no_context_no_prefix() -> None:
    record = _make_record()
    ContextFilter().filter(record)
    assert record.ctxprefix == ""
    assert record.run_id is None


def test_json_formatter_emits_valid_json_with_context() -> None:
    set_run_context(run_id=7, phase="notify")
    record = _make_record(msg="finished")
    ContextFilter().filter(record)
    out = JsonFormatter().format(record)
    data = json.loads(out)
    assert data["run_id"] == 7
    assert data["phase"] == "notify"
    assert data["msg"] == "finished"
    assert data["level"] == "INFO"
    assert data["logger"] == "test"


def test_json_formatter_omits_unset_fields() -> None:
    record = _make_record()
    ContextFilter().filter(record)
    data = json.loads(JsonFormatter().format(record))
    assert "run_id" not in data
    assert "phase" not in data
    assert "error_hash" not in data
