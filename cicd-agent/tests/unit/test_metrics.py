"""Tests for the Prometheus metrics + cost tracking."""

from __future__ import annotations

import pytest

from metrics import (
    cost_total_today,
    gemini_calls_total,
    gemini_cost_dollars_total,
    gemini_tokens_total,
    pipeline_duration_seconds,
    pipeline_outcomes_total,
    record_gemini_call,
    record_pipeline,
)
from metrics.pricing import cost_for_call
from metrics.registry import reset_cost_today_for_testing


@pytest.fixture(autouse=True)
def _reset_cost() -> None:
    reset_cost_today_for_testing()
    yield


def _counter_value(counter, **labels) -> float:
    """Read a labelled counter's current value."""
    return counter.labels(**labels)._value.get() if labels else counter._value.get()


def test_record_pipeline_increments_outcome_counter() -> None:
    before = _counter_value(pipeline_outcomes_total, outcome="success")
    record_pipeline("success", 12.5)
    assert _counter_value(pipeline_outcomes_total, outcome="success") == before + 1


def test_record_pipeline_observes_duration() -> None:
    before = pipeline_duration_seconds._sum.get()
    record_pipeline("escalated", 4.2)
    assert pipeline_duration_seconds._sum.get() == pytest.approx(before + 4.2, abs=1e-6)


def test_pricing_known_model() -> None:
    # flash: $0.075 input + $0.30 output per million.
    # 1M input + 1M output → $0.375
    assert cost_for_call("gemini-2.5-flash", 1_000_000, 1_000_000) == pytest.approx(0.375, abs=1e-6)


def test_pricing_unknown_model_falls_back() -> None:
    # Unknown model: should still produce a > 0 cost via fallback rates.
    cost = cost_for_call("gemini-future-9000", 1000, 1000)
    assert cost > 0


def test_record_gemini_call_increments_all_counters() -> None:
    model = "gemini-2.5-flash"
    calls_before = _counter_value(gemini_calls_total, model=model)
    in_before = _counter_value(gemini_tokens_total, model=model, direction="input")
    out_before = _counter_value(gemini_tokens_total, model=model, direction="output")
    cost_before = _counter_value(gemini_cost_dollars_total, model=model)

    cost = record_gemini_call(model, duration_seconds=1.5, input_tokens=500, output_tokens=200)
    assert cost > 0

    assert _counter_value(gemini_calls_total, model=model) == calls_before + 1
    assert _counter_value(gemini_tokens_total, model=model, direction="input") == in_before + 500
    assert _counter_value(gemini_tokens_total, model=model, direction="output") == out_before + 200
    assert _counter_value(gemini_cost_dollars_total, model=model) == pytest.approx(
        cost_before + cost, abs=1e-9
    )


def test_cost_total_today_accumulates() -> None:
    assert cost_total_today() == 0.0
    record_gemini_call("gemini-2.5-flash", 1.0, 1_000_000, 1_000_000)  # $0.375
    record_gemini_call("gemini-2.5-flash", 1.0, 1_000_000, 1_000_000)  # +$0.375
    assert cost_total_today() == pytest.approx(0.75, abs=1e-6)


def test_record_gemini_call_zero_tokens_no_cost() -> None:
    cost = record_gemini_call("gemini-2.5-flash", 0.1, 0, 0)
    assert cost == 0.0
