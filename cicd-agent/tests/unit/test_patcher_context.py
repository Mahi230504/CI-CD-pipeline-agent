"""Tests for code_patcher's import-aware context and honest patch labelling.

These cover the generalisable parts of the patch-quality work: resolving the
first-party modules a failing file imports (so the patcher can see BOTH ends of
a mismatch), and the summary line that only calls a fix "[FIXED]" once CI is
verified. Nothing here is specific to any one error type.
"""

from __future__ import annotations

from agents.code_patcher import (
    _fetch_import_context,
    _first_party_root,
    _module_to_paths,
    _rank_imports,
    _resolve_first_party_imports,
)
from models.run import PatchResult
from models.task import NotificationPayload

_SRC = """\
from __future__ import annotations
from datetime import datetime
from fastapi import APIRouter
from app.models import Item
from app.schemas import ItemCreate, ItemOut
from .deps import db_session
import app.config
"""


def test_first_party_root():
    assert _first_party_root("app/api/items.py") == "app"
    assert _first_party_root("service.py") is None
    assert _first_party_root("") is None


def test_module_to_paths():
    assert _module_to_paths("app.models") == ["app/models.py", "app/models/__init__.py"]


def test_resolve_first_party_imports_filters_and_resolves_relative():
    found = _resolve_first_party_imports("app/api/items.py", _SRC, "app")
    modules = {m for m, _ in found}
    # First-party absolute, dotted, and relative imports are included…
    assert "app.models" in modules
    assert "app.schemas" in modules
    assert "app.api.deps" in modules  # `from .deps` resolved against app/api
    assert "app.config" in modules  # bare `import app.config`
    # …third-party / stdlib / __future__ are excluded.
    assert "datetime" not in modules
    assert "fastapi" not in modules
    assert "__future__" not in modules
    # imported names are captured for ranking
    names = dict(found)["app.schemas"]
    assert set(names) == {"ItemCreate", "ItemOut"}


def test_resolve_first_party_imports_tolerates_syntax_error():
    # The failure itself may be a syntax error — never raise, just return [].
    assert _resolve_first_party_imports("app/x.py", "def broken(:\n", "app") == []


def test_rank_imports_prioritises_symbols_near_failure_line():
    content = "\n".join(
        [
            "x = 1",  # 1
            "y = 2",  # 2
            "z = Item()",  # 3  <- failure line references Item
            "w = 4",  # 4
        ]
    )
    imports = [("app.schemas", ["ItemCreate"]), ("app.models", ["Item"])]
    ranked = _rank_imports(imports, content, line_number=3)
    assert ranked[0][0] == "app.models"  # the one used near line 3 floats up


class _FakeMCP:
    def __init__(self, files: dict[str, str]) -> None:
        self.files = files
        self.requested: list[str] = []

    async def get_file_contents(self, path: str, **kwargs) -> str:
        self.requested.append(path)
        return self.files.get(path, "")


async def test_fetch_import_context_resolves_first_candidate_and_bounds():
    mcp = _FakeMCP({"app/models.py": "class Item: ...", "app/schemas.py": "class ItemCreate: ..."})
    imports = [("app.models", ["Item"]), ("app.schemas", ["ItemCreate"])]
    out = await _fetch_import_context(imports, mcp, "deadbeefcafe")
    fetched = {p for p, _ in out}
    assert fetched == {"app/models.py", "app/schemas.py"}
    # `app/models.py` resolved on the first candidate, so __init__.py isn't tried.
    assert "app/models/__init__.py" not in mcp.requested


async def test_fetch_import_context_skips_missing_modules():
    mcp = _FakeMCP({})  # nothing resolves
    out = await _fetch_import_context([("app.gone", [])], mcp, "ref")
    assert out == []


# ── honest labelling ──────────────────────────────────────────────────────


def _payload(pr: PatchResult | None) -> NotificationPayload:
    return NotificationPayload(
        run_id=1,
        repo_full_name="o/r",
        branch="main",
        html_url="u",
        is_flaky=False,
        flakiness_reason=None,
        diagnosis=None,
        patch_result=pr,
        optimization_result=None,
        pipeline_duration_seconds=1.0,
    )


def _pr(**over) -> PatchResult:
    base = dict(branch_name="agent/fixes", success=True, attempt_number=1, pr_url="http://pr/15")
    base.update(over)
    return PatchResult(**base)


def test_summary_fixed_only_when_verified():
    assert _payload(_pr(verified=True)).summary_line.startswith("[FIXED]")


def test_summary_needs_review_when_ci_red():
    line = _payload(_pr(verified=False)).summary_line
    assert line.startswith("[PATCH NEEDS REVIEW]")


def test_summary_opened_when_unconfirmed():
    line = _payload(_pr(verified=None)).summary_line
    assert line.startswith("[PATCH OPENED]")


def test_summary_duplicate_for_dedup_path():
    line = _payload(_pr(attempt_number=0, verified=None)).summary_line
    assert line.startswith("[DUPLICATE]")


def test_summary_failed_when_patch_unsuccessful():
    line = _payload(_pr(success=False)).summary_line
    assert line.startswith("[FAILED]")
