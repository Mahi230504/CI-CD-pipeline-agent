"""
Per-task logging context — propagated via contextvars so every `logger.info`
issued inside `run_pipeline(event)` carries the run_id, error_hash, and
current pipeline phase without anyone having to thread them through manually.

Wire-up:
- `set_run_context(run_id=..., error_hash=..., phase=...)` at the top of each
  pipeline step.
- `clear_run_context()` once the pipeline finishes.
- `ContextFilter` (in audit/setup.py) reads the contextvars and attaches them
  as attributes on every LogRecord. Both human and JSON formatters consume
  those attributes.

contextvars are async-safe: each asyncio task sees its own copy, so concurrent
pipeline runs (when we add them) won't bleed context into each other.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

_run_id: ContextVar[int | None] = ContextVar("cicd_run_id", default=None)
_error_hash: ContextVar[str | None] = ContextVar("cicd_error_hash", default=None)
_phase: ContextVar[str | None] = ContextVar("cicd_phase", default=None)


@dataclass(frozen=True)
class LogContext:
    run_id: int | None
    error_hash: str | None
    phase: str | None

    def as_extras(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.run_id is not None:
            out["run_id"] = self.run_id
        if self.error_hash:
            out["error_hash"] = self.error_hash
        if self.phase:
            out["phase"] = self.phase
        return out


def set_run_context(
    *,
    run_id: int | None = None,
    error_hash: str | None = None,
    phase: str | None = None,
) -> None:
    """Update the current context. None values leave that field unchanged.
    Pass an explicit empty string ("") to clear a string field."""
    if run_id is not None:
        _run_id.set(run_id)
    if error_hash is not None:
        _error_hash.set(error_hash if error_hash != "" else None)
    if phase is not None:
        _phase.set(phase if phase != "" else None)


def get_run_context() -> LogContext:
    return LogContext(
        run_id=_run_id.get(),
        error_hash=_error_hash.get(),
        phase=_phase.get(),
    )


def clear_run_context() -> None:
    _run_id.set(None)
    _error_hash.set(None)
    _phase.set(None)
