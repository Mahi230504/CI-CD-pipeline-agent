"""
Rate limiter for the Gemini free tier API.

Enforces:
- One concurrent Gemini call at a time (asyncio.Semaphore)
- Minimum 7 seconds between calls (stays under 10 RPM free tier limit)
- Daily request counter with warning at 200/250 requests
- Exponential backoff on 429 responses (2^n seconds, max 3 retries)

All Gemini calls in the project MUST go through rate_limited_call() in this module.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Request-count cap was a Gemini free-tier guard (250/day). On paid OpenRouter
# the meaningful ceiling is the daily *cost* cap (DAILY_COST_CAP_DOLLARS), so
# this count limit is set high enough to be effectively a safety backstop only.
DAILY_REQUEST_LIMIT = 100_000
DAILY_WARNING_THRESHOLD = 90_000


class GeminiError(Exception):
    def __init__(self, message: str, agent: str = "unknown", original: Exception | None = None):
        super().__init__(message)
        self.agent = agent
        self.original = original


class GeminiRateLimitError(Exception):
    pass


class DailyLimitReachedError(Exception):
    pass


class DailyCostCapReachedError(DailyLimitReachedError):
    """Raised when the day's cumulative Gemini cost would exceed the configured cap.

    Inherits from DailyLimitReachedError so the agents' existing
    `except DailyLimitReachedError` handlers catch this without code changes.
    """


@dataclass
class RateLimiterStats:
    requests_today: int = 0
    requests_total: int = 0
    rate_limit_hits: int = 0
    last_call_at: float = 0.0
    stat_date: date = field(default_factory=date.today)

    def reset_if_new_day(self) -> None:
        today = date.today()
        if today != self.stat_date:
            logger.info(
                "rate_limiter: new day, resetting counter (was %d on %s)",
                self.requests_today,
                self.stat_date,
            )
            self.requests_today = 0
            self.stat_date = today

    def record_call(self) -> None:
        self.reset_if_new_day()
        self.requests_today += 1
        self.requests_total += 1
        self.last_call_at = time.monotonic()
        if DAILY_WARNING_THRESHOLD <= self.requests_today < DAILY_REQUEST_LIMIT:
            logger.warning(
                "rate_limiter: %d/%d daily Gemini requests used",
                self.requests_today,
                DAILY_REQUEST_LIMIT,
            )

    def record_rate_limit_hit(self) -> None:
        self.rate_limit_hits += 1

    @property
    def is_daily_limit_reached(self) -> bool:
        self.reset_if_new_day()
        return self.requests_today >= DAILY_REQUEST_LIMIT

    @property
    def seconds_since_last_call(self) -> float:
        if self.last_call_at == 0.0:
            return float("inf")
        return time.monotonic() - self.last_call_at


_RATE_LIMIT_TYPE_KEYWORDS = ("ratelimit", "resourceexhausted", "quotaexceeded", "unavailable")
_RATE_LIMIT_MESSAGE_KEYWORDS = (
    "rate limit",
    "quota exceeded",
    "resource exhausted",
    "too many requests",
    "unavailable",
    "currently experiencing high demand",
    "503",
)


def _is_rate_limit_error(e: Exception) -> bool:
    type_name = type(e).__name__.lower()
    if any(k in type_name for k in _RATE_LIMIT_TYPE_KEYWORDS):
        return True
    raw = str(e)
    if "429" in raw or "503" in raw:
        return True
    lower = raw.lower()
    return any(k in lower for k in _RATE_LIMIT_MESSAGE_KEYWORDS)


class GeminiRateLimiter:
    def __init__(self, min_delay_seconds: float = 7.0):
        self._min_delay = float(min_delay_seconds)
        self._semaphore = asyncio.Semaphore(1)
        self._stats = RateLimiterStats()

    @property
    def stats(self) -> RateLimiterStats:
        return self._stats

    async def _wait_for_min_delay(self) -> None:
        remaining = self._min_delay - self._stats.seconds_since_last_call
        if remaining > 0:
            await asyncio.sleep(remaining)

    async def execute(
        self,
        coro_fn: Callable[[], Awaitable[T]],
        max_retries: int = 3,
    ) -> T:
        if self._stats.is_daily_limit_reached:
            raise DailyLimitReachedError(
                f"Daily Gemini request limit of {DAILY_REQUEST_LIMIT} reached"
            )

        # Daily $ cost cap — checked at call time so the next ATTEMPTED call
        # short-circuits, not the previous one. Cost is updated *after* each
        # successful call in gemini_client.record_gemini_call().
        from config.settings import get_settings
        from metrics import cost_total_today

        cap = float(get_settings().daily_cost_cap_dollars or 0.0)
        if cap > 0:
            spent = cost_total_today()
            if spent >= cap:
                raise DailyCostCapReachedError(
                    f"Daily cost cap reached: ${spent:.4f} / ${cap:.4f}"
                )

        async with self._semaphore:
            await self._wait_for_min_delay()

            last_error: Exception | None = None
            for attempt in range(max_retries + 1):
                try:
                    result = await coro_fn()
                    self._stats.record_call()
                    return result
                except Exception as e:
                    if not _is_rate_limit_error(e):
                        raise
                    self._stats.record_rate_limit_hit()
                    last_error = e
                    if attempt >= max_retries:
                        break
                    backoff = 2 ** (attempt + 1)
                    logger.warning(
                        "rate_limiter: 429 hit (attempt %d/%d), backing off %ds",
                        attempt + 1,
                        max_retries + 1,
                        backoff,
                    )
                    await asyncio.sleep(backoff)

            raise GeminiRateLimitError(
                f"Gemini rate limit not cleared after {max_retries} retries"
            ) from last_error


_rate_limiter: GeminiRateLimiter | None = None


def init_rate_limiter(min_delay_seconds: float = 7.0) -> GeminiRateLimiter:
    global _rate_limiter
    _rate_limiter = GeminiRateLimiter(min_delay_seconds=min_delay_seconds)
    logger.info("rate_limiter initialised: min_delay=%.1fs", min_delay_seconds)
    return _rate_limiter


def get_rate_limiter() -> GeminiRateLimiter:
    if _rate_limiter is None:
        raise RuntimeError(
            "rate_limiter not initialised — call init_rate_limiter() at startup"
        )
    return _rate_limiter


async def rate_limited_call(
    coro_fn: Callable[[], Awaitable[T]],
    max_retries: int = 3,
) -> T:
    return await get_rate_limiter().execute(coro_fn, max_retries=max_retries)
