"""
Structured audit logger — records every agent action.

Writes JSON lines to logs/{YYYY-MM-DD}.jsonl
Each line is a JSON object with: timestamp, run_id, step, result, duration_ms, metadata

What is NEVER logged:
- Raw log file contents
- API keys or tokens
- Full file contents
- Personal data

Provides: log_step(run_id, step, result, duration_ms, **metadata)
Uses Python's logging module internally — never print().
"""

from __future__ import annotations

import json
import logging
import sys
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from config.constants import DEAD_LETTER_FILE

_LOGGER_NAME = "cicd_agent.audit"
_SENSITIVE_KEY_FRAGMENTS = ("key", "token", "secret", "password")


def _scrub_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for k, v in metadata.items():
        lower_k = str(k).lower()
        if any(frag in lower_k for frag in _SENSITIVE_KEY_FRAGMENTS):
            continue
        cleaned[k] = v
    return cleaned


def _step_value(step: Any) -> str:
    if hasattr(step, "value"):
        return str(step.value)
    return str(step)


class AuditLogger:
    def __init__(self, log_dir: Path):
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._current_date: date | None = None
        self._current_file: Path | None = None

        self._logger = logging.getLogger(_LOGGER_NAME)
        self._logger.setLevel(logging.INFO)
        if not any(
            isinstance(h, logging.StreamHandler) and getattr(h, "_cicd_audit", False)
            for h in self._logger.handlers
        ):
            handler = logging.StreamHandler(sys.stderr)
            handler.setLevel(logging.WARNING)
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
            )
            setattr(handler, "_cicd_audit", True)
            self._logger.addHandler(handler)
            self._logger.propagate = False

    def _rotate_if_needed(self) -> Path:
        today = date.today()
        if self._current_date != today or self._current_file is None:
            self._current_date = today
            self._current_file = self._log_dir / f"{today.isoformat()}.jsonl"
        return self._current_file

    def _append_json(self, target: Path, record: dict[str, Any]) -> None:
        try:
            with target.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except Exception as e:
            self._logger.error("audit log write failed: %s", e)

    def log_step(
        self,
        run_id: int | str,
        step: Any,
        result: str,
        duration_ms: int = 0,
        **metadata: Any,
    ) -> None:
        record: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
            "step": _step_value(step),
            "result": result,
            "duration_ms": duration_ms,
        }
        record.update(_scrub_metadata(metadata))
        target = self._rotate_if_needed()
        self._append_json(target, record)
        self._logger.info(
            "audit: run=%s step=%s result=%s duration_ms=%d",
            run_id,
            record["step"],
            result,
            duration_ms,
        )

    def log_error(self, run_id: int | str, step: Any, error: Exception) -> None:
        self.log_step(
            run_id,
            step,
            "error",
            error_type=type(error).__name__,
            error_message=str(error)[:200],
        )
        self._logger.error(
            "audit-error: run=%s step=%s %s: %s",
            run_id,
            _step_value(step),
            type(error).__name__,
            str(error)[:200],
        )

    def log_dead_letter(
        self,
        run_id: int | str,
        reason: str,
        raw_event: dict[str, Any],
    ) -> None:
        try:
            target = self._log_dir / DEAD_LETTER_FILE
            record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "run_id": run_id,
                "reason": reason,
                "raw_event": raw_event,
            }
            with target.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except Exception as e:
            try:
                self._logger.error("dead letter write failed: %s", e)
            except Exception:
                pass


_audit_logger: AuditLogger | None = None


def init_audit_logger(log_dir: Path) -> AuditLogger:
    global _audit_logger
    _audit_logger = AuditLogger(log_dir)
    return _audit_logger


def get_audit_logger() -> AuditLogger:
    if _audit_logger is None:
        raise RuntimeError(
            "audit_logger not initialised — call init_audit_logger() at startup"
        )
    return _audit_logger


@asynccontextmanager
async def audit_step(run_id: int | str, step: Any, **metadata: Any):
    from audit.context import set_run_context  # local import: audit/context imports nothing

    step_name = _step_value(step)
    set_run_context(phase=step_name)
    started = time.monotonic()
    try:
        yield
    except Exception as e:
        duration_ms = int((time.monotonic() - started) * 1000)
        get_audit_logger().log_step(
            run_id,
            step,
            "error",
            duration_ms,
            error_type=type(e).__name__,
            error_message=str(e)[:200],
            **metadata,
        )
        raise
    else:
        duration_ms = int((time.monotonic() - started) * 1000)
        get_audit_logger().log_step(run_id, step, "success", duration_ms, **metadata)
