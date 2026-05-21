"""
FastAPI application — the entry point for GitHub webhook events.

Routes:
  POST /webhook          — main GitHub webhook endpoint
  GET  /health           — liveness probe (returns 200 + timestamp)
  GET  /status           — queue depth, last run info, daily Gemini call count
  POST /trigger          — manual test trigger (disabled in PRODUCTION_MODE=true)

Uses FastAPI lifespan to start/stop the task queue worker.
All requests are logged via middleware (method, path, status, duration_ms).
"""
