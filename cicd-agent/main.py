"""
Entry point for the CI/CD Intelligence Agent.

On startup:
1. Loads and validates all settings (hard stop on missing keys)
2. Pings Gemini API to confirm the key works
3. Pings GitHub MCP to confirm PAT has correct permissions
4. Starts the FastAPI server via uvicorn
5. Registers SIGTERM/SIGINT handlers for graceful shutdown

Prints a startup summary with: models in use, repo being watched,
rate limit config, webhook URL to register in GitHub.
"""
