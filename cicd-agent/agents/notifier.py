"""
Notifier agent — sends the full pipeline report to Slack and/or Telegram.

Sends a single formatted message containing:
- Whether the failure was flaky or real
- Diagnosis summary (error type, file, line)
- Patch PR link (if created)
- Optimization PR link + estimated time saved (if applicable)
- Total time taken by the agent pipeline

Supports both Slack (via incoming webhook + Block Kit) and Telegram (bot API).
If both are configured, sends to both. If neither is configured, logs only.

Uses LIGHT_MODEL (gemini-2.5-flash-lite) for message formatting.
Runs async — never blocks the pipeline. Retries once on failure.
"""
