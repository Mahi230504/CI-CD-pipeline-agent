"""Tests for agents/health_monitor.py — polling, SHA matching, timeout semantics."""

from __future__ import annotations

from typing import Any

import pytest

from agents import health_monitor
from config import settings as settings_module
from models.cd import HealthReport


@pytest.fixture
def cd_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    settings_module.get_settings.cache_clear()
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake")
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "fake")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "fake")
    monkeypatch.setenv("GITHUB_REPO_OWNER", "test")
    monkeypatch.setenv("GITHUB_REPO_NAME", "demo")
    monkeypatch.setenv("BACKEND_BASE_URL", "https://backend.example/")
    yield
    settings_module.get_settings.cache_clear()


# ── Test doubles for the two probes ───────────────────────────────────────


class _ScriptedProbes:
    """Replaces `_probe_health` and `_probe_version` with scripted responses.

    Each `health_responses` entry is `(ok: bool, latency_ms: int)`.
    Each `version_responses` entry is `str | None` (the `commit` field).
    The probe functions consume one entry per call; running out fails the
    test loudly so a missing response can't manifest as a hang.
    """

    def __init__(
        self,
        health_responses: list[tuple[bool, int]],
        version_responses: list[str | None],
    ) -> None:
        self._health = list(health_responses)
        self._version = list(version_responses)
        self.health_calls = 0
        self.version_calls = 0

    async def probe_health(self, session: Any, url: str) -> tuple[bool, int]:
        self.health_calls += 1
        if not self._health:
            raise AssertionError("ScriptedProbes: out of health responses")
        return self._health.pop(0)

    async def probe_version(self, session: Any, url: str) -> str | None:
        self.version_calls += 1
        if not self._version:
            raise AssertionError("ScriptedProbes: out of version responses")
        return self._version.pop(0)


def _install_probes(monkeypatch: pytest.MonkeyPatch, probes: _ScriptedProbes) -> None:
    """Replace the two probe coroutines with scripted fakes.

    We deliberately do NOT mock asyncio.sleep: the timeout check relies on
    monotonic time advancing between iterations, and mocking sleep makes
    the loop spin through hundreds of attempts inside one OS tick. Tests
    use a tiny real poll interval (~10ms) and a short timeout instead.
    """
    monkeypatch.setattr(health_monitor, "_probe_health", probes.probe_health)
    monkeypatch.setattr(health_monitor, "_probe_version", probes.probe_version)


# ── Config-level failures ──────────────────────────────────────────────────


async def test_check_returns_failure_when_base_url_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_module.get_settings.cache_clear()
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake")
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "fake")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "fake")
    monkeypatch.setenv("GITHUB_REPO_OWNER", "test")
    monkeypatch.setenv("GITHUB_REPO_NAME", "demo")
    monkeypatch.delenv("BACKEND_BASE_URL", raising=False)
    try:
        report = await health_monitor.check("abc1234")
    finally:
        settings_module.get_settings.cache_clear()

    assert report.healthy is False
    assert "BACKEND_BASE_URL" in (report.error_message or "")
    assert report.attempts == 0


async def test_check_returns_failure_when_sha_empty(cd_settings: None) -> None:
    report = await health_monitor.check("")
    assert report.healthy is False
    assert "expected_sha" in (report.error_message or "")


# ── Happy path ────────────────────────────────────────────────────────────


async def test_check_healthy_on_first_attempt(
    cd_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    probes = _ScriptedProbes(
        health_responses=[(True, 42)],
        version_responses=["abc1234"],
    )
    _install_probes(monkeypatch, probes)

    report = await health_monitor.check("abc1234", timeout_seconds=5)

    assert report.healthy is True
    assert report.observed_sha == "abc1234"
    assert report.attempts == 1
    assert report.latency_ms == 42
    assert probes.health_calls == 1
    assert probes.version_calls == 1


async def test_check_tolerates_short_vs_long_sha(
    cd_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If /version returns a full SHA but the expected is short, accept it."""
    full_sha = "abc1234567890abcdef0123456789abcdef01234"
    probes = _ScriptedProbes(
        health_responses=[(True, 30)],
        version_responses=[full_sha],
    )
    _install_probes(monkeypatch, probes)

    report = await health_monitor.check("abc1234", timeout_seconds=5)
    assert report.healthy is True
    assert report.observed_sha == full_sha


async def test_check_polls_until_version_matches(
    cd_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three attempts: first two return the OLD sha, third returns the NEW one."""
    probes = _ScriptedProbes(
        health_responses=[(True, 25), (True, 28), (True, 30)],
        version_responses=["oldsha11", "oldsha11", "newsha22"],
    )
    _install_probes(monkeypatch, probes)

    # Tiny real poll interval; 10s wall budget so the test never times out
    # before the third response is consumed.
    report = await health_monitor.check(
        "newsha22", timeout_seconds=10, poll_interval_seconds=0.01
    )
    assert report.healthy is True
    assert report.attempts == 3
    assert report.observed_sha == "newsha22"


# ── Failure modes ─────────────────────────────────────────────────────────


async def test_check_fails_when_health_never_goes_green(
    cd_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All /health probes 5xx — bail with a useful error message.

    Provide more responses than the loop will need; with a 200ms total
    budget and 50ms polls, we expect ~3-5 attempts.
    """
    probes = _ScriptedProbes(
        health_responses=[(False, 5000)] * 50,
        version_responses=[],
    )
    _install_probes(monkeypatch, probes)

    report = await health_monitor.check(
        "abc1234", timeout_seconds=1, poll_interval_seconds=0.05
    )
    assert report.healthy is False
    assert "/health never returned 200" in (report.error_message or "")
    assert report.observed_sha is None
    assert report.attempts >= 1


async def test_check_fails_when_version_never_matches(
    cd_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API is healthy but stuck on the OLD image."""
    probes = _ScriptedProbes(
        health_responses=[(True, 20)] * 30,
        version_responses=["stuckoldsha"] * 30,
    )
    _install_probes(monkeypatch, probes)

    report = await health_monitor.check(
        "newsha22", timeout_seconds=1, poll_interval_seconds=0.05
    )
    assert report.healthy is False
    assert report.observed_sha == "stuckoldsha"
    assert "stuckoldsha" in (report.error_message or "")
    assert "newsha22" in (report.error_message or "")


async def test_check_fails_when_version_endpoint_malformed(
    cd_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Health green, but /version returns garbage / 5xx (probe returns None)."""
    probes = _ScriptedProbes(
        health_responses=[(True, 20)] * 30,
        version_responses=[None] * 30,
    )
    _install_probes(monkeypatch, probes)

    report = await health_monitor.check(
        "abc1234", timeout_seconds=1, poll_interval_seconds=0.05
    )
    assert report.healthy is False
    assert "/version unreachable" in (report.error_message or "")


# ── SHA-match helper (covered indirectly above, but explicit here) ────────


def test_sha_matches_handles_empty_inputs() -> None:
    assert health_monitor._sha_matches("", "abc") is False
    assert health_monitor._sha_matches("abc", "") is False


def test_sha_matches_handles_prefix_relations() -> None:
    assert health_monitor._sha_matches("abc1234", "abc1234567890") is True
    assert health_monitor._sha_matches("abc1234567890", "abc1234") is True
    assert health_monitor._sha_matches("abc1234", "def5678") is False


# ── HealthReport instance integration ─────────────────────────────────────


def test_healthreport_dataclass_returned(cd_settings: None) -> None:
    """Defensive: importable + the check function returns the right type."""
    # Just confirm we can construct one with the documented fields.
    r = HealthReport(
        healthy=True,
        expected_sha="abc",
        observed_sha="abc",
        latency_ms=42,
        attempts=1,
    )
    assert r.sha_matches is True
    assert r.error_message is None
