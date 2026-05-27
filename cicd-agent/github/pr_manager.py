"""
Branches, commits, and PR creation for the CI/CD agent.

Phase-1 production hardening (2026-05):
- ROLLING fix branch (`agent/fixes`) instead of one branch per run. All fixes
  accumulate as commits on this branch until the open PR is merged or closed.
- MULTI-FILE diffs land as one atomic commit via the Git Database API.
- DRY-RUN syntax check after applying the diff in memory — .py is ast-parsed,
  .yml/.yaml is yaml.safe_load'd, .json is json.loads'd. Broken results abort
  before any push.
- BLOCKED_FILE_PATTERNS enforced on every path in a multi-file diff, not just
  the primary file from the diagnosis.
- COMMENT-ON-EXISTING: when an open PR is already addressing the same rolling
  branch, the new fix is appended as a commit + a PR comment with the run id.

Public surface:
- is_file_blocked(path) -> bool
- apply_diff(original_content, diff_text) -> str | None       (single-file, legacy)
- build_patch_set(diff_text, base_ref, mcp_client) -> PatchSet | None
- apply_patch_set(...) -> PatchResult                         (the new entry point)
- create_optimize_pr(...)                                     (unchanged)
"""

from __future__ import annotations

import ast
import fnmatch
import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import whatthepatch
import yaml

from config.constants import (
    AGENT_FIX_COMMIT_TAG,
    BLOCKED_FILE_PATTERNS,
    OPTIMIZE_BRANCH_PREFIX,
    ROLLING_PATCH_BRANCH,
)
from github import rest_api
from github.mcp_client import GitHubMCPClient, GitHubMCPError
from github.pr_risk import assess_risk, format_risk_section, labels_for
from models.run import PatchResult

if TYPE_CHECKING:
    from models.run import Diagnosis

logger = logging.getLogger("cicd_agent.pr_manager")

_DEFAULT_BRANCH = "main"
_PATH_PREFIX_RE = re.compile(r"^[ab]/")
# Matches each per-file header start in a multi-file unified diff. We pre-split
# on this because whatthepatch.parse_patch sometimes collapses adjacent file
# sections into a single Diff object with mixed hunks.
_FILE_SECTION_START_RE = re.compile(r"^--- ", re.MULTILINE)


def is_file_blocked(path: str) -> bool:
    if not path:
        return True
    return any(fnmatch.fnmatch(path, pattern) for pattern in BLOCKED_FILE_PATTERNS)


def _validate_syntax(path: str, content: str) -> tuple[bool, str]:
    """Return (ok, reason). Unknown extensions pass through."""
    if path.endswith(".py"):
        try:
            ast.parse(content)
        except SyntaxError as e:
            return False, f"python syntax error: {e.msg} at line {e.lineno}"
        return True, ""
    if path.endswith((".yml", ".yaml")):
        try:
            yaml.safe_load(content)
        except yaml.YAMLError as e:
            return False, f"yaml syntax error: {e}"
        return True, ""
    if path.endswith(".json"):
        try:
            json.loads(content)
        except json.JSONDecodeError as e:
            return False, f"json syntax error: {e.msg} at line {e.lineno}"
        return True, ""
    return True, ""


def _extract_path(diff: "whatthepatch.patch.diffobj") -> str | None:
    """Pull the post-image path from a parsed diff. Strips `a/` or `b/` prefix."""
    header = getattr(diff, "header", None)
    if header is None:
        return None
    for attr in ("new_path", "old_path", "index_path"):
        candidate = getattr(header, attr, None)
        if isinstance(candidate, str) and candidate.strip() and candidate != "/dev/null":
            return _PATH_PREFIX_RE.sub("", candidate.strip())
    return None


def apply_diff(original_content: str, diff_text: str) -> str | None:
    """Single-file legacy applier. Used by tests and the optimize-PR path."""
    if not diff_text or not diff_text.strip():
        return None

    try:
        diffs = list(whatthepatch.parse_patch(diff_text))
    except Exception as e:
        logger.warning("apply_diff: parse failed: %s", e)
        return None

    if not diffs:
        return None

    diff = diffs[0]
    if not getattr(diff, "changes", None):
        return None

    try:
        result = whatthepatch.apply_diff(diff, original_content)
    except Exception as e:
        logger.warning("apply_diff: %s: %s", type(e).__name__, e)
        return None

    if result is None:
        return None
    if isinstance(result, list):
        patched = "\n".join(result)
    else:
        patched = str(result)
    if not patched.strip():
        return None
    if original_content.endswith("\n") and not patched.endswith("\n"):
        patched += "\n"
    return patched


@dataclass
class PatchSet:
    """Result of parsing + applying a multi-file diff in memory."""

    files: dict[str, str]  # path -> new content
    paths: list[str]       # in diff order, primary first


def _split_file_sections(diff_text: str) -> list[str]:
    """Split a multi-file unified diff on `^--- ` lines.

    whatthepatch.parse_patch sometimes returns a single Diff for inputs that
    span multiple files when there is no `diff --git` or `Index:` separator
    between sections. Splitting first and parsing each section in isolation
    sidesteps that.
    """
    matches = list(_FILE_SECTION_START_RE.finditer(diff_text))
    if not matches:
        return []
    sections: list[str] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(diff_text)
        sections.append(diff_text[start:end])
    return sections


def _normalize_blank_context_lines(section: str) -> str:
    """Pad bare empty lines inside a hunk body with a single space.

    Unified-diff context lines are supposed to start with one space. Many
    LLMs and editors strip trailing whitespace, turning context blank lines
    into bare empty lines that strict patch parsers reject. Only pads empty
    lines that are followed by another diff-content line in the same section
    — trailing empties are left alone.
    """
    if "@@" not in section:
        return section
    lines = section.split("\n")
    n = len(lines)
    in_hunk = False
    for i in range(n):
        if lines[i].startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk or lines[i] != "":
            continue
        for j in range(i + 1, n):
            nxt = lines[j]
            if nxt.startswith((" ", "+", "-", "@@", "\\")):
                lines[i] = " "
                break
            if nxt == "":
                continue
            break
    return "\n".join(lines)


async def build_patch_set(
    diff_text: str,
    base_ref: str,
    mcp_client: GitHubMCPClient,
) -> PatchSet | None:
    """Parse a (possibly multi-file) unified diff and apply each hunk in memory.

    Returns None on any failure: parse error, missing path, blocked path, hunk
    context mismatch, post-patch syntax check failure. We don't return a partial
    set — the patch is all-or-nothing so callers don't have to think about
    intermediate states.
    """
    if not diff_text or not diff_text.strip():
        return None

    sections = _split_file_sections(diff_text)
    if not sections:
        logger.warning("build_patch_set: no `--- ` headers in diff")
        return None

    diffs = []
    for sec in sections:
        normalized = _normalize_blank_context_lines(sec)
        try:
            parsed = list(whatthepatch.parse_patch(normalized))
        except Exception as e:
            logger.warning("build_patch_set: parse section failed: %s", e)
            return None
        if not parsed:
            logger.warning("build_patch_set: section produced no diff")
            return None
        diffs.extend(parsed)

    if not diffs:
        logger.warning("build_patch_set: no diffs parsed")
        return None

    out_files: dict[str, str] = {}
    out_paths: list[str] = []

    for d in diffs:
        path = _extract_path(d)
        if not path:
            logger.warning("build_patch_set: diff entry has no resolvable path")
            return None
        if is_file_blocked(path):
            logger.warning("build_patch_set: blocked path in multi-file diff: %s", path)
            return None
        if not getattr(d, "changes", None):
            logger.warning("build_patch_set: empty hunk for %s", path)
            return None

        try:
            original = await mcp_client.get_file_contents(path, ref=base_ref)
        except GitHubMCPError as e:
            logger.warning("build_patch_set: fetch %s failed: %s", path, e)
            return None

        try:
            patched_raw = whatthepatch.apply_diff(d, original)
        except Exception as e:
            logger.warning("build_patch_set: apply %s raised %s: %s", path, type(e).__name__, e)
            return None
        if patched_raw is None:
            logger.warning("build_patch_set: apply %s returned None (context mismatch)", path)
            return None

        if isinstance(patched_raw, list):
            patched = "\n".join(patched_raw)
        else:
            patched = str(patched_raw)
        if not patched.strip():
            logger.warning("build_patch_set: %s would be empty", path)
            return None
        if original.endswith("\n") and not patched.endswith("\n"):
            patched += "\n"

        ok, reason = _validate_syntax(path, patched)
        if not ok:
            logger.warning("build_patch_set: post-patch syntax invalid for %s: %s", path, reason)
            return None

        out_files[path] = patched
        out_paths.append(path)

    if not out_files:
        return None
    logger.info("build_patch_set: %d file(s) patched in memory: %s", len(out_paths), out_paths)
    return PatchSet(files=out_files, paths=out_paths)


def _format_patch_pr_body(diagnosis: "Diagnosis", paths: list[str], failing_run_url: str) -> str:
    risk = assess_risk(diagnosis, paths)
    lines = [
        "Auto-generated by the CI/CD intelligence agent.",
        "",
        format_risk_section(risk),
        "",
        "**Files changed**",
        *(f"- `{p}`" for p in paths),
        "",
        f"**Error type:** `{diagnosis.error_type}`",
        "",
        "**Diagnosis**",
        diagnosis.explanation,
        "",
        f"**Failing run:** {failing_run_url}" if failing_run_url else "",
        "",
        "Subsequent agent fixes will be appended to this PR as additional commits.",
        "Please review the diff before merging.",
    ]
    return "\n".join(line for line in lines if line is not None)


def _format_patch_pr_comment(
    diagnosis: "Diagnosis",
    paths: list[str],
    run_id: int,
    commit_sha: str,
) -> str:
    lines = [
        f"Additional fix appended for run {run_id} (commit `{commit_sha[:8]}`).",
        "",
        f"**Files changed:** {', '.join(f'`{p}`' for p in paths)}",
        f"**Error type:** `{diagnosis.error_type}`",
        f"**Confidence:** {diagnosis.confidence:.2f}",
        "",
        "**Diagnosis**",
        diagnosis.explanation,
    ]
    return "\n".join(lines)


def _build_commit_message(diagnosis: "Diagnosis", paths: list[str], run_id: int) -> str:
    primary = paths[0]
    short_expl = diagnosis.explanation.replace("\n", " ").strip()[:120]
    subject = f"{AGENT_FIX_COMMIT_TAG} fix {diagnosis.error_type} in {primary} (run {run_id})"
    body_lines = ["", short_expl, "", "Files:"]
    body_lines += [f"- {p}" for p in paths]
    return subject + "\n" + "\n".join(body_lines)


async def _resolve_rolling_branch_state(
    mcp_client: GitHubMCPClient,
    fallback_sha: str,
) -> tuple[str, dict | None]:
    """Return (parent_sha_for_next_commit, open_pr_or_None).

    - If a PR is open for the rolling branch: parent = current branch HEAD
      (append commit).
    - If no PR is open and the branch exists: reset branch to current main
      (avoid carrying stale commits across PR merges).
    - If the branch doesn't exist: create from current main.
    """
    rolling = ROLLING_PATCH_BRANCH
    open_pr = await rest_api.find_open_pr_by_head(rolling)

    main_sha = await rest_api.get_branch_sha(_DEFAULT_BRANCH) or fallback_sha

    if open_pr is not None:
        branch_sha = await rest_api.get_branch_sha(rolling)
        if branch_sha is None:
            # Open PR but no branch? Shouldn't happen — fall back to creating one.
            try:
                await mcp_client.create_branch(rolling, main_sha)
            except GitHubMCPError as e:
                logger.warning("rolling branch create fallback failed: %s", e)
                return main_sha, open_pr
            return main_sha, open_pr
        return branch_sha, open_pr

    # No open PR.
    branch_sha = await rest_api.get_branch_sha(rolling)
    if branch_sha is None:
        await mcp_client.create_branch(rolling, main_sha)
        return main_sha, None

    if branch_sha == main_sha:
        return main_sha, None

    # Branch exists but diverged from main, with no open PR → previous fixes
    # were merged or abandoned. Reset the branch to main so the new fix lands
    # cleanly on top.
    if await rest_api.update_ref(rolling, main_sha, force=True):
        return main_sha, None

    logger.warning("could not reset %s to main — appending to existing tip", rolling)
    return branch_sha, None


async def apply_patch_set(
    diagnosis: "Diagnosis",
    diff: str,
    run_id: int,
    head_sha: str,
    mcp_client: GitHubMCPClient,
) -> PatchResult:
    """Validate, apply, commit, and open/update the PR for a (possibly multi-file)
    unified diff. Replaces the per-run-branch `create_patch_pr` flow."""

    patch_set = await build_patch_set(diff, base_ref=head_sha, mcp_client=mcp_client)
    if patch_set is None:
        return PatchResult(
            branch_name=ROLLING_PATCH_BRANCH,
            success=False,
            attempt_number=1,
            error_message="diff parse/apply/validate failed",
            diff=diff,
        )

    failing_run_url = getattr(diagnosis, "raw_response", "") or ""

    try:
        parent_sha, open_pr = await _resolve_rolling_branch_state(
            mcp_client, fallback_sha=head_sha
        )
    except GitHubMCPError as e:
        return PatchResult(
            branch_name=ROLLING_PATCH_BRANCH,
            success=False,
            attempt_number=1,
            error_message=f"branch setup failed: {e}",
            diff=diff,
        )

    commit_message = _build_commit_message(diagnosis, patch_set.paths, run_id)
    new_sha = await rest_api.create_atomic_commit(
        branch=ROLLING_PATCH_BRANCH,
        files=patch_set.files,
        message=commit_message,
        parent_sha=parent_sha,
    )
    if new_sha is None:
        return PatchResult(
            branch_name=ROLLING_PATCH_BRANCH,
            success=False,
            attempt_number=1,
            error_message="atomic commit failed",
            diff=diff,
        )

    if open_pr is not None:
        pr_number = open_pr.get("number")
        pr_url = open_pr.get("html_url")
        if isinstance(pr_number, int):
            comment_body = _format_patch_pr_comment(
                diagnosis, patch_set.paths, run_id, new_sha
            )
            try:
                await rest_api.post_issue_comment(pr_number, comment_body)
            except Exception as e:
                logger.warning("comment on existing PR #%s failed: %s", pr_number, e)
        return PatchResult(
            branch_name=ROLLING_PATCH_BRANCH,
            success=True,
            attempt_number=1,
            pr_url=pr_url if isinstance(pr_url, str) else None,
            pr_number=pr_number if isinstance(pr_number, int) else None,
            diff=diff,
        )

    title = f"[agent] auto-fixes ({diagnosis.error_type})"
    body = _format_patch_pr_body(diagnosis, patch_set.paths, failing_run_url)
    try:
        pr = await mcp_client.create_pull_request(
            title=title, body=body, head=ROLLING_PATCH_BRANCH
        )
    except GitHubMCPError as e:
        return PatchResult(
            branch_name=ROLLING_PATCH_BRANCH,
            success=False,
            attempt_number=1,
            error_message=f"create_pull_request failed: {e}",
            diff=diff,
        )

    pr_number = pr.get("number") if isinstance(pr.get("number"), int) else None
    pr_url = pr.get("html_url") if isinstance(pr.get("html_url"), str) else None

    # The MCP create_pull_request response doesn't reliably expose number/html_url
    # at the top level (response shape varies). Fall back to REST so dedup
    # (record_open_pr), labels, and the notification link all get a real PR number.
    if pr_number is None or pr_url is None:
        fresh = await rest_api.find_open_pr_by_head(ROLLING_PATCH_BRANCH)
        if fresh is not None:
            if pr_number is None and isinstance(fresh.get("number"), int):
                pr_number = fresh["number"]
            if pr_url is None and isinstance(fresh.get("html_url"), str):
                pr_url = fresh["html_url"]

    # Best-effort labelling — failures don't fail the patch result.
    if isinstance(pr_number, int):
        risk = assess_risk(diagnosis, patch_set.paths)
        labels = labels_for(diagnosis, risk)
        try:
            await rest_api.add_labels(pr_number, labels)
        except Exception as e:
            logger.warning("add_labels for PR #%d failed: %s", pr_number, e)

    return PatchResult(
        branch_name=ROLLING_PATCH_BRANCH,
        success=True,
        attempt_number=1,
        pr_url=pr_url,
        pr_number=pr_number,
        diff=diff,
    )


# ───────────────────────────── YAML optimize PR (unchanged shape) ─────────────


def _format_optimize_pr_body(summary: dict) -> str:
    jobs = summary.get("jobs_parallelized", []) or []
    caches = summary.get("cache_steps_added", []) or []
    savings = int(summary.get("estimated_savings_seconds", 0) or 0)
    explanation = summary.get("explanation", "") or ""

    if savings >= 60:
        savings_display = f"{savings // 60}m {savings % 60}s"
    else:
        savings_display = f"{savings}s"

    lines = [
        "Auto-generated workflow optimization by the CI/CD intelligence agent.",
        "",
        f"**Estimated savings:** {savings_display}",
        "",
        "**Jobs parallelized**",
        ("- " + "\n- ".join(jobs)) if jobs else "- (none)",
        "",
        "**Cache steps added**",
        ("- " + "\n- ".join(caches)) if caches else "- (none)",
        "",
        "**Explanation**",
        explanation or "(no explanation provided)",
        "",
        "Review the YAML before merging — runtime estimates are approximate.",
    ]
    return "\n".join(lines)


async def _get_file_sha(path: str, ref: str, mcp_client: GitHubMCPClient) -> str | None:
    try:
        return await mcp_client.get_file_sha(path, ref)
    except GitHubMCPError as e:
        logger.warning("could not fetch file metadata for %s: %s", path, e)
        return None


async def create_optimize_pr(
    run_id: int,
    optimized_yaml: str,
    workflow_filename: str,
    summary: dict,
    head_sha: str,
    mcp_client: GitHubMCPClient,
) -> tuple[str, int] | None:
    branch_name = f"{OPTIMIZE_BRANCH_PREFIX}-{run_id}"
    workflow_path = f".github/workflows/{workflow_filename}"

    try:
        await mcp_client.create_branch(branch_name, head_sha)
        file_sha = await _get_file_sha(workflow_path, head_sha, mcp_client)
        await mcp_client.push_file(
            path=workflow_path,
            content=optimized_yaml,
            branch=branch_name,
            message=f"{AGENT_FIX_COMMIT_TAG} optimize {workflow_filename} (run {run_id})",
            sha=file_sha,
        )
        body = _format_optimize_pr_body(summary)
        title = f"[agent] optimize {workflow_filename} (run {run_id})"
        pr = await mcp_client.create_pull_request(title=title, body=body, head=branch_name)
    except GitHubMCPError as e:
        logger.warning("create_optimize_pr failed (non-fatal): %s", e)
        return None

    pr_number = pr.get("number") if isinstance(pr.get("number"), int) else None
    pr_url = pr.get("html_url") if isinstance(pr.get("html_url"), str) else None
    if pr_number is None or pr_url is None:
        return None
    return (pr_url, pr_number)
