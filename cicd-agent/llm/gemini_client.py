"""
LLM client wrapper — OpenRouter (OpenAI-compatible chat completions).

Provides the async surface used by all agents:
- generate(prompt, system_prompt, agent, use_light_model, temperature, strip_pii) → str
- ping() → bool

Internally:
- Talks to OpenRouter's /chat/completions over httpx (no extra SDK dependency).
- Routes all calls through rate_limiter.py (concurrency + min-gap + 429 backoff).
- Strips potential PII patterns from log content before sending.
- Validates that responses are non-empty.
- Raises GeminiError (project-specific) on unrecoverable failures.

Naming note: the module/class/exceptions keep the historical "gemini" names so
the agents and metrics don't have to change. The provider is now OpenRouter; the
default models are still the google/gemini-2.5-flash family via OpenRouter.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

from config.settings import get_settings
from llm.rate_limiter import (
    DailyLimitReachedError,
    GeminiError,
    GeminiRateLimitError,
    rate_limited_call,
)
from metrics import record_gemini_call

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = httpx.Timeout(120.0, connect=10.0)


def _extract_token_counts(data: dict[str, Any]) -> tuple[int, int]:
    """Pull (input_tokens, output_tokens) from an OpenAI-shape usage block."""
    usage = data.get("usage") or {}
    inp = usage.get("prompt_tokens", 0) or 0
    out = usage.get("completion_tokens", 0) or 0
    try:
        return int(inp), int(out)
    except (TypeError, ValueError):
        return 0, 0


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


def _extract_text(data: dict[str, Any], agent: str) -> str:
    choices = data.get("choices")
    if not choices:
        raise GeminiError("LLM returned no choices", agent=agent)
    first = choices[0] or {}
    finish = str(first.get("finish_reason", "")).lower()
    if finish in ("content_filter",):
        raise GeminiError(f"LLM response blocked: {finish}", agent=agent)
    message = first.get("message") or {}
    text = message.get("content")
    if isinstance(text, list):  # some providers return content as parts
        text = "".join(
            part.get("text", "") for part in text if isinstance(part, dict)
        )
    if not text or not str(text).strip():
        raise GeminiError("LLM returned empty response", agent=agent)
    return str(text)


class GeminiClient:
    """OpenRouter-backed chat client. Name kept for call-site compatibility."""

    def __init__(self) -> None:
        settings = get_settings()
        self._api_key = settings.openrouter_api_key
        self._base_url = settings.openrouter_base_url.rstrip("/")
        self._primary_model = settings.primary_model
        self._light_model = settings.light_model

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            # Optional OpenRouter attribution headers — harmless if ignored.
            "HTTP-Referer": "https://github.com/cicd-intelligence-agent",
            "X-Title": "CI/CD Intelligence Agent",
        }

    async def _chat(
        self,
        model: str,
        system_prompt: str,
        prompt: str,
        temperature: float,
    ) -> dict[str, Any]:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
        }
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            resp = await client.post(
                f"{self._base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
            )
        # Surface HTTP errors so the rate limiter can classify 429/503 and back off.
        if resp.status_code != 200:
            body = resp.text[:300]
            raise httpx.HTTPStatusError(
                f"OpenRouter {resp.status_code}: {body}",
                request=resp.request,
                response=resp,
            )
        return resp.json()

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

        async def _call() -> dict[str, Any]:
            return await self._chat(model, system_prompt, prompt, temperature)

        started = time.monotonic()
        try:
            data = await rate_limited_call(_call)
        except (GeminiError, GeminiRateLimitError, DailyLimitReachedError):
            raise
        except Exception as e:
            raise GeminiError(f"{agent}: {e}", agent=agent, original=e) from e

        duration = time.monotonic() - started
        in_toks, out_toks = _extract_token_counts(data)
        cost = record_gemini_call(model, duration, in_toks, out_toks)
        logger.info(
            "llm: agent=%s model=%s dur=%.2fs in=%d out=%d cost=$%.5f",
            agent, model, duration, in_toks, out_toks, cost,
        )
        return _extract_text(data, agent)

    async def ping(self) -> bool:
        import asyncio as _asyncio

        last_exc: Exception | None = None
        for attempt in (1, 2, 3):
            try:
                data = await self._chat(
                    self._primary_model,
                    "You are a health check.",
                    "Reply with just the word OK.",
                    0.0,
                )
                text = _extract_text(data, "ping")
                if text.strip():
                    logger.info("llm ping: ok (attempt %d)", attempt)
                    return True
                logger.warning("llm ping: empty response (attempt %d)", attempt)
            except Exception as e:
                last_exc = e
                logger.warning("llm ping failed (attempt %d): %s", attempt, e)
            if attempt < 3:
                await _asyncio.sleep(2 * attempt)
        if last_exc is not None:
            logger.warning("llm ping: giving up after 3 attempts: %s", last_exc)
        return False


_gemini_client: GeminiClient | None = None


def init_gemini_client() -> GeminiClient:
    global _gemini_client
    _gemini_client = GeminiClient()
    logger.info(
        "llm_client initialised (OpenRouter): primary=%s light=%s",
        _gemini_client._primary_model,
        _gemini_client._light_model,
    )
    return _gemini_client


def get_gemini_client() -> GeminiClient:
    if _gemini_client is None:
        raise RuntimeError(
            "llm_client not initialised — call init_gemini_client() at startup"
        )
    return _gemini_client
