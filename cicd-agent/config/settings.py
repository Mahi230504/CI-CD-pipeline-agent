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
    )
    print(
        f"⚙ Settings loaded: repo={settings.full_repo_name}, "
        f"model={settings.primary_model}, port={settings.webhook_port}"
    )
    return settings
