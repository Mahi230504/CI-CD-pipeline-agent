"""
Post-deploy health probe.

Polls two endpoints on the target backend, in lockstep, until they BOTH go
green or the total timeout is exceeded:

  GET {base_url}/health    → expect HTTP 200
  GET {base_url}/version   → expect 200 + {"commit": "<expected short sha>"}

A 200 from `/health` alone is not enough. During a deploy, the old container
keeps serving for a few seconds after the new one is started; if we only
checked `/health`, we'd happily declare "healthy" against the prior image
and miss a failed rollout. Verifying `/version` proves we're talking to the
new container.

Returns a HealthReport — never raises. The orchestrator branches on
`.healthy` and may trigger rollback when False.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp

from config.settings import get_settings
from models.cd import HealthReport

logger = logging.getLogger("cicd_agent.health_monitor")

# Per-request HTTP timeout. Kept tight so a stuck request doesn't eat the
# whole polling budget — the next loop iteration will retry against the
# same endpoint.
_PROBE_TIMEOUT_SECONDS = 5.0


async def _probe_health(session: aiohttp.ClientSession, url: str) -> tuple[bool, int]:
    """Return (ok, latency_ms). `ok` is True only on HTTP 200."""
    start = time.monotonic()
    try:
        async with session.get(url) as resp:
            await resp.read()  # drain body — keeps the connection clean
            latency_ms = int((time.monotonic() - start) * 1000)
            return resp.status == 200, latency_ms
    except Exception as e:
        logger.debug("health_monitor: /health probe error: %s", e)
        return False, int((time.monotonic() - start) * 1000)


async def _probe_version(session: aiohttp.ClientSession, url: str) -> str | None:
    """Return the `commit` field from /version, or None on any failure."""
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            data: dict[str, Any] = await resp.json(content_type=None)
            commit = data.get("commit")
            if not isinstance(commit, str) or not commit.strip():
                return None
            return commit.strip()
    except Exception as e:
        logger.debug("health_monitor: /version probe error: %s", e)
        return None


def _sha_matches(observed: str, expected: str) -> bool:
    """Loose SHA equality — same logic as HealthReport.sha_matches.

    Re-implemented here as a free function so the polling loop can short-
    circuit before constructing a HealthReport.
    """
    if not observed or not expected:
        return False
    if observed == expected:
        return True
    # Tolerate full-vs-short SHA in either direction.
    return observed.startswith(expected) or expected.startswith(observed)


async def check(
    expected_sha: str,
    *,
    base_url: str | None = None,
    timeout_seconds: int | None = None,
    poll_interval_seconds: float | None = None,
) -> HealthReport:
    """Poll until both endpoints agree we're healthy on `expected_sha`.

    Arguments are explicit overrides for tests; in production the orchestrator
    calls `check(expected_sha)` and accepts defaults from settings.
    """
    settings = get_settings()
    base = (base_url or settings.backend_base_url).rstrip("/")
    total_timeout = timeout_seconds or settings.deploy_health_timeout_seconds
    interval = poll_interval_seconds or settings.deploy_health_poll_interval_seconds

    if not base:
        return HealthReport(
            healthy=False,
            expected_sha=expected_sha,
            observed_sha=None,
            latency_ms=0,
            attempts=0,
            error_message="BACKEND_BASE_URL is not configured",
        )

    if not expected_sha:
        return HealthReport(
            healthy=False,
            expected_sha=expected_sha,
            observed_sha=None,
            latency_ms=0,
            attempts=0,
            error_message="expected_sha is empty",
        )

    health_url = f"{base}/health"
    version_url = f"{base}/version"
    deadline = time.monotonic() + total_timeout
    attempts = 0
    last_latency = 0
    last_observed: str | None = None
    last_health_ok = False

    timeout_cfg = aiohttp.ClientTimeout(total=_PROBE_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
        while True:
            attempts += 1
            health_ok, latency_ms = await _probe_health(session, health_url)
            last_health_ok = health_ok
            last_latency = latency_ms

            observed_sha: str | None = None
            if health_ok:
                observed_sha = await _probe_version(session, version_url)
                last_observed = observed_sha or last_observed
                if observed_sha and _sha_matches(observed_sha, expected_sha):
                    return HealthReport(
                        healthy=True,
                        expected_sha=expected_sha,
                        observed_sha=observed_sha,
                        latency_ms=latency_ms,
                        attempts=attempts,
                    )

            if time.monotonic() >= deadline:
                # Build a precise error message so the rollback path and the
                # notifier can both say WHY we gave up.
                if not last_health_ok:
                    reason = f"/health never returned 200 after {attempts} attempts"
                elif last_observed is None:
                    reason = (
                        f"/health green but /version unreachable / malformed "
                        f"after {attempts} attempts"
                    )
                else:
                    reason = (
                        f"/version reported commit={last_observed!r}, "
                        f"expected {expected_sha!r}"
                    )
                return HealthReport(
                    healthy=False,
                    expected_sha=expected_sha,
                    observed_sha=last_observed,
                    latency_ms=last_latency,
                    attempts=attempts,
                    error_message=reason,
                )

            await asyncio.sleep(interval)
