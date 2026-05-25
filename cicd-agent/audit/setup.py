"""
Root logger configuration for the CI/CD agent.

Two output modes selected via `CICD_AGENT_LOG_FORMAT`:
- `human` (default) — single-line with timestamp, level, logger, and an inline
  `[run=X phase=Y]` prefix when log context is set.
- `json` — newline-delimited JSON, one object per log record. For production
  log shippers / `jq` workflows.

Both modes pull run-scoped fields (run_id, error_hash, phase) from contextvars
in `audit/context.py` via the `ContextFilter`. Configure once at startup;
idempotent so it can be called from main.py AND the FastAPI lifespan.

The audit logger (`cicd_agent.audit`) keeps its own file handler and stays
non-propagating so structured JSON audit lines don't double up on stderr.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any, Final

from audit.context import get_run_context

_FORMAT_HUMAN: Final = "%(asctime)s %(levelname)-7s %(name)-30s%(ctxprefix)s %(message)s"
_DATEFMT: Final = "%H:%M:%S"

# Third-party libs we don't want to hear from unless they error.
_QUIET_LIBS: Final = (
    "httpx",
    "httpcore",
    "anyio",
    "mcp.client",
    "mcp.shared",
    "asyncio",
    "google_genai",
    "urllib3",
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
)

# LogRecord attributes the json formatter should NOT emit (they're either
# noise or already covered by named fields).
_LOG_RECORD_BUILTIN_ATTRS: Final = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "message", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName", "taskName",
        "ctxprefix",
    }
)


class ContextFilter(logging.Filter):
    """Pull run-scoped contextvars into every LogRecord."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        ctx = get_run_context()
        record.run_id = ctx.run_id
        record.error_hash = ctx.error_hash
        record.phase = ctx.phase
        # The human formatter consumes `ctxprefix`; computed here so the
        # format string itself stays simple.
        parts: list[str] = []
        if ctx.run_id is not None:
            parts.append(f"run={ctx.run_id}")
        if ctx.phase:
            parts.append(f"phase={ctx.phase}")
        if ctx.error_hash:
            parts.append(f"err={ctx.error_hash[:8]}")
        record.ctxprefix = f" [{' '.join(parts)}]" if parts else ""
        return True


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per record. Keys: ts, level, logger, msg, run_id,
    error_hash, phase, plus any extras passed via `logger.x(..., extra={...})`."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        run_id = getattr(record, "run_id", None)
        if run_id is not None:
            payload["run_id"] = run_id
        for key in ("error_hash", "phase"):
            value = getattr(record, key, None)
            if value:
                payload[key] = value
        # Any extras passed via `logger.info("...", extra={...})`.
        for k, v in record.__dict__.items():
            if k in _LOG_RECORD_BUILTIN_ATTRS or k in payload or k.startswith("_"):
                continue
            if k in ("run_id", "error_hash", "phase"):
                continue
            try:
                json.dumps(v)
            except (TypeError, ValueError):
                v = repr(v)
            payload[k] = v
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    """Install a stderr handler on the root logger. Idempotent.

    Honours `CICD_AGENT_LOG_FORMAT` (`human` | `json`) and
    `CICD_AGENT_LOG_LEVEL` (`DEBUG` | `INFO` | ...).
    """
    level_name = os.getenv("CICD_AGENT_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    mode = os.getenv("CICD_AGENT_LOG_FORMAT", "human").lower()

    root = logging.getLogger()
    if any(getattr(h, "_cicd_root", False) for h in root.handlers):
        root.setLevel(level)
        return

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)
    if mode == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(_FORMAT_HUMAN, datefmt=_DATEFMT))
    handler.addFilter(ContextFilter())
    setattr(handler, "_cicd_root", True)
    root.addHandler(handler)
    root.setLevel(level)

    for name in _QUIET_LIBS:
        logging.getLogger(name).setLevel(logging.WARNING)
