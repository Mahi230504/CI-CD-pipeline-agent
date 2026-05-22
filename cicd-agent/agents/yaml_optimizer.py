"""
YAML optimizer agent — reduces pipeline runtime by parallelization and caching.

Flow:
1. Reads all .github/workflows/*.yml via run_history.get_workflow_files()
2. Parses each YAML with PyYAML
3. Builds a job dependency graph using networkx (DiGraph)
4. Identifies jobs with no dependency path between them → parallelizable
5. Sends original YAML + graph summary + YAML_OPTIMIZER_PROMPT to Gemini
6. Parses two YAML blocks from response (original + optimized)
7. Estimates time saved: sum(sequential_times) - critical_path_length
8. Opens a SEPARATE PR (never mixed with code patch PR)
9. Returns OptimizationResult

Runs regardless of patch success/failure — these are independent concerns.
Uses PRIMARY_MODEL (gemini-2.5-flash).
"""

from __future__ import annotations

import json
import logging

import networkx as nx
import yaml

from config.prompts import YAML_OPTIMIZER_SYSTEM_PROMPT
from github.mcp_client import GitHubMCPClient
from github.pr_manager import create_optimize_pr
from github.run_history import get_workflow_yaml_files
from llm.gemini_client import get_gemini_client
from llm.rate_limiter import (
    DailyLimitReachedError,
    GeminiError,
    GeminiRateLimitError,
)
from llm.response_parser import parse_optimization_summary, parse_yaml_blocks
from models.events import WorkflowFailureEvent
from models.task import OptimizationResult

logger = logging.getLogger(__name__)


def _no_op_result(reason: str) -> OptimizationResult:
    return OptimizationResult(
        original_yaml="",
        optimized_yaml="",
        jobs_parallelized=(),
        cache_steps_added=(),
        estimated_savings_seconds=0,
        explanation=reason,
    )


_PREFERRED_WORKFLOW_NAMES = (
    "ci.yml",
    "ci.yaml",
    "main.yml",
    "main.yaml",
    "build.yml",
    "build.yaml",
)


def _select_primary_workflow(files: dict[str, str]) -> tuple[str, str]:
    lower_to_orig = {name.lower(): name for name in files}
    for preferred in _PREFERRED_WORKFLOW_NAMES:
        if preferred in lower_to_orig:
            actual = lower_to_orig[preferred]
            return actual, files[actual]
    first = next(iter(files))
    return first, files[first]


def _build_graph_and_summary(yaml_text: str) -> tuple[nx.DiGraph, dict]:
    parsed = yaml.safe_load(yaml_text) or {}
    jobs = parsed.get("jobs", {}) if isinstance(parsed, dict) else {}
    if not isinstance(jobs, dict):
        jobs = {}

    graph: nx.DiGraph = nx.DiGraph()
    dependencies: dict[str, list[str]] = {}

    for job_name, job_def in jobs.items():
        graph.add_node(job_name)
        needs_raw = job_def.get("needs", []) if isinstance(job_def, dict) else []
        if isinstance(needs_raw, str):
            needs = [needs_raw]
        elif isinstance(needs_raw, list):
            needs = [n for n in needs_raw if isinstance(n, str)]
        else:
            needs = []
        dependencies[job_name] = needs
        for dep in needs:
            if dep:
                graph.add_edge(dep, job_name)

    nodes = list(graph.nodes)
    parallelizable_pairs: list[list[str]] = []
    for i, a in enumerate(nodes):
        for b in nodes[i + 1 :]:
            if not nx.has_path(graph, a, b) and not nx.has_path(graph, b, a):
                parallelizable_pairs.append([a, b])

    summary = {
        "jobs": nodes,
        "dependencies": dependencies,
        "parallelizable_pairs": parallelizable_pairs,
        "total_jobs": len(nodes),
    }
    return graph, summary


async def optimize(
    event: WorkflowFailureEvent,
    mcp_client: GitHubMCPClient,
) -> OptimizationResult:
    logger.info("yaml_optimizer: run=%d", event.run_id)
    try:
        files = await get_workflow_yaml_files(mcp_client)
        if not files:
            logger.warning("yaml_optimizer: no workflow files found")
            return _no_op_result("No workflow files found")

        workflow_filename, original_yaml = _select_primary_workflow(files)

        try:
            _, graph_summary = _build_graph_and_summary(original_yaml)
        except Exception as e:
            logger.warning("yaml_optimizer: graph build failed: %s", e)
            return _no_op_result(f"Could not parse original YAML: {e}")

        prompt = "\n".join(
            [
                f"Workflow file: {workflow_filename}",
                "--- ORIGINAL YAML ---",
                original_yaml,
                "--- JOB DEPENDENCY GRAPH ---",
                json.dumps(graph_summary, indent=2),
                "--- END ---",
                "Optimize this workflow to reduce runtime.",
            ]
        )

        try:
            response_text = await get_gemini_client().generate(
                prompt=prompt,
                system_prompt=YAML_OPTIMIZER_SYSTEM_PROMPT,
                agent="yaml_optimizer",
                strip_pii=False,
                temperature=0.2,
            )
        except (GeminiError, GeminiRateLimitError, DailyLimitReachedError) as e:
            logger.warning("yaml_optimizer: gemini error: %s", e)
            return _no_op_result(f"Gemini error: {e}")

        yaml_pair = parse_yaml_blocks(response_text)
        summary = parse_optimization_summary(response_text)
        if yaml_pair is None or summary is None:
            logger.warning("yaml_optimizer: failed to parse optimizer response")
            return _no_op_result("Failed to parse optimizer response")

        _, optimized_yaml = yaml_pair

        try:
            parsed_opt = yaml.safe_load(optimized_yaml)
        except Exception as e:
            return _no_op_result(f"Optimized YAML invalid: {e}")
        if not isinstance(parsed_opt, dict):
            return _no_op_result("Optimized YAML is not a mapping")

        original_jobs = set(graph_summary.get("jobs", []))
        opt_jobs_obj = parsed_opt.get("jobs") or {}
        opt_jobs = set(opt_jobs_obj.keys()) if isinstance(opt_jobs_obj, dict) else set()
        missing = original_jobs - opt_jobs
        if missing:
            return _no_op_result(
                f"Optimized YAML dropped jobs — unsafe, discarding: {sorted(missing)}"
            )

        savings = int(summary.get("estimated_savings_seconds", 0) or 0)
        pr_url: str | None = None
        pr_number: int | None = None
        if savings > 0:
            pr_result = await create_optimize_pr(
                run_id=event.run_id,
                optimized_yaml=optimized_yaml,
                workflow_filename=workflow_filename,
                summary=summary,
                head_sha=event.head_sha,
                mcp_client=mcp_client,
            )
            if pr_result is not None:
                pr_url, pr_number = pr_result

        return OptimizationResult(
            original_yaml=original_yaml,
            optimized_yaml=optimized_yaml,
            jobs_parallelized=tuple(summary.get("jobs_parallelized", []) or []),
            cache_steps_added=tuple(summary.get("cache_steps_added", []) or []),
            estimated_savings_seconds=savings,
            pr_url=pr_url,
            pr_number=pr_number,
            explanation=summary.get("explanation", "") or "",
        )
    except Exception as e:
        logger.error("yaml_optimizer: unexpected error for run %d: %s", event.run_id, e)
        return _no_op_result(str(e))
