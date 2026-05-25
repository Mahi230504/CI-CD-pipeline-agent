"""Tests for the PR risk scorer and auto-label generator."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from github.pr_risk import (
    RiskLevel,
    assess_risk,
    format_risk_section,
    labels_for,
)


@dataclass(frozen=True)
class _FakeDiagnosis:
    """Minimal stand-in for models.run.Diagnosis."""
    error_type: str
    confidence: float


def diag(error_type: str = "test_failure", confidence: float = 0.95) -> _FakeDiagnosis:
    return _FakeDiagnosis(error_type=error_type, confidence=confidence)


# ────────────────────────────── Risk level rules ──────────────────────────────


def test_low_risk_single_file_high_confidence() -> None:
    r = assess_risk(diag(confidence=0.95), ["src/utils/helpers.py"])
    assert r.level == RiskLevel.LOW
    assert r.file_count == 1
    assert r.sensitive_path is None


def test_medium_risk_moderate_confidence() -> None:
    r = assess_risk(diag(confidence=0.75), ["src/utils/helpers.py"])
    assert r.level == RiskLevel.MEDIUM


def test_medium_risk_multiple_files() -> None:
    r = assess_risk(diag(confidence=0.95), ["src/a.py", "src/b.py", "src/c.py"])
    assert r.level == RiskLevel.MEDIUM
    assert r.file_count == 3


def test_high_risk_low_confidence() -> None:
    r = assess_risk(diag(confidence=0.5), ["src/utils/helpers.py"])
    assert r.level == RiskLevel.HIGH


def test_high_risk_many_files() -> None:
    paths = [f"src/m{i}.py" for i in range(7)]
    r = assess_risk(diag(confidence=0.95), paths)
    assert r.level == RiskLevel.HIGH
    assert r.file_count == 7


# ─────────────────── Sensitive paths always escalate to HIGH ──────────────────


@pytest.mark.parametrize(
    "path",
    [
        "alembic/versions/0001_initial.py",
        "src/migrations/0002_add_idx.py",
        "app/auth/permissions.py",
        "app/security/csrf.py",
        "app/middleware/cors.py",
        "app/payment/stripe.py",
        "app/config.py",
        "app/settings.py",
        "app/main.py",
    ],
)
def test_sensitive_path_is_always_high_risk(path: str) -> None:
    r = assess_risk(diag(confidence=0.99), [path])
    assert r.level == RiskLevel.HIGH
    assert r.sensitive_path == path
    assert any("sensitive path" in reason for reason in r.reasons)


def test_mixed_paths_with_one_sensitive_escalate() -> None:
    r = assess_risk(diag(confidence=0.99), ["src/foo.py", "app/auth/login.py"])
    assert r.level == RiskLevel.HIGH


# ────────────────────────────────── Labels ────────────────────────────────────


def test_labels_include_agent_fix_and_error_type() -> None:
    risk = assess_risk(diag("test_failure", confidence=0.95), ["a.py"])
    labels = labels_for(diag("test_failure", confidence=0.95), risk)
    assert "agent-fix" in labels
    assert "error-type:test_failure" in labels


def test_labels_include_risk_level() -> None:
    risk = assess_risk(diag(confidence=0.5), ["a.py"])
    labels = labels_for(diag(confidence=0.5), risk)
    assert "risk:high" in labels


def test_labels_confidence_buckets() -> None:
    high = assess_risk(diag(confidence=0.95), ["a.py"])
    med = assess_risk(diag(confidence=0.8), ["a.py"])
    low = assess_risk(diag(confidence=0.5), ["a.py"])
    assert "confidence:high" in labels_for(diag(confidence=0.95), high)
    assert "confidence:medium" in labels_for(diag(confidence=0.8), med)
    assert "confidence:low" in labels_for(diag(confidence=0.5), low)


# ──────────────────────────── Markdown formatting ─────────────────────────────


def test_format_risk_section_contains_level_and_files() -> None:
    risk = assess_risk(diag(confidence=0.95), ["a.py"])
    md = format_risk_section(risk)
    assert "Risk:" in md
    assert "low" in md
    assert "Files:" in md
    assert "Confidence:" in md


def test_format_risk_section_has_emoji() -> None:
    risk = assess_risk(diag(confidence=0.5), ["a.py"])  # high risk
    assert "🔴" in format_risk_section(risk)
