"""
Settings management for the CI/CD agent.

Loads all configuration from environment variables via python-dotenv.
Exposes a single Settings dataclass instance (`get_settings()`) used
everywhere in the project. Raises a descriptive ValueError on startup
if any required variable is missing or invalid.

Never import os.environ directly in other modules — always use get_settings().
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    openrouter_api_key: str
    github_pat: str
    github_webhook_secret: str
    github_repo_owner: str
    github_repo_name: str

    # LLM provider: OpenRouter (OpenAI-compatible API). Model IDs are OpenRouter
    # slugs. gemini_api_key is retained as optional for any direct-Gemini fallback
    # but is no longer required.
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    gemini_api_key: str = ""
    primary_model: str = "google/gemini-2.5-flash"
    light_model: str = "google/gemini-2.5-flash-lite"
    max_patch_attempts: int = 2
    session_timeout_seconds: int = 180
    max_loop_iterations: int = 30
    # Minimum seconds between LLM calls. OpenRouter is paid, so a small courtesy
    # gap is enough; the free-tier 7s gap is no longer needed.
    rate_limit_delay_seconds: float = 0.5
    webhook_port: int = 8000
    log_dir: str = "./logs"
    production_mode: bool = False
    slack_webhook_url: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    daily_cost_cap_dollars: float = 1.0
    cost_cap_warn_pct: float = 0.8
    # Path for the persistent webhook outbox + task-queue replay store.
    queue_db_path: str = "./queue.sqlite3"
    # Path for the JSON run registry (dedup + attempt counting).
    run_registry_path: str = "./run_registry.json"

    # ── CD pipeline (Phase 3) ────────────────────────────────────────────
    # GitHub Codespace name the deployer SSHes into. Resolved via
    # `gh codespace ssh -c $codespace_name -- "<cmd>"`. Empty disables the
    # CD half of the pipeline (orchestrator skips the release event).
    codespace_name: str = ""
    # Absolute path inside the codespace where the demo repo is checked out
    # — used as the working directory for `docker compose` invocations and
    # for the `.env` edit that pins IMAGE_TAG.
    codespace_workdir: str = "/workspaces/cicd-agent-demo"
    # Public tunnel URL of the codespace's port 8000. The health monitor
    # probes /health and /version against this; the agent posts reasoning
    # events here too. Trailing slash is stripped at access time.
    backend_base_url: str = ""
    # Shared secret used in the X-Agent-Token header when posting events to
    # /internal/agent-event. Must match the demo backend's AGENT_SHARED_SECRET.
    # Empty disables event publishing (the agent runs but the dashboard
    # won't show its reasoning live).
    agent_shared_secret: str = ""
    # The GHCR image repo. Combined with the head_sha to form the full
    # image reference (`<repo>:<short_sha>`) the deployer writes into the
    # codespace's .env before `docker compose pull && up -d`.
    deploy_image_repository: str = ""
    # The .env variable name the codespace's docker-compose.yml interpolates
    # for the api/worker service `image:` line. Default matches the demo
    # repo's compose file (`${API_IMAGE:-inventory-flow-api:local}`).
    deploy_image_env_var: str = "API_IMAGE"
    # Name of the release workflow whose successful runs trigger CD. The
    # router filters workflow_run events by this name in addition to the
    # existing failure routing.
    release_workflow_name: str = "release"
    # Wait this many seconds for /health to return 200 AND /version to
    # report the expected SHA before declaring the deploy a failure.
    deploy_health_timeout_seconds: int = 90
    # Sleep between health probe attempts. Combined with the timeout this
    # gives the number of attempts the monitor will make.
    deploy_health_poll_interval_seconds: float = 3.0
    # If false, a failed health check is reported but no rollback happens.
    # Default to enabling rollback because that's the demoable behaviour.
    auto_rollback_enabled: bool = True

    # ── Patch verification (close the fix→verify loop) ───────────────────
    # After opening/updating the fix PR, watch that PR's OWN CI run and only
    # report the failure as fixed when CI goes green. When it stays red, the
    # patcher re-attempts using the new failing output as feedback, up to
    # patch_verify_max_iterations extra tries. False = open the PR and report
    # it as unverified (the pre-verification behaviour), no waiting.
    patch_verify_enabled: bool = True
    # Hard cap (seconds) on waiting for ONE patch CI run to reach a verdict —
    # covers both the time for the run to appear and to complete. On expiry the
    # patch is reported as unverified rather than fixed.
    patch_verify_timeout_seconds: int = 240
    # Extra re-patch attempts after the first, each fed the prior run's failing
    # output. 1 ⇒ up to two patch attempts total per pipeline run. Bounded
    # independently of max_patch_attempts (which counts across webhooks).
    patch_verify_max_iterations: int = 1
    # Seconds between CI status polls while verifying.
    patch_verify_poll_interval_seconds: float = 6.0

    @property
    def github_mcp_url(self) -> str:
        return "https://api.githubcopilot.com/mcp"

    @property
    def github_mcp_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.github_pat}",
            "X-MCP-Toolsets": "all",
        }

    @property
    def log_dir_path(self) -> Path:
        p = Path(self.log_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def has_notifications(self) -> bool:
        return bool(
            self.slack_webhook_url
            or (self.telegram_bot_token and self.telegram_chat_id)
        )

    @property
    def full_repo_name(self) -> str:
        return f"{self.github_repo_owner}/{self.github_repo_name}"

    @property
    def cd_enabled(self) -> bool:
        """True only when every input required to actually deploy is set.

        Used by the router to decide whether release-success events should be
        accepted, and by the CD orchestrator as a guard at entry so a
        misconfigured environment fails loud once rather than producing a
        cascade of mysterious subprocess errors.
        """
        return bool(
            self.codespace_name
            and self.backend_base_url
            and self.deploy_image_repository
        )

    @property
    def backend_base_url_clean(self) -> str:
        """Trailing-slash-free form for safe URL concatenation."""
        return self.backend_base_url.rstrip("/")

    @property
    def agent_events_url(self) -> str:
        """Where event_publisher POSTs reasoning events.

        Returns the empty string when the backend URL is not configured; the
        publisher treats that as a no-op rather than a hard failure so unit
        tests and dev runs without a backend still work.
        """
        if not self.backend_base_url:
            return ""
        return f"{self.backend_base_url_clean}/internal/agent-event"


def _required(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        raise ValueError(f"{name} missing — see .env.example")
    return val


def _optional(name: str, default: str) -> str:
    val = os.getenv(name, "").strip()
    return val if val else default


def _optional_or_none(name: str) -> str | None:
    val = os.getenv(name, "").strip()
    return val if val else None


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ValueError(f"{name} must be an integer — see .env.example") from e


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as e:
        raise ValueError(f"{name} must be a float — see .env.example") from e


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_dotenv()
    settings = Settings(
        openrouter_api_key=_required("OPENROUTER_API_KEY"),
        github_pat=_required("GITHUB_PERSONAL_ACCESS_TOKEN"),
        github_webhook_secret=_required("GITHUB_WEBHOOK_SECRET"),
        github_repo_owner=_required("GITHUB_REPO_OWNER"),
        github_repo_name=_required("GITHUB_REPO_NAME"),
        openrouter_base_url=_optional("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        gemini_api_key=_optional("GEMINI_API_KEY", ""),
        primary_model=_optional("PRIMARY_MODEL", "google/gemini-2.5-flash"),
        light_model=_optional("LIGHT_MODEL", "google/gemini-2.5-flash-lite"),
        max_patch_attempts=_int_env("MAX_PATCH_ATTEMPTS", 2),
        session_timeout_seconds=_int_env("SESSION_TIMEOUT_SECONDS", 180),
        max_loop_iterations=_int_env("MAX_LOOP_ITERATIONS", 30),
        rate_limit_delay_seconds=_float_env("RATE_LIMIT_DELAY_SECONDS", 0.5),
        webhook_port=_int_env("WEBHOOK_PORT", 8000),
        log_dir=_optional("LOG_DIR", "./logs"),
        production_mode=_bool_env("PRODUCTION_MODE", False),
        slack_webhook_url=_optional_or_none("SLACK_WEBHOOK_URL"),
        telegram_bot_token=_optional_or_none("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_optional_or_none("TELEGRAM_CHAT_ID"),
        daily_cost_cap_dollars=_float_env("DAILY_COST_CAP_DOLLARS", 1.0),
        cost_cap_warn_pct=_float_env("COST_CAP_WARN_PCT", 0.8),
        queue_db_path=_optional("QUEUE_DB_PATH", "./queue.sqlite3"),
        run_registry_path=_optional("RUN_REGISTRY_PATH", "./run_registry.json"),
        codespace_name=_optional("CODESPACE_NAME", ""),
        codespace_workdir=_optional("CODESPACE_WORKDIR", "/workspaces/cicd-agent-demo"),
        backend_base_url=_optional("BACKEND_BASE_URL", ""),
        agent_shared_secret=_optional("AGENT_SHARED_SECRET", ""),
        deploy_image_repository=_optional("DEPLOY_IMAGE_REPOSITORY", ""),
        deploy_image_env_var=_optional("DEPLOY_IMAGE_ENV_VAR", "API_IMAGE"),
        release_workflow_name=_optional("RELEASE_WORKFLOW_NAME", "release"),
        deploy_health_timeout_seconds=_int_env("DEPLOY_HEALTH_TIMEOUT_SECONDS", 90),
        deploy_health_poll_interval_seconds=_float_env(
            "DEPLOY_HEALTH_POLL_INTERVAL_SECONDS", 3.0
        ),
        auto_rollback_enabled=_bool_env("AUTO_ROLLBACK_ENABLED", True),
        patch_verify_enabled=_bool_env("PATCH_VERIFY_ENABLED", True),
        patch_verify_timeout_seconds=_int_env("PATCH_VERIFY_TIMEOUT_SECONDS", 240),
        patch_verify_max_iterations=_int_env("PATCH_VERIFY_MAX_ITERATIONS", 1),
        patch_verify_poll_interval_seconds=_float_env(
            "PATCH_VERIFY_POLL_INTERVAL_SECONDS", 6.0
        ),
    )
    print(
        f"⚙ Settings loaded: repo={settings.full_repo_name}, "
        f"model={settings.primary_model}, port={settings.webhook_port}"
    )
    return settings
