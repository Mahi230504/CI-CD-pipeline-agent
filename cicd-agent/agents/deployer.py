"""
Deployer — pushes a new image tag onto the GitHub Codespace target.

The deploy is three remote shell steps wrapped in a single SSH session:

  1. Capture the current `API_IMAGE=` line from the codespace's `.env`.
     This becomes `prev_tag` on the returned DeployResult so a later
     rollback can re-apply it without re-querying the host.
  2. Replace (or append) `API_IMAGE=<new>` in the same `.env`.
  3. `docker compose pull` then `docker compose up -d` from the repo root.

Connectivity is via `gh codespace ssh -c $CODESPACE_NAME -- "<bash>"`. The
agent must have `gh` on its PATH and be logged into a GitHub account that
owns the codespace. Failure modes:

- gh CLI missing / not logged in       → returns DeployResult(success=False)
- codespace asleep                     → gh will wake it; longer first call
- non-zero exit from any remote step   → returns DeployResult(success=False)
- whole session times out              → returns DeployResult(success=False)

This module NEVER raises. The orchestrator branches on `.success`.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
import time

from config.settings import get_settings
from models.cd import DeployResult

logger = logging.getLogger("cicd_agent.deployer")

# Wall-clock cap on a single `gh codespace ssh` call. Docker compose pull on
# a cold codespace can easily take 30-60s; a generous default lets the demo
# survive a first-time pull without spuriously timing out, but still bounds
# how long a wedged session can hang the pipeline.
_SSH_TIMEOUT_SECONDS = 180

# How much of the remote stdout/stderr to retain on DeployResult. Enough to
# diagnose a failure from logs alone, small enough not to bloat audit lines.
_MAX_CAPTURED_OUTPUT_CHARS = 2000

# Bash one-liners are built from this template. Anything substituted in
# MUST go through shlex.quote — these are real shell commands on a real
# host.
_BUILD_REMOTE_SCRIPT_PREV_TAG = r"""
set -eu
cd {workdir}
if [ -f .env ] && grep -qE '^{env_var}=' .env; then
  grep -E '^{env_var}=' .env | head -n1 | cut -d= -f2-
fi
"""

_BUILD_REMOTE_SCRIPT_DEPLOY = r"""
set -eu
cd {workdir}
touch .env
# Replace each existing line if present, otherwise append. Use a single
# python pass so a partial write can't leave .env corrupt. macOS/BSD sed
# semantics differ from GNU; avoid sed entirely.
#
# We set the image ref AND the VERSION marker the demo's /version endpoint
# reads (mirroring scripts/deploy.sh). Without VERSION, /version keeps
# reporting the previous commit and health_monitor's SHA check never
# passes — so every deploy would spuriously roll back.
DEPLOYED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
python3 - "$DEPLOYED_AT" <<'PYEOF'
import sys, pathlib
deployed_at = sys.argv[1]
updates = {updates_repr}
updates["VERSION_DEPLOYED_AT"] = deployed_at
p = pathlib.Path('.env')
lines = p.read_text().splitlines() if p.exists() else []
out = []
seen = set()
for ln in lines:
    key = ln.split('=', 1)[0] if '=' in ln else None
    if key in updates:
        out.append(key + '=' + updates[key])
        seen.add(key)
    else:
        out.append(ln)
for key, val in updates.items():
    if key not in seen:
        out.append(key + '=' + val)
p.write_text('\n'.join(out) + '\n')
PYEOF
echo "[deployer] {env_var}=$(grep -E '^{env_var}=' .env | head -n1 | cut -d= -f2-)"
echo "[deployer] VERSION=$(grep -E '^VERSION=' .env | head -n1 | cut -d= -f2-)"
docker compose pull api worker
docker compose up -d api worker
echo "[deployer] running containers:"
docker compose ps --status running --format '{{{{.Name}}}} {{{{.Image}}}}'
"""


def _clip(s: str, limit: int = _MAX_CAPTURED_OUTPUT_CHARS) -> str:
    if len(s) <= limit:
        return s
    # Keep the tail — that's where docker compose's "Error response from
    # daemon" message would be on a failing pull.
    return "...[truncated]\n" + s[-(limit - 16):]


def _python_literal(value: str) -> str:
    """Embed a string safely inside the python heredoc on the remote host.

    Uses repr() — guarantees the resulting literal is a valid python str
    regardless of contents (including the values that contain newlines or
    quotes). Equivalent of shlex.quote for python source.
    """
    return repr(value)


async def _run_ssh(remote_script: str, timeout: int) -> tuple[int, str, str]:
    """Execute `remote_script` on the configured codespace via `gh codespace ssh`.

    Returns (returncode, stdout, stderr). Wraps subprocess + timeout into
    something callers can branch on without dealing with asyncio plumbing.
    """
    settings = get_settings()
    if not settings.codespace_name:
        return 127, "", "CODESPACE_NAME not configured"

    cmd = [
        "gh",
        "codespace",
        "ssh",
        "-c",
        settings.codespace_name,
        "--",
        # Run the script with bash so the heredoc and pipes behave even if
        # the codespace's default shell is something else.
        "bash",
        "-c",
        remote_script,
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return 127, "", "gh CLI not found on PATH"
    except Exception as e:
        return 1, "", f"subprocess launch failed: {e}"

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        # Drain remaining output without blocking forever.
        try:
            await asyncio.wait_for(proc.communicate(), timeout=2.0)
        except Exception:
            pass
        return 124, "", f"gh codespace ssh timed out after {timeout}s"

    return (
        proc.returncode or 0,
        stdout_bytes.decode("utf-8", errors="replace"),
        stderr_bytes.decode("utf-8", errors="replace"),
    )


# Exposed for tests so they can replace the subprocess call with a fake
# without monkey-patching asyncio internals.
run_ssh = _run_ssh


async def _read_prev_tag() -> str:
    """Best-effort read of the host's current API_IMAGE value. Empty on miss."""
    settings = get_settings()
    script = _BUILD_REMOTE_SCRIPT_PREV_TAG.format(
        workdir=shlex.quote(settings.codespace_workdir),
        env_var=settings.deploy_image_env_var,
    )
    rc, stdout, stderr = await run_ssh(script, timeout=30)
    if rc != 0:
        logger.warning(
            "deployer: prev-tag read returned rc=%d stderr=%s",
            rc,
            stderr.strip()[:200],
        )
        return ""
    return stdout.strip()


# Image references look like: <host>/<path>:<tag> or <path>:<tag>. We're
# permissive on host/path (no scheme assumed) but reject anything with
# whitespace or shell metacharacters — those would be a config bug, not a
# legitimate tag.
_IMAGE_REF_RE = re.compile(r"^[A-Za-z0-9_./\-]+:[A-Za-z0-9_.\-]+$")


def _looks_like_image_ref(value: str) -> bool:
    return bool(_IMAGE_REF_RE.match(value))


async def deploy(image_tag: str) -> DeployResult:
    """Apply `image_tag` (a full `<repo>:<tag>` ref) to the codespace.

    Steps run in order:
      1. Snapshot the previous tag (for rollback).
      2. Edit .env, then docker compose pull + up -d on api+worker.

    The function always returns a DeployResult — exceptions are caught and
    summarised in `error_message`.
    """
    settings = get_settings()
    started = time.monotonic()

    if not image_tag or not _looks_like_image_ref(image_tag):
        return DeployResult(
            success=False,
            image_tag=image_tag,
            error_message=f"invalid image reference: {image_tag!r}",
        )

    if not settings.codespace_name:
        return DeployResult(
            success=False,
            image_tag=image_tag,
            error_message="CODESPACE_NAME is not configured",
        )

    try:
        prev_tag = await _read_prev_tag()
    except Exception as e:
        logger.warning("deployer: prev-tag read raised: %s", e)
        prev_tag = ""

    # The VERSION marker must equal the tag the health check expects, which
    # is the tag portion of the image ref (== ReleaseSuccessEvent.short_sha).
    version = image_tag.rsplit(":", 1)[-1]
    updates = {
        settings.deploy_image_env_var: image_tag,
        "VERSION": version,
    }
    script = _BUILD_REMOTE_SCRIPT_DEPLOY.format(
        workdir=shlex.quote(settings.codespace_workdir),
        env_var=settings.deploy_image_env_var,
        updates_repr=repr(updates),
    )

    try:
        rc, stdout, stderr = await run_ssh(script, timeout=_SSH_TIMEOUT_SECONDS)
    except Exception as e:
        return DeployResult(
            success=False,
            image_tag=image_tag,
            prev_tag=prev_tag,
            error_message=f"ssh raised: {e}",
            duration_seconds=time.monotonic() - started,
        )

    combined = stdout if not stderr else f"{stdout}\n--- stderr ---\n{stderr}"
    duration = time.monotonic() - started

    if rc != 0:
        return DeployResult(
            success=False,
            image_tag=image_tag,
            prev_tag=prev_tag,
            output=_clip(combined),
            error_message=f"remote exit rc={rc}",
            duration_seconds=duration,
        )

    return DeployResult(
        success=True,
        image_tag=image_tag,
        prev_tag=prev_tag,
        output=_clip(combined),
        duration_seconds=duration,
    )


def build_image_ref(short_sha: str) -> str:
    """Compose the full image reference from settings.

    Convenience used by the orchestrator (Phase 4) — keeps the construction
    rule in one place so it stays consistent between forward deploys and
    the rollback path's pre-flight checks.
    """
    settings = get_settings()
    repo = settings.deploy_image_repository.rstrip("/")
    if not repo:
        raise ValueError("DEPLOY_IMAGE_REPOSITORY is not configured")
    if not short_sha:
        raise ValueError("short_sha is empty")
    return f"{repo}:{short_sha}"
