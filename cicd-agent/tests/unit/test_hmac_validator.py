"""Unit tests for webhook/validator.py — valid, invalid, and tampered signatures."""

from __future__ import annotations

import hashlib
import hmac
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from config.settings import get_settings
from webhook.validator import verify_github_signature


def make_mock_request(body: bytes, signature: str | None) -> MagicMock:
    mock = MagicMock()
    mock.body = AsyncMock(return_value=body)
    headers: dict[str, str] = {}
    if signature is not None:
        headers["X-Hub-Signature-256"] = signature
    mock.headers = headers
    return mock


def _sign(body: bytes) -> str:
    secret = get_settings().github_webhook_secret.encode("utf-8")
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


async def test_valid_signature_returns_body():
    body = b'{"hello": "world"}'
    req = make_mock_request(body, _sign(body))
    result = await verify_github_signature(req)
    assert result == body


async def test_wrong_signature_raises_403():
    body = b'{"a": 1}'
    bad_sig = "sha256=" + "00" * 32
    req = make_mock_request(body, bad_sig)
    with pytest.raises(HTTPException) as exc_info:
        await verify_github_signature(req)
    assert exc_info.value.status_code == 403


async def test_missing_header_raises_403():
    req = make_mock_request(b"{}", None)
    with pytest.raises(HTTPException) as exc_info:
        await verify_github_signature(req)
    assert exc_info.value.status_code == 403


async def test_malformed_header_raises_403():
    req = make_mock_request(b"{}", "notsha256=abc")
    with pytest.raises(HTTPException) as exc_info:
        await verify_github_signature(req)
    assert exc_info.value.status_code == 403


async def test_empty_body_valid_signature():
    body = b""
    req = make_mock_request(body, _sign(body))
    result = await verify_github_signature(req)
    assert result == b""


async def test_tampered_body_raises_403():
    original = b'{"action": "completed"}'
    sig = _sign(original)
    tampered = b'{"action": "tampered"}'
    req = make_mock_request(tampered, sig)
    with pytest.raises(HTTPException) as exc_info:
        await verify_github_signature(req)
    assert exc_info.value.status_code == 403
