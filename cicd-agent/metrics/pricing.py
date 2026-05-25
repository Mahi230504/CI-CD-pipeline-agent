"""Gemini pricing table (paid-tier rates, USD per million tokens).

We track costs even on the free tier so the daily-cap mechanic exercises the
same code path in dev and prod. Numbers are approximate and may drift — they
exist to power an upper-bound spend alert, not a billing system.

Source: https://ai.google.dev/pricing (as of 2026-05).
"""

from __future__ import annotations

from typing import Final

# Per million tokens, USD. (input_rate, output_rate).
_PRICING: Final[dict[str, tuple[float, float]]] = {
    "gemini-2.5-flash": (0.075, 0.30),
    "gemini-2.5-flash-lite": (0.05, 0.10),
    "gemini-2.5-pro": (3.50, 10.50),
    "gemini-2.0-flash": (0.075, 0.30),
}

_FALLBACK: Final[tuple[float, float]] = (0.075, 0.30)


def cost_for_call(model: str, input_tokens: int, output_tokens: int) -> float:
    """Approximate dollar cost of one Gemini call. Returns 0.0 on unknown models
    only if the rates table really has no entry."""
    rates = _PRICING.get(model, _FALLBACK)
    in_rate, out_rate = rates
    return (input_tokens / 1_000_000.0) * in_rate + (output_tokens / 1_000_000.0) * out_rate
