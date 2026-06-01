"""Tests for orchestrator/cd_pipeline.py — composition + branching.

These tests patch each of the five agent modules to return scripted
results; the goal is to exercise every branch of the orchestrator (block,
deploy fail, health fail + rollback, rollback fail, etc.) without any
real HTTP / ssh / LLM calls.
"""

from __future__ import annotations

from typing import Any

import pytest

from audit.logger import init_audit_logger
from config import settings as settings_module
from models.cd import (
    DeployResult,
    DeployRisk,
    DeployVerdict,
    HealthReport,
    ReleaseSuccessEvent,
)
from orchestrator import cd_pipeline


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def cd_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    settings_module.get_settings.cache_clear()
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake")
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "fake")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "fake")
    monkeypatch.setenv("GITHUB_REPO_OWNER", "x")
    monkeypatch.setenv("GITHUB_REPO_NAME", "y")
    monkeypatch.setenv("CODESPACE_NAME", "test-cs")
    monkeypatch.setenv("BACKEND_BASE_URL", "https://example")
    monkeypatch.setenv("DEPLOY_IMAGE_REPOSITORY", "ghcr.io/x/y")
    monkeypatch.setenv("AUTO_ROLLBACK_ENABLED", "true")
    monkeypatch.setenv("SESSION_TIMEOUT_SECONDS", "30")
    # audit_step requires the audit logger to be initialised — usually done
    # in the FastAPI lifespan. Point it at a tmp_path so tests don't write
    # to ./logs.
    init_audit_logger(tmp_path)
    yield
    settings_module.get_settings.cache_clear()


def _event(**overrides: Any) -> ReleaseSuccessEvent:
    base = {
        "run_id": 42,
        "repo_owner": "x",
        "repo_name": "y",
        "workflow_name": "release",
        "branch": "main",
        "head_sha": "abc1234567890abcdef0123456789abcdef01234",
        "html_url": "https://github.com/x/y/actions/runs/42",
        "sender_login": "alice",
    }
    base.update(overrides)
    return ReleaseSuccessEvent(**base)


class _Spy:
    """Tracks calls + serves scripted responses for each of the 5 modules."""

    def __init__(self) -> None:
        self.verdict: DeployVerdict = DeployVerdict(
            approve=True,
            risk=DeployRisk.LOW,
            reason="looks fine",
            confidence=0.9,
        )
        self.deploy_result: DeployResult = DeployResult(
            success=True,
            image_tag="ghcr.io/x/y:abc1234",
            prev_tag="ghcr.io/x/y:old1111",
        )
        self.health_report: HealthReport = HealthReport(
            healthy=True,
            expected_sha="abc1234",
            observed_sha="abc1234",
            latency_ms=42,
            attempts=1,
        )
        # If set, the SECOND health check (post-rollback) returns this
        # instead of `health_report`.
        self.rollback_health: HealthReport | None = None
        self.rollback_result: DeployResult | None = None
        self.pr_resolution: dict[str, Any] | None = {
            "number": 7,
            "title": "fix: stuff",
            "body": "body",
            "files": ["app/x.py"],
            "diff": "--- a\n+++ b\n@@\n-a\n+b\n",
        }
        # Records of calls
        self.judge_calls: list[dict[str, Any]] = []
        self.deploy_calls: list[str] = []
        self.health_calls: list[str] = []
        self.rollback_calls: list[str] = []
        self.published: list[tuple[str, str, str]] = []  # (stage, msg, level)

    # ── Patches ──────────────────────────────────────────────────────────

    async def fake_resolve_pr(self, state: cd_pipeline.CDTaskState) -> None:
        pr = self.pr_resolution
        if pr is None:
            return
        state.pr_number = pr["number"]
        state.pr_title = pr["title"]
        state.pr_body = pr["body"]
        state.files_changed = pr["files"]
        state.diff_text = pr["diff"]

    async def fake_judge(self, **kwargs: Any) -> DeployVerdict:
        self.judge_calls.append(kwargs)
        return self.verdict

    async def fake_deploy(self, image_ref: str) -> DeployResult:
        self.deploy_calls.append(image_ref)
        return self.deploy_result

    async def fake_health(self, expected_sha: str, **_: Any) -> HealthReport:
        self.health_calls.append(expected_sha)
        # First health call uses self.health_report; second uses
        # rollback_health if it's set (post-rollback path).
        if len(self.health_calls) > 1 and self.rollback_health is not None:
            return self.rollback_health
        return self.health_report

    async def fake_rollback(self, prev_tag: str) -> DeployResult:
        self.rollback_calls.append(prev_tag)
        if self.rollback_result is not None:
            return self.rollback_result
        # Default rollback success — mirrors the prev_tag back as the new
        # image, with a fresh prev_tag (the one we just tried and failed).
        return DeployResult(success=True, image_tag=prev_tag, prev_tag="failed-tag")

    async def fake_publish_safe(
        self,
        stage: str,
        message: str,
        *,
        level: str = "info",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.published.append((stage, message, level))


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> _Spy:
    s = _Spy()
    monkeypatch.setattr(cd_pipeline, "_resolve_pr", s.fake_resolve_pr)
    monkeypatch.setattr(cd_pipeline.deploy_guard, "judge", s.fake_judge)
    monkeypatch.setattr(cd_pipeline.deployer, "deploy", s.fake_deploy)
    monkeypatch.setattr(cd_pipeline.health_monitor, "check", s.fake_health)
    monkeypatch.setattr(cd_pipeline.rollback, "rollback_to", s.fake_rollback)
    monkeypatch.setattr(cd_pipeline.event_publisher, "publish_safe", s.fake_publish_safe)
    # Notifier hits real HTTP otherwise; stub it.
    async def _no_notify(_state: cd_pipeline.CDTaskState) -> None:
        return None
    monkeypatch.setattr(cd_pipeline, "_notify", _no_notify)
    return s


# ── Happy path ────────────────────────────────────────────────────────────


async def test_happy_path_deploys_and_verifies(cd_settings: None, spy: _Spy) -> None:
    state = cd_pipeline.CDTaskState(event=_event())
    await cd_pipeline._execute(state, cd_pipeline.get_settings())

    assert state.outcome == "deployed"
    assert state.error_message is None
    assert spy.deploy_calls == ["ghcr.io/x/y:abc1234"]
    assert spy.health_calls == ["abc1234"]
    assert spy.rollback_calls == []
    # Sanity check that key publish events fired.
    stages = [p[0] for p in spy.published]
    assert "cd_start" in stages
    assert "deploy_guard" in stages
    assert "deploy" in stages
    assert "health_check" in stages
    assert "cd_done" in stages


# ── Guard blocks ──────────────────────────────────────────────────────────


async def test_guard_block_short_circuits(cd_settings: None, spy: _Spy) -> None:
    spy.verdict = DeployVerdict(
        approve=False,
        risk=DeployRisk.HIGH,
        reason="destructive migration",
        confidence=0.95,
    )
    state = cd_pipeline.CDTaskState(event=_event())
    await cd_pipeline._execute(state, cd_pipeline.get_settings())

    assert state.outcome == "blocked"
    # We never reached deploy / health / rollback.
    assert spy.deploy_calls == []
    assert spy.health_calls == []
    assert spy.rollback_calls == []
    # The block was published as a warn-level event.
    block_events = [p for p in spy.published if "BLOCKED" in p[1]]
    assert block_events
    assert block_events[0][2] == "warn"


# ── Deploy fails ──────────────────────────────────────────────────────────


async def test_deploy_failure_skips_rollback(cd_settings: None, spy: _Spy) -> None:
    """Nothing changed on the host yet, so no rollback is appropriate."""
    spy.deploy_result = DeployResult(
        success=False,
        image_tag="ghcr.io/x/y:abc12345",
        prev_tag="ghcr.io/x/y:old1111",
        error_message="manifest unknown",
    )
    state = cd_pipeline.CDTaskState(event=_event())
    await cd_pipeline._execute(state, cd_pipeline.get_settings())

    assert state.outcome == "deploy_failed"
    assert "manifest unknown" in (state.error_message or "")
    assert spy.health_calls == []
    assert spy.rollback_calls == []


# ── Health fail → rollback ────────────────────────────────────────────────


async def test_unhealthy_triggers_rollback_when_prev_tag_present(
    cd_settings: None, spy: _Spy
) -> None:
    spy.health_report = HealthReport(
        healthy=False,
        expected_sha="abc12345",
        observed_sha="oldsha",
        latency_ms=30,
        attempts=5,
        error_message="version mismatch",
    )
    # Post-rollback health comes back green.
    spy.rollback_health = HealthReport(
        healthy=True,
        expected_sha="old1111",
        observed_sha="old1111",
        latency_ms=20,
        attempts=1,
    )
    state = cd_pipeline.CDTaskState(event=_event())
    await cd_pipeline._execute(state, cd_pipeline.get_settings())

    assert state.outcome == "rolled_back"
    assert spy.rollback_calls == ["ghcr.io/x/y:old1111"]
    # Two health checks: forward + post-rollback.
    assert len(spy.health_calls) == 2
    # Second call targets the tag portion of prev_tag.
    assert spy.health_calls[1] == "old1111"


async def test_unhealthy_without_prev_tag_does_not_attempt_rollback(
    cd_settings: None, spy: _Spy
) -> None:
    spy.deploy_result = DeployResult(
        success=True,
        image_tag="ghcr.io/x/y:abc12345",
        prev_tag="",  # first deploy ever — no previous to roll to
    )
    spy.health_report = HealthReport(
        healthy=False,
        expected_sha="abc12345",
        observed_sha=None,
        latency_ms=0,
        attempts=10,
        error_message="/health never returned 200",
    )
    state = cd_pipeline.CDTaskState(event=_event())
    await cd_pipeline._execute(state, cd_pipeline.get_settings())

    assert state.outcome == "unhealthy_no_prev_tag"
    assert spy.rollback_calls == []


async def test_unhealthy_with_auto_rollback_disabled(
    cd_settings: None, monkeypatch: pytest.MonkeyPatch, spy: _Spy
) -> None:
    # Need to recreate settings with the new env var.
    monkeypatch.setenv("AUTO_ROLLBACK_ENABLED", "false")
    settings_module.get_settings.cache_clear()
    try:
        spy.health_report = HealthReport(
            healthy=False,
            expected_sha="abc12345",
            observed_sha="x",
            latency_ms=0,
            attempts=3,
            error_message="version mismatch",
        )
        state = cd_pipeline.CDTaskState(event=_event())
        await cd_pipeline._execute(state, cd_pipeline.get_settings())

        assert state.outcome == "unhealthy_no_rollback"
        assert spy.rollback_calls == []
    finally:
        settings_module.get_settings.cache_clear()


# ── Rollback fails ────────────────────────────────────────────────────────


async def test_rollback_failure_is_terminal(cd_settings: None, spy: _Spy) -> None:
    spy.health_report = HealthReport(
        healthy=False,
        expected_sha="abc12345",
        observed_sha="x",
        latency_ms=0,
        attempts=3,
        error_message="bad",
    )
    spy.rollback_result = DeployResult(
        success=False,
        image_tag="ghcr.io/x/y:old1111",
        prev_tag="",
        error_message="ssh timeout",
    )
    state = cd_pipeline.CDTaskState(event=_event())
    await cd_pipeline._execute(state, cd_pipeline.get_settings())

    assert state.outcome == "rollback_failed"
    assert "ssh timeout" in (state.error_message or "")
    # No post-rollback health check — we bail out immediately because
    # the bad image is still live and someone has to look at it.
    assert len(spy.health_calls) == 1


async def test_post_rollback_still_unhealthy(cd_settings: None, spy: _Spy) -> None:
    """Rollback succeeded technically, but health didn't recover.

    This is the worst case — neither the new nor the old image is healthy.
    Pipeline reports it explicitly so the operator can intervene.
    """
    spy.health_report = HealthReport(
        healthy=False,
        expected_sha="abc12345",
        observed_sha="x",
        latency_ms=0,
        attempts=3,
        error_message="version mismatch",
    )
    spy.rollback_health = HealthReport(
        healthy=False,
        expected_sha="old1111",
        observed_sha=None,
        latency_ms=0,
        attempts=3,
        error_message="/health never returned 200",
    )
    state = cd_pipeline.CDTaskState(event=_event())
    await cd_pipeline._execute(state, cd_pipeline.get_settings())

    assert state.outcome == "rollback_health_failed"
    assert "never returned 200" in (state.error_message or "")


# ── PR resolution edges ───────────────────────────────────────────────────


async def test_missing_pr_falls_back_to_synthetic_diff(
    cd_settings: None, spy: _Spy
) -> None:
    """No PR mapped to head_sha → guard still runs with a synthesised diff."""
    spy.pr_resolution = None  # _resolve_pr returns without populating state
    state = cd_pipeline.CDTaskState(event=_event())
    await cd_pipeline._execute(state, cd_pipeline.get_settings())

    # Guard was called with a non-empty diff (the synthesised marker text).
    assert spy.judge_calls
    call = spy.judge_calls[0]
    assert call["diff_summary"]
    assert "no diff available" in call["diff_summary"]
    # And we still made it through the happy path because the default
    # verdict is approve=True.
    assert state.outcome == "deployed"


# ── Notification body shape ───────────────────────────────────────────────


def test_format_notification_renders_all_phases(cd_settings: None) -> None:
    state = cd_pipeline.CDTaskState(event=_event())
    state.pr_number = 7
    state.pr_title = "fix: thing"
    state.verdict = DeployVerdict(
        approve=True, risk=DeployRisk.MEDIUM, reason="ok", confidence=0.8
    )
    state.deploy_result = DeployResult(
        success=True, image_tag="ghcr.io/x/y:abc1234", prev_tag="ghcr.io/x/y:old1"
    )
    state.health_report = HealthReport(
        healthy=True,
        expected_sha="abc1234",
        observed_sha="abc1234",
        latency_ms=42,
        attempts=1,
    )
    state.outcome = "deployed"
    text = cd_pipeline._format_notification(state, cd_pipeline.get_settings())
    assert "deployed" in text
    assert "PR: #7" in text
    assert "deploy_guard: approve" in text
    assert "health: ok" in text
    assert "duration:" in text


def test_format_notification_when_blocked(cd_settings: None) -> None:
    state = cd_pipeline.CDTaskState(event=_event())
    state.verdict = DeployVerdict(
        approve=False,
        risk=DeployRisk.HIGH,
        reason="destructive migration",
        confidence=0.9,
    )
    state.outcome = "blocked"
    text = cd_pipeline._format_notification(state, cd_pipeline.get_settings())
    assert "blocked" in text
    assert "deploy_guard: block" in text
    # No deploy / health / rollback lines because none ran.
    assert "deploy:" not in text
    assert "health:" not in text
    assert "rollback" not in text
