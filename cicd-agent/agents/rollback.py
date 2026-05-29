"""
Rollback — re-deploys the previous image tag captured by the deployer.

This module exists as a separate file (rather than a method on deployer)
because the orchestrator audits it as a distinct PipelineStep and the
notifier formats it differently. The actual work is just delegating back
to deployer.deploy with the prior tag.

Contract:
- Returns a DeployResult identical in shape to a forward deploy, so the
  pipeline can log/notify with one branch.
- Refuses to roll back when prev_tag is empty: there's nothing to revert
  to (first-ever deploy, or .env hand-edited). Refusing fast prevents the
  orchestrator from getting into a tight retry loop.
- Never raises. Errors from the underlying deploy surface in DeployResult.
"""

from __future__ import annotations

import logging

from agents.deployer import deploy
from models.cd import DeployResult

logger = logging.getLogger("cicd_agent.rollback")


async def rollback_to(prev_tag: str) -> DeployResult:
    """Re-apply `prev_tag` to the target host.

    Use the `prev_tag` field on the DeployResult from a failed forward
    deploy — that's the value the deployer captured before flipping the
    .env line. Passing in a tag picked by other means is supported (the
    orchestrator may also pass a known-good tag from history) but the
    caller must ensure it's a valid `<repo>:<tag>` reference.
    """
    if not prev_tag:
        logger.warning("rollback: no prev_tag captured — refusing to roll back")
        return DeployResult(
            success=False,
            image_tag="",
            prev_tag="",
            error_message="no prev_tag available — cannot roll back",
        )

    logger.warning("rollback: re-deploying prev_tag=%s", prev_tag)
    # Delegate. The returned DeployResult's `.prev_tag` will be whatever the
    # host had right before this re-deploy (typically the failed tag we just
    # tried to ship); that's correct — a subsequent rollback would target
    # that. For audit clarity the orchestrator should still pass `.image_tag`
    # from this result, not from the original deploy, when reporting.
    return await deploy(prev_tag)
