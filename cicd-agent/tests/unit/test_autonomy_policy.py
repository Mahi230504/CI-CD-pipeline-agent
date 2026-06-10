"""Slice-4 tests: the AUTO-toggle decision truth table (§1.6).

Pure function, so we feed lightweight stand-ins for VerifyResult / RiskAssessment
/ DeployVerdict (only the read attributes matter). RiskLevel is the real enum
because should_ship_unattended compares against it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agents.autonomy_policy import should_ship_unattended
from github.pr_risk import RiskLevel


def _verify(verified):
    return SimpleNamespace(verified=verified, detail="ci detail", failing_log=None)


def _risk(level):
    return SimpleNamespace(level=level)


def _verdict(approve=True, confidence=0.9):
    return SimpleNamespace(
        approve=approve, confidence=confidence, is_high_confidence=confidence >= 0.6, reason="r"
    )


def test_ships_when_all_green_and_auto() -> None:
    d = should_ship_unattended(_verify(True), _risk(RiskLevel.LOW), _verdict(), "auto")
    assert d.ship is True and d.gate == "auto"


@pytest.mark.parametrize(
    ("autonomy", "verified", "level", "approve", "conf"),
    [
        ("manual", True, RiskLevel.LOW, True, 0.9),   # manual never ships
        ("auto", False, RiskLevel.LOW, True, 0.9),    # CI red
        ("auto", None, RiskLevel.LOW, True, 0.9),     # CI unknown
        ("auto", True, RiskLevel.MEDIUM, True, 0.9),  # risk not low
        ("auto", True, RiskLevel.HIGH, True, 0.9),    # risk high
        ("auto", True, RiskLevel.LOW, False, 0.9),    # guard blocked
        ("auto", True, RiskLevel.LOW, True, 0.4),     # low confidence
    ],
)
def test_pauses_otherwise(autonomy, verified, level, approve, conf) -> None:
    d = should_ship_unattended(_verify(verified), _risk(level), _verdict(approve, conf), autonomy)
    assert d.ship is False and d.gate == "manual"
    assert d.reason  # always carries a human-readable reason


def test_reason_mentions_the_failing_gate() -> None:
    assert "MANUAL" in should_ship_unattended(_verify(True), _risk(RiskLevel.LOW), _verdict(), "manual").reason
    assert "CI not green" in should_ship_unattended(_verify(False), _risk(RiskLevel.LOW), _verdict(), "auto").reason
    assert "Risk is high" in should_ship_unattended(_verify(True), _risk(RiskLevel.HIGH), _verdict(), "auto").reason
