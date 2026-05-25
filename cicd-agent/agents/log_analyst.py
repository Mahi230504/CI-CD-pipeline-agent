"""
Log analyst agent — diagnoses why a CI job failed.

Flow:
1. Calls log_fetcher.slice_log() to get the relevant log window
2. Sends sliced log + LOG_ANALYST_PROMPT to Gemini via gemini_client
3. Parses response through response_parser.parse_diagnosis()
4. Returns Diagnosis dataclass

Confidence gate: if diagnosis.confidence < 0.6, returns the Diagnosis
with a flag indicating the orchestrator should escalate to human rather
than attempting a patch. A low-confidence diagnosis patched blindly makes
things worse.

Uses PRIMARY_MODEL (gemini-2.5-flash).
"""

from __future__ import annotations

import logging
import re

from config.prompts import LOG_ANALYST_SYSTEM_PROMPT
from github.mcp_client import GitHubMCPClient
from llm.gemini_client import get_gemini_client
from llm.rate_limiter import (
    DailyLimitReachedError,
    GeminiError,
    GeminiRateLimitError,
)
from llm.response_parser import parse_diagnosis
from models.events import WorkflowFailureEvent
from models.run import Diagnosis, JobLog

logger = logging.getLogger(__name__)


_RAW_LOG_FALLBACK_CHARS = 8000
_MAX_AUX_FILES = 2
_MAX_AUX_CHARS_TOTAL = 4000
_TEST_PATH_RE = re.compile(r"\b((?:tests?|spec)/[\w\-./]+\.py)\b")


def _extract_test_paths(log_excerpt: str) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for m in _TEST_PATH_RE.finditer(log_excerpt):
        path = m.group(1)
        if path not in seen:
            seen.add(path)
            result.append(path)
            if len(result) >= _MAX_AUX_FILES:
                break
    return result


async def _fetch_aux_files(
    paths: list[str],
    event: WorkflowFailureEvent,
    mcp_client: GitHubMCPClient,
) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    remaining = _MAX_AUX_CHARS_TOTAL
    for path in paths:
        if remaining <= 0:
            break
        try:
            content = await mcp_client.get_file_contents(path, ref=event.head_sha)
        except Exception as e:
            logger.info("log_analyst: aux fetch skipped for %s: %s", path, e)
            continue
        slice_ = content[:remaining]
        out.append((path, slice_))
        remaining -= len(slice_)
    return out


async def diagnose(
    job_logs: list[JobLog],
    event: WorkflowFailureEvent,
    mcp_client: GitHubMCPClient | None = None,
) -> Diagnosis | None:
    try:
        if not job_logs:
            logger.error("log_analyst: no job_logs provided for run %d", event.run_id)
            return None

        logger.info("log_analyst: diagnosing run=%d jobs=%d", event.run_id, len(job_logs))

        target = next((jl for jl in job_logs if jl.sliced_log), None)
        if target is not None:
            log_excerpt = target.sliced_log or ""
        else:
            target = job_logs[0]
            log_excerpt = target.raw_log[:_RAW_LOG_FALLBACK_CHARS]
            logger.warning(
                "log_analyst: no sliced_log available, using raw_log head for job %d",
                target.job_id,
            )

        aux_files: list[tuple[str, str]] = []
        if mcp_client is not None:
            test_paths = _extract_test_paths(log_excerpt)
            if test_paths:
                aux_files = await _fetch_aux_files(test_paths, event, mcp_client)
                if aux_files:
                    logger.info(
                        "log_analyst: attached %d test file(s) as context: %s",
                        len(aux_files),
                        [p for p, _ in aux_files],
                    )

        prompt_parts: list[str] = [
            f"Repository: {event.full_repo}",
            f"Branch: {event.branch}",
            f"Workflow: {event.workflow_name}",
            f"Job: {target.job_name}",
            "--- LOG EXCERPT ---",
            log_excerpt,
            "--- END LOG ---",
        ]
        for path, content in aux_files:
            prompt_parts.append(f"--- TEST FILE: {path} ---")
            prompt_parts.append(content)
            prompt_parts.append("--- END FILE ---")
        prompt_parts.append("Diagnose the failure and respond with JSON only.")
        prompt = "\n".join(prompt_parts)

        response_text = await get_gemini_client().generate(
            prompt=prompt,
            system_prompt=LOG_ANALYST_SYSTEM_PROMPT,
            agent="log_analyst",
            strip_pii=True,
            temperature=0.1,
        )

        diagnosis = parse_diagnosis(response_text)
        if diagnosis is None:
            head = response_text[:200].replace("\n", " ") if response_text else ""
            logger.warning(
                "log_analyst: parse failed for run %d, response head: %s",
                event.run_id,
                head,
            )
            return None

        if diagnosis.confidence < 0.6:
            logger.warning(
                "Low confidence diagnosis (%.2f) for run %d",
                diagnosis.confidence,
                event.run_id,
            )
        else:
            logger.info(
                "log_analyst: run=%d confidence=%.2f file=%s line=%s",
                event.run_id,
                diagnosis.confidence,
                diagnosis.file,
                diagnosis.line_number,
            )
        return diagnosis

    except (GeminiError, GeminiRateLimitError, DailyLimitReachedError) as e:
        logger.error("log_analyst: gemini error for run %d: %s", event.run_id, e)
        return None
    except Exception as e:
        logger.error("log_analyst: unexpected error for run %d: %s", event.run_id, e)
        return None
