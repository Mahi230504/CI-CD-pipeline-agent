"""
Code patcher agent — reads the failing file and generates a fix.

Flow:
1. Reads the failing file via github/mcp_client.get_file_contents()
2. Checks BLOCKED_FILE_PATTERNS — hard stop if matched
3. Sends line-numbered file content + Diagnosis + CODE_PATCHER_PROMPT to Gemini
4. Gemini returns the FULL corrected file in a fenced code block (or CANNOT_PATCH)
5. We synthesize the unified diff locally with difflib.unified_diff — never trust
   the LLM to count lines for hunk headers
6. Validates: removal cap, blocked-file recheck, syntax check happen downstream
7. Calls pr_manager to apply, commit, open/update PR
8. Returns PatchResult with PR URL and attempt number

Uses PRIMARY_MODEL (gemini-2.5-flash).
Never touches main branch. Never patches secrets or config files.
"""

from __future__ import annotations

import ast
import difflib
import json
import logging
import re
from dataclasses import replace

from agents.ci_verifier import verify_patch_ci
from agents.log_analyst import _extract_test_paths, _fetch_aux_files
from config.prompts import CODE_PATCHER_SYSTEM_PROMPT
from config.settings import get_settings
from github.mcp_client import GitHubMCPClient, GitHubMCPError
from github.pr_manager import apply_patch_set, is_file_blocked
from llm.gemini_client import get_gemini_client
from llm.rate_limiter import (
    DailyLimitReachedError,
    GeminiError,
    GeminiRateLimitError,
)
from models.events import WorkflowFailureEvent
from models.run import Diagnosis, PatchResult

logger = logging.getLogger("cicd_agent.code_patcher")

# Total `-` line cap across all files in the diff. Stricter than 10 per file
# (single-file limit pre-Phase-1) but generous enough for multi-file refactors
# that the LLM might propose for a single failure.
_MAX_REMOVAL_LINES = 30

# Bounds on the referenced-module context attached to the patch prompt. Enough
# to surface the declarations a fix must reconcile against (types, signatures)
# without blowing up the prompt or the cost.
_MAX_IMPORT_FILES = 3
_MAX_IMPORT_CHARS_TOTAL = 6000


def _first_party_root(file_path: str) -> str | None:
    """Top-level package of a repo source path: 'app/api/items.py' -> 'app'.
    Returns None for a bare top-level file (no package to resolve against)."""
    if not file_path or "/" not in file_path:
        return None
    return file_path.split("/", 1)[0]


def _module_to_paths(module: str) -> list[str]:
    base = module.replace(".", "/")
    return [f"{base}.py", f"{base}/__init__.py"]


def _resolve_first_party_imports(
    file_path: str, content: str, root: str
) -> list[tuple[str, list[str]]]:
    """Dotted modules imported by `content` that belong to the repo's own top
    package `root`, each paired with the names imported from it.

    Resolves relative imports against the file's package. Returns [] when the
    file can't be parsed (e.g. the failure itself is a syntax error) — callers
    just proceed without the extra context. Keyed on imports only; no
    error-type-specific logic lives here."""
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError):
        return []
    pkg_parts = file_path.split("/")[:-1]  # 'app/api/items.py' -> ['app', 'api']
    out: list[tuple[str, list[str]]] = []
    seen: set[str] = set()

    def _consider(module: str, names: list[str]) -> None:
        if module and module.split(".")[0] == root and module not in seen:
            seen.add(module)
            out.append((module, names))

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                drop = node.level - 1
                base_parts = (
                    pkg_parts[: len(pkg_parts) - drop] if drop <= len(pkg_parts) else []
                )
                module = ".".join(base_parts)
                if node.module:
                    module = f"{module}.{node.module}" if module else node.module
            else:
                module = node.module or ""
            _consider(module, [a.name for a in node.names])
        elif isinstance(node, ast.Import):
            for a in node.names:
                _consider(a.name or "", [])
    return out


def _rank_imports(
    imports: list[tuple[str, list[str]]], content: str, line_number: int | None
) -> list[tuple[str, list[str]]]:
    """Float imports whose symbols appear near the failure line to the front,
    so the bounded fetch budget is spent on the most relevant declarations."""
    if not line_number:
        return imports
    lines = content.splitlines()
    if not (1 <= line_number <= len(lines)):
        return imports
    lo = max(0, line_number - 1 - 5)
    hi = min(len(lines), line_number + 5)
    window = "\n".join(lines[lo:hi])

    def _score(item: tuple[str, list[str]]) -> int:
        module, names = item
        tokens = list(names) + [module.split(".")[-1]]
        return sum(1 for t in tokens if t and t in window)

    return sorted(imports, key=_score, reverse=True)


async def _fetch_import_context(
    imports: list[tuple[str, list[str]]],
    event: WorkflowFailureEvent,
    mcp_client: GitHubMCPClient,
) -> list[tuple[str, str]]:
    """Fetch the source of referenced first-party modules, bounded by file
    count and total chars. Tries `<mod>.py` then `<mod>/__init__.py`; the first
    that resolves wins. Missing modules are skipped silently."""
    out: list[tuple[str, str]] = []
    files_left = _MAX_IMPORT_FILES
    chars_left = _MAX_IMPORT_CHARS_TOTAL
    for module, _names in imports:
        if files_left <= 0 or chars_left <= 0:
            break
        for candidate in _module_to_paths(module):
            try:
                content = await mcp_client.get_file_contents(candidate, ref=event.head_sha)
            except Exception:
                continue
            if not content:
                continue
            slice_ = content[:chars_left]
            out.append((candidate, slice_))
            chars_left -= len(slice_)
            files_left -= 1
            break
    return out


def _format_with_line_numbers(content: str) -> str:
    lines = content.splitlines()
    if not lines:
        return ""
    width = max(4, len(str(len(lines))))
    return "\n".join(f"{i + 1:>{width}} | {line}" for i, line in enumerate(lines))


_FENCE_RE = re.compile(r"```(?:[\w+-]*)\s*\n(.*?)```", re.DOTALL)


def _extract_full_file(response_text: str) -> str | None:
    """Pull the corrected file content from Gemini's response.

    Accepts a fenced code block (any language tag) or, as a fallback, the entire
    trimmed response if it contains no fence and no diff syntax. Returns None for
    the CANNOT_PATCH refusal token or anything unparseable.
    """
    if not response_text:
        return None
    text = response_text.strip()
    if "CANNOT_PATCH" in text:
        return None

    m = _FENCE_RE.search(response_text)
    if m:
        candidate = m.group(1).rstrip("\n")
        return candidate if candidate.strip() else None

    if text.startswith("---") or text.startswith("@@"):
        return None
    return text if text else None


def _synthesize_diff(original: str, new_content: str, path: str) -> str:
    """Build a unified diff that whatthepatch will apply cleanly."""
    if original.endswith("\n") and not new_content.endswith("\n"):
        new_content += "\n"
    original_lines = original.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    diff_iter = difflib.unified_diff(
        original_lines,
        new_lines,
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=3,
    )
    return "".join(diff_iter)


async def patch(
    diagnosis: Diagnosis,
    event: WorkflowFailureEvent,
    attempt_number: int,
    mcp_client: GitHubMCPClient,
    failing_log: str | None = None,
) -> PatchResult:
    logger.info(
        "code_patcher: run=%d attempt=%d file=%s",
        event.run_id,
        attempt_number,
        diagnosis.file,
    )

    if diagnosis.file is None:
        return PatchResult(
            branch_name="",
            success=False,
            attempt_number=attempt_number,
            error_message="No file identified in diagnosis",
        )
    if is_file_blocked(diagnosis.file):
        logger.warning("code_patcher: refusing blocked file %s", diagnosis.file)
        return PatchResult(
            branch_name="",
            success=False,
            attempt_number=attempt_number,
            error_message=f"File blocked: {diagnosis.file}",
        )
    if not diagnosis.is_patchable:
        return PatchResult(
            branch_name="",
            success=False,
            attempt_number=attempt_number,
            error_message="Diagnosis marked as not patchable",
        )

    try:
        file_content = await mcp_client.get_file_contents(diagnosis.file, ref=event.head_sha)
    except GitHubMCPError as e:
        return PatchResult(
            branch_name="",
            success=False,
            attempt_number=attempt_number,
            error_message=f"get_file_contents failed: {e}",
        )
    except Exception as e:
        logger.error("code_patcher: file fetch error for %s: %s", diagnosis.file, e)
        return PatchResult(
            branch_name="",
            success=False,
            attempt_number=attempt_number,
            error_message=f"file fetch error: {e}",
        )

    diagnosis_summary = json.dumps(
        {
            "error_type": str(diagnosis.error_type),
            "explanation": diagnosis.explanation,
            "line_number": diagnosis.line_number,
        }
    )
    numbered_content = _format_with_line_numbers(file_content)

    # Pull in the failing test(s) so the model can see the EXPECTED behaviour at
    # every boundary — pytest only prints the first failing assertion per test,
    # so the log alone hides edge cases (e.g. "value exactly at threshold"). A fix
    # that satisfies one assertion but violates another would otherwise slip
    # through. We never patch the test file; it's read-only context here.
    test_context = ""
    if failing_log:
        test_paths = [p for p in _extract_test_paths(failing_log) if p != diagnosis.file]
        if test_paths:
            aux = await _fetch_aux_files(test_paths, event, mcp_client)
            for path, content in aux:
                test_context += (
                    f"\n--- FAILING TEST FILE: {path} (read-only context, do NOT edit) ---\n"
                    f"{content}\n--- END TEST FILE ---\n"
                )

    # Pull in the definitions of first-party modules the failing file imports.
    # Many errors (type mismatches, changed signatures, wrong attributes) can
    # only be fixed correctly by seeing BOTH ends — the use site (this file) AND
    # the declaration (another module). Without this the model must guess the
    # other end. General: keyed on the file's imports, not any error type.
    import_context = ""
    root = _first_party_root(diagnosis.file)
    if root:
        imports = _resolve_first_party_imports(diagnosis.file, file_content, root)
        imports = _rank_imports(imports, file_content, diagnosis.line_number)
        referenced = await _fetch_import_context(imports, event, mcp_client)
        for path, content in referenced:
            import_context += (
                f"\n--- REFERENCED MODULE: {path} (read-only context; "
                f"edit ONLY if the fix belongs here) ---\n{content}\n--- END MODULE ---\n"
            )
        if referenced:
            logger.info(
                "code_patcher: attached %d referenced module(s): %s",
                len(referenced),
                [p for p, _ in referenced],
            )

    prompt_parts = [
        f"File: {diagnosis.file}",
        "--- FILE CONTENT (each line prefixed with `NNNN | `; the prefix is NOT part of the file) ---",
        numbered_content,
        "--- DIAGNOSIS ---",
        diagnosis_summary,
    ]
    if failing_log:
        prompt_parts += ["--- FAILING CI OUTPUT ---", failing_log.strip()]
    if test_context:
        prompt_parts.append(test_context)
    if import_context:
        prompt_parts.append(import_context)
    prompt_parts += [
        "--- END ---",
        "Return the complete corrected file. Make the failing CI output above go away "
        "while keeping every passing check green: satisfy EVERY assertion in any "
        "failing test (including boundary cases), and for type/lint/build errors make "
        "your change consistent with the declarations in the referenced modules above "
        "rather than guessing. Do NOT include the `NNNN | ` prefix in your output.",
    ]
    prompt = "\n".join(prompt_parts)

    try:
        response_text = await get_gemini_client().generate(
            prompt=prompt,
            system_prompt=CODE_PATCHER_SYSTEM_PROMPT,
            agent="code_patcher",
            strip_pii=False,
            temperature=0.1,
        )
    except (GeminiError, GeminiRateLimitError, DailyLimitReachedError) as e:
        return PatchResult(
            branch_name="",
            success=False,
            attempt_number=attempt_number,
            error_message=f"gemini error: {e}",
        )
    except Exception as e:
        logger.error("code_patcher: unexpected gemini error: %s", e)
        return PatchResult(
            branch_name="",
            success=False,
            attempt_number=attempt_number,
            error_message=str(e),
        )

    new_content = _extract_full_file(response_text)
    if new_content is None:
        return PatchResult(
            branch_name="",
            success=False,
            attempt_number=attempt_number,
            error_message="Model signalled CANNOT_PATCH or response unparseable",
        )
    if new_content == file_content:
        return PatchResult(
            branch_name="",
            success=False,
            attempt_number=attempt_number,
            error_message="Model returned file unchanged",
        )
    diff = _synthesize_diff(file_content, new_content, diagnosis.file)
    if not diff.strip():
        return PatchResult(
            branch_name="",
            success=False,
            attempt_number=attempt_number,
            error_message="Synthesized diff is empty",
        )

    removal_count = sum(
        1
        for line in diff.splitlines()
        if line.startswith("-") and not line.startswith("---")
    )
    if removal_count > _MAX_REMOVAL_LINES:
        logger.warning("code_patcher: rejecting destructive diff (%d removals)", removal_count)
        return PatchResult(
            branch_name="",
            success=False,
            attempt_number=attempt_number,
            error_message=f"Diff too destructive: {removal_count} removals",
            diff=diff,
        )

    try:
        result = await apply_patch_set(
            diagnosis=diagnosis,
            diff=diff,
            run_id=event.run_id,
            head_sha=event.head_sha,
            mcp_client=mcp_client,
        )
    except Exception as e:
        logger.error("code_patcher: apply_patch_set error: %s", e)
        return PatchResult(
            branch_name="",
            success=False,
            attempt_number=attempt_number,
            error_message=str(e),
            diff=diff,
        )

    if result.attempt_number != attempt_number:
        result = replace(result, attempt_number=attempt_number)
    return result


async def patch_and_verify(
    diagnosis: Diagnosis,
    event: WorkflowFailureEvent,
    attempt_number: int,
    mcp_client: GitHubMCPClient,
    failing_log: str | None = None,
) -> PatchResult:
    """Open the fix PR, then VERIFY it against its OWN CI before reporting it as
    a fix. While CI stays red and iterations remain, re-patch using the new
    failing output as feedback. Each attempt re-derives the file from the
    original source (fixes don't stack); the rolling-branch PR just gains a new
    commit. The returned PatchResult's `verified` reflects the final CI verdict
    (True = passed, False = still red, None = could not confirm).

    Drop-in replacement for `patch()` — identical signature. With verification
    disabled it behaves exactly like `patch()` (verified left as None)."""
    settings = get_settings()
    result = await patch(diagnosis, event, attempt_number, mcp_client, failing_log)
    if not result.success or not settings.patch_verify_enabled:
        return result

    current = result
    current_log = failing_log
    for iteration in range(settings.patch_verify_max_iterations + 1):
        verdict = await verify_patch_ci(current, event, mcp_client, settings)
        if verdict.verified is True:
            logger.info("patch_and_verify: run=%d verified — %s", event.run_id, verdict.detail)
            return replace(current, verified=True, verification_detail=verdict.detail)
        if verdict.verified is None:
            logger.info(
                "patch_and_verify: run=%d unverified — %s", event.run_id, verdict.detail
            )
            return replace(current, verified=None, verification_detail=verdict.detail)

        # CI is red. Retry with the new failing output as feedback, if budget remains.
        if iteration >= settings.patch_verify_max_iterations:
            logger.info(
                "patch_and_verify: run=%d still red after %d extra attempt(s) — %s",
                event.run_id,
                settings.patch_verify_max_iterations,
                verdict.detail,
            )
            return replace(current, verified=False, verification_detail=verdict.detail)

        logger.info(
            "patch_and_verify: run=%d CI red, re-patching with feedback (retry %d/%d)",
            event.run_id,
            iteration + 1,
            settings.patch_verify_max_iterations,
        )
        retry_log = verdict.failing_log or current_log
        retry = await patch(diagnosis, event, attempt_number, mcp_client, retry_log)
        if not retry.success:
            # Couldn't produce a new fix (e.g. model returned the file unchanged).
            # Keep the PR we have, but report it honestly as still failing.
            return replace(
                current,
                verified=False,
                verification_detail=f"{verdict.detail}; re-patch produced no new change",
            )
        current = retry
        current_log = retry_log

    return replace(current, verified=False, verification_detail="verification budget exhausted")
