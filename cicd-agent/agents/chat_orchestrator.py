"""ChatOrchestrator — turns one chat message into shipped software.

This is the conversational layer over the existing agent machinery. A
ChatTaskEvent arrives off the Redis tasks stream; handle_turn drives the turn
lifecycle and streams every step to the dashboard:

    classify intent → read files → generate edit → diff preview
        → open a CI-verified PR → AUTO-gate (ship or pause)
        → merge → deploy → live URL

Slice 3 lands the lifecycle skeleton + all the state/streaming plumbing (turn
state machine via ConsoleApiClient, event emission with correlation metadata,
never-raises error handling). Slice 4 fills `_execute_feature` with the real
edit→PR→verify→ship pipeline. The seam is deliberate so each slice is runnable.

Contract: handle_turn NEVER raises — any failure is converted into a failed
turn + a surfaced event, mirroring run_pipeline/run_cd_pipeline.
"""

from __future__ import annotations

import logging
from typing import Any

from agents import event_publisher
from agents.console_client import ConsoleApiClient
from models.chat import ChatTaskEvent, TurnStatus

logger = logging.getLogger("cicd_agent.chat_orchestrator")


class ChatOrchestrator:
    def __init__(self, client: ConsoleApiClient | None = None) -> None:
        self.client = client or ConsoleApiClient()

    # ── public entry point ────────────────────────────────────────────────

    async def handle_turn(self, event: ChatTaskEvent) -> None:
        """Route a chat turn by kind. Never raises."""
        logger.info("chat turn %s kind=%s", event.turn_id, event.kind)
        try:
            if event.kind in ("approve", "reject"):
                await self._resume_turn(event)
            else:
                await self._run_chat_turn(event)
        except Exception as e:  # noqa: BLE001 — a turn must never crash the worker
            logger.exception("chat turn %s failed: %s", event.turn_id, e)
            await self._fail_turn(event, f"internal error: {type(e).__name__}")

    # ── chat (feature/bugfix) ─────────────────────────────────────────────

    async def _run_chat_turn(self, event: ChatTaskEvent) -> None:
        await self.client.patch_turn(event.turn_id, status=TurnStatus.RUNNING.value)
        await self._emit(event, "chat_received", f"On it — “{event.message}”")
        await self._execute_feature(event)

    async def _execute_feature(self, event: ChatTaskEvent) -> None:
        """SLICE 3 STUB — replaced in slice 4 by the real
        classify→edit→PR→verify→ship pipeline. For now it acknowledges the turn
        end-to-end so the plumbing (stream → run_events → SSE, turn state, and
        the assistant message callback) is verifiable on its own."""
        await self._emit(
            event,
            "chat_pending",
            "Feature pipeline lands in slice 4 — turn plumbing verified.",
            level="warn",
        )
        await self.client.post_message(
            event.conversation_id,
            role="assistant",
            kind="status",
            content="Received. (Feature execution arrives in the next slice.)",
            run_id=event.run_id,
        )
        await self.client.patch_turn(event.turn_id, status=TurnStatus.DONE.value)

    # ── approve / reject (resume a paused turn) ───────────────────────────

    async def _resume_turn(self, event: ChatTaskEvent) -> None:
        if event.kind == "reject":
            await self._emit(event, "chat_rejected", "Change rejected — not shipping.", level="warn")
            await self.client.patch_turn(event.turn_id, status=TurnStatus.REJECTED.value)
            return
        # approve → slice 4 performs merge + deploy here.
        await self._emit(event, "chat_approved", "Approved — merge/deploy lands in slice 4.")
        await self.client.patch_turn(event.turn_id, status=TurnStatus.DONE.value)

    # ── helpers ───────────────────────────────────────────────────────────

    async def _fail_turn(self, event: ChatTaskEvent, reason: str) -> None:
        await self._emit(event, "chat_error", reason, level="error")
        await self.client.patch_turn(event.turn_id, status=TurnStatus.FAILED.value, error=reason)

    async def _emit(
        self,
        event: ChatTaskEvent,
        stage: str,
        message: str,
        *,
        level: str = "info",
        meta: dict[str, Any] | None = None,
    ) -> None:
        """Stream one reasoning step. Correlation keys (tenant/conversation/run)
        ride in metadata so the demo backend persists it to run_events and the
        per-conversation SSE filter routes it to the right client."""
        base: dict[str, Any] = {
            "tenant_id": event.tenant_id,
            "conversation_id": event.conversation_id,
            "turn_id": event.turn_id,
            "run_id": event.run_id,
        }
        if meta:
            base.update(meta)
        await event_publisher.publish_safe(stage, message, level=level, metadata=base)  # type: ignore[arg-type]
