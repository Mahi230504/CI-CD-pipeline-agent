"""Prometheus metric definitions + the helpers agents call to update them.

A single global CollectorRegistry is exposed at /metrics. We use a custom
registry instead of the default global so tests can build fresh registries
without leaking state across test cases.

Metric naming follows the standard `<service>_<thing>_<unit>` convention so
Grafana dashboards translate cleanly across deployments.
"""

from __future__ import annotations

import threading
from datetime import date, datetime, timezone

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from metrics.pricing import cost_for_call

registry = CollectorRegistry()

pipeline_duration_seconds = Histogram(
    "cicd_agent_pipeline_duration_seconds",
    "End-to-end pipeline duration, from event dequeue to notification.",
    buckets=(1, 2.5, 5, 10, 20, 30, 60, 120, 300),
    registry=registry,
)

pipeline_outcomes_total = Counter(
    "cicd_agent_pipeline_outcomes_total",
    "Pipeline runs by final outcome.",
    labelnames=("outcome",),  # success | escalated | failed | deduped | timed_out | flaky
    registry=registry,
)

queue_depth = Gauge(
    "cicd_agent_queue_depth",
    "Current depth of the in-process task queue.",
    registry=registry,
)

active_runs = Gauge(
    "cicd_agent_active_runs",
    "Pipelines currently executing (excludes queued).",
    registry=registry,
)

gemini_calls_total = Counter(
    "cicd_agent_gemini_calls_total",
    "Gemini API calls, labelled by model.",
    labelnames=("model",),
    registry=registry,
)

gemini_call_duration_seconds = Histogram(
    "cicd_agent_gemini_call_duration_seconds",
    "Wall-clock duration of Gemini calls.",
    labelnames=("model",),
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60),
    registry=registry,
)

gemini_tokens_total = Counter(
    "cicd_agent_gemini_tokens_total",
    "Total tokens consumed, by model and direction.",
    labelnames=("model", "direction"),  # direction = "input" | "output"
    registry=registry,
)

gemini_cost_dollars_total = Counter(
    "cicd_agent_gemini_cost_dollars_total",
    "Cumulative estimated Gemini cost in USD.",
    labelnames=("model",),
    registry=registry,
)


# ── Daily cost rollup (used by the cap check + /status, not a Prometheus metric) ─

_cost_lock = threading.Lock()
_cost_today: float = 0.0
_cost_date: date = datetime.now(timezone.utc).date()


def cost_total_today() -> float:
    """Return the cumulative estimated Gemini cost in USD for the current UTC day."""
    global _cost_today, _cost_date
    today = datetime.now(timezone.utc).date()
    with _cost_lock:
        if today != _cost_date:
            _cost_today = 0.0
            _cost_date = today
        return _cost_today


def _add_to_today(amount: float) -> float:
    """Add to today's rollup and return the new total."""
    global _cost_today, _cost_date
    today = datetime.now(timezone.utc).date()
    with _cost_lock:
        if today != _cost_date:
            _cost_today = 0.0
            _cost_date = today
        _cost_today += amount
        return _cost_today


def reset_cost_today_for_testing() -> None:
    global _cost_today, _cost_date
    with _cost_lock:
        _cost_today = 0.0
        _cost_date = datetime.now(timezone.utc).date()


# ── Recording helpers ────────────────────────────────────────────────────────


def record_pipeline(outcome: str, duration_seconds: float) -> None:
    pipeline_outcomes_total.labels(outcome=outcome).inc()
    pipeline_duration_seconds.observe(max(0.0, duration_seconds))


def record_gemini_call(
    model: str,
    duration_seconds: float,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """Update Gemini call/token/cost counters. Returns the dollar cost of this call."""
    gemini_calls_total.labels(model=model).inc()
    gemini_call_duration_seconds.labels(model=model).observe(max(0.0, duration_seconds))
    if input_tokens > 0:
        gemini_tokens_total.labels(model=model, direction="input").inc(input_tokens)
    if output_tokens > 0:
        gemini_tokens_total.labels(model=model, direction="output").inc(output_tokens)
    cost = cost_for_call(model, input_tokens, output_tokens)
    if cost > 0:
        gemini_cost_dollars_total.labels(model=model).inc(cost)
        _add_to_today(cost)
    return cost
