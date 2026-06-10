"""The AUTO-toggle decision — the single source of truth for whether a chat
turn ships unattended or pauses for a human.

Fail-closed by construction: a degraded LLM or ambiguous diff makes deploy_guard
block and pr_risk escalate to HIGH, so the run pauses rather than ships. Pure
function (no I/O) so the policy is exhaustively unit-testable as a truth table.

See AGENT_CONSOLE_ARCHITECTURE.md §1.6.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from github.pr_risk import RiskLevel

if TYPE_CHECKING:  # structural — avoids importing heavy modules at runtime
    from agents.ci_verifier import VerifyResult
    from github.pr_risk import RiskAssessment
    from models.cd import DeployVerdict


@dataclass(frozen=True)
class ShipDecision:
    ship: bool
    reason: str
    gate: Literal["auto", "manual"]


def should_ship_unattended(
    verify: "VerifyResult",
    risk: "RiskAssessment",
    verdict: "DeployVerdict",
    autonomy: str,
) -> ShipDecision:
    """Ship unattended only when ALL hold: autonomy is AUTO, CI verified green,
    risk is LOW, deploy_guard approved, and the guard is high-confidence.
    Otherwise pause for human approval."""
    if autonomy != "auto":
        return ShipDecision(False, "Autonomy is MANUAL — pausing for approval.", "manual")
    if verify.verified is not True:
        return ShipDecision(False, f"CI not green ({verify.detail}) — needs human review.", "manual")
    if risk.level != RiskLevel.LOW:
        return ShipDecision(False, f"Risk is {risk.level.value} — needs human review.", "manual")
    if not verdict.approve:
        return ShipDecision(False, f"deploy_guard blocked: {verdict.reason}", "manual")
    if not verdict.is_high_confidence:
        return ShipDecision(False, f"deploy_guard low confidence ({verdict.confidence:.2f}).", "manual")
    return ShipDecision(True, "Verified-green + low-risk + guard-approved — shipping unattended.", "auto")
