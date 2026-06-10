"""The MCP client must never hang.

Every GitHub tool call and the initial connect handshake are bounded by a hard
timeout that surfaces as a normal ``GitHubMCPError`` — the same exception every
caller already degrades on. This is the guard that fixes the "stuck at the
flakiness check" freeze, where ``list_workflow_runs`` stalled on a flaky GitHub
edge and only the multi-minute outer budget could break it.

These tests use tiny timeouts against deliberately-hanging stand-ins, so they
prove the bound fires *fast* rather than waiting on a real network.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

import github.mcp_client as mcp_mod
from github.mcp_client import GitHubMCPClient, GitHubMCPError


class _HangingSession:
    """ClientSession stand-in whose call_tool never returns on its own."""

    async def call_tool(self, name, arguments):  # noqa: ANN001 - test stub
        await asyncio.sleep(3600)


async def test_call_tool_times_out_fast():
    client = GitHubMCPClient()
    client._session = _HangingSession()  # bypass the real connect
    client._call_timeout = 0.05

    started = time.monotonic()
    with pytest.raises(GitHubMCPError) as ei:
        await client._call_tool("actions_list", {"method": "list_workflow_runs"})
    elapsed = time.monotonic() - started

    assert "timed out" in str(ei.value).lower()
    assert ei.value.tool_name == "actions_list"
    # Bounded by the 0.05s timeout, not the 3600s sleep.
    assert elapsed < 1.0


async def test_high_level_method_degrades_on_hang():
    """A hang surfaces through a public method as GitHubMCPError — which
    run_history.get_last_n_runs already swallows into an empty list, so the
    flakiness detector proceeds instead of freezing."""
    client = GitHubMCPClient()
    client._session = _HangingSession()
    client._call_timeout = 0.05

    with pytest.raises(GitHubMCPError):
        await client.list_workflow_runs("CI", per_page=5)


async def test_run_history_returns_empty_when_calls_hang(monkeypatch):
    """End-to-end of the degradation path: a hanging MCP call makes
    get_last_n_runs return [] (no signal) rather than block the pipeline."""
    from github import run_history

    run_history.clear_cache()
    client = GitHubMCPClient()
    client._session = _HangingSession()
    client._call_timeout = 0.05

    # get_last_n_runs reads settings for the cache key; give it deterministic ids.
    monkeypatch.setattr(
        run_history,
        "get_settings",
        lambda: SimpleNamespace(github_repo_owner="o", github_repo_name="r"),
    )

    runs = await run_history.get_last_n_runs("CI", 5, client)
    assert runs == []
    run_history.clear_cache()


async def test_connect_times_out_fast(monkeypatch):
    """A dead/slow MCP edge fails fast at connect rather than hanging before the
    pipeline's first phase."""
    fake_settings = SimpleNamespace(
        github_repo_owner="o",
        github_repo_name="r",
        mcp_call_timeout_seconds=45,
        mcp_connect_timeout_seconds=0.05,
        github_mcp_url="http://example.invalid/mcp",
        github_mcp_headers={},
    )
    monkeypatch.setattr(mcp_mod, "get_settings", lambda: fake_settings)

    class _HangingTransport:
        async def __aenter__(self):
            await asyncio.sleep(3600)

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(
        mcp_mod, "streamablehttp_client", lambda *a, **k: _HangingTransport()
    )

    started = time.monotonic()
    with pytest.raises(GitHubMCPError) as ei:
        async with GitHubMCPClient():
            pass
    elapsed = time.monotonic() - started

    assert "connect" in str(ei.value).lower()
    assert ei.value.tool_name == "__connect__"
    assert elapsed < 1.0
