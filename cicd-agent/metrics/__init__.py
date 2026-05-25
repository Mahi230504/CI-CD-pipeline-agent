"""Prometheus metrics for the CI/CD agent.

Public surface:
- `registry`        — the prometheus_client CollectorRegistry to expose at /metrics
- the metric objects themselves (counters/histograms/gauges)
- `record_pipeline(outcome, duration_seconds)`
- `record_gemini_call(model, duration_seconds, input_tokens, output_tokens)`
- `cost_total_today()` for the /status endpoint
"""

from __future__ import annotations

from metrics.registry import (
    active_runs,
    cost_total_today,
    gemini_calls_total,
    gemini_call_duration_seconds,
    gemini_cost_dollars_total,
    gemini_tokens_total,
    pipeline_duration_seconds,
    pipeline_outcomes_total,
    queue_depth,
    record_gemini_call,
    record_pipeline,
    registry,
)

__all__ = [
    "active_runs",
    "cost_total_today",
    "gemini_calls_total",
    "gemini_call_duration_seconds",
    "gemini_cost_dollars_total",
    "gemini_tokens_total",
    "pipeline_duration_seconds",
    "pipeline_outcomes_total",
    "queue_depth",
    "record_gemini_call",
    "record_pipeline",
    "registry",
]
