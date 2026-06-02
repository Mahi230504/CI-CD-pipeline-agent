"""Tests for the fix→verify loop: ci_verifier.verify_patch_ci and
code_patcher.patch_and_verify.

verify_patch_ci reads CI status via a fake mcp_client; patch_and_verify is
tested with patch()/verify monkeypatched so we exercise the orchestration
(green first try, red→retry→green, red→red→give up, disabled, no-op retry)
without real GitHub or LLM calls.
"""

from __future__ import annotations

from agents import ci_verifier, code_patcher
from agents.ci_verifier import VerifyResult, verify_patch_ci
from config.constants import ROLLING_PATCH_BRANCH
from config.settings import Settings
from models.events import WorkflowFailureEvent
from models.run import Diagnosis, JobLog, PatchResult
from config.constants import ErrorType


def _settings(**over) -> Settings:
    base = dict(
        openrouter_api_key="x",
        github_pat="x",
        github_webhook_secret="x",
        github_repo_owner="o",
        github_repo_name="r",
        patch_verify_enabled=True,
        patch_verify_timeout_seconds=2,
        patch_verify_max_iterations=1,
        patch_verify_poll_interval_seconds=0.01,
    )
    base.update(over)
    return Settings(**base)


def _event() -> WorkflowFailureEvent:
    return WorkflowFailureEvent(
        run_id=1,
        repo_owner="o",
        repo_name="r",
        workflow_name="CI",
        branch="main",
        head_sha="cafebabe" * 5,
        html_url="http://x",
        sender_login="u",
    )


def _diag() -> Diagnosis:
    return Diagnosis(
        error_type=ErrorType.LINT_ERROR,
        file="app/api/items.py",
        line_number=38,
        explanation="type mismatch",
        confidence=0.9,
        is_patchable=True,
        raw_response="",
    )


def _pr(head_sha: str = "abc123", **over) -> PatchResult:
    base = dict(
        branch_name=ROLLING_PATCH_BRANCH,
        success=True,
        attempt_number=1,
        pr_number=7,
        pr_url="http://pr/7",
        head_sha=head_sha,
    )
    base.update(over)
    return PatchResult(**base)


def _run(**over) -> dict:
    base = dict(
        id=99,
        name="CI",
        head_branch=ROLLING_PATCH_BRANCH,
        head_sha="abc123",
        status="completed",
        conclusion="success",
        created_at="2026-06-02T07:00:00Z",
    )
    base.update(over)
    return base


class _FakeMCP:
    """Scripted list_workflow_runs: pops a batch per call, last batch repeats."""

    def __init__(self, batches: list[list[dict]]) -> None:
        self._batches = list(batches)

    async def list_workflow_runs(self, *args, **kwargs) -> list[dict]:
        if len(self._batches) > 1:
            return self._batches.pop(0)
        return self._batches[0] if self._batches else []


# ── verify_patch_ci ────────────────────────────────────────────────────────


async def test_verify_success_by_head_sha():
    mcp = _FakeMCP([[_run(conclusion="success")]])
    res = await verify_patch_ci(_pr("abc123"), _event(), mcp, _settings())
    assert res.verified is True


async def test_verify_failure_returns_new_log(monkeypatch):
    async def fake_logs(run_id, mcp):
        return [JobLog(job_id=5, job_name="lint", raw_log="x", sliced_log="items.py:38 error: bad type")]

    monkeypatch.setattr(ci_verifier, "fetch_job_logs", fake_logs)
    mcp = _FakeMCP([[_run(conclusion="failure")]])
    res = await verify_patch_ci(_pr("abc123"), _event(), mcp, _settings())
    assert res.verified is False
    assert "bad type" in (res.failing_log or "")


async def test_verify_none_when_no_run_found():
    mcp = _FakeMCP([[]])
    res = await verify_patch_ci(_pr("abc123"), _event(), mcp, _settings(patch_verify_timeout_seconds=1))
    assert res.verified is None


async def test_verify_none_on_inconclusive_conclusion():
    mcp = _FakeMCP([[_run(status="completed", conclusion="cancelled")]])
    res = await verify_patch_ci(_pr("abc123"), _event(), mcp, _settings())
    assert res.verified is None


async def test_verify_polls_through_in_progress():
    mcp = _FakeMCP(
        [
            [_run(status="in_progress", conclusion=None)],
            [_run(status="completed", conclusion="success")],
        ]
    )
    res = await verify_patch_ci(_pr("abc123"), _event(), mcp, _settings())
    assert res.verified is True


async def test_verify_ignores_other_branches():
    # A completed run on a different branch must not be treated as our fix's CI.
    mcp = _FakeMCP([[_run(head_branch="main", conclusion="success")]])
    res = await verify_patch_ci(_pr("zzz999"), _event(), mcp, _settings(patch_verify_timeout_seconds=1))
    assert res.verified is None


# ── patch_and_verify orchestration ─────────────────────────────────────────


def _install(monkeypatch, patch_seq, verify_seq, settings):
    pc = {"n": 0}
    vc = {"n": 0}

    async def fake_patch(*args, **kwargs):
        r = patch_seq[pc["n"]]
        pc["n"] += 1
        return r

    async def fake_verify(*args, **kwargs):
        r = verify_seq[vc["n"]]
        vc["n"] += 1
        return r

    monkeypatch.setattr(code_patcher, "patch", fake_patch)
    monkeypatch.setattr(code_patcher, "verify_patch_ci", fake_verify)
    monkeypatch.setattr(code_patcher, "get_settings", lambda: settings)
    return pc, vc


async def test_pav_green_first_try(monkeypatch):
    pc, vc = _install(
        monkeypatch,
        [_pr("s1")],
        [VerifyResult(True, "CI passed")],
        _settings(),
    )
    res = await code_patcher.patch_and_verify(_diag(), _event(), 1, object())
    assert res.verified is True
    assert (pc["n"], vc["n"]) == (1, 1)


async def test_pav_red_then_green_retries(monkeypatch):
    pc, vc = _install(
        monkeypatch,
        [_pr("s1"), _pr("s2")],
        [VerifyResult(False, "CI failed", failing_log="still bad"), VerifyResult(True, "CI passed")],
        _settings(patch_verify_max_iterations=1),
    )
    res = await code_patcher.patch_and_verify(_diag(), _event(), 1, object())
    assert res.verified is True
    assert res.head_sha == "s2"  # the retry's commit
    assert (pc["n"], vc["n"]) == (2, 2)


async def test_pav_red_twice_gives_up_honestly(monkeypatch):
    pc, vc = _install(
        monkeypatch,
        [_pr("s1"), _pr("s2")],
        [VerifyResult(False, "fail1", failing_log="a"), VerifyResult(False, "fail2", failing_log="b")],
        _settings(patch_verify_max_iterations=1),
    )
    res = await code_patcher.patch_and_verify(_diag(), _event(), 1, object())
    assert res.verified is False
    assert res.verification_detail == "fail2"
    assert (pc["n"], vc["n"]) == (2, 2)


async def test_pav_disabled_is_passthrough(monkeypatch):
    pc, vc = _install(
        monkeypatch,
        [_pr("s1")],
        [VerifyResult(True, "should not be called")],
        _settings(patch_verify_enabled=False),
    )
    res = await code_patcher.patch_and_verify(_diag(), _event(), 1, object())
    assert res.verified is None
    assert (pc["n"], vc["n"]) == (1, 0)


async def test_pav_failed_initial_patch_not_verified(monkeypatch):
    pc, vc = _install(
        monkeypatch,
        [PatchResult(ROLLING_PATCH_BRANCH, success=False, attempt_number=1, error_message="boom")],
        [VerifyResult(True, "n/a")],
        _settings(),
    )
    res = await code_patcher.patch_and_verify(_diag(), _event(), 1, object())
    assert res.success is False
    assert res.verified is None
    assert (pc["n"], vc["n"]) == (1, 0)


async def test_pav_repatch_no_change_reports_failed(monkeypatch):
    pc, vc = _install(
        monkeypatch,
        [_pr("s1"), PatchResult(ROLLING_PATCH_BRANCH, success=False, attempt_number=1, error_message="unchanged")],
        [VerifyResult(False, "CI failed", failing_log="a")],
        _settings(patch_verify_max_iterations=1),
    )
    res = await code_patcher.patch_and_verify(_diag(), _event(), 1, object())
    assert res.verified is False
    assert "no new change" in (res.verification_detail or "")
    assert (pc["n"], vc["n"]) == (2, 1)
