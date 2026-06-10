"""ConsoleApiClient — the agent's HTTP client for the demo backend's
/internal/console endpoints.

The agent worker never opens the console DB directly (clean process boundary):
it reads repo context and writes turn/run/message state through here. Auth is the
same X-Agent-Token shared secret used by event_publisher.

Resilience mirrors event_publisher: bounded timeout, never raises (returns a
bool / None), and a no-op when the backend URL or token is unconfigured — so
unit tests and dev runs without a live backend still work. Unlike events,
state writes are important, so failures are logged at WARNING (not dropped
silently), letting an operator see when a turn's state didn't persist.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from config.settings import get_settings

logger = logging.getLogger("cicd_agent.console_client")

_TIMEOUT_SECONDS = 5.0


class ConsoleApiClient:
    def __init__(self, base: str | None = None, token: str | None = None) -> None:
        settings = get_settings()
        self._base = (base if base is not None else settings.console_internal_base).rstrip("/")
        self._token = token if token is not None else settings.agent_shared_secret

    @property
    def enabled(self) -> bool:
        return bool(self._base and self._token)

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-Agent-Token": self._token, "Content-Type": "application/json"}

    async def _request(
        self, method: str, path: str, *, json: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        if not self.enabled:
            logger.debug("console_client disabled (no base/token) — skipping %s %s", method, path)
            return None
        url = f"{self._base}{path}"
        try:
            timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(method, url, headers=self._headers, json=json) as resp:
                    if resp.status // 100 == 2:
                        try:
                            return await resp.json()
                        except Exception:
                            return {}
                    detail = (await resp.text())[:200]
                    logger.warning("console_client %s %s → %d: %s", method, path, resp.status, detail)
                    return None
        except Exception as e:  # never raise into the orchestrator
            logger.warning("console_client %s %s failed: %s", method, path, e)
            return None

    async def get_repo(self) -> dict[str, Any] | None:
        """Repo context: owner, name, default_branch, ci/release workflow names,
        deploy_config, autonomy_mode, live_url."""
        return await self._request("GET", "/repo")

    async def patch_turn(self, turn_id: str, **fields: Any) -> bool:
        body = {k: v for k, v in fields.items() if v is not None}
        return await self._request("PATCH", f"/turns/{turn_id}", json=body) is not None

    async def patch_run(self, run_id: str, **fields: Any) -> bool:
        body = {k: v for k, v in fields.items() if v is not None}
        return await self._request("PATCH", f"/runs/{run_id}", json=body) is not None

    async def post_message(
        self,
        conversation_id: str,
        *,
        role: str = "assistant",
        kind: str = "text",
        content: str = "",
        payload: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> bool:
        body: dict[str, Any] = {"role": role, "kind": kind, "content": content}
        if payload is not None:
            body["payload"] = payload
        if run_id is not None:
            body["run_id"] = run_id
        return await self._request(
            "POST", f"/conversations/{conversation_id}/messages", json=body
        ) is not None
