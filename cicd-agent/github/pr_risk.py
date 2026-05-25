"""
Risk scoring + auto-labels for agent-opened PRs.

The risk score is a heuristic over the diagnosis and the patch set; reviewers
look at it first to decide how carefully to read the diff. It is not a
correctness signal — high-confidence fixes can still touch dangerous code.

Three buckets:
- LOW    : single-file, high-confidence diagnosis, non-critical path.
- MEDIUM : up to a handful of files, moderate confidence, no critical paths.
- HIGH   : low confidence, touches many files, or touches sensitive paths
           (migrations, auth, payments, security, config loaders, etc.).
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from models.run import Diagnosis


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# Path patterns that bump risk to HIGH regardless of other factors. Tuned for
# the common shapes of "things you really want a human to look at".
_SENSITIVE_PATH_PATTERNS: tuple[str, ...] = (
    # Match both root-level and nested layouts for migration directories.
    "migrations/*",
    "*/migrations/*",
    "alembic/versions/*",
    "*/alembic/versions/*",
    "*auth*",
    "*authn*",
    "*authz*",
    "*permission*",
    "*payment*",
    "*billing*",
    "*security*",
    "*crypto*",
    "*cors*",
    "*config.py",
    "*settings.py",
    "*/middleware/*",
    "*main.py",
    "*entry*",
)


def _touches_sensitive_path(paths: list[str]) -> str | None:
    """Return the first sensitive path matched, or None."""
    for path in paths:
        for pattern in _SENSITIVE_PATH_PATTERNS:
            if fnmatch.fnmatch(path, pattern):
                return path
    return None


@dataclass(frozen=True)
class RiskAssessment:
    level: RiskLevel
    confidence: float
    file_count: int
    sensitive_path: str | None
    reasons: tuple[str, ...]

    @property
    def emoji(self) -> str:
        return {RiskLevel.LOW: "🟢", RiskLevel.MEDIUM: "🟡", RiskLevel.HIGH: "🔴"}[self.level]


def assess_risk(diagnosis: "Diagnosis", paths: list[str]) -> RiskAssessment:
    confidence = float(diagnosis.confidence)
    file_count = len(paths)
    sensitive = _touches_sensitive_path(paths)
    reasons: list[str] = []

    level = RiskLevel.LOW

    if sensitive is not None:
        level = RiskLevel.HIGH
        reasons.append(f"touches sensitive path `{sensitive}`")
    if confidence < 0.7:
        level = RiskLevel.HIGH
        reasons.append(f"low diagnosis confidence ({confidence:.2f})")
    if file_count > 5:
        level = RiskLevel.HIGH
        reasons.append(f"spans {file_count} files")

    if level == RiskLevel.LOW:
        if confidence < 0.85:
            level = RiskLevel.MEDIUM
            reasons.append(f"moderate confidence ({confidence:.2f})")
        elif file_count > 1:
            level = RiskLevel.MEDIUM
            reasons.append(f"touches {file_count} files")
        else:
            reasons.append("single-file, high-confidence fix")

    return RiskAssessment(
        level=level,
        confidence=confidence,
        file_count=file_count,
        sensitive_path=sensitive,
        reasons=tuple(reasons),
    )


def labels_for(diagnosis: "Diagnosis", risk: RiskAssessment) -> list[str]:
    """Labels the agent applies to every PR it opens."""
    labels = [
        "agent-fix",
        f"error-type:{diagnosis.error_type}",
        f"risk:{risk.level}",
    ]
    # confidence bucketed to avoid label explosion.
    if risk.confidence >= 0.9:
        labels.append("confidence:high")
    elif risk.confidence >= 0.7:
        labels.append("confidence:medium")
    else:
        labels.append("confidence:low")
    return labels


def format_risk_section(risk: RiskAssessment) -> str:
    """Markdown block to embed in the PR body."""
    bullets = "\n".join(f"- {r}" for r in risk.reasons) if risk.reasons else "- (no notes)"
    return (
        f"### {risk.emoji} Risk: `{risk.level}`\n"
        f"**Confidence:** {risk.confidence:.2f} · **Files:** {risk.file_count}\n\n"
        f"{bullets}"
    )
