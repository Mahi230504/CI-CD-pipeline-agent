"""
Unit tests for the open-PR dedup methods added to RunRegistry in Phase 1.

Covers record_open_pr / get_open_pr / clear_open_pr, plus interaction with
the existing escalation flag and attempt counter.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.run_registry import RunRegistry


@pytest.fixture
def registry(tmp_path: Path) -> RunRegistry:
    return RunRegistry(registry_path=tmp_path / "reg.json")


def test_open_pr_initially_none(registry: RunRegistry):
    assert registry.get_open_pr("abc123") is None


def test_record_and_get_open_pr(registry: RunRegistry):
    registry.record_open_pr("hash1", pr_number=42, pr_url="https://example/pr/42")
    result = registry.get_open_pr("hash1")
    assert result == (42, "https://example/pr/42")


def test_record_open_pr_with_no_url(registry: RunRegistry):
    registry.record_open_pr("hash1", pr_number=7, pr_url=None)
    assert registry.get_open_pr("hash1") == (7, None)


def test_record_open_pr_overwrites(registry: RunRegistry):
    registry.record_open_pr("hash1", 1, "url1")
    registry.record_open_pr("hash1", 2, "url2")
    assert registry.get_open_pr("hash1") == (2, "url2")


def test_clear_open_pr(registry: RunRegistry):
    registry.record_open_pr("hash1", 5, "u")
    registry.clear_open_pr("hash1")
    assert registry.get_open_pr("hash1") is None


def test_clear_open_pr_no_entry_is_noop(registry: RunRegistry):
    # Should not raise if the hash has no entry yet.
    registry.clear_open_pr("never-seen")
    assert registry.get_open_pr("never-seen") is None


def test_open_pr_survives_attempt_increment(registry: RunRegistry):
    registry.record_open_pr("hash1", 11, "url")
    registry.increment_attempt("hash1")
    registry.increment_attempt("hash1")
    # Attempt counter unrelated to open PR pointer.
    assert registry.get_attempt_count("hash1") == 2
    assert registry.get_open_pr("hash1") == (11, "url")


def test_open_pr_survives_persistence_reload(tmp_path: Path):
    path = tmp_path / "reg.json"
    reg1 = RunRegistry(registry_path=path)
    reg1.record_open_pr("h", 99, "u")
    reg1.increment_attempt("h")

    reg2 = RunRegistry(registry_path=path)
    assert reg2.get_open_pr("h") == (99, "u")
    assert reg2.get_attempt_count("h") == 1


def test_mark_escalated_preserves_open_pr_pointer(registry: RunRegistry):
    registry.record_open_pr("h", 7, "url")
    registry.mark_escalated("h")
    # Escalation must keep the open PR pointer so dedup still works on next run.
    assert registry.is_escalated("h") is True
    assert registry.get_open_pr("h") == (7, "url")
