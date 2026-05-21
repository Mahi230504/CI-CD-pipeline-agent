"""
Gemini API client wrapper.

Provides two async functions used by all agents:
- generate(prompt, system_prompt) → str
- generate_with_tools(prompt, system_prompt, mcp_session) → str

Internally:
- Uses google-genai SDK (NOT google-generativeai)
- Routes all calls through rate_limiter.py
- Strips potential PII patterns from log content before sending
- Validates that responses are non-empty and not error messages
- Raises GeminiError (a project-specific exception) on unrecoverable failures
"""

from __future__ import annotations

import logging
import re
from typing import Any

from google import genai
from google.genai import types

from config.settings import get_settings
from llm.rate_limiter import (
    DailyLimitReachedError,
    GeminiError,
    GeminiRateLimitError,
    rate_limited_call,
)

logger = logging.getLogger(__name__)


_AWS_KEY_PATTERN = re.compile(r"AKIA[0-9A-Z]{16}")
_GITHUB_PAT_PATTERN = re.compile(r"gh[pousr]_[A-Za-z0-9]{36}")
_JWT_PATTERN = re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_GENERIC_SECRET_PATTERN = re.compile(
    r"(key|token|secret|password|credential|auth)=['\"]?[A-Za-z0-9+/=_\-]{20,}",
    flags=re.IGNORECASE,
)


def _strip_pii(text: str) -> str:
    if not text:
        return text
    text = _AWS_KEY_PATTERN.sub("[AWS_KEY_REDACTED]", text)
    text = _GITHUB_PAT_PATTERN.sub("[GH_TOKEN_REDACTED]", text)
    text = _JWT_PATTERN.sub("[JWT_REDACTED]", text)
    text = _GENERIC_SECRET_PATTERN.sub(lambda m: f"{m.group(1)}=[REDACTED]", text)
    return text


def _validate_response(response: Any, agent: str) -> str:
    candidates = getattr(response, "candidates", None)
    if not candidates:
        raise GeminiError("Gemini returned no candidates", agent=agent)

    finish_reason = getattr(candidates[0], "finish_reason", None)
    if finish_reason is not None:
        fr_str = str(finish_reason).upper()
        if "SAFETY" in fr_str or "BLOCKED" in fr_str:
            raise GeminiError(f"Gemini response blocked: {fr_str}", agent=agent)

    text = getattr(response, "text", None)
    if not text:
        try:
            parts = candidates[0].content.parts
            text = "".join(getattr(p, "text", "") or "" for p in parts)
        except Exception:
            text = ""

    if not text or not text.strip():
        raise GeminiError("Gemini returned empty response", agent=agent)

    return text


class GeminiClient:
    def __init__(self) -> None:
        settings = get_settings()
        self._client = genai.Client(api_key=settings.gemini_api_key)
        self._primary_model = settings.primary_model
        self._light_model = settings.light_model

    async def generate(
        self,
        prompt: str,
        system_prompt: str,
        agent: str,
        use_light_model: bool = False,
        temperature: float = 0.1,
        strip_pii: bool = False,
    ) -> str:
        if strip_pii:
            prompt = _strip_pii(prompt)

        model = self._light_model if use_light_model else self._primary_model
        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=temperature,
        )

        async def _call():
            return await self._client.aio.models.generate_content(
                model=model,
                contents=prompt,
                config=config,
            )

        try:
            response = await rate_limited_call(_call)
        except (GeminiError, GeminiRateLimitError, DailyLimitReachedError):
            raise
        except Exception as e:
            raise GeminiError(f"{agent}: {e}", agent=agent, original=e) from e

        return _validate_response(response, agent)

    async def generate_with_tools(
        self,
        prompt: str,
        system_prompt: str,
        mcp_session: Any,
        agent: str,
        temperature: float = 0.1,
        max_tool_calls: int = 10,
    ) -> str:
        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=temperature,
            tools=[mcp_session],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                maximum_remote_calls=max_tool_calls,
            ),
        )

        async def _call():
            return await self._client.aio.models.generate_content(
                model=self._primary_model,
                contents=prompt,
                config=config,
            )

        try:
            response = await rate_limited_call(_call)
        except (GeminiError, GeminiRateLimitError, DailyLimitReachedError):
            raise
        except Exception as e:
            raise GeminiError(f"{agent}: {e}", agent=agent, original=e) from e

        return _validate_response(response, agent)

    async def ping(self) -> bool:
        try:
            response = await self._client.aio.models.generate_content(
                model=self._light_model,
                contents="Reply with just the word OK.",
                config=types.GenerateContentConfig(temperature=0.0),
            )
            text = getattr(response, "text", "") or ""
            ok = bool(text.strip())
            logger.info("gemini ping: %s", "ok" if ok else "empty response")
            return ok
        except Exception as e:
            logger.warning("gemini ping failed: %s", e)
            return False


_gemini_client: GeminiClient | None = None


def init_gemini_client() -> GeminiClient:
    global _gemini_client
    _gemini_client = GeminiClient()
    logger.info(
        "gemini_client initialised: primary=%s light=%s",
        _gemini_client._primary_model,
        _gemini_client._light_model,
    )
    return _gemini_client


def get_gemini_client() -> GeminiClient:
    if _gemini_client is None:
        raise RuntimeError(
            "gemini_client not initialised — call init_gemini_client() at startup"
        )
    return _gemini_client
