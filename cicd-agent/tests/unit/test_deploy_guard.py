"""Tests for agents/deploy_guard.py — LLM input, response parsing, gating."""

from __future__ import annotations

import json

import pytest

from agents import deploy_guard
from config import settings as settings_module
from llm.rate_limiter import GeminiError, GeminiRateLimitError
from llm.response_parser import parse_deploy_verdict
from models.cd import DeployRisk, DeployVerdict


@pytest.fixture
def cd_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    settings_module.get_settings.cache_clear()
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake")
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "fake")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "fake")
    monkeypatch.setenv("GITHUB_REPO_OWNER", "test")
    monkeypatch.setenv("GITHUB_REPO_NAME", "demo")
    yield
    settings_module.get_settings.cache_clear()


def _typical_inputs(**overrides: object) -> dict[str, object]:
    """Reasonable default kwargs for judge()."""
    base: dict[str, object] = {
        "pr_number": 42,
        "pr_title": "fix: cap on_hand at threshold for low-stock",
        "pr_body": "Fixes #41 — boundary value handling in is_low_stock.",
        "files_changed": ["app/service.py", "tests/unit/test_service.py"],
        "diff_summary": (
            "--- a/app/service.py\n"
            "+++ b/app/service.py\n"
            "@@ -10,5 +10,5 @@\n"
            "-    return on_hand < threshold\n"
            "+    return on_hand <= threshold\n"
        ),
        "head_sha": "abc1234567890",
        "recent_deploys": [
            {"sha": "deadbeef", "outcome": "success"},
            {"sha": "0000ffff", "outcome": "success"},
        ],
    }
    base.update(overrides)
    return base


# ── parse_deploy_verdict ──────────────────────────────────────────────────


def test_parse_deploy_verdict_happy_path() -> None:
    raw = json.dumps(
        {
            "approve": True,
            "risk": "low",
            "reason": "small bug fix, well-tested",
            "concerns": ["check stock metric after deploy"],
            "confidence": 0.85,
        }
    )
    v = parse_deploy_verdict(raw)
    assert v is not None
    assert v.approve is True
    assert v.risk is DeployRisk.LOW
    assert v.confidence == 0.85
    assert v.concerns == ("check stock metric after deploy",)


def test_parse_deploy_verdict_tolerates_fences() -> None:
    raw = (
        "Here's my verdict:\n"
        "```json\n"
        '{"approve": false, "risk": "high", "reason": "destructive migration",'
        ' "concerns": ["drop_column on items"], "confidence": 0.9}\n'
        "```\n"
    )
    v = parse_deploy_verdict(raw)
    assert v is not None
    assert v.approve is False
    assert v.risk is DeployRisk.HIGH


def test_parse_deploy_verdict_returns_none_on_garbage() -> None:
    assert parse_deploy_verdict("LGTM ship it") is None
    assert parse_deploy_verdict("") is None


def test_parse_deploy_verdict_requires_approve_field() -> None:
    raw = json.dumps({"risk": "low", "reason": "ok", "confidence": 0.9})
    assert parse_deploy_verdict(raw) is None


def test_parse_deploy_verdict_coerces_unknown_risk_to_medium() -> None:
    raw = json.dumps(
        {"approve": True, "risk": "unknown", "reason": "ok", "confidence": 0.8}
    )
    v = parse_deploy_verdict(raw)
    assert v is not None
    assert v.risk is DeployRisk.MEDIUM


def test_parse_deploy_verdict_clamps_confidence() -> None:
    raw = json.dumps(
        {"approve": True, "risk": "low", "reason": "ok", "confidence": 999.0}
    )
    v = parse_deploy_verdict(raw)
    assert v is not None
    assert v.confidence == 1.0


def test_parse_deploy_verdict_concerns_filters_empty_strings() -> None:
    raw = json.dumps(
        {
            "approve": True,
            "risk": "low",
            "reason": "ok",
            "concerns": ["real concern", "   ", "", "another"],
            "confidence": 0.7,
        }
    )
    v = parse_deploy_verdict(raw)
    assert v is not None
    assert v.concerns == ("real concern", "another")


# ── _truncate_diff ────────────────────────────────────────────────────────


def test_truncate_diff_keeps_short_diffs_intact() -> None:
    short = "a" * 100
    assert deploy_guard._truncate_diff(short) == short


def test_truncate_diff_keeps_both_ends() -> None:
    huge = "HEAD" + "x" * 60_000 + "TAIL"
    truncated = deploy_guard._truncate_diff(huge)
    assert truncated.startswith("HEAD")
    assert truncated.endswith("TAIL")
    assert "elided" in truncated
    assert len(truncated) <= deploy_guard._MAX_DIFF_CHARS + 200


# ── judge() — empty-diff short circuit ────────────────────────────────────


async def test_judge_blocks_on_empty_diff(cd_settings: None) -> None:
    inputs = _typical_inputs(diff_summary="")
    verdict = await deploy_guard.judge(**inputs)  # type: ignore[arg-type]
    assert verdict.approve is False
    assert "empty diff" in verdict.reason
    # We never reached the LLM — risk is the synthetic HIGH default.
    assert verdict.risk is DeployRisk.HIGH


async def test_judge_blocks_on_whitespace_only_diff(cd_settings: None) -> None:
    inputs = _typical_inputs(diff_summary="   \n   ")
    verdict = await deploy_guard.judge(**inputs)  # type: ignore[arg-type]
    assert verdict.approve is False
    assert "empty diff" in verdict.reason


# ── judge() — LLM-mocked happy path ───────────────────────────────────────


class _FakeClient:
    """Stand-in for the singleton returned by get_gemini_client().

    Records the last call and returns a scripted response (or raises a
    scripted exception). Tests use the helper `install_fake_client` to
    swap it in.
    """

    def __init__(self, response: str | Exception) -> None:
        self.response = response
        self.last_prompt: str | None = None
        self.last_system_prompt: str | None = None
        self.last_kwargs: dict[str, object] = {}

    async def generate(self, prompt: str, system_prompt: str, **kwargs: object) -> str:
        self.last_prompt = prompt
        self.last_system_prompt = system_prompt
        self.last_kwargs = kwargs
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _install_fake_client(
    monkeypatch: pytest.MonkeyPatch, response: str | Exception
) -> _FakeClient:
    client = _FakeClient(response)
    monkeypatch.setattr(deploy_guard, "get_gemini_client", lambda: client)
    return client


async def test_judge_returns_llm_verdict_when_confident(
    cd_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = json.dumps(
        {
            "approve": True,
            "risk": "low",
            "reason": "small targeted fix",
            "concerns": ["watch low-stock alert volume"],
            "confidence": 0.92,
        }
    )
    client = _install_fake_client(monkeypatch, response)

    verdict = await deploy_guard.judge(**_typical_inputs())  # type: ignore[arg-type]
    assert verdict.approve is True
    assert verdict.risk is DeployRisk.LOW
    assert verdict.confidence == 0.92

    # Sanity: the LLM received the structured JSON payload, not the raw
    # diff strings concatenated.
    assert client.last_prompt is not None
    decoded = json.loads(client.last_prompt)
    assert decoded["pr_number"] == 42
    assert "app/service.py" in decoded["files_changed"]
    assert client.last_kwargs.get("agent") == "deploy_guard"
    assert client.last_kwargs.get("strip_pii") is True


async def test_judge_low_confidence_is_force_blocked(
    cd_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even with approve=true, confidence < 0.6 must flip to a block."""
    response = json.dumps(
        {
            "approve": True,
            "risk": "low",
            "reason": "looks fine to me",
            "concerns": [],
            "confidence": 0.4,
        }
    )
    _install_fake_client(monkeypatch, response)

    verdict = await deploy_guard.judge(**_typical_inputs())  # type: ignore[arg-type]
    assert verdict.approve is False
    assert "low confidence" in verdict.reason
    # The LLM's original reason is still surfaced.
    assert "looks fine" in verdict.reason
    # The synthetic concern about confidence is appended.
    assert any("confidence=" in c for c in verdict.concerns)


async def test_judge_blocks_on_unparseable_llm_response(
    cd_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_client(monkeypatch, "ship it 🚀")

    verdict = await deploy_guard.judge(**_typical_inputs())  # type: ignore[arg-type]
    assert verdict.approve is False
    assert "malformed" in verdict.reason


async def test_judge_blocks_when_llm_errors(
    cd_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_client(monkeypatch, GeminiError("upstream 502", agent="deploy_guard"))

    verdict = await deploy_guard.judge(**_typical_inputs())  # type: ignore[arg-type]
    assert verdict.approve is False
    assert "LLM error" in verdict.reason
    assert verdict.risk is DeployRisk.HIGH


async def test_judge_blocks_when_rate_limited(
    cd_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_client(monkeypatch, GeminiRateLimitError("429 too many requests"))

    verdict = await deploy_guard.judge(**_typical_inputs())  # type: ignore[arg-type]
    assert verdict.approve is False
    assert "unavailable" in verdict.reason.lower()
    # Rate-limit blocks are downgraded to MEDIUM — not the change's fault,
    # so the notifier shouldn't escalate as if it were a destructive PR.
    assert verdict.risk is DeployRisk.MEDIUM


async def test_judge_serialises_recent_deploys(
    cd_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The history list reaches the LLM intact — important for the prompt's
    'previous deploy at preceding SHA failed' check."""
    response = json.dumps(
        {"approve": True, "risk": "medium", "reason": "ok", "confidence": 0.8}
    )
    client = _install_fake_client(monkeypatch, response)

    inputs = _typical_inputs(
        recent_deploys=[
            {"sha": "aaa1", "outcome": "failed"},
            {"sha": "bbb2", "outcome": "success"},
        ]
    )
    await deploy_guard.judge(**inputs)  # type: ignore[arg-type]

    assert client.last_prompt is not None
    decoded = json.loads(client.last_prompt)
    assert decoded["recent_deploys"] == [
        {"sha": "aaa1", "outcome": "failed"},
        {"sha": "bbb2", "outcome": "success"},
    ]
