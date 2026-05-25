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

import difflib
import json
import logging
import re
from dataclasses import replace

from config.prompts import CODE_PATCHER_SYSTEM_PROMPT
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
    prompt = "\n".join(
        [
            f"File: {diagnosis.file}",
            "--- FILE CONTENT (each line prefixed with `NNNN | `; the prefix is NOT part of the file) ---",
            numbered_content,
            "--- DIAGNOSIS ---",
            diagnosis_summary,
            "--- END ---",
            "Generate a unified diff to fix this bug. Use the prefixed line numbers to "
            "compute correct `@@ -X,Y +A,B @@` hunk headers. Do NOT include the `NNNN | ` "
            "prefix in any line of the diff itself.",
        ]
    )

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
