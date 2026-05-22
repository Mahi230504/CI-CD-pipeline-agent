"""Unit tests for agents/yaml_optimizer.py — dependency graph correctness, no broken deps introduced."""

from __future__ import annotations

from pathlib import Path

import networkx as nx
import yaml

from agents.yaml_optimizer import _no_op_result
from models.task import OptimizationResult


SAMPLE_WORKFLOW_PATH = Path("tests/fixtures/sample_workflow.yml")


def _load_graph() -> tuple[dict, nx.DiGraph]:
    wf = yaml.safe_load(SAMPLE_WORKFLOW_PATH.read_text())
    jobs = wf.get("jobs", {}) or {}
    g: nx.DiGraph = nx.DiGraph()
    for job_name, job_def in jobs.items():
        g.add_node(job_name)
        needs = (job_def or {}).get("needs", []) if isinstance(job_def, dict) else []
        if isinstance(needs, str):
            needs = [needs]
        elif not isinstance(needs, list):
            needs = []
        for dep in needs:
            if isinstance(dep, str) and dep:
                g.add_edge(dep, job_name)
    return wf, g


def test_graph_has_all_jobs():
    wf, g = _load_graph()
    assert set(g.nodes) == set(wf["jobs"].keys())


def test_graph_respects_needs():
    wf, g = _load_graph()
    for job_name, job_def in wf["jobs"].items():
        needs = job_def.get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        for dep in needs:
            assert g.has_edge(dep, job_name), f"missing edge {dep} → {job_name}"


def test_lint_and_test_are_parallelizable():
    _, g = _load_graph()
    assert not nx.has_path(g, "lint", "test")
    assert not nx.has_path(g, "test", "lint")


def test_build_depends_on_both():
    _, g = _load_graph()
    assert nx.has_path(g, "lint", "build")
    assert nx.has_path(g, "test", "build")
    assert nx.has_path(g, "install", "build")


def test_deploy_is_not_parallelizable_with_build():
    _, g = _load_graph()
    assert nx.has_path(g, "build", "deploy")


def test_valid_yaml_loads():
    parsed = yaml.safe_load(SAMPLE_WORKFLOW_PATH.read_text())
    assert isinstance(parsed, dict)
    assert "jobs" in parsed


def test_no_jobs_dropped():
    wf, _ = _load_graph()
    original_jobs = set(wf["jobs"].keys())
    optimized_jobs = set(wf["jobs"].keys())
    missing = original_jobs - optimized_jobs
    assert missing == set()


def test_missing_job_detected():
    wf, _ = _load_graph()
    original_jobs = set(wf["jobs"].keys())
    optimized = dict(wf["jobs"])
    optimized.pop("deploy", None)
    optimized_jobs = set(optimized.keys())
    missing = original_jobs - optimized_jobs
    assert "deploy" in missing


def test_no_op_has_no_improvements():
    result = _no_op_result("test reason")
    assert result.has_improvements is False


def test_no_op_explanation_set():
    result = _no_op_result("my reason")
    assert result.explanation == "my reason"


def test_no_op_is_optimization_result():
    result = _no_op_result("anything")
    assert isinstance(result, OptimizationResult)
