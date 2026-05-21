"""
GitHub webhook HMAC-SHA256 signature validator.

GitHub signs every webhook delivery with:
  X-Hub-Signature-256: sha256=<hex_digest>

This module verifies that signature using the GITHUB_WEBHOOK_SECRET.

Uses hmac.compare_digest() — constant-time comparison, immune to timing attacks.
Raises HTTPException(403) immediately on mismatch.
Never logs the raw body or the secret.
"""
