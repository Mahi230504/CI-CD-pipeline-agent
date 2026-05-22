"""
GitHub MCP client — manages the MCP ClientSession lifecycle.

Connects to the GitHub MCP server at https://api.githubcopilot.com/mcp
using the GITHUB_PERSONAL_ACCESS_TOKEN from settings.

Exposes async context manager: async with GitHubMCPClient() as client
Inside the context, all tool call methods are available.

Methods (all async):
- get_workflow_run(run_id) → dict
- list_jobs(run_id) → list[dict]
- get_job_logs(job_id) → str
- get_file_contents(path, ref) → str
- list_workflow_files() → list[str]
- get_workflow_yaml(filename) → str
- create_branch(branch_name, sha) → bool
- push_file(path, content, branch, message, sha) → bool
- create_pull_request(title, body, head, base) → dict
- create_issue_comment(issue_number, body) → bool
- list_workflow_runs(workflow_id, per_page) → list[dict]
- get_repo_default_branch() → str

Session is created once per pipeline run, reused across all agent calls.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from config.settings import get_settings

logger = logging.getLogger("cicd_agent.mcp")


class GitHubMCPError(Exception):
    def __init__(
        self,
        message: str,
        tool_name: str = "",
        original: Exception | None = None,
    ):
        super().__init__(message)
        self.tool_name = tool_name
        self.original = original


class GitHubMCPClient:
    def __init__(self) -> None:
        self._session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None
        self._repo_owner: str = ""
        self._repo_name: str = ""

    async def __aenter__(self) -> "GitHubMCPClient":
        settings = get_settings()
        self._repo_owner = settings.github_repo_owner
        self._repo_name = settings.github_repo_name

        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        try:
            transport = await self._stack.enter_async_context(
                streamablehttp_client(
                    settings.github_mcp_url,
                    headers=settings.github_mcp_headers,
                )
            )
            read_stream, write_stream, _ = transport
            self._session = await self._stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await self._session.initialize()
            logger.info("GitHub MCP session initialised for %s/%s", self._repo_owner, self._repo_name)
        except Exception:
            await self._stack.__aexit__(None, None, None)
            self._stack = None
            self._session = None
            raise
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._stack is not None:
            try:
                await self._stack.__aexit__(exc_type, exc, tb)
            except Exception as e:
                logger.warning("error closing GitHub MCP session: %s", e)
        self._stack = None
        self._session = None

    @property
    def session(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError("GitHubMCPClient session not active — use `async with`")
        return self._session

    async def _call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        # Log the call (arg keys only — values can contain large file contents / secrets).
        logger.info("mcp call: %s args=%s", tool_name, sorted(arguments.keys()))
        try:
            result = await self.session.call_tool(tool_name, arguments)
        except GitHubMCPError:
            raise
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            # Don't swallow user/runtime signals or task cancellation.
            raise
        except BaseException as e:
            # IMPORTANT: anyio task-group errors can come back as BaseExceptionGroup
            # (Python 3.11+), which is NOT an Exception. The previous handler caught
            # only Exception, causing the entire process to die mid-pipeline when a
            # PR-creation call hit one of those. Catch BaseException, log everything
            # we can, and surface a normal exception to callers.
            err_type = type(e).__name__
            try:
                inner = list(getattr(e, "exceptions", []) or [])
            except Exception:
                inner = []
            if inner:
                inner_summary = "; ".join(f"{type(x).__name__}: {x}" for x in inner)
                logger.error(
                    "mcp tool %s raised %s (group of %d): %s",
                    tool_name,
                    err_type,
                    len(inner),
                    inner_summary,
                )
            else:
                logger.error("mcp tool %s raised %s: %s", tool_name, err_type, e)
            original = e if isinstance(e, Exception) else None
            raise GitHubMCPError(
                f"{err_type}: {e}",
                tool_name=tool_name,
                original=original,
            ) from e

        if getattr(result, "isError", False):
            # Surface the textual content the MCP server returned, so logs are useful.
            err_text = ""
            for item in getattr(result, "content", []) or []:
                txt = getattr(item, "text", None)
                if isinstance(txt, str):
                    err_text += txt + "\n"
            logger.error("mcp tool %s returned error result: %s", tool_name, err_text.strip()[:500])
            raise GitHubMCPError(
                f"MCP tool {tool_name} returned error: {err_text.strip()[:200]}",
                tool_name=tool_name,
            )
        return result

    @staticmethod
    def _extract_content(result: Any) -> Any:
        content = getattr(result, "content", None)
        if not content:
            return None
        if not isinstance(content, list) or not content:
            return content
        first = content[0]
        text = getattr(first, "text", None)
        if text is None:
            return first
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return text

    async def get_workflow_run(self, run_id: int) -> dict:
        result = await self._call_tool(
            "actions_get",
            {
                "method": "get_workflow_run",
                "owner": self._repo_owner,
                "repo": self._repo_name,
                "resource_id": str(run_id),
            },
        )
        data = self._extract_content(result)
        return data if isinstance(data, dict) else {}

    async def list_jobs(self, run_id: int) -> list[dict]:
        result = await self._call_tool(
            "actions_list",
            {
                "method": "list_workflow_jobs",
                "owner": self._repo_owner,
                "repo": self._repo_name,
                "resource_id": str(run_id),
            },
        )
        data = self._extract_content(result)
        # Response shape: {"jobs": {"total_count": N, "jobs": [...]}}  (nested)
        # or {"jobs": [...]} (flat) — handle both.
        if isinstance(data, dict):
            jobs = data.get("jobs")
            if isinstance(jobs, dict):
                inner = jobs.get("jobs")
                if isinstance(inner, list):
                    return inner
            if isinstance(jobs, list):
                return jobs
        if isinstance(data, list):
            return data
        return []

    async def get_job_logs(self, job_id: int) -> str:
        result = await self._call_tool(
            "get_job_logs",
            {
                "owner": self._repo_owner,
                "repo": self._repo_name,
                "job_id": job_id,
                "return_content": True,
            },
        )
        data = self._extract_content(result)
        if isinstance(data, dict):
            text = (
                data.get("logs_content")
                or data.get("text")
                or data.get("content")
                or data.get("logs")
                or ""
            )
        else:
            text = data if isinstance(data, str) else ""
        logger.info("Fetched logs for job %s, size=%d chars", job_id, len(text))
        return text

    async def get_file_contents(self, path: str, ref: str = "main") -> str:
        result = await self._call_tool(
            "get_file_contents",
            {
                "owner": self._repo_owner,
                "repo": self._repo_name,
                "path": path,
                "ref": ref,
            },
        )
        # GitHub MCP returns multiple content items for files:
        #   [0] TextContent: status message "successfully downloaded text file (SHA: ...)"
        #   [1] EmbeddedResource: .resource.text holds the file body
        for item in getattr(result, "content", []) or []:
            resource = getattr(item, "resource", None)
            if resource is not None:
                text = getattr(resource, "text", None)
                if isinstance(text, str):
                    return text
        # Fallback to old code path (TextContent-only response, e.g. directory listings)
        data = self._extract_content(result)
        if isinstance(data, dict):
            content = data.get("content", "")
            if data.get("encoding") == "base64" and isinstance(content, str):
                try:
                    return base64.b64decode(content).decode("utf-8", errors="replace")
                except Exception as e:
                    logger.warning("base64 decode failed for %s: %s", path, e)
                    return ""
            if isinstance(content, str):
                return content
        if isinstance(data, str):
            return data
        return ""

    @staticmethod
    def _extract_sha_from_status(result: Any) -> str | None:
        import re
        for item in getattr(result, "content", []) or []:
            text = getattr(item, "text", None)
            if isinstance(text, str):
                m = re.search(r"SHA:\s*([0-9a-f]{40})", text)
                if m:
                    return m.group(1)
        return None

    async def get_file_sha(self, path: str, ref: str = "main") -> str | None:
        result = await self._call_tool(
            "get_file_contents",
            {
                "owner": self._repo_owner,
                "repo": self._repo_name,
                "path": path,
                "ref": ref,
            },
        )
        return self._extract_sha_from_status(result)

    async def list_workflow_files(self) -> list[str]:
        result = await self._call_tool(
            "get_file_contents",
            {
                "owner": self._repo_owner,
                "repo": self._repo_name,
                "path": ".github/workflows",
            },
        )

        data = self._extract_content(result)
        entries: list[Any] = []
        if isinstance(data, list):
            entries = data
        elif isinstance(data, dict):
            for key in ("entries", "contents", "tree", "items"):
                value = data.get(key)
                if isinstance(value, list):
                    entries = value
                    break

        files: list[str] = []
        for item in entries:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("path", "").rsplit("/", 1)[-1]
            if isinstance(name, str) and name.endswith((".yml", ".yaml")):
                files.append(name)
        return files

    async def get_workflow_yaml(self, filename: str) -> str:
        return await self.get_file_contents(f".github/workflows/{filename}")

    async def create_branch(self, branch_name: str, sha: str) -> bool:
        """Create a branch at `sha`. Raises GitHubMCPError on failure."""
        await self._call_tool(
            "create_branch",
            {
                "owner": self._repo_owner,
                "repo": self._repo_name,
                "branch": branch_name,
                "sha": sha,
            },
        )
        logger.info("created branch %s at %s", branch_name, sha[:8])
        return True

    async def ensure_branch(self, branch_name: str, base_sha: str) -> bool:
        """Idempotent: create the branch if missing. Returns True if newly created,
        False if it already existed. Raises on any other failure."""
        try:
            await self.create_branch(branch_name, base_sha)
            return True
        except GitHubMCPError as e:
            msg = str(e).lower()
            if "already exists" in msg or "reference already exists" in msg:
                logger.info("branch %s already exists — reusing", branch_name)
                return False
            raise

    async def push_file(
        self,
        path: str,
        content: str,
        branch: str,
        message: str,
        sha: str | None = None,
    ) -> bool:
        # GitHub MCP server expects raw text for `content`, not base64.
        args: dict[str, Any] = {
            "owner": self._repo_owner,
            "repo": self._repo_name,
            "path": path,
            "content": content,
            "branch": branch,
            "message": message,
        }
        if sha:
            args["sha"] = sha
        await self._call_tool("create_or_update_file", args)
        logger.info("pushed %s to %s (size=%d)", path, branch, len(content))
        return True

    async def create_pull_request(
        self,
        title: str,
        body: str,
        head: str,
        base: str = "main",
    ) -> dict:
        result = await self._call_tool(
            "create_pull_request",
            {
                "owner": self._repo_owner,
                "repo": self._repo_name,
                "title": title,
                "body": body,
                "head": head,
                "base": base,
            },
        )
        data = self._extract_content(result)
        return data if isinstance(data, dict) else {}

    async def create_issue_comment(self, issue_number: int, body: str) -> bool:
        await self._call_tool(
            "add_issue_comment",
            {
                "owner": self._repo_owner,
                "repo": self._repo_name,
                "issue_number": issue_number,
                "body": body,
            },
        )
        return True

    async def list_workflow_runs(self, workflow_id: str, per_page: int = 10) -> list[dict]:
        result = await self._call_tool(
            "actions_list",
            {
                "method": "list_workflow_runs",
                "owner": self._repo_owner,
                "repo": self._repo_name,
                "per_page": per_page,
                "workflow_runs_filter": {"workflow_id": workflow_id},
            },
        )
        data = self._extract_content(result)
        if isinstance(data, dict):
            runs = data.get("workflow_runs")
            if isinstance(runs, list):
                return runs
        if isinstance(data, list):
            return data
        return []

    async def get_repo_default_branch(self) -> str:
        return "main"
