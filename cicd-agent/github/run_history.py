"""
Fetches historical run data for flakiness detection.

Functions:
- get_last_n_runs(workflow_name, n, mcp_client) → list[dict]
- compute_pass_rate(runs) → float
- get_workflow_yaml_files(mcp_client) → dict[str, str]
- clear_cache() → None

Results are cached in-memory per pipeline execution (not persisted)
to avoid redundant MCP calls within the same agent task.
"""

from __future__ import annotations

import logging

from config.settings import get_settings
from github.mcp_client import GitHubMCPClient

logger = logging.getLogger(__name__)


_run_cache: dict[str, list[dict]] = {}


async def get_last_n_runs(
    workflow_name: str,
    n: int,
    mcp_client: GitHubMCPClient,
) -> list[dict]:
    settings = get_settings()
    key = f"{settings.github_repo_owner}/{settings.github_repo_name}/{workflow_name}"

    if key in _run_cache:
        return _run_cache[key][:n]

    try:
        runs = await mcp_client.list_workflow_runs(workflow_name, per_page=n)
    except Exception as e:
        logger.warning("failed to list runs for %s: %s", workflow_name, e)
        return []

    sorted_runs = sorted(
        runs,
        key=lambda r: r.get("created_at", "") if isinstance(r, dict) else "",
        reverse=True,
    )[:n]
    _run_cache[key] = sorted_runs
    return sorted_runs


def compute_pass_rate(runs: list[dict]) -> float:
    """Pass rate over runs that actually finished. `skipped` / `cancelled` /
    `neutral` runs are excluded from both numerator and denominator — they
    carry no signal about whether the workflow is stable.

    Returns 0.0 if no runs finished with a real verdict (so the caller can
    distinguish "no signal" from "always failing")."""
    if not runs:
        return 0.0
    decisive: list[dict] = []
    for r in runs:
        if not isinstance(r, dict):
            continue
        conclusion = r.get("conclusion")
        if conclusion in ("success", "failure"):
            decisive.append(r)
    if not decisive:
        return 0.0
    successes = sum(1 for r in decisive if r.get("conclusion") == "success")
    return successes / len(decisive)


def had_success_at_sha(runs: list[dict], head_sha: str) -> bool:
    """Did any of the supplied runs succeed at exactly this head_sha?
    A clean "yes" is the canonical signal for genuine flakiness: same code,
    sometimes passes, sometimes fails."""
    if not head_sha:
        return False
    for r in runs:
        if (
            isinstance(r, dict)
            and r.get("conclusion") == "success"
            and r.get("head_sha") == head_sha
        ):
            return True
    return False


async def get_workflow_yaml_files(mcp_client: GitHubMCPClient) -> dict[str, str]:
    try:
        filenames = await mcp_client.list_workflow_files()
    except Exception as e:
        logger.warning("failed to list workflow files: %s", e)
        return {}

    out: dict[str, str] = {}
    for filename in filenames:
        try:
            content = await mcp_client.get_workflow_yaml(filename)
            if content:
                out[filename] = content
        except Exception as e:
            logger.warning("failed to fetch workflow %s: %s", filename, e)
            continue
    return out


def clear_cache() -> None:
    _run_cache.clear()
