"""
Persistent run registry — prevents infinite patch loops.

Stores run metadata in run_registry.json (never committed to git).

Responsibilities:
- is_duplicate(run_id) → bool: was this run already processed?
- get_attempt_count(error_hash) → int: how many patch attempts for this error pattern?
- increment_attempt(error_hash): called before each patch
- mark_escalated(error_hash): stops all future auto-patching for this error
- cleanup(): removes entries older than 7 days

error_hash = sha256(repo + workflow_name + error_type + file + line_number)
This means: the same type of error in the same file counts across runs.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_RETENTION_DAYS = 7


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class RunRegistry:
    def __init__(self, registry_path: Path = Path("run_registry.json")):
        self._path = Path(registry_path)
        self._data: dict[str, dict] = {"runs": {}, "error_attempts": {}}
        if self._path.exists():
            try:
                with self._path.open("r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self._data["runs"] = loaded.get("runs", {}) if isinstance(loaded.get("runs"), dict) else {}
                    attempts = loaded.get("error_attempts", {})
                    self._data["error_attempts"] = attempts if isinstance(attempts, dict) else {}
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("registry load failed for %s: %s — using empty store", self._path, e)
                self._data = {"runs": {}, "error_attempts": {}}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, default=str)
        except Exception as e:
            logger.warning("registry save failed for %s: %s", self._path, e)

    def _cleanup_old_entries(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=_RETENTION_DAYS)

        runs_to_remove: list[str] = []
        for run_id, entry in self._data.get("runs", {}).items():
            ts = _parse_iso((entry or {}).get("processed_at"))
            if ts is not None and ts < cutoff:
                runs_to_remove.append(run_id)
        for run_id in runs_to_remove:
            self._data["runs"].pop(run_id, None)

        attempts_to_remove: list[str] = []
        for error_hash, entry in self._data.get("error_attempts", {}).items():
            ts = _parse_iso((entry or {}).get("last_seen"))
            if ts is not None and ts < cutoff:
                attempts_to_remove.append(error_hash)
        for error_hash in attempts_to_remove:
            self._data["error_attempts"].pop(error_hash, None)

        if runs_to_remove or attempts_to_remove:
            logger.info(
                "registry cleanup: removed %d runs, %d attempts",
                len(runs_to_remove),
                len(attempts_to_remove),
            )
        self._save()

    def is_duplicate(self, run_id: int) -> bool:
        return str(run_id) in self._data.get("runs", {})

    def mark_run_processed(self, run_id: int, state: str) -> None:
        self._data.setdefault("runs", {})[str(run_id)] = {
            "processed_at": _now_iso(),
            "state": state,
        }
        self._save()
        self._cleanup_old_entries()

    def get_attempt_count(self, error_hash: str) -> int:
        entry = self._data.get("error_attempts", {}).get(error_hash, {}) or {}
        try:
            return int(entry.get("count", 0))
        except (TypeError, ValueError):
            return 0

    def is_escalated(self, error_hash: str) -> bool:
        entry = self._data.get("error_attempts", {}).get(error_hash, {}) or {}
        return bool(entry.get("escalated", False))

    def increment_attempt(self, error_hash: str) -> int:
        attempts = self._data.setdefault("error_attempts", {})
        entry = attempts.get(error_hash) or {"count": 0, "escalated": False}
        new_count = int(entry.get("count", 0)) + 1
        attempts[error_hash] = {
            "count": new_count,
            "last_seen": _now_iso(),
            "escalated": bool(entry.get("escalated", False)),
            "open_pr_number": entry.get("open_pr_number"),
            "open_pr_url": entry.get("open_pr_url"),
        }
        self._save()
        return new_count

    def mark_escalated(self, error_hash: str) -> None:
        attempts = self._data.setdefault("error_attempts", {})
        entry = attempts.get(error_hash) or {"count": 0}
        attempts[error_hash] = {
            "count": int(entry.get("count", 0)),
            "last_seen": _now_iso(),
            "escalated": True,
            "open_pr_number": entry.get("open_pr_number"),
            "open_pr_url": entry.get("open_pr_url"),
        }
        self._save()

    def record_open_pr(self, error_hash: str, pr_number: int, pr_url: str | None) -> None:
        """Remember which PR is currently addressing this error pattern.
        Used by the dedup gate to comment on an existing PR rather than opening a
        duplicate one for a recurring failure."""
        attempts = self._data.setdefault("error_attempts", {})
        entry = attempts.get(error_hash) or {"count": 0, "escalated": False}
        attempts[error_hash] = {
            "count": int(entry.get("count", 0)),
            "last_seen": _now_iso(),
            "escalated": bool(entry.get("escalated", False)),
            "open_pr_number": int(pr_number),
            "open_pr_url": pr_url if isinstance(pr_url, str) else None,
        }
        self._save()

    def get_open_pr(self, error_hash: str) -> tuple[int, str | None] | None:
        entry = self._data.get("error_attempts", {}).get(error_hash, {}) or {}
        pr_number = entry.get("open_pr_number")
        if not isinstance(pr_number, int):
            return None
        url = entry.get("open_pr_url")
        return pr_number, url if isinstance(url, str) else None

    def clear_open_pr(self, error_hash: str) -> None:
        attempts = self._data.get("error_attempts", {})
        entry = attempts.get(error_hash)
        if not entry:
            return
        entry.pop("open_pr_number", None)
        entry.pop("open_pr_url", None)
        self._save()


_registry: RunRegistry | None = None


def init_registry(path: Path | str = Path("run_registry.json")) -> None:
    global _registry
    _registry = RunRegistry(Path(path))
    logger.info("run_registry initialised at %s", path)


def get_registry() -> RunRegistry:
    if _registry is None:
        raise RuntimeError(
            "run_registry not initialised — call init_registry() at startup"
        )
    return _registry
