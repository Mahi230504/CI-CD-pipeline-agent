"""Tests for agents/deployer.py — ssh wrapper, image-ref parsing, prev-tag capture."""

from __future__ import annotations

import pytest

from agents import deployer
from config import settings as settings_module


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


@pytest.fixture
def no_codespace(monkeypatch: pytest.MonkeyPatch) -> None:
    settings_module.get_settings.cache_clear()
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake")
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "fake")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "fake")
    monkeypatch.setenv("GITHUB_REPO_OWNER", "test")
    monkeypatch.setenv("GITHUB_REPO_NAME", "demo")
    monkeypatch.delenv("CODESPACE_NAME", raising=False)
    yield
    settings_module.get_settings.cache_clear()


class _FakeSSH:
    """Records every call to run_ssh and replays scripted responses.

    The deployer makes up to two ssh calls per deploy:
      1. prev-tag read  — short script, 30s timeout
      2. main deploy    — long script, 180s timeout
    Tests script the responses in order via `responses=[...]`.
    """

    def __init__(self, responses: list[tuple[int, str, str]]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, script: str, timeout: int) -> tuple[int, str, str]:
        self.calls.append((script, timeout))
        if not self._responses:
            raise AssertionError("FakeSSH ran out of scripted responses")
        return self._responses.pop(0)


# ── image-ref validation ──────────────────────────────────────────────────


def test_looks_like_image_ref_accepts_standard_ghcr() -> None:
    assert deployer._looks_like_image_ref("ghcr.io/mahi230504/inventory-flow:abc1234")
    assert deployer._looks_like_image_ref("inventory-flow:local")
    assert deployer._looks_like_image_ref("foo/bar:v1.2.3")


def test_looks_like_image_ref_rejects_empty_or_no_tag() -> None:
    assert not deployer._looks_like_image_ref("")
    assert not deployer._looks_like_image_ref("ghcr.io/foo/bar")  # no :tag


def test_looks_like_image_ref_rejects_shell_metas() -> None:
    assert not deployer._looks_like_image_ref("foo:tag;rm -rf /")
    assert not deployer._looks_like_image_ref("foo:tag with space")
    assert not deployer._looks_like_image_ref("foo:tag$VAR")


def test_build_image_ref_combines_repo_and_sha(cd_settings: None) -> None:
    assert deployer.build_image_ref("abc1234") == (
        "ghcr.io/mahi230504/inventory-flow:abc1234"
    )


def test_build_image_ref_strips_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    settings_module.get_settings.cache_clear()
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake")
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "fake")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "fake")
    monkeypatch.setenv("GITHUB_REPO_OWNER", "test")
    monkeypatch.setenv("GITHUB_REPO_NAME", "demo")
    monkeypatch.setenv("DEPLOY_IMAGE_REPOSITORY", "ghcr.io/foo/bar/")
    try:
        assert deployer.build_image_ref("xyz") == "ghcr.io/foo/bar:xyz"
    finally:
        settings_module.get_settings.cache_clear()


def test_build_image_ref_rejects_missing_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    settings_module.get_settings.cache_clear()
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake")
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "fake")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "fake")
    monkeypatch.setenv("GITHUB_REPO_OWNER", "test")
    monkeypatch.setenv("GITHUB_REPO_NAME", "demo")
    monkeypatch.delenv("DEPLOY_IMAGE_REPOSITORY", raising=False)
    try:
        with pytest.raises(ValueError, match="DEPLOY_IMAGE_REPOSITORY"):
            deployer.build_image_ref("xyz")
    finally:
        settings_module.get_settings.cache_clear()


# ── deploy() happy path + failure modes ───────────────────────────────────


async def test_deploy_invalid_image_ref_returns_failure(cd_settings: None) -> None:
    result = await deployer.deploy("not-a-valid-ref")
    assert result.success is False
    assert "invalid image reference" in (result.error_message or "")


async def test_deploy_without_codespace_returns_failure(no_codespace: None) -> None:
    result = await deployer.deploy("ghcr.io/foo/bar:abc1234")
    assert result.success is False
    assert "CODESPACE_NAME" in (result.error_message or "")


async def test_deploy_happy_path_captures_prev_tag(
    cd_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeSSH(
        [
            # prev-tag read
            (0, "ghcr.io/mahi230504/inventory-flow:old1111\n", ""),
            # main deploy
            (
                0,
                "[deployer] API_IMAGE=ghcr.io/mahi230504/inventory-flow:new2222\n"
                "[deployer] running containers:\napi inventory-flow:new2222\n",
                "",
            ),
        ]
    )
    monkeypatch.setattr(deployer, "run_ssh", fake)

    result = await deployer.deploy("ghcr.io/mahi230504/inventory-flow:new2222")

    assert result.success is True
    assert result.prev_tag == "ghcr.io/mahi230504/inventory-flow:old1111"
    assert result.image_tag == "ghcr.io/mahi230504/inventory-flow:new2222"
    assert "running containers" in result.output
    assert len(fake.calls) == 2
    # First call: prev-tag script, short timeout. Second: deploy, long timeout.
    assert fake.calls[0][1] == 30
    assert fake.calls[1][1] == deployer._SSH_TIMEOUT_SECONDS


async def test_deploy_handles_missing_prev_tag(
    cd_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If .env has no API_IMAGE line, prev-tag read returns empty stdout."""
    fake = _FakeSSH(
        [
            (0, "", ""),  # no API_IMAGE line
            (0, "[deployer] running containers:\napi new\n", ""),
        ]
    )
    monkeypatch.setattr(deployer, "run_ssh", fake)

    result = await deployer.deploy("ghcr.io/foo/bar:abc1234")
    assert result.success is True
    assert result.prev_tag == ""


async def test_deploy_propagates_nonzero_exit(
    cd_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeSSH(
        [
            (0, "ghcr.io/foo/bar:old1\n", ""),
            (1, "", "Error response from daemon: manifest unknown"),
        ]
    )
    monkeypatch.setattr(deployer, "run_ssh", fake)

    result = await deployer.deploy("ghcr.io/foo/bar:bad")
    assert result.success is False
    assert "rc=1" in (result.error_message or "")
    assert "manifest unknown" in result.output
    # prev_tag still captured — important for the rollback path.
    assert result.prev_tag == "ghcr.io/foo/bar:old1"


async def test_deploy_clips_huge_stdout(
    cd_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    huge = "x" * 50_000
    fake = _FakeSSH(
        [
            (0, "", ""),
            (0, huge, ""),
        ]
    )
    monkeypatch.setattr(deployer, "run_ssh", fake)

    result = await deployer.deploy("ghcr.io/foo/bar:abc1234")
    assert result.success is True
    assert len(result.output) <= deployer._MAX_CAPTURED_OUTPUT_CHARS + 50


async def test_deploy_failed_prev_tag_does_not_block(
    cd_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the prev-tag read itself returns non-zero, we still attempt the deploy.

    Empty prev_tag is OK — it just disables rollback for this run.
    """
    fake = _FakeSSH(
        [
            (1, "", "permission denied"),
            (0, "[deployer] OK\n", ""),
        ]
    )
    monkeypatch.setattr(deployer, "run_ssh", fake)

    result = await deployer.deploy("ghcr.io/foo/bar:abc1234")
    assert result.success is True
    assert result.prev_tag == ""


async def test_deploy_script_includes_workdir_and_envvar(
    cd_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sanity-check that the substituted bash carries the configured paths."""
    fake = _FakeSSH(
        [
            (0, "ghcr.io/foo/bar:old1\n", ""),
            (0, "ok", ""),
        ]
    )
    monkeypatch.setattr(deployer, "run_ssh", fake)

    await deployer.deploy("ghcr.io/foo/bar:new2")

    prev_tag_script = fake.calls[0][0]
    deploy_script = fake.calls[1][0]
    assert "/workspaces/cicd-agent-demo" in prev_tag_script
    assert "/workspaces/cicd-agent-demo" in deploy_script
    assert "API_IMAGE" in deploy_script
    # Sanity: the new value reaches the script via the python heredoc.
    assert "ghcr.io/foo/bar:new2" in deploy_script
