"""Unit tests for orchestrator/run_registry.py — deduplication and attempt counting."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from orchestrator.run_registry import RunRegistry


def test_fresh_registry_no_duplicates(tmp_path):
    reg = RunRegistry(tmp_path / "reg.json")
    assert reg.is_duplicate(1) is False
    assert reg.is_duplicate(42) is False


def test_marks_and_detects_duplicate(tmp_path):
    reg = RunRegistry(tmp_path / "reg.json")
    reg.mark_run_processed(42, "done")
    assert reg.is_duplicate(42) is True


def test_different_run_ids_independent(tmp_path):
    reg = RunRegistry(tmp_path / "reg.json")
    reg.mark_run_processed(1, "done")
    assert reg.is_duplicate(1) is True
    assert reg.is_duplicate(2) is False


def test_initial_count_zero(tmp_path):
    reg = RunRegistry(tmp_path / "reg.json")
    assert reg.get_attempt_count("hash_a") == 0


def test_increment_returns_new_count(tmp_path):
    reg = RunRegistry(tmp_path / "reg.json")
    assert reg.increment_attempt("hash_a") == 1
    assert reg.increment_attempt("hash_a") == 2


def test_max_attempts_gate(tmp_path):
    reg = RunRegistry(tmp_path / "reg.json")
    reg.increment_attempt("hash_b")
    reg.increment_attempt("hash_b")
    assert reg.get_attempt_count("hash_b") == 2


def test_escalation_flag(tmp_path):
    reg = RunRegistry(tmp_path / "reg.json")
    reg.mark_escalated("hash_c")
    assert reg.is_escalated("hash_c") is True


def test_not_escalated_by_default(tmp_path):
    reg = RunRegistry(tmp_path / "reg.json")
    reg.increment_attempt("hash_d")
    assert reg.is_escalated("hash_d") is False


def test_survives_reload(tmp_path):
    path = tmp_path / "reg.json"
    reg = RunRegistry(path)
    reg.mark_run_processed(123, "done")
    reg.increment_attempt("hash_x")
    reg.increment_attempt("hash_x")
    reg.mark_escalated("hash_x")

    reg2 = RunRegistry(path)
    assert reg2.is_duplicate(123) is True
    assert reg2.get_attempt_count("hash_x") == 2
    assert reg2.is_escalated("hash_x") is True


def test_empty_registry_file_handled(tmp_path):
    path = tmp_path / "does_not_exist.json"
    reg = RunRegistry(path)
    assert reg.is_duplicate(1) is False
    assert reg.get_attempt_count("anything") == 0


def test_old_entries_removed(tmp_path):
    path = tmp_path / "reg.json"
    reg = RunRegistry(path)
    old_iso = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    reg._data["runs"]["old_run"] = {"processed_at": old_iso, "state": "done"}
    reg._save()

    reg.mark_run_processed(999, "done")

    assert "old_run" not in reg._data["runs"]
    assert "999" in reg._data["runs"]
