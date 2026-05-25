"""
Unit tests for github/pr_manager.py — the multi-file diff applier and
dry-run validator that landed in Phase 1.

The tests use a fake mcp_client that serves up file contents from an in-memory
dict, so we exercise the real `build_patch_set` flow without hitting GitHub.
"""

from __future__ import annotations

import textwrap

import pytest

from github.pr_manager import (
    _extract_path,
    _validate_syntax,
    apply_diff,
    build_patch_set,
    is_file_blocked,
)


# ───────────────────────────── is_file_blocked ────────────────────────────────


def test_is_file_blocked_env():
    assert is_file_blocked(".env") is True
    assert is_file_blocked(".env.production") is True


def test_is_file_blocked_pem_key():
    assert is_file_blocked("certs/server.pem") is True
    assert is_file_blocked("deploy.key") is True


def test_is_file_blocked_workflow():
    assert is_file_blocked(".github/workflows/ci.yml") is True


def test_is_file_blocked_secret_pattern():
    assert is_file_blocked("config/db_password.txt") is True
    assert is_file_blocked("aws_credentials.json") is True


def test_is_file_blocked_normal_source():
    assert is_file_blocked("src/app/main.py") is False
    assert is_file_blocked("tests/test_math.py") is False


def test_is_file_blocked_empty():
    assert is_file_blocked("") is True


# ───────────────────────────── _validate_syntax ───────────────────────────────


def test_validate_python_good():
    ok, _ = _validate_syntax("foo.py", "def hello():\n    return 1\n")
    assert ok is True


def test_validate_python_bad():
    ok, reason = _validate_syntax("foo.py", "def hello(:\n    return\n")
    assert ok is False
    assert "syntax" in reason.lower()


def test_validate_yaml_good():
    ok, _ = _validate_syntax("ci.yml", "name: CI\non: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n")
    assert ok is True


def test_validate_yaml_bad():
    ok, reason = _validate_syntax("ci.yml", "name: CI\n  bad indent: x\n: ::\n")
    assert ok is False
    assert "yaml" in reason.lower()


def test_validate_json_good():
    ok, _ = _validate_syntax("pkg.json", '{"name": "x", "deps": []}')
    assert ok is True


def test_validate_json_bad():
    ok, reason = _validate_syntax("pkg.json", '{"name": "x", "deps": [},,}')
    assert ok is False


def test_validate_unknown_extension_passes():
    ok, _ = _validate_syntax("README.md", "some text content")
    assert ok is True


# ─────────────────────────────── _extract_path ────────────────────────────────


def test_extract_path_strips_ab_prefix():
    """Whatthepatch returns paths like 'a/foo.py' / 'b/foo.py' — we strip them."""
    import whatthepatch

    diff_text = textwrap.dedent(
        """\
        --- a/src/app.py
        +++ b/src/app.py
        @@ -1,2 +1,2 @@
         x = 1
        -y = 2
        +y = 3
        """
    )
    diffs = list(whatthepatch.parse_patch(diff_text))
    assert len(diffs) == 1
    assert _extract_path(diffs[0]) == "src/app.py"


# ───────────────────────────────── apply_diff ─────────────────────────────────


def test_apply_diff_single_file_legacy():
    original = "line one\nline two\nline three\n"
    diff = textwrap.dedent(
        """\
        --- a/foo.txt
        +++ b/foo.txt
        @@ -1,3 +1,3 @@
         line one
        -line two
        +line TWO
         line three
        """
    )
    result = apply_diff(original, diff)
    assert result is not None
    assert "line TWO" in result
    assert "line two" not in result


# ───────────────────────────── build_patch_set ────────────────────────────────


class _FakeMCP:
    """Stand-in mcp_client for build_patch_set: serves files from a dict."""

    def __init__(self, files: dict[str, str]):
        self._files = files

    async def get_file_contents(self, path: str, ref: str = "main") -> str:
        if path not in self._files:
            from github.mcp_client import GitHubMCPError
            raise GitHubMCPError(f"not found: {path}")
        return self._files[path]


@pytest.mark.asyncio
async def test_build_patch_set_single_file():
    original = "x = 1\ny = 2\nz = 3\n"
    diff = textwrap.dedent(
        """\
        --- a/foo.py
        +++ b/foo.py
        @@ -1,3 +1,3 @@
         x = 1
        -y = 2
        +y = 99
         z = 3
        """
    )
    mcp = _FakeMCP({"foo.py": original})
    result = await build_patch_set(diff, base_ref="main", mcp_client=mcp)
    assert result is not None
    assert result.paths == ["foo.py"]
    assert "y = 99" in result.files["foo.py"]
    assert "y = 2" not in result.files["foo.py"]


@pytest.mark.asyncio
async def test_build_patch_set_multi_file():
    file_a = "def a():\n    return 1\n"
    file_b = "from a import a\n\ndef b():\n    return a() + 1\n"
    diff = textwrap.dedent(
        """\
        --- a/a.py
        +++ b/a.py
        @@ -1,2 +1,2 @@
         def a():
        -    return 1
        +    return 10
        --- a/b.py
        +++ b/b.py
        @@ -1,4 +1,4 @@
         from a import a

         def b():
        -    return a() + 1
        +    return a() + 10
        """
    )
    mcp = _FakeMCP({"a.py": file_a, "b.py": file_b})
    result = await build_patch_set(diff, base_ref="main", mcp_client=mcp)
    assert result is not None
    assert sorted(result.paths) == ["a.py", "b.py"]
    assert "return 10" in result.files["a.py"]
    assert "return a() + 10" in result.files["b.py"]


@pytest.mark.asyncio
async def test_build_patch_set_rejects_blocked_file():
    diff = textwrap.dedent(
        """\
        --- a/.env
        +++ b/.env
        @@ -1 +1 @@
        -API_KEY=old
        +API_KEY=new
        """
    )
    mcp = _FakeMCP({".env": "API_KEY=old\n"})
    result = await build_patch_set(diff, base_ref="main", mcp_client=mcp)
    assert result is None


@pytest.mark.asyncio
async def test_build_patch_set_rejects_python_syntax_break():
    """If the diff would produce invalid Python, build_patch_set must reject it."""
    original = "def hello():\n    return 1\n"
    diff = textwrap.dedent(
        """\
        --- a/foo.py
        +++ b/foo.py
        @@ -1,2 +1,2 @@
        -def hello():
        -    return 1
        +def hello(:
        +    return broken
        """
    )
    mcp = _FakeMCP({"foo.py": original})
    result = await build_patch_set(diff, base_ref="main", mcp_client=mcp)
    assert result is None


@pytest.mark.asyncio
async def test_build_patch_set_rejects_unparseable_diff():
    mcp = _FakeMCP({})
    result = await build_patch_set("this is not a diff", base_ref="main", mcp_client=mcp)
    assert result is None
