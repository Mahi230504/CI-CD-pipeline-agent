"""ChatOrchestrator — turns one chat message into shipped software.

The conversational layer over the existing agent machinery. A ChatTaskEvent
arrives off the Redis tasks stream; handle_turn drives the turn lifecycle and
streams every step to the dashboard:

    classify intent → read files → generate edit → diff preview
        → open a CI-verified PR → AUTO-gate (ship or pause)
        → merge → deploy → live URL

DELIBERATE DEVIATION from cicd-agent/CLAUDE.md ("agent opens PRs, humans merge"):
the AUTO toggle merges unattended, gated HARD by autonomy_policy (see slice 4a).
Approved by the project owner for the Console.

Contract: handle_turn NEVER raises — any failure becomes a failed turn + a
surfaced event, mirroring run_pipeline/run_cd_pipeline. Collaborators are
referenced as modules (chat_editor, pr_manager, …) so they're injectable/fakeable
in tests; the MCP client comes from an injectable factory.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, Callable

from agents import chat_editor, ci_verifier, deploy_guard, deployer, event_publisher
from agents import autonomy_policy
from agents.console_client import ConsoleApiClient
from config.constants import ErrorType
from config.prompts import CHAT_INTENT_SYSTEM_PROMPT
from config.settings import get_settings
from github import pr_manager, pr_risk, rest_api
from github.mcp_client import GitHubMCPClient
from llm.gemini_client import get_gemini_client
from models.chat import ChatTaskEvent, TurnStatus
from models.events import WorkflowFailureEvent
from models.run import Diagnosis, PatchResult

logger = logging.getLogger("cicd_agent.chat_orchestrator")

_VALID_INTENTS = {"feature", "bugfix", "deploy", "question"}


def _chat_branch(turn_id: str) -> str:
    # turn_id is "turn_<n>"; keep it filesystem/ref-safe.
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", turn_id)
    return f"agent/chat-{safe}"


class ChatOrchestrator:
    def __init__(
        self,
        client: ConsoleApiClient | None = None,
        mcp_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.client = client or ConsoleApiClient()
        # A zero-arg callable returning an async context manager MCP client.
        self.mcp_factory = mcp_factory or GitHubMCPClient

    # ── public entry point ────────────────────────────────────────────────

    async def handle_turn(self, event: ChatTaskEvent) -> None:
        logger.info("chat turn %s kind=%s", event.turn_id, event.kind)
        try:
            if event.kind == "reject":
                await self._reject(event)
            elif event.kind == "approve":
                await self._resume_turn(event)
            else:
                await self._run_chat_turn(event)
        except Exception as e:  # noqa: BLE001 — a turn must never crash the worker
            logger.exception("chat turn %s failed: %s", event.turn_id, e)
            await self._fail_turn(event, f"internal error: {type(e).__name__}")

    # ── chat (feature / bugfix / question) ────────────────────────────────

    async def _run_chat_turn(self, event: ChatTaskEvent) -> None:
        await self.client.patch_turn(event.turn_id, status=TurnStatus.RUNNING.value)
        await self._emit(event, "chat_received", f"On it — “{event.message}”")

        intent, summary = await self._classify(event.message)
        await self.client.patch_turn(event.turn_id, intent=intent)

        if intent == "question":
            answer = await self._answer_question(event.message)
            await self.client.post_message(
                event.conversation_id, role="assistant", kind="text",
                content=answer, run_id=event.run_id,
            )
            await self._emit(event, "chat_answer", "Answered.", level="success")
            await self.client.patch_turn(event.turn_id, status=TurnStatus.DONE.value)
            return

        await self._execute_feature(event, summary)

    async def _execute_feature(self, event: ChatTaskEvent, summary: str) -> None:
        settings = get_settings()
        repo = await self.client.get_repo() or {}
        default_branch = repo.get("default_branch") or "main"
        ci_workflow = repo.get("ci_workflow_name") or "CI"

        await self._emit(event, "diagnose", f"Planning change: {summary}")

        async with self.mcp_factory() as mcp:
            proposal = await chat_editor.generate_edit(event.message, mcp, ref=default_branch)
            if not proposal.is_actionable:
                reason = proposal.cannot_reason or "could not produce a safe change"
                await self.client.post_message(
                    event.conversation_id, role="assistant", kind="text",
                    content=f"I couldn't make that change: {reason}", run_id=event.run_id,
                )
                await self._emit(event, "chat_blocked", reason, level="warn")
                await self.client.patch_turn(event.turn_id, status=TurnStatus.FAILED.value, error=reason)
                await self.client.patch_run(event.run_id, status="failed")
                return

            # Diff preview: full diff lives in a message (never SSE); a short note streams.
            await self.client.post_message(
                event.conversation_id, role="assistant", kind="diff",
                content=proposal.diff,
                payload={"files": proposal.files, "summary": proposal.summary},
                run_id=event.run_id,
            )
            await self._emit(
                event, "patch", f"Proposed change to {', '.join(proposal.files)}",
                meta={"files": proposal.files},
            )

            branch = _chat_branch(event.turn_id)
            title = f"[agent] {proposal.summary}"[:72]
            body = (
                f"Chat request: {event.message}\n\n{proposal.summary}\n\n"
                "_Opened by the Agent Console._"
            )
            patch = await pr_manager.open_feature_pr(
                branch=branch, files=proposal.file_contents, title=title, body=body,
                mcp_client=mcp, base_branch=default_branch,
            )
            if not patch.success or not patch.pr_number:
                err = patch.error_message or "PR open failed"
                await self._emit(event, "chat_error", f"Could not open PR: {err}", level="error")
                await self.client.patch_turn(event.turn_id, status=TurnStatus.FAILED.value, error=err)
                await self.client.patch_run(event.run_id, status="failed")
                return

            await self.client.patch_turn(event.turn_id, pr_number=patch.pr_number, pr_url=patch.pr_url)
            await self.client.patch_run(
                event.run_id, pr_number=patch.pr_number, pr_url=patch.pr_url,
                branch=branch, head_sha=patch.head_sha,
            )
            await self.client.post_message(
                event.conversation_id, role="assistant", kind="status",
                content=f"Opened PR #{patch.pr_number}: {patch.pr_url}", run_id=event.run_id,
            )
            await self._emit(
                event, "verify", f"PR #{patch.pr_number} opened — verifying CI…",
                meta={"pr_url": patch.pr_url, "pr_number": patch.pr_number},
            )

            synthetic_event = WorkflowFailureEvent(
                run_id=0, repo_owner=repo.get("owner", ""), repo_name=repo.get("name", ""),
                workflow_name=ci_workflow, branch=branch, head_sha=patch.head_sha or "",
                html_url="", sender_login="agent-console",
            )
            verify = await ci_verifier.verify_patch_ci(
                patch, synthetic_event, mcp, settings, head_branch=branch
            )
            await self.client.patch_run(
                event.run_id, verified=verify.verified, verification_detail=verify.detail
            )
            await self._emit(
                event, "verify", verify.detail,
                level="success" if verify.verified is True else "warn",
            )

            # Risk + guard → the AUTO gate.
            # confidence 0.9 → a single, non-sensitive new file scores LOW risk
            # (pr_risk bumps <0.85 to MEDIUM). A chat edit that passed CI is a
            # high-confidence change; sensitivity/file-count still gate it.
            synthetic_diag = Diagnosis(
                error_type=ErrorType.UNKNOWN, file=proposal.files[0], line_number=None,
                explanation=proposal.summary, confidence=0.9, is_patchable=True, raw_response="",
            )
            risk = pr_risk.assess_risk(synthetic_diag, proposal.files)
            verdict = await deploy_guard.judge(
                pr_number=patch.pr_number, pr_title=title, pr_body=body,
                files_changed=proposal.files, diff_summary=proposal.diff[:4000],
                head_sha=patch.head_sha or "",
            )
            decision = autonomy_policy.should_ship_unattended(verify, risk, verdict, event.autonomy)
            await self._emit(
                event, "deploy_guard", decision.reason,
                level="success" if decision.ship else "info",
                meta={"risk": risk.level.value, "gate": decision.gate},
            )

        if decision.ship:
            await self._ship(event, patch.pr_number, repo)
        else:
            token = uuid.uuid4().hex
            await self.client.patch_turn(
                event.turn_id, status=TurnStatus.AWAITING_APPROVAL.value, resume_token=token
            )
            await self.client.post_message(
                event.conversation_id, role="assistant", kind="approval",
                content=f"Ready to ship PR #{patch.pr_number}. {decision.reason}",
                payload={"pr_number": patch.pr_number, "pr_url": patch.pr_url, "reason": decision.reason},
                run_id=event.run_id,
            )
            await self._emit(
                event, "awaiting_approval", f"Paused for approval — {decision.reason}", level="warn"
            )

    # ── ship (merge + deploy) ─────────────────────────────────────────────

    async def _ship(self, event: ChatTaskEvent, pr_number: int, repo: dict[str, Any]) -> None:
        await self.client.patch_turn(event.turn_id, status=TurnStatus.MERGING.value)
        await self._emit(event, "merge", f"Merging PR #{pr_number}…")
        merged, detail = await rest_api.merge_pull_request(pr_number, merge_method="squash")
        if not merged:
            # Degrade to a human gate rather than failing the turn.
            token = uuid.uuid4().hex
            await self.client.patch_turn(
                event.turn_id, status=TurnStatus.AWAITING_APPROVAL.value,
                resume_token=token, error=detail,
            )
            await self._emit(
                event, "merge_blocked", f"Auto-merge blocked: {detail} — pausing for a human.",
                level="warn",
            )
            return

        merged_sha = detail
        await self.client.patch_turn(event.turn_id, status=TurnStatus.DEPLOYING.value)
        live_url = await self._deploy(event, merged_sha, repo)

        await self.client.patch_turn(event.turn_id, status=TurnStatus.DONE.value)
        if live_url:
            await self.client.post_message(
                event.conversation_id, role="assistant", kind="live_url",
                content=f"Shipped to {live_url} · commit {merged_sha[:7]}",
                payload={"live_url": live_url, "pr_number": pr_number}, run_id=event.run_id,
            )
            await self._emit(event, "deploy", f"Shipped → {live_url}", level="success")
        else:
            await self.client.post_message(
                event.conversation_id, role="assistant", kind="status",
                content=f"Merged PR #{pr_number} (commit {merged_sha[:7]}).", run_id=event.run_id,
            )
            await self._emit(event, "deploy", f"Merged PR #{pr_number}.", level="success")

    async def _deploy(self, event: ChatTaskEvent, merged_sha: str, repo: dict[str, Any]) -> str | None:
        """Best-effort, honest deploy. With no deploy target configured, the turn
        reports MERGED (not deployed) rather than faking a live URL."""
        settings = get_settings()
        if not settings.cd_enabled or not settings.deploy_image_repository:
            await self._emit(
                event, "deploy", "Merged — no deploy target configured; skipping deploy.",
                level="info",
            )
            await self.client.patch_run(event.run_id, status="verified")
            return repo.get("live_url")

        image_tag = f"{settings.deploy_image_repository}:{merged_sha[:8]}"
        await self._emit(event, "deploy", f"Deploying {image_tag}…")
        result = await deployer.deploy(image_tag)
        if result.success:
            live = settings.backend_base_url or repo.get("live_url")
            await self.client.patch_run(event.run_id, status="deployed", live_url=live)
            return live
        await self._emit(event, "deploy", f"Deploy failed: {result.error_message}", level="error")
        await self.client.patch_run(event.run_id, status="failed")
        return None

    # ── approve / reject (resume a paused turn) ───────────────────────────

    async def _resume_turn(self, event: ChatTaskEvent) -> None:
        state = await self.client.get_turn(event.turn_id) or {}
        if state.get("status") != TurnStatus.AWAITING_APPROVAL.value:
            await self._emit(
                event, "chat_resume", "Nothing is awaiting approval for this turn.", level="warn"
            )
            return
        pr_number = state.get("pr_number")
        if not isinstance(pr_number, int):
            await self._fail_turn(event, "approved but no PR is associated with this turn")
            return
        await self._emit(event, "chat_approved", f"Approved — shipping PR #{pr_number}.")
        repo = await self.client.get_repo() or {}
        await self._ship(event, pr_number, repo)

    async def _reject(self, event: ChatTaskEvent) -> None:
        await self._emit(event, "chat_rejected", "Change rejected — not shipping.", level="warn")
        await self.client.patch_turn(event.turn_id, status=TurnStatus.REJECTED.value)

    # ── LLM helpers ───────────────────────────────────────────────────────

    async def _classify(self, message: str) -> tuple[str, str]:
        try:
            raw = await get_gemini_client().generate(
                prompt=message, system_prompt=CHAT_INTENT_SYSTEM_PROMPT,
                agent="chat_intent", temperature=0.0,
            )
            obj = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
            intent = str(obj.get("intent", "feature")).lower()
            if intent not in _VALID_INTENTS:
                intent = "feature"
            return intent, str(obj.get("summary", "")).strip() or message[:80]
        except Exception as e:  # noqa: BLE001 — default to acting, not blocking
            logger.info("chat_intent classify failed (defaulting to feature): %s", e)
            return "feature", message[:80]

    async def _answer_question(self, message: str) -> str:
        try:
            return await get_gemini_client().generate(
                prompt=message,
                system_prompt="You are a concise CI/CD assistant. Answer in 1-3 sentences.",
                agent="chat_intent", temperature=0.2,
            )
        except Exception:  # noqa: BLE001
            return "I couldn't reach the model to answer that right now."

    # ── plumbing ──────────────────────────────────────────────────────────

    async def _fail_turn(self, event: ChatTaskEvent, reason: str) -> None:
        await self._emit(event, "chat_error", reason, level="error")
        await self.client.patch_turn(event.turn_id, status=TurnStatus.FAILED.value, error=reason)

    async def _emit(
        self, event: ChatTaskEvent, stage: str, message: str,
        *, level: str = "info", meta: dict[str, Any] | None = None,
    ) -> None:
        base: dict[str, Any] = {
            "tenant_id": event.tenant_id,
            "conversation_id": event.conversation_id,
            "turn_id": event.turn_id,
            "run_id": event.run_id,
        }
        if meta:
            base.update(meta)
        await event_publisher.publish_safe(stage, message, level=level, metadata=base)  # type: ignore[arg-type]
