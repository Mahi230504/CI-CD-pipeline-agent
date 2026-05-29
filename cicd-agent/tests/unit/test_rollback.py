"""Tests for agents/rollback.py — delegation to deployer + empty-tag refusal."""

from __future__ import annotations

import pytest

from agents import deployer, rollback
from config import settings as settings_module
from models.cd import DeployResult


@pytest.fixture
def cd_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    settings_module.get_settings.cache_clear()
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake")
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "fake")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "fake")
    monkeypatch.setenv("GITHUB_REPO_OWNER", "test")
    monkeypatch.setenv("GITHUB_REPO_NAME", "demo")
    monkeypatch.setenv("CODESPACE_NAME", "test-codespace")
    monkeypatch.setenv("CODESPACE_WORKDIR", "/workspaces/cicd-agent-demo")
    monkeypatch.setenv("DEPLOY_IMAGE_REPOSITORY", "ghcr.io/mahi230504/inventory-flow")
    yield
    settings_module.get_settings.cache_clear()


async def test_rollback_refuses_empty_prev_tag() -> None:
    """An empty prev_tag means we have nothing to revert to; refuse fast."""
    result = await rollback.rollback_to("")
    assert result.success is False
    assert "no prev_tag" in (result.error_message or "")


async def test_rollback_delegates_to_deployer(
    cd_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rollback_to(tag) must call deployer.deploy(tag) verbatim."""
    captured: dict[str, str] = {}

    async def fake_deploy(image_tag: str) -> DeployResult:
        captured["image_tag"] = image_tag
        return DeployResult(success=True, image_tag=image_tag, prev_tag="failed-tag")

    monkeypatch.setattr(rollback, "deploy", fake_deploy)

    result = await rollback.rollback_to("ghcr.io/foo/bar:good1")

    assert captured == {"image_tag": "ghcr.io/foo/bar:good1"}
    assert result.success is True
    assert result.image_tag == "ghcr.io/foo/bar:good1"


async def test_rollback_propagates_deploy_failure(
    cd_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed underlying deploy must surface as a failed DeployResult.

    The orchestrator depends on this to send a 'rollback also failed' alert.
    """

    async def fake_deploy(image_tag: str) -> DeployResult:
        return DeployResult(
            success=False,
            image_tag=image_tag,
            prev_tag="",
            error_message="manifest unknown",
        )

    monkeypatch.setattr(rollback, "deploy", fake_deploy)

    result = await rollback.rollback_to("ghcr.io/foo/bar:bad")
    assert result.success is False
    assert "manifest unknown" in (result.error_message or "")


async def test_rollback_uses_real_deployer_validation(
    cd_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If prev_tag isn't a valid image ref, the underlying deployer rejects it.

    We don't double-validate in the rollback module — the deployer is the
    single source of truth for what counts as a valid `<repo>:<tag>` ref.
    """
    # Use the real deploy() which calls _looks_like_image_ref. No SSH calls
    # happen because validation fails before that branch.
    result = await rollback.rollback_to("not-an-image-ref")
    assert result.success is False
    assert "invalid image reference" in (result.error_message or "")
    # Sanity: we never reached the codespace check (that error message would
    # be different). Confirms the validation gate fires first.
    assert "CODESPACE_NAME" not in (result.error_message or "")


def test_rollback_uses_deployer_deploy_function(monkeypatch: pytest.MonkeyPatch) -> None:
    """Light import-graph check: rollback imports `deploy` from agents.deployer.

    Guards against an accidental refactor that breaks the delegation chain
    silently (e.g. shadowing `deploy` with something else in rollback.py).
    """
    # The symbol bound in the rollback module's namespace must be the same
    # function exported by agents.deployer.
    assert rollback.deploy is deployer.deploy
