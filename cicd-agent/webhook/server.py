"""
FastAPI application — the entry point for GitHub webhook events.

Routes:
  POST /webhook          — main GitHub webhook endpoint
  GET  /health           — liveness probe (returns 200 + timestamp)
  GET  /status           — queue depth, last run info, daily Gemini call count
  POST /trigger          — manual test trigger (disabled in PRODUCTION_MODE=true)

Uses FastAPI lifespan to start/stop the task queue worker.
All requests are logged via middleware (method, path, status, duration_ms).
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from audit.logger import init_audit_logger
from audit.setup import configure_logging
from config.settings import get_settings
from metrics import (
    active_runs,
    cost_total_today,
    pipeline_outcomes_total,
    queue_depth,
    registry as metrics_registry,
)

configure_logging()
from llm.gemini_client import init_gemini_client
from llm.rate_limiter import DAILY_REQUEST_LIMIT, get_rate_limiter, init_rate_limiter
from models.events import WorkflowFailureEvent
from orchestrator.event_store import init_event_store
from orchestrator.run_registry import init_registry
from orchestrator.task_queue import (
    enqueue_event,
    get_task_queue,
    init_task_queue,
)
from webhook.dedup import get_dedup
from webhook.router import route_webhook
from webhook.validator import verify_github_signature

logger = logging.getLogger("cicd_agent.server")

_start_time: float = time.monotonic()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _start_time
    _start_time = time.monotonic()
    settings = get_settings()

    init_audit_logger(settings.log_dir_path)
    init_rate_limiter(settings.rate_limit_delay_seconds)
    init_gemini_client()
    init_registry(settings.run_registry_path)
    init_event_store(settings.queue_db_path)
    init_task_queue()
    queue = get_task_queue()
    replayed = await queue.replay_unfinished()
    if replayed:
        logger.info("Replayed %d unfinished event(s) from %s", replayed, settings.queue_db_path)
    await queue.start()

    # Agent Console chat: start the Redis consumer that pulls chat turns and
    # runs them through the ChatOrchestrator. No-op unless CHAT_ENABLED=true.
    from orchestrator.chat_consumer import start_chat_consumer, stop_chat_consumer

    try:
        await start_chat_consumer()
    except Exception as e:
        logger.warning("chat_consumer failed to start (chat disabled this run): %s", e)
    logger.info("Webhook server lifespan started")

    try:
        yield
    finally:
        try:
            await get_task_queue().stop()
        except Exception as e:
            logger.warning("task_queue stop failed: %s", e)
        try:
            await stop_chat_consumer()
        except Exception as e:
            logger.warning("chat_consumer stop failed: %s", e)
        logger.info("Server shutdown complete")


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    started = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        duration_ms = int((time.monotonic() - started) * 1000)
        logger.exception(
            "%s %s → 500 (%dms)",
            request.method,
            request.url.path,
            duration_ms,
        )
        raise
    duration_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "%s %s → %d (%dms)",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response


@app.post("/webhook")
async def webhook(request: Request) -> JSONResponse:
    body = await verify_github_signature(request)
    github_event = request.headers.get("X-GitHub-Event", "")
    delivery_id = request.headers.get("X-GitHub-Delivery", "")
    if get_dedup().seen_before(delivery_id):
        logger.info("Webhook %s: duplicate delivery %s — skipped", github_event, delivery_id)
        return JSONResponse(
            {"status": "duplicate", "reason": f"delivery {delivery_id} already processed"},
            status_code=200,
        )
    accepted, reason = await route_webhook(body, github_event)
    logger.info("Webhook %s (%s): %s", github_event, delivery_id[:8], reason)
    return JSONResponse(
        {"status": "accepted" if accepted else "ignored", "reason": reason},
        status_code=200,
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


def _outcomes_breakdown() -> dict[str, float]:
    """Snapshot the pipeline-outcome counter from the metrics registry."""
    out: dict[str, float] = {}
    for metric in metrics_registry.collect():
        if metric.name != "cicd_agent_pipeline_outcomes":
            continue
        for sample in metric.samples:
            if not sample.name.endswith("_total"):
                continue
            label = sample.labels.get("outcome", "")
            if label:
                out[label] = sample.value
    return out


@app.get("/status")
async def status() -> dict:
    settings = get_settings()
    try:
        queue = get_task_queue()
        depth = queue.depth
        processed = queue.processed
        failed = queue.failed
    except RuntimeError:
        depth = processed = failed = 0
    try:
        rl = get_rate_limiter()
        gemini_today = rl.stats.requests_today
    except RuntimeError:
        gemini_today = 0
    cost_today = cost_total_today()
    return {
        "uptime_seconds": round(time.monotonic() - _start_time, 1),
        "queue": {
            "depth": depth,
            "max": getattr(get_task_queue(), "_queue", None) and depth,
            "processed": processed,
            "failed": failed,
        },
        "active_runs": active_runs._value.get() if hasattr(active_runs, "_value") else 0,
        "gemini": {
            "requests_today": gemini_today,
            "daily_request_limit": DAILY_REQUEST_LIMIT,
            "cost_today_dollars": round(cost_today, 6),
            "daily_cost_cap_dollars": settings.daily_cost_cap_dollars,
            "cost_pct_of_cap": (
                round(100.0 * cost_today / settings.daily_cost_cap_dollars, 1)
                if settings.daily_cost_cap_dollars > 0
                else None
            ),
        },
        "outcomes": _outcomes_breakdown(),
        "repo": settings.full_repo_name,
        "model": settings.primary_model,
    }


@app.get("/version")
async def version() -> dict:
    settings = get_settings()
    sha = os.getenv("GIT_SHA", "unknown")
    build_time = os.getenv("BUILD_TIME", "unknown")
    return {
        "git_sha": sha,
        "build_time": build_time,
        "primary_model": settings.primary_model,
        "light_model": settings.light_model,
        "repo": settings.full_repo_name,
    }


@app.get("/metrics")
async def metrics() -> Response:
    """Prometheus exposition endpoint. Scrape with default Prom config."""
    return Response(content=generate_latest(metrics_registry), media_type=CONTENT_TYPE_LATEST)


class TriggerRequest(BaseModel):
    run_id: int
    repo_owner: str
    repo_name: str
    workflow_name: str
    branch: str
    head_sha: str
    html_url: str


@app.post("/trigger")
async def trigger(body: TriggerRequest) -> JSONResponse:
    settings = get_settings()
    if settings.production_mode:
        return JSONResponse({"error": "disabled in production"}, status_code=404)

    event = WorkflowFailureEvent(
        run_id=body.run_id,
        repo_owner=body.repo_owner,
        repo_name=body.repo_name,
        workflow_name=body.workflow_name,
        branch=body.branch,
        head_sha=body.head_sha,
        html_url=body.html_url,
        sender_login="cli",
    )

    try:
        accepted = await enqueue_event(event)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    except Exception as e:
        logger.exception("trigger: unexpected error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)

    return JSONResponse(
        {
            "status": "triggered" if accepted else "rejected",
            "run_id": body.run_id,
        },
        status_code=200,
    )
