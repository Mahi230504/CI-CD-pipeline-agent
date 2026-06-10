"""Slice-4 tests: chat_editor.generate_edit (fake LLM + fake MCP)."""

from __future__ import annotations

import pytest

from agents import chat_editor as ce


class FakeMCP:
    async def get_file_contents(self, path, ref="main"):
        raise FileNotFoundError  # treat as a new file


def _patch_llm(monkeypatch, response: str):
    class FakeGemini:
        async def generate(self, *a, **k):
            return response

    monkeypatch.setattr(ce, "get_gemini_client", lambda: FakeGemini())


async def test_creates_new_file(monkeypatch) -> None:
    _patch_llm(
        monkeypatch,
        '{"files":[{"path":"app/api/low_stock.py","content":"def f():\\n    return 1\\n"}],'
        '"summary":"add low-stock endpoint"}',
    )
    proposal = await ce.generate_edit("add a low-stock endpoint", FakeMCP())
    assert proposal.is_actionable
    assert proposal.files == ["app/api/low_stock.py"]
    assert "low_stock" in proposal.diff
    assert proposal.summary == "add low-stock endpoint"


async def test_tolerates_markdown_fence(monkeypatch) -> None:
    _patch_llm(
        monkeypatch,
        '```json\n{"files":[{"path":"app/x.py","content":"x = 1\\n"}],"summary":"s"}\n```',
    )
    proposal = await ce.generate_edit("do it", FakeMCP())
    assert proposal.is_actionable and proposal.files == ["app/x.py"]


async def test_refuses_blocked_path(monkeypatch) -> None:
    # ".env" is in BLOCKED_FILE_PATTERNS (pr_manager.is_file_blocked).
    _patch_llm(
        monkeypatch,
        '{"files":[{"path":".env","content":"SECRET=1\\n"}],"summary":"s"}',
    )
    proposal = await ce.generate_edit("change env", FakeMCP())
    assert not proposal.is_actionable
    assert "protected" in (proposal.cannot_reason or "")


async def test_cannot_response(monkeypatch) -> None:
    _patch_llm(monkeypatch, '{"cannot":"out of scope"}')
    proposal = await ce.generate_edit("delete the database", FakeMCP())
    assert not proposal.is_actionable
    assert proposal.cannot_reason == "out of scope"


async def test_rejects_invalid_syntax(monkeypatch) -> None:
    _patch_llm(
        monkeypatch,
        '{"files":[{"path":"app/bad.py","content":"def broken(\\n"}],"summary":"s"}',
    )
    proposal = await ce.generate_edit("add broken code", FakeMCP())
    assert not proposal.is_actionable
    assert "syntax" in (proposal.cannot_reason or "")
