"""
Local integration test — verifies the pipeline works end-to-end
without needing a real GitHub repository or a live webhook.

Uses fixtures from tests/fixtures/:
- sample_webhook.json  — a real GitHub workflow_run failure payload
- sample_log.txt       — a realistic CI log with an intentional failure
- sample_workflow.yml  — a slow sequential workflow for the optimizer

Does NOT create any real PRs. Does NOT call GitHub MCP.
Uses the real Gemini API (requires GEMINI_API_KEY in .env).

Run with: python test_local.py
Or:        make test-local
"""
