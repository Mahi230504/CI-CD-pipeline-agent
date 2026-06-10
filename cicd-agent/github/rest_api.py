"""
Thin GitHub REST helpers for operations where the MCP server's tool shape is
awkward or unreliable — branch SHA lookup, listing open PRs by head, posting a
comment to an existing PR.

We still prefer MCP for the hot path (logs, file contents, create branch/file/PR).
This module exists for narrow ops where REST is simpler than coaxing MCP.

All functions use the same PAT and the same User-Agent. Each call is bounded by
a 10s timeout so a flaky network can't hang the pipeline.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from config.settings import get_settings

logger = logging.getLogger(__name__)

_BASE = "https://api.github.com"
_TIMEOUT = aiohttp.ClientTimeout(total=10)


def _headers() -> dict[str, str]:
    settings = get_settings()
    return {
        "Authorization": f"Bearer {settings.github_pat}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "cicd-agent",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _repo_path() -> str:
    settings = get_settings()
    return f"{settings.github_repo_owner}/{settings.github_repo_name}"


async def get_branch_sha(branch: str) -> str | None:
    """Return the HEAD SHA of a branch, or None if it doesn't exist."""
    url = f"{_BASE}/repos/{_repo_path()}/branches/{branch}"
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        async with session.get(url, headers=_headers()) as resp:
            if resp.status == 404:
                return None
            if resp.status != 200:
                text = await resp.text()
                logger.warning("get_branch_sha %s -> %d: %s", branch, resp.status, text[:200])
                return None
            data = await resp.json()
    sha = (data.get("commit") or {}).get("sha")
    return sha if isinstance(sha, str) else None


async def find_open_pr_by_head(branch: str) -> dict[str, Any] | None:
    """Return the first open PR with the given head branch, or None."""
    settings = get_settings()
    head = f"{settings.github_repo_owner}:{branch}"
    url = f"{_BASE}/repos/{_repo_path()}/pulls?state=open&head={head}"
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        async with session.get(url, headers=_headers()) as resp:
            if resp.status != 200:
                text = await resp.text()
                logger.warning("find_open_pr_by_head %s -> %d: %s", branch, resp.status, text[:200])
                return None
            data = await resp.json()
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            return first
    return None


async def get_pull_request(pr_number: int) -> dict[str, Any] | None:
    """Return the PR object, or None if not found."""
    url = f"{_BASE}/repos/{_repo_path()}/pulls/{pr_number}"
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        async with session.get(url, headers=_headers()) as resp:
            if resp.status == 404:
                return None
            if resp.status != 200:
                text = await resp.text()
                logger.warning("get_pull_request %d -> %d: %s", pr_number, resp.status, text[:200])
                return None
            return await resp.json()


async def add_labels(issue_number: int, labels: list[str]) -> bool:
    """Apply labels to an issue or PR. GitHub auto-creates labels that don't exist yet."""
    if not labels:
        return True
    url = f"{_BASE}/repos/{_repo_path()}/issues/{issue_number}/labels"
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        async with session.post(url, headers=_headers(), json={"labels": labels}) as resp:
            if resp.status in (200, 201):
                return True
            text = await resp.text()
            logger.warning("add_labels #%d -> %d: %s", issue_number, resp.status, text[:200])
            return False


async def post_issue_comment(issue_number: int, body: str) -> bool:
    url = f"{_BASE}/repos/{_repo_path()}/issues/{issue_number}/comments"
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        async with session.post(url, headers=_headers(), json={"body": body}) as resp:
            if resp.status in (200, 201):
                return True
            text = await resp.text()
            logger.warning("post_issue_comment %d -> %d: %s", issue_number, resp.status, text[:200])
            return False


async def merge_pull_request(
    pr_number: int,
    *,
    merge_method: str = "squash",
    sha: str | None = None,
) -> tuple[bool, str]:
    """Merge a PR. Returns (merged, detail).

    DELIBERATE DEVIATION from cicd-agent/CLAUDE.md ("Do not merge PRs — agent
    opens PRs, humans merge"): the Agent Console's AUTO toggle ships verified-
    green + low-risk changes unattended, which requires merging. This is gated
    HARD upstream by autonomy_policy.should_ship_unattended (autonomy==auto AND
    CI verified green AND pr_risk LOW AND deploy_guard approved AND high
    confidence); the webhook CI path never calls this. Approved by the project
    owner for the Console feature.

    Degrade-don't-fail: branch protection, lost merge rights, or a conflict
    (403/405/409) return (False, reason) so the chat turn flips to
    AWAITING_APPROVAL with that reason rather than raising. Never raises.
    """
    url = f"{_BASE}/repos/{_repo_path()}/pulls/{pr_number}/merge"
    payload: dict[str, Any] = {"merge_method": merge_method}
    if sha:
        payload["sha"] = sha
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.put(url, headers=_headers(), json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    merged = bool(data.get("merged"))
                    return (merged, str(data.get("sha", "")) if merged else "merge not confirmed")
                detail = (await resp.text())[:200]
                reasons = {
                    403: "no permission to merge (PAT lacks rights / protected branch)",
                    404: "PR or repo not found",
                    405: "PR not mergeable (checks pending or merge blocked)",
                    409: "head SHA out of date or merge conflict",
                    422: "merge rejected (e.g. required reviews)",
                }
                reason = reasons.get(resp.status, f"merge failed (HTTP {resp.status})")
                logger.warning("merge_pull_request #%d -> %d: %s", pr_number, resp.status, detail)
                return (False, reason)
    except Exception as e:  # network/timeout — degrade to pause, never raise
        logger.warning("merge_pull_request #%d errored: %s", pr_number, e)
        return (False, f"merge request errored: {type(e).__name__}")


async def update_ref(branch: str, sha: str, force: bool = False) -> bool:
    """Fast-forward (or force-move) `branch` to point at `sha`."""
    url = f"{_BASE}/repos/{_repo_path()}/git/refs/heads/{branch}"
    payload = {"sha": sha, "force": force}
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        async with session.patch(url, headers=_headers(), json=payload) as resp:
            if resp.status == 200:
                return True
            text = await resp.text()
            logger.warning("update_ref %s -> %d: %s", branch, resp.status, text[:200])
            return False


async def create_atomic_commit(
    branch: str,
    files: dict[str, str],
    message: str,
    parent_sha: str,
) -> str | None:
    """Create a single commit on `branch` that updates all `files` at once.

    Uses GitHub's Git Database API (blob → tree → commit → update ref). This is
    the only way to land a multi-file change in one commit; create_or_update_file
    produces one commit per file. Returns the new commit SHA, or None on failure.
    """
    if not files:
        return None

    headers = _headers()
    repo_path = _repo_path()
    base_url = f"{_BASE}/repos/{repo_path}"

    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        # 1. Parent commit → its tree sha
        async with session.get(f"{base_url}/git/commits/{parent_sha}", headers=headers) as resp:
            if resp.status != 200:
                logger.warning("atomic_commit: parent commit %s -> %d", parent_sha, resp.status)
                return None
            parent_commit = await resp.json()
        parent_tree_sha = (parent_commit.get("tree") or {}).get("sha")
        if not isinstance(parent_tree_sha, str):
            logger.warning("atomic_commit: parent tree sha missing")
            return None

        # 2. Blob per file
        tree_entries: list[dict[str, Any]] = []
        for path, content in files.items():
            async with session.post(
                f"{base_url}/git/blobs",
                headers=headers,
                json={"content": content, "encoding": "utf-8"},
            ) as resp:
                if resp.status not in (200, 201):
                    text = await resp.text()
                    logger.warning(
                        "atomic_commit: blob %s -> %d: %s", path, resp.status, text[:200]
                    )
                    return None
                blob = await resp.json()
            tree_entries.append(
                {"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]}
            )

        # 3. Tree referencing parent tree + new blobs
        async with session.post(
            f"{base_url}/git/trees",
            headers=headers,
            json={"base_tree": parent_tree_sha, "tree": tree_entries},
        ) as resp:
            if resp.status not in (200, 201):
                text = await resp.text()
                logger.warning("atomic_commit: tree -> %d: %s", resp.status, text[:200])
                return None
            tree = await resp.json()

        # 4. Commit
        async with session.post(
            f"{base_url}/git/commits",
            headers=headers,
            json={
                "message": message,
                "tree": tree["sha"],
                "parents": [parent_sha],
            },
        ) as resp:
            if resp.status not in (200, 201):
                text = await resp.text()
                logger.warning("atomic_commit: commit -> %d: %s", resp.status, text[:200])
                return None
            commit = await resp.json()

        # 5. Move branch ref to the new commit
        async with session.patch(
            f"{base_url}/git/refs/heads/{branch}",
            headers=headers,
            json={"sha": commit["sha"], "force": False},
        ) as resp:
            if resp.status not in (200, 201):
                text = await resp.text()
                logger.warning(
                    "atomic_commit: update ref %s -> %d: %s", branch, resp.status, text[:200]
                )
                return None

    logger.info(
        "atomic_commit: %s -> %s (%d file%s)",
        branch,
        commit["sha"][:8],
        len(files),
        "" if len(files) == 1 else "s",
    )
    return commit["sha"]


# ── CD inputs ────────────────────────────────────────────────────────────
# These helpers exist for the deploy guard: it needs the unified diff of a
# merged PR plus the list of files changed. The MCP server can serve this
# data too but with awkward pagination; REST is simpler for the synchronous
# shape this code path needs.


async def get_pr_diff(pr_number: int) -> str | None:
    """Fetch the unified diff of a PR via the `vnd.github.v3.diff` Accept header.

    Returns None on any non-200, including the 406 GitHub returns when a PR
    has zero files (rare but possible for revert-then-merge sequences).
    Diff size is unbounded — the caller (deploy_guard) truncates if needed.
    """
    url = f"{_BASE}/repos/{_repo_path()}/pulls/{pr_number}"
    headers = {**_headers(), "Accept": "application/vnd.github.v3.diff"}
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                text = await resp.text()
                logger.warning(
                    "get_pr_diff #%d -> %d: %s", pr_number, resp.status, text[:200]
                )
                return None
            return await resp.text()


async def get_pr_files(pr_number: int) -> list[str]:
    """Return the file paths changed by a PR.

    Returns an empty list on any failure — deploy_guard tolerates missing
    file lists (the diff carries the signal); this is a convenience for the
    notifier and audit log.
    """
    url = f"{_BASE}/repos/{_repo_path()}/pulls/{pr_number}/files?per_page=100"
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        async with session.get(url, headers=_headers()) as resp:
            if resp.status != 200:
                text = await resp.text()
                logger.warning(
                    "get_pr_files #%d -> %d: %s", pr_number, resp.status, text[:200]
                )
                return []
            data = await resp.json()
    if not isinstance(data, list):
        return []
    out: list[str] = []
    for entry in data:
        if isinstance(entry, dict):
            path = entry.get("filename")
            if isinstance(path, str) and path:
                out.append(path)
    return out


async def get_pulls_for_commit(sha: str) -> list[dict[str, Any]]:
    """List PRs that contain `sha`. Used to map a workflow_run head_sha
    back to its merged PR when GitHub doesn't populate workflow_run.pull_requests.

    Returns the raw PR dicts (with `number`, `title`, `body`, `state`,
    `merged_at`, etc.). Empty list on any failure.
    """
    url = f"{_BASE}/repos/{_repo_path()}/commits/{sha}/pulls"
    # The endpoint requires this specific Accept value to enumerate associated PRs.
    headers = {**_headers(), "Accept": "application/vnd.github.groot-preview+json"}
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                text = await resp.text()
                logger.warning(
                    "get_pulls_for_commit %s -> %d: %s", sha[:8], resp.status, text[:200]
                )
                return []
            data = await resp.json()
    return data if isinstance(data, list) else []
