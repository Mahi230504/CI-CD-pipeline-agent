"""
Fetches and processes GitHub Actions job logs.

The key challenge: real pipeline logs are 50K–200K tokens of noise
(Docker pulls, npm installs, setup steps). This module finds the signal.

Functions:
- fetch_job_logs(run_id, mcp_client) → list[JobLog]
- process_job_log(job_log) → JobLog (mutates and returns same instance)
- find_failed_step(log_text) → tuple[int, str]
- slice_log(log_text, error_line, window) → str
- token_guard(text, max_chars) → str
- has_infra_error(log_text) → bool
- extract_failed_test_names(log_text) → list[str]

The output of slice_log() is what gets sent to Gemini — not the full log.
"""

from __future__ import annotations

import logging
import re

from config.constants import (
    INFRA_ERROR_KEYWORDS,
    LOG_SLICE_WINDOW,
    MAX_LOG_CHARS,
)
from github.mcp_client import GitHubMCPClient
from models.run import JobLog

logger = logging.getLogger(__name__)


_GROUP_PREFIX_RE = re.compile(r"^##\[group\](?:Run\s+)?(.*?)$")
_GH_ERROR_RE = re.compile(r"##\[error\]")
_GENERIC_ERROR_RE = re.compile(r"(?:^|\s)(?:ERROR:|Error:|error:|FAILED)\b")
_EXIT_CODE_RE = re.compile(r"Process completed with exit code\s+(\d+)")


async def fetch_job_logs(
    run_id: int,
    mcp_client: GitHubMCPClient,
) -> list[JobLog]:
    jobs = await mcp_client.list_jobs(run_id)
    failed = [j for j in jobs if isinstance(j, dict) and j.get("conclusion") == "failure"]
    if not failed:
        logger.warning("no failed jobs found for run %d", run_id)
        return []

    out: list[JobLog] = []
    for job in failed:
        try:
            job_id = int(job.get("id"))
        except (TypeError, ValueError):
            logger.warning("job has invalid id, skipping: %r", job)
            continue
        job_name = str(job.get("name") or "unknown")
        try:
            raw = await mcp_client.get_job_logs(job_id)
        except Exception as e:
            logger.error("failed to fetch logs for job %d: %s", job_id, e)
            continue
        job_log = JobLog(job_id=job_id, job_name=job_name, raw_log=raw)
        out.append(process_job_log(job_log))

    out.sort(key=lambda j: j.job_id)
    return out


def process_job_log(job_log: JobLog) -> JobLog:
    line_number, step_name = find_failed_step(job_log.raw_log)
    job_log.error_line_number = line_number
    job_log.error_step_name = step_name
    sliced = slice_log(job_log.raw_log, line_number)
    job_log.sliced_log = token_guard(sliced)
    return job_log


def find_failed_step(log_text: str) -> tuple[int, str]:
    if not log_text:
        return (0, "unknown")

    lines = log_text.splitlines()
    error_line: int | None = None

    for i, line in enumerate(lines):
        if _GH_ERROR_RE.search(line):
            error_line = i
            break

    if error_line is None:
        for i, line in enumerate(lines):
            if _GENERIC_ERROR_RE.search(line):
                error_line = i
                break

    if error_line is None:
        for i, line in enumerate(lines):
            m = _EXIT_CODE_RE.search(line)
            if m and m.group(1) != "0":
                error_line = i
                break

    if error_line is None:
        return (0, "unknown")

    step_name = "unknown"
    for j in range(error_line, -1, -1):
        m = _GROUP_PREFIX_RE.match(lines[j])
        if m:
            captured = m.group(1).strip()
            step_name = captured if captured else "unknown"
            break

    return (error_line, step_name)


def slice_log(
    log_text: str,
    error_line: int,
    window: int = LOG_SLICE_WINDOW,
) -> str:
    if not log_text:
        return ""

    lines = log_text.splitlines()
    if not lines:
        return ""

    if error_line <= 0 and not _GH_ERROR_RE.search(log_text) and not _GENERIC_ERROR_RE.search(log_text):
        tail_count = min(window * 2, len(lines))
        start = len(lines) - tail_count
        selected = lines[start:]
        return "\n".join(f"{start + i:4d} | {line}" for i, line in enumerate(selected))

    start = max(0, error_line - window)
    end = min(len(lines), error_line + window + 1)
    selected = lines[start:end]
    return "\n".join(f"{start + i:4d} | {line}" for i, line in enumerate(selected))


def token_guard(text: str, max_chars: int = MAX_LOG_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    original_size = len(text)
    truncated = text[:max_chars]
    logger.warning(
        "token_guard: log truncated from %d to %d chars",
        original_size,
        max_chars,
    )
    return (
        f"{truncated}\n[LOG TRUNCATED: original {original_size} chars, "
        f"showing first {max_chars} chars only]"
    )


def has_infra_error(log_text: str) -> bool:
    if not log_text:
        return False
    lower = log_text.lower()
    return any(keyword in lower for keyword in INFRA_ERROR_KEYWORDS)


_PYTEST_FAIL_RE = re.compile(r"FAILED\s+(\S+)")
_JEST_FAIL_RE = re.compile(r"●\s+(.+?)\s*$", re.MULTILINE)
_GO_FAIL_RE = re.compile(r"---\s+FAIL:\s+(\S+)")
_JUNIT_FAIL_RE = re.compile(
    r"<testcase\b[^>]*\bname=[\"']([^\"']+)[\"'][^>]*>.*?<failure",
    re.DOTALL,
)


def extract_failed_test_names(log_text: str) -> list[str]:
    if not log_text:
        return []

    names: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        name = name.strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)

    for m in _PYTEST_FAIL_RE.finditer(log_text):
        add(m.group(1))
    for m in _JEST_FAIL_RE.finditer(log_text):
        add(m.group(1))
    for m in _GO_FAIL_RE.finditer(log_text):
        add(m.group(1))
    for m in _JUNIT_FAIL_RE.finditer(log_text):
        add(m.group(1))

    return names
