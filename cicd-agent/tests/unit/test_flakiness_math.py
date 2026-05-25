"""Tests for the pass-rate and same-SHA helpers used by the flakiness detector.

Discovered live during the first end-to-end run: skipped runs were depressing
the pass rate so a real bug got marked flaky. These tests pin the corrected
semantics so we don't regress."""

from __future__ import annotations

from github.run_history import compute_pass_rate, had_success_at_sha


def _run(conclusion: str, head_sha: str = "deadbeef") -> dict:
    return {"conclusion": conclusion, "head_sha": head_sha}


# ────────────────────────────── compute_pass_rate ─────────────────────────────


def test_empty_runs_returns_zero() -> None:
    assert compute_pass_rate([]) == 0.0


def test_all_success() -> None:
    assert compute_pass_rate([_run("success")] * 3) == 1.0


def test_all_failure() -> None:
    assert compute_pass_rate([_run("failure")] * 3) == 0.0


def test_skipped_runs_excluded_from_denominator() -> None:
    runs = [_run("success"), _run("skipped"), _run("failure"), _run("skipped"), _run("success")]
    # 2 success, 1 failure decisive → 2/3
    assert compute_pass_rate(runs) == pytest_approx(2 / 3)


def test_cancelled_runs_excluded() -> None:
    runs = [_run("success"), _run("cancelled"), _run("failure")]
    assert compute_pass_rate(runs) == 0.5


def test_neutral_runs_excluded() -> None:
    runs = [_run("success"), _run("neutral"), _run("failure"), _run("neutral")]
    assert compute_pass_rate(runs) == 0.5


def test_no_decisive_runs_returns_zero() -> None:
    """5 skipped runs in a row carry no signal — pass_rate is undefined."""
    assert compute_pass_rate([_run("skipped")] * 5) == 0.0


def test_ignores_non_dict_entries() -> None:
    runs = [_run("success"), None, "garbage", _run("failure")]  # type: ignore[list-item]
    assert compute_pass_rate(runs) == 0.5


# ─────────────────────────── had_success_at_sha ───────────────────────────────


def test_no_runs_no_success_at_sha() -> None:
    assert had_success_at_sha([], "abc") is False


def test_empty_head_sha_is_false() -> None:
    runs = [_run("success", head_sha="abc")]
    assert had_success_at_sha(runs, "") is False


def test_same_sha_success_detected() -> None:
    runs = [
        _run("failure", head_sha="abc"),
        _run("success", head_sha="abc"),
        _run("failure", head_sha="xyz"),
    ]
    assert had_success_at_sha(runs, "abc") is True


def test_different_sha_success_not_counted() -> None:
    runs = [_run("success", head_sha="xyz"), _run("failure", head_sha="abc")]
    assert had_success_at_sha(runs, "abc") is False


def test_same_sha_failure_only_is_not_a_success() -> None:
    runs = [_run("failure", head_sha="abc")] * 3
    assert had_success_at_sha(runs, "abc") is False


# Tiny helper so we don't pull in `from pytest import approx` at the top.
def pytest_approx(expected: float):
    import pytest
    return pytest.approx(expected)
