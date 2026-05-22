"""Unit tests for github/log_fetcher.slice_log() and find_failed_step()."""

from __future__ import annotations

from config.constants import MAX_LOG_CHARS
from github.log_fetcher import (
    find_failed_step,
    has_infra_error,
    slice_log,
    token_guard,
)


def test_finds_error_prefix():
    log = "line 1\nline 2\n##[error]something bad\nline 4"
    line, _ = find_failed_step(log)
    assert line == 2


def test_finds_failed_keyword():
    log = "starting tests\ntests/foo.py::bar FAILED\nthe rest"
    line, _ = find_failed_step(log)
    assert line == 1


def test_finds_nonzero_exit():
    log = "starting tests\nProcess completed with exit code 1.\n"
    line, _ = find_failed_step(log)
    assert line == 1


def test_empty_log():
    line, step = find_failed_step("")
    assert line == 0
    assert step == "unknown"


def test_extracts_step_name():
    log = "##[group]Run pytest tests/\nsome output\n##[error]Process completed with exit code 1.\n"
    _, step = find_failed_step(log)
    assert "pytest" in step


def test_window_centered():
    log = "\n".join(f"line {i}" for i in range(100))
    sliced = slice_log(log, error_line=50, window=30)
    assert len(sliced.split("\n")) == 61


def test_window_at_start():
    log = "\n".join(f"line {i}" for i in range(100))
    sliced = slice_log(log, error_line=2, window=30)
    sliced_lines = sliced.split("\n")
    assert len(sliced_lines) == 33
    assert sliced_lines[0].lstrip().startswith("0 ")


def test_window_at_end():
    log = "\n".join(f"line {i}" for i in range(100))
    sliced = slice_log(log, error_line=99, window=30)
    sliced_lines = sliced.split("\n")
    assert len(sliced_lines) <= 31


def test_line_numbers_prefixed():
    log = "alpha\nbeta\ngamma"
    sliced = slice_log(log, error_line=1, window=5)
    assert " | " in sliced


def test_empty_log_returns_something():
    assert slice_log("", 0) == ""


def test_short_text_unchanged():
    short = "x" * 100
    assert token_guard(short) == short


def test_long_text_truncated():
    long_text = "x" * (MAX_LOG_CHARS + 1000)
    out = token_guard(long_text)
    assert "TRUNCATED" in out
    assert len(out) < len(long_text)


def test_truncated_length():
    long_text = "x" * (MAX_LOG_CHARS + 5000)
    out = token_guard(long_text)
    assert len(out) <= MAX_LOG_CHARS + 200


def test_detects_disk_full():
    assert has_infra_error("ERROR: no space left on device") is True


def test_detects_oom():
    assert has_infra_error("Process killed: out of memory") is True


def test_detects_network_timeout():
    assert has_infra_error("network timeout while connecting") is True


def test_clean_log():
    assert has_infra_error("ZeroDivisionError at line 47") is False


def test_case_insensitive():
    assert has_infra_error("NO SPACE LEFT ON DEVICE") is True
