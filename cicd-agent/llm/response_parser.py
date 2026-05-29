"""
Parsers for structured Gemini agent outputs.

Functions:
- parse_diagnosis(text) → Diagnosis | None
- parse_diff(text) → str | None
- parse_yaml_blocks(text) → tuple[str, str] | None  (original, optimized)
- parse_flakiness_verdict(text) → dict | None
- validate_json_output(text, schema) → dict | None

All parsers: attempt strict JSON first, fall back to regex extraction,
return None (never raise) so the orchestrator can handle parse failures gracefully.
"""

from __future__ import annotations

import json
import re
from typing import Optional

from config.constants import ErrorType
from models.run import Diagnosis


_JSON_FENCE_RE = re.compile(r"```json\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_PLAIN_FENCE_RE = re.compile(r"```\s*(.*?)```", re.DOTALL)
_YAML_FENCE_RE = re.compile(r"```yaml\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_DIFF_FENCE_RE = re.compile(r"```diff\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_DIFF_OLD_RE = re.compile(r"^--- ", re.MULTILINE)
_DIFF_NEW_RE = re.compile(r"^\+\+\+ ", re.MULTILINE)
_DIFF_HUNK_RE = re.compile(r"^@@", re.MULTILINE)


def _try_parse_json(s: str) -> Optional[object]:
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _extract_json_from_text(text: str) -> Optional[str]:
    if not text:
        return None

    stripped = text.strip()
    if _try_parse_json(stripped) is not None:
        return stripped

    m = _JSON_FENCE_RE.search(text)
    if m:
        candidate = m.group(1).strip()
        if _try_parse_json(candidate) is not None:
            return candidate

    for fence_match in _PLAIN_FENCE_RE.finditer(text):
        candidate = fence_match.group(1).strip()
        if _try_parse_json(candidate) is not None:
            return candidate

    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    if _try_parse_json(candidate) is not None:
                        return candidate
                    break
        start = text.find("{", start + 1)

    return None


def _clamp_float(value: object, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return lo
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


def _coerce_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1", "on")
    return default


def _is_valid_diff(text: str) -> bool:
    if not text:
        return False
    return bool(
        _DIFF_OLD_RE.search(text)
        and _DIFF_NEW_RE.search(text)
        and _DIFF_HUNK_RE.search(text)
    )


def parse_diagnosis(text: str) -> Optional[Diagnosis]:
    try:
        raw = _extract_json_from_text(text)
        if raw is None:
            return None
        data = _try_parse_json(raw)
        if not isinstance(data, dict):
            return None

        error_type = ErrorType.coerce(data.get("error_type"))

        file_val = data.get("file")
        file = file_val if isinstance(file_val, str) and file_val.strip() else None

        line_val = data.get("line_number", data.get("line"))
        line_number: Optional[int] = None
        if line_val is not None:
            try:
                ln = int(line_val)
                if ln > 0:
                    line_number = ln
            except (TypeError, ValueError):
                line_number = None

        explanation_val = data.get("explanation")
        explanation = explanation_val if isinstance(explanation_val, str) and explanation_val.strip() else "No explanation"

        confidence = _clamp_float(data.get("confidence", 0.0))
        is_patchable = _coerce_bool(data.get("is_patchable", False))

        return Diagnosis(
            error_type=error_type,
            file=file,
            line_number=line_number,
            explanation=explanation,
            confidence=confidence,
            is_patchable=is_patchable,
            raw_response=text,
        )
    except Exception:
        return None


def parse_diff(text: str) -> Optional[str]:
    if not text:
        return None
    if "CANNOT_PATCH" in text:
        return None

    m = _DIFF_FENCE_RE.search(text)
    if m:
        candidate = m.group(1).strip()
        if _is_valid_diff(candidate):
            return candidate

    for fence_match in _PLAIN_FENCE_RE.finditer(text):
        candidate = fence_match.group(1).strip()
        if _is_valid_diff(candidate):
            return candidate

    stripped = text.strip()
    if stripped.startswith("---") and _is_valid_diff(stripped):
        return stripped

    return None


def parse_yaml_blocks(text: str) -> Optional[tuple[str, str]]:
    if not text:
        return None
    blocks = [m.group(1) for m in _YAML_FENCE_RE.finditer(text)]
    if len(blocks) < 2:
        return None
    original = blocks[0].strip()
    optimized = blocks[1].strip()
    if not original or not optimized:
        return None
    return (original, optimized)


def parse_optimization_summary(text: str) -> Optional[dict]:
    try:
        raw = _extract_json_from_text(text)
        if raw is None:
            return None
        data = _try_parse_json(raw)
        if not isinstance(data, dict):
            return None

        jobs_parallelized = data.get("jobs_parallelized", [])
        if not isinstance(jobs_parallelized, list):
            jobs_parallelized = []

        cache_steps_added = data.get("cache_steps_added", [])
        if not isinstance(cache_steps_added, list):
            cache_steps_added = []

        try:
            estimated_savings_seconds = max(0, int(data.get("estimated_savings_seconds", 0)))
        except (TypeError, ValueError):
            estimated_savings_seconds = 0

        explanation = data.get("explanation", "")
        if not isinstance(explanation, str):
            explanation = str(explanation)

        return {
            "jobs_parallelized": jobs_parallelized,
            "cache_steps_added": cache_steps_added,
            "estimated_savings_seconds": estimated_savings_seconds,
            "explanation": explanation,
        }
    except Exception:
        return None


def parse_flakiness_verdict(text: str) -> Optional[dict]:
    try:
        raw = _extract_json_from_text(text)
        if raw is None:
            return None
        data = _try_parse_json(raw)
        if not isinstance(data, dict):
            return None
        return {
            "is_flaky": _coerce_bool(data.get("is_flaky", False)),
            "reason": str(data.get("reason", "")),
            "pass_rate": _clamp_float(data.get("pass_rate", 0.0)),
        }
    except Exception:
        return None


def parse_deploy_verdict(text: str) -> Optional["DeployVerdict"]:
    """Parse the deploy guard's JSON response into a DeployVerdict.

    Tolerant of the same wrapping a strict prompt may still produce: bare
    JSON, ```json``` fences, or JSON embedded in surrounding prose. Returns
    None when no JSON object can be extracted or required fields are
    missing — the orchestrator treats None as an automatic block.

    The import is deferred so this module stays usable in any context
    where models.cd hasn't been loaded yet (e.g. log-side-only tools).
    """
    from models.cd import DeployRisk, DeployVerdict

    raw = _extract_json_from_text(text)
    if raw is None:
        return None
    data = _try_parse_json(raw)
    if not isinstance(data, dict):
        return None

    if "approve" not in data:
        return None

    concerns_raw = data.get("concerns", [])
    if isinstance(concerns_raw, list):
        concerns = tuple(str(c).strip() for c in concerns_raw if str(c).strip())
    else:
        concerns = ()

    return DeployVerdict(
        approve=_coerce_bool(data.get("approve", False)),
        risk=DeployRisk.coerce(data.get("risk")),
        reason=str(data.get("reason", "")).strip() or "no reason provided",
        concerns=concerns,
        confidence=_clamp_float(data.get("confidence", 0.0)),
        raw_response=text,
    )


def validate_json_output(text: str, required_keys: list[str]) -> Optional[dict]:
    try:
        raw = _extract_json_from_text(text)
        if raw is None:
            return None
        data = _try_parse_json(raw)
        if not isinstance(data, dict):
            return None
        for key in required_keys:
            if key not in data:
                return None
        return data
    except Exception:
        return None
