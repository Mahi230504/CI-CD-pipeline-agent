"""
Local integration test — verifies the pipeline works end-to-end
without needing a real GitHub repository or a live webhook.

Uses fixtures from tests/fixtures/:
- sample_webhook.json  — a real GitHub workflow_run failure payload
- sample_log.txt       — a realistic CI log with an intentional failure
- sample_workflow.yml  — a slow sequential workflow for the optimizer

Does NOT create any real PRs. Does NOT call GitHub MCP.
Uses the real Gemini API (requires GEMINI_API_KEY in .env).

Run with: python test_local.py
Or:        make test-local
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import networkx as nx
import yaml
from dotenv import load_dotenv

from agents.flakiness_detector import check as flakiness_check
from agents.log_analyst import diagnose
from config.constants import ErrorCategory, ErrorType
from github.log_fetcher import process_job_log
from llm.gemini_client import init_gemini_client
from llm.rate_limiter import init_rate_limiter
from llm.response_parser import parse_diagnosis, parse_diff, parse_yaml_blocks
from models.events import WorkflowFailureEvent
from models.run import JobLog


_FIXTURES = Path("tests/fixtures")


def _mock_event() -> WorkflowFailureEvent:
    return WorkflowFailureEvent(
        run_id=987654321,
        repo_owner="testowner",
        repo_name="cicd-agent-demo",
        workflow_name="CI",
        branch="main",
        head_sha="a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
        html_url="https://github.com/testowner/cicd-agent-demo/actions/runs/987654321",
        sender_login="testowner",
    )


async def test_log_analysis_end_to_end():
    log_text = (_FIXTURES / "sample_log.txt").read_text(encoding="utf-8")
    job_log = JobLog(job_id=1, job_name="test", raw_log=log_text)
    process_job_log(job_log)
    assert job_log.sliced_log is not None, "sliced_log should be set"
    assert job_log.error_line_number is not None and job_log.error_line_number > 0, (
        "error_line_number should be detected and > 0"
    )

    init_rate_limiter(0.5)
    init_gemini_client()

    diagnosis = await diagnose([job_log], _mock_event())
    assert diagnosis is not None, "diagnose should not return None"
    assert diagnosis.error_type in (ErrorType.TEST_FAILURE, ErrorType.BUILD_ERROR), (
        f"unexpected error_type: {diagnosis.error_type}"
    )
    assert diagnosis.confidence > 0.3, f"confidence too low: {diagnosis.confidence}"
    print(f"  Diagnosis: {diagnosis.error_type} | confidence={diagnosis.confidence:.2f}")
    print(f"  File: {diagnosis.file} | line: {diagnosis.line_number}")
    print(f"  Explanation: {diagnosis.explanation}")


async def test_yaml_optimization_graph():
    workflow = yaml.safe_load((_FIXTURES / "sample_workflow.yml").read_text(encoding="utf-8"))
    jobs = workflow.get("jobs", {})
    g: nx.DiGraph = nx.DiGraph()
    for job_name, job_def in jobs.items():
        g.add_node(job_name)
        needs = (job_def or {}).get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        for dep in needs:
            g.add_edge(dep, job_name)

    assert not nx.has_path(g, "lint", "test")
    assert not nx.has_path(g, "test", "lint")
    assert nx.has_path(g, "install", "build")
    assert nx.has_path(g, "build", "deploy")
    print("  Graph: parallelizable pairs identified correctly")


async def test_flakiness_detection_infra():
    job_log = JobLog(
        job_id=1,
        job_name="test",
        raw_log="setting up runner...\nERROR: no space left on device\nworkflow aborted",
    )
    verdict = await flakiness_check(_mock_event(), [job_log], mcp_client=None)
    assert verdict.is_flaky is True
    assert verdict.error_category == ErrorCategory.INFRA_NOISE
    print("  Infra error correctly classified as flaky")


async def test_response_parser_roundtrip():
    diag_json = json.dumps(
        {
            "error_type": "test_failure",
            "file": "tests/test_math.py",
            "line_number": 47,
            "explanation": "ZeroDivisionError in test_divide",
            "confidence": 0.9,
            "is_patchable": True,
        }
    )
    diagnosis = parse_diagnosis(diag_json)
    assert diagnosis is not None
    assert diagnosis.error_type == ErrorType.TEST_FAILURE

    diff_text = (
        "--- a/tests/test_math.py\n"
        "+++ b/tests/test_math.py\n"
        "@@ -1,3 +1,3 @@\n"
        " def test_divide():\n"
        "-    result = 10 / 0\n"
        "+    result = 10 / 2\n"
        "     assert result == 5\n"
    )
    parsed_diff = parse_diff(diff_text)
    assert parsed_diff is not None

    yaml_response = (
        "```yaml\nname: Original\non: [push]\n```\n"
        "```yaml\nname: Optimized\non: [push]\n```\n"
        '{"jobs_parallelized": ["lint","test"], "cache_steps_added": [], '
        '"estimated_savings_seconds": 120, "explanation": "ok"}'
    )
    blocks = parse_yaml_blocks(yaml_response)
    assert blocks is not None and len(blocks) == 2
    print("  All parsers: roundtrip OK")


def main() -> None:
    load_dotenv()
    print("CI/CD Agent — Local Integration Test")
    print("=" * 45)

    tests = [
        ("Log analysis (real Gemini)", test_log_analysis_end_to_end),
        ("YAML graph logic", test_yaml_optimization_graph),
        ("Flakiness detection", test_flakiness_detection_infra),
        ("Response parser roundtrip", test_response_parser_roundtrip),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        print(f"Running: {name}...")
        try:
            asyncio.run(fn())
            print("  ✓ PASSED")
            passed += 1
        except Exception as e:
            print(f"  ✗ FAILED: {e}")
            failed += 1

    print("=" * 45)
    print(f"Results: {passed} passed, {failed} failed")
    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
