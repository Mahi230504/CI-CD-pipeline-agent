"""Chat editor — turns a natural-language instruction into an EditProposal.

Unlike code_patcher (which fixes ONE known failing file from a Diagnosis), the
editor is instruction-driven and decides which file(s) to create/modify. It asks
the LLM for COMPLETE file contents (not a diff) — robust for new files, where
there's no original to apply hunks against — then validates paths + syntax and
synthesizes a display-only diff for the chat UI.

Safety: blocked/sensitive paths are refused here (defense in depth on top of the
prompt and the downstream pr_risk gate). Never raises — returns an EditProposal
with cannot_reason set on any problem.
"""

from __future__ import annotations

import json
import logging
import re

from agents.code_patcher import _synthesize_diff
from config.prompts import CHAT_EDITOR_SYSTEM_PROMPT
from github.mcp_client import GitHubMCPClient
from github.pr_manager import _validate_syntax, is_file_blocked
from llm.gemini_client import get_gemini_client
from llm.rate_limiter import DailyLimitReachedError, GeminiError, GeminiRateLimitError
from models.chat import EditProposal

logger = logging.getLogger("cicd_agent.chat_editor")

_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.DOTALL)


def _parse_json(text: str) -> dict | None:
    """Parse the editor's JSON reply, tolerating a markdown fence wrapper."""
    if not text:
        return None
    candidate = text.strip()
    m = _FENCE_RE.search(candidate)
    if m:
        candidate = m.group(1).strip()
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError:
        # Last resort: grab the outermost {...}.
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            obj = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            return None
    return obj if isinstance(obj, dict) else None


async def generate_edit(
    instruction: str,
    mcp_client: GitHubMCPClient,
    *,
    ref: str = "main",
) -> EditProposal:
    """Produce an EditProposal for `instruction`. `ref` is the base branch the
    display diff is computed against (and where existing files are read from)."""
    try:
        response = await get_gemini_client().generate(
            prompt=f"Instruction:\n{instruction}",
            system_prompt=CHAT_EDITOR_SYSTEM_PROMPT,
            agent="chat_editor",
            temperature=0.1,
        )
    except (GeminiError, GeminiRateLimitError, DailyLimitReachedError) as e:
        return EditProposal(cannot_reason=f"LLM error: {e}")
    except Exception as e:  # noqa: BLE001
        logger.error("chat_editor: unexpected LLM error: %s", e)
        return EditProposal(cannot_reason=f"LLM error: {type(e).__name__}")

    obj = _parse_json(response)
    if obj is None:
        return EditProposal(cannot_reason="could not parse the editor response")
    if obj.get("cannot"):
        return EditProposal(cannot_reason=str(obj["cannot"]))

    raw_files = obj.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        return EditProposal(cannot_reason="editor returned no files")

    file_contents: dict[str, str] = {}
    for entry in raw_files:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        content = entry.get("content")
        if not isinstance(path, str) or not isinstance(content, str) or not path:
            continue
        if is_file_blocked(path):
            return EditProposal(cannot_reason=f"refusing to edit a protected path: {path}")
        ok, reason = _validate_syntax(path, content)
        if not ok:
            return EditProposal(cannot_reason=f"generated {path} has invalid syntax: {reason}")
        file_contents[path] = content

    if not file_contents:
        return EditProposal(cannot_reason="no valid files in the editor response")

    # Synthesize a display-only diff (new files diff against an empty original).
    diff_parts: list[str] = []
    for path, new_content in file_contents.items():
        try:
            original = await mcp_client.get_file_contents(path, ref=ref)
        except Exception:
            original = ""  # new file
        diff_parts.append(_synthesize_diff(original or "", new_content, path))

    return EditProposal(
        file_contents=file_contents,
        diff="".join(diff_parts),
        summary=str(obj.get("summary", "")).strip() or "Apply the requested change",
    )
