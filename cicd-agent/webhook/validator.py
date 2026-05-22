"""
GitHub webhook HMAC-SHA256 signature validator.

GitHub signs every webhook delivery with:
  X-Hub-Signature-256: sha256=<hex_digest>

This module verifies that signature using the GITHUB_WEBHOOK_SECRET.

Uses hmac.compare_digest() — constant-time comparison, immune to timing attacks.
Raises HTTPException(403) immediately on mismatch.
Never logs the raw body or the secret.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import HTTPException, Request

from config.settings import get_settings

logger = logging.getLogger("cicd_agent.validator")


async def verify_github_signature(request: Request) -> bytes:
    body = await request.body()

    sig_header = request.headers.get("X-Hub-Signature-256", "")
    if not sig_header or not sig_header.startswith("sha256="):
        logger.warning("Missing or malformed X-Hub-Signature-256 header")
        raise HTTPException(status_code=403, detail="Missing signature")

    their_hex = sig_header.removeprefix("sha256=")

    secret = get_settings().github_webhook_secret.encode("utf-8")
    our_hex = hmac.new(secret, body, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(our_hex, their_hex):
        logger.warning("Webhook signature mismatch — possible replay attack")
        raise HTTPException(status_code=403, detail="Invalid signature")

    return body
