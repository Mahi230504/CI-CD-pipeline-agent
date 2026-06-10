"""Tests for agents/event_publisher.py — fire-and-forget POST + safe defaults."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp
import pytest

from agents import event_publisher
from config import settings as settings_module


@pytest.fixture
def cd_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure the agent with a backend URL + token so publish() actually fires."""
    settings_module.get_settings.cache_clear()
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake")
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "fake")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "fake")
    monkeypatch.setenv("GITHUB_REPO_OWNER", "test")
    monkeypatch.setenv("GITHUB_REPO_NAME", "demo")
    monkeypatch.setenv("BACKEND_BASE_URL", "https://backend.example/")
    monkeypatch.setenv("AGENT_SHARED_SECRET", "s3cret")
    yield
    settings_module.get_settings.cache_clear()


@pytest.fixture
def disabled_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """No backend URL / no token — publish() should no-op."""
    settings_module.get_settings.cache_clear()
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake")
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "fake")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "fake")
    monkeypatch.setenv("GITHUB_REPO_OWNER", "test")
    monkeypatch.setenv("GITHUB_REPO_NAME", "demo")
    # setenv("") not delenv: get_settings() loads via load_dotenv(), which would
    # otherwise repopulate these from the developer's real .env and falsely enable
    # the channel this test asserts is disabled. An explicit empty value wins over
    # the dotenv file. See reference_cd_test_isolation_dotenv.
    monkeypatch.setenv("BACKEND_BASE_URL", "")
    monkeypatch.setenv("AGENT_SHARED_SECRET", "")
    yield
    settings_module.get_settings.cache_clear()


# ── Fake aiohttp.ClientSession ────────────────────────────────────────────
# We swap in a minimal stand-in instead of using a real HTTP test server.
# The publisher only uses .post(json=...) inside an async context manager,
# so the fake reproduces just that surface.


class _FakeResponse:
    def __init__(self, status: int, text: str = "") -> None:
        self.status = status
        self._text = text

    async def text(self) -> str:
        return self._text

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class _FakeSession:
    """Captures the most recent POST so tests can assert on URL / headers / body."""

    def __init__(self, response: _FakeResponse | Exception) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any]) -> Any:
        self.calls.append({"url": url, "headers": headers, "json": json})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


def _install_session(monkeypatch: pytest.MonkeyPatch, session: _FakeSession) -> None:
    monkeypatch.setattr(
        event_publisher.aiohttp,
        "ClientSession",
        lambda timeout=None: session,
    )


async def test_publish_posts_to_configured_url(
    cd_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeSession(_FakeResponse(status=202))
    _install_session(monkeypatch, fake)

    ok = await event_publisher.publish(
        "deploy", "starting docker compose pull", metadata={"image_tag": "abc1234"}
    )

    assert ok is True
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["url"] == "https://backend.example/internal/agent-event"
    assert call["headers"]["X-Agent-Token"] == "s3cret"
    assert call["json"]["stage"] == "deploy"
    assert call["json"]["level"] == "info"
    assert call["json"]["message"] == "starting docker compose pull"
    assert call["json"]["metadata"] == {"image_tag": "abc1234"}
    assert "timestamp" in call["json"]


async def test_publish_returns_false_on_non_2xx(
    cd_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeSession(_FakeResponse(status=401, text="bad token"))
    _install_session(monkeypatch, fake)

    ok = await event_publisher.publish("deploy", "trying")

    assert ok is False
    # The publisher still sent the request — failure is observed via the
    # backend's response, not by the publisher refusing to call.
    assert len(fake.calls) == 1


async def test_publish_swallows_client_errors(
    cd_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeSession(aiohttp.ClientConnectionError("boom"))
    _install_session(monkeypatch, fake)

    # Must NOT raise — fire-and-forget contract.
    ok = await event_publisher.publish("deploy", "trying")
    assert ok is False


async def test_publish_swallows_timeouts(
    cd_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeSession(asyncio.TimeoutError())
    _install_session(monkeypatch, fake)

    ok = await event_publisher.publish("deploy", "trying")
    assert ok is False


async def test_publish_noop_when_disabled(
    disabled_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No URL / no token means we don't even open a session."""
    fake = _FakeSession(_FakeResponse(status=202))
    _install_session(monkeypatch, fake)

    ok = await event_publisher.publish("deploy", "trying")

    assert ok is False
    assert fake.calls == []  # never reached the HTTP layer


async def test_publish_clips_long_messages(
    cd_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeSession(_FakeResponse(status=202))
    _install_session(monkeypatch, fake)

    long_message = "x" * 5000
    await event_publisher.publish("deploy", long_message)

    body = fake.calls[0]["json"]
    assert len(body["message"]) <= 1800
    assert body["message"].endswith("[truncated]")


async def test_publish_safe_never_returns_value(
    cd_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeSession(_FakeResponse(status=202))
    _install_session(monkeypatch, fake)

    # publish_safe returns None and never raises, regardless of success.
    result = await event_publisher.publish_safe("deploy", "go")
    assert result is None


async def test_publish_uses_provided_timestamp(
    cd_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeSession(_FakeResponse(status=202))
    _install_session(monkeypatch, fake)

    ts = "2026-05-29T12:34:56+00:00"
    await event_publisher.publish("deploy", "go", timestamp=ts)
    assert fake.calls[0]["json"]["timestamp"] == ts


async def test_publish_level_propagates(
    cd_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeSession(_FakeResponse(status=202))
    _install_session(monkeypatch, fake)

    await event_publisher.publish("deploy", "rollback fired", level="warn")
    assert fake.calls[0]["json"]["level"] == "warn"


async def test_publish_serialises_to_valid_json_body(
    cd_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """aiohttp passes the dict to json= which serialises it — confirm shape."""
    fake = _FakeSession(_FakeResponse(status=202))
    _install_session(monkeypatch, fake)

    await event_publisher.publish(
        "deploy",
        "ok",
        metadata={"nested": {"image": "ghcr.io/foo:abc"}, "count": 3},
    )
    body = fake.calls[0]["json"]
    # Round-trip through json to confirm it would serialise cleanly.
    encoded = json.dumps(body)
    decoded = json.loads(encoded)
    assert decoded["metadata"]["nested"]["image"] == "ghcr.io/foo:abc"
    assert decoded["metadata"]["count"] == 3
