"""Tests for the daily-cost-cap guard in the rate limiter."""

from __future__ import annotations

import asyncio

import pytest

from llm.rate_limiter import (
    DailyCostCapReachedError,
    DailyLimitReachedError,
    GeminiRateLimiter,
)
from metrics import record_gemini_call
from metrics.registry import reset_cost_today_for_testing


@pytest.fixture(autouse=True)
def _reset_cost() -> None:
    reset_cost_today_for_testing()
    yield


@pytest.fixture
def low_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force a tiny daily cap so we can trip it in tests."""
    from config import settings as settings_module

    settings_module.get_settings.cache_clear()
    monkeypatch.setenv("DAILY_COST_CAP_DOLLARS", "0.0001")
    yield
    settings_module.get_settings.cache_clear()


async def test_below_cap_executes(low_cap: None) -> None:
    rl = GeminiRateLimiter(min_delay_seconds=0.0)

    async def noop() -> str:
        return "ok"

    result = await rl.execute(noop)
    assert result == "ok"


async def test_above_cap_raises(low_cap: None) -> None:
    # Spend more than the $0.0001 cap.
    record_gemini_call("gemini-2.5-flash", 0.1, 10_000, 10_000)  # $0.00375

    rl = GeminiRateLimiter(min_delay_seconds=0.0)

    async def noop() -> str:
        return "ok"

    with pytest.raises(DailyCostCapReachedError):
        await rl.execute(noop)


async def test_cost_cap_inherits_daily_limit_error(low_cap: None) -> None:
    """Agents catch DailyLimitReachedError generically — cost cap must hit that
    handler too, not surface as an uncaught GeminiError."""
    record_gemini_call("gemini-2.5-flash", 0.1, 10_000, 10_000)
    rl = GeminiRateLimiter(min_delay_seconds=0.0)

    async def noop() -> str:
        return "ok"

    with pytest.raises(DailyLimitReachedError):  # parent class
        await rl.execute(noop)


async def test_cap_disabled_when_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cap of 0 means 'no cap' — cost cap must not trigger."""
    from config import settings as settings_module

    settings_module.get_settings.cache_clear()
    monkeypatch.setenv("DAILY_COST_CAP_DOLLARS", "0")
    try:
        record_gemini_call("gemini-2.5-flash", 0.1, 1_000_000, 1_000_000)  # $0.375

        rl = GeminiRateLimiter(min_delay_seconds=0.0)

        async def noop() -> str:
            return "ok"

        # Should NOT raise — cap=0 disables it.
        assert await rl.execute(noop) == "ok"
    finally:
        settings_module.get_settings.cache_clear()
