"""
Deploy guard — LLM-judged go/no-go for promoting a merged PR.

Gathers a compact, structured view of the candidate change (title, body,
files, truncated diff, optional history) and asks the LLM whether it's
safe to ship. Returns a DeployVerdict with a confidence-gated approve flag.

The CD orchestrator's contract:
- `judge(...)` ALWAYS returns a DeployVerdict — never raises.
- A None LLM response or a low-confidence verdict counts as a block.
- The orchestrator is responsible for fetching the PR diff (via the
  GitHub MCP client) and passing it in here. This module does no I/O
  besides the single rate-limited LLM call.

Why no I/O here: keeping the LLM module pure makes it trivially testable
(no MCP fixtures, no network) and lets the orchestrator decide how much
diff to fetch (full vs. summarised) without round-tripping through here.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from config.prompts import DEPLOY_GUARD_SYSTEM_PROMPT
from llm.gemini_client import get_gemini_client
from llm.rate_limiter import (
    DailyLimitReachedError,
    GeminiError,
    GeminiRateLimitError,
)
from llm.response_parser import parse_deploy_verdict
from models.cd import DeployRisk, DeployVerdict

logger = logging.getLogger("cicd_agent.deploy_guard")

# How much of the unified diff to send to the model. Most LLMs handle 50K
# tokens fine, but a tighter cap keeps cost predictable and forces the
# orchestrator to send the most meaningful hunks rather than the whole
# repo. 30K chars roughly maps to ~7-8K tokens.
_MAX_DIFF_CHARS = 30_000

# Confidence floor below which we ALWAYS block, regardless of `approve`.
# Matches the diagnosis pattern (Diagnosis.is_high_confidence at 0.6).
_MIN_CONFIDENCE = 0.6


def _truncate_diff(diff: str, limit: int = _MAX_DIFF_CHARS) -> str:
    """Keep the head + tail of a large diff — both ends are diagnostically
    valuable (early changes set the tone, late changes are often the risky
    integration glue).
    """
    if len(diff) <= limit:
        return diff
    half = (limit - 80) // 2
    return (
        diff[:half]
        + f"\n\n... [{len(diff) - 2 * half} characters elided by deploy_guard] ...\n\n"
        + diff[-half:]
    )


def _build_user_payload(
    *,
    pr_number: int,
    pr_title: str,
    pr_body: str,
    files_changed: list[str],
    diff_summary: str,
    head_sha: str,
    recent_deploys: list[dict[str, Any]] | None,
) -> str:
    """Serialise the LLM input. Single JSON blob to match the prompt contract."""
    return json.dumps(
        {
            "pr_number": pr_number,
            "pr_title": pr_title,
            "pr_body": pr_body or "(empty body)",
            "head_sha": head_sha,
            "files_changed": files_changed,
            "diff_summary": _truncate_diff(diff_summary),
            "recent_deploys": recent_deploys or [],
        },
        ensure_ascii=False,
    )


def _force_block(reason: str, *, risk: DeployRisk = DeployRisk.HIGH) -> DeployVerdict:
    """Build a synthetic 'block' verdict for the cases where we never even
    reached the LLM (rate limit, parse failure, empty diff). Surfaces the
    reason in the same shape downstream code expects, so notifications /
    SSE events render uniformly."""
    return DeployVerdict(
        approve=False,
        risk=risk,
        reason=reason,
        concerns=(reason,),
        confidence=0.0,
        raw_response="",
    )


async def judge(
    *,
    pr_number: int,
    pr_title: str,
    pr_body: str,
    files_changed: list[str],
    diff_summary: str,
    head_sha: str,
    recent_deploys: list[dict[str, Any]] | None = None,
) -> DeployVerdict:
    """Ask the LLM whether the candidate change is safe to deploy.

    See the prompt in `config/prompts.DEPLOY_GUARD_SYSTEM_PROMPT` for the
    full decision criteria. The function always returns a DeployVerdict —
    LLM errors, rate-limit hits, and parse failures all surface as a
    block-with-reason rather than raising.
    """
    if not diff_summary or not diff_summary.strip():
        # An empty diff is a sign of a bad upstream extraction — block
        # before we waste an LLM call on it.
        return _force_block("empty diff — refusing to judge")

    payload = _build_user_payload(
        pr_number=pr_number,
        pr_title=pr_title,
        pr_body=pr_body,
        files_changed=files_changed,
        diff_summary=diff_summary,
        head_sha=head_sha,
        recent_deploys=recent_deploys,
    )

    try:
        raw = await get_gemini_client().generate(
            prompt=payload,
            system_prompt=DEPLOY_GUARD_SYSTEM_PROMPT,
            agent="deploy_guard",
            use_light_model=False,
            temperature=0.1,
            # Diff content can include API keys / tokens in tests or
            # accidentally committed snippets. Strip before sending.
            strip_pii=True,
        )
    except (GeminiRateLimitError, DailyLimitReachedError) as e:
        logger.warning("deploy_guard: rate / budget cap hit: %s", e)
        return _force_block(f"LLM unavailable: {e}", risk=DeployRisk.MEDIUM)
    except GeminiError as e:
        logger.warning("deploy_guard: LLM error: %s", e)
        return _force_block(f"LLM error: {e}")
    except Exception as e:  # pragma: no cover — defensive
        logger.error("deploy_guard: unexpected error: %s", e, exc_info=True)
        return _force_block(f"deploy_guard crashed: {e}")

    verdict = parse_deploy_verdict(raw)
    if verdict is None:
        logger.warning(
            "deploy_guard: could not parse LLM response (first 300 chars): %s",
            raw[:300],
        )
        return _force_block("malformed LLM response")

    if not verdict.is_high_confidence:
        # The LLM said something, but it's hedged enough that we treat it
        # as an automatic block. We preserve everything the model said so
        # operators can see the reasoning in the dashboard.
        logger.info(
            "deploy_guard: low confidence (%.2f) — forcing block. reason=%s",
            verdict.confidence,
            verdict.reason,
        )
        return DeployVerdict(
            approve=False,
            risk=verdict.risk,
            reason=f"low confidence ({verdict.confidence:.2f}): {verdict.reason}",
            concerns=verdict.concerns + (f"confidence={verdict.confidence:.2f}",),
            confidence=verdict.confidence,
            raw_response=verdict.raw_response,
        )

    return verdict
