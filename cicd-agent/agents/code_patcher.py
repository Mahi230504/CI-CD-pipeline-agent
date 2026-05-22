"""
Code patcher agent — reads the failing file and generates a fix.

Flow:
1. Reads the failing file via github/mcp_client.get_file_contents()
2. Checks BLOCKED_FILE_PATTERNS — hard stop if matched
3. Sends file content + Diagnosis + CODE_PATCHER_PROMPT to Gemini
4. Parses response through response_parser.parse_diff()
5. Validates diff: must be valid unified diff, must not delete >50% of file
6. Calls pr_manager to create branch, apply diff, commit, open PR
7. Returns PatchResult with PR URL and attempt number

Uses PRIMARY_MODEL (gemini-2.5-flash).
Never touches main branch. Never patches secrets or config files.
"""

from __future__ import annotations

import json
import logging
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
from llm.response_parser import parse_diff
from models.events import WorkflowFailureEvent
from models.run import Diagnosis, PatchResult

logger = logging.getLogger("cicd_agent.code_patcher")

# Total `-` line cap across all files in the diff. Stricter than 10 per file
# (single-file limit pre-Phase-1) but generous enough for multi-file refactors
# that the LLM might propose for a single failure.
_MAX_REMOVAL_LINES = 30


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
    prompt = "\n".join(
        [
            f"File: {diagnosis.file}",
            "--- FILE CONTENT ---",
            file_content,
            "--- DIAGNOSIS ---",
            diagnosis_summary,
            "--- END ---",
            "Generate a unified diff to fix this bug.",
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

    diff = parse_diff(response_text)
    if diff is None:
        return PatchResult(
            branch_name="",
            success=False,
            attempt_number=attempt_number,
            error_message="Model signalled CANNOT_PATCH or diff unparseable",
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
