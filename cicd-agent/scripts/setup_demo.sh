#!/bin/bash
# setup_demo.sh — Sets up the demo GitHub repo for the masterclass.
#
# Pushes a broken test (10 / 0 → ZeroDivisionError), a slow sequential CI workflow,
# registers a webhook pointing at the given ngrok URL, and triggers the first run.
#
# Usage:
#   ./scripts/setup_demo.sh https://abc123.ngrok.io

set -euo pipefail

# ─── Args ─────────────────────────────────────────────────────────────────────
if [ "$#" -lt 1 ] || [ -z "${1:-}" ]; then
    echo "Usage: $0 <ngrok-url>"
    echo "Example: $0 https://abc123.ngrok.io"
    exit 1
fi
NGROK_URL="${1%/}"

# ─── Load .env ────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/../.env"
if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: .env not found at $ENV_FILE"
    exit 1
fi

read_env() {
    grep -E "^${1}=" "$ENV_FILE" | head -1 | sed -E "s/^${1}=//" | tr -d '\r\n'
}

PAT="$(read_env GITHUB_PERSONAL_ACCESS_TOKEN)"
OWNER="$(read_env GITHUB_REPO_OWNER)"
REPO="$(read_env GITHUB_REPO_NAME)"
SECRET="$(read_env GITHUB_WEBHOOK_SECRET)"

if [ -z "$PAT" ] || [ -z "$OWNER" ] || [ -z "$REPO" ] || [ -z "$SECRET" ]; then
    echo "ERROR: missing required values in .env (PAT, OWNER, REPO, WEBHOOK_SECRET)"
    exit 1
fi

API="https://api.github.com"
AUTH_HDR="Authorization: Bearer ${PAT}"
ACCEPT_HDR="Accept: application/vnd.github+json"
VERSION_HDR="X-GitHub-Api-Version: 2022-11-28"

# ─── Step 1 — Create repo if needed ───────────────────────────────────────────
echo "Checking repo ${OWNER}/${REPO}..."
STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "$AUTH_HDR" -H "$ACCEPT_HDR" -H "$VERSION_HDR" \
    "${API}/repos/${OWNER}/${REPO}")

if [ "$STATUS" = "404" ]; then
    echo "Creating repo ${OWNER}/${REPO}..."
    curl -s -X POST -H "$AUTH_HDR" -H "$ACCEPT_HDR" -H "$VERSION_HDR" \
        "${API}/user/repos" \
        -d "{\"name\":\"${REPO}\",\"private\":true,\"auto_init\":true,\"description\":\"CI/CD Agent demo repo — do not use in production\"}" \
        > /dev/null
    sleep 2
fi
echo "✓ Repo: ${OWNER}/${REPO}"

# ─── Helper: PUT a file (create or update) ───────────────────────────────────
put_file() {
    local repo_path="$1"
    local content="$2"
    local message="$3"

    local existing_sha
    existing_sha=$(curl -s -H "$AUTH_HDR" -H "$ACCEPT_HDR" -H "$VERSION_HDR" \
        "${API}/repos/${OWNER}/${REPO}/contents/${repo_path}" | \
        python3 -c "import json,sys
try:
    d = json.load(sys.stdin)
    print(d.get('sha','') if isinstance(d, dict) else '')
except Exception:
    print('')" || echo "")

    local b64
    b64=$(printf '%s' "$content" | base64 | tr -d '\r\n')

    local body
    if [ -n "$existing_sha" ]; then
        body=$(printf '{"message":"%s","content":"%s","sha":"%s"}' "$message" "$b64" "$existing_sha")
    else
        body=$(printf '{"message":"%s","content":"%s"}' "$message" "$b64")
    fi

    curl -s -X PUT -H "$AUTH_HDR" -H "$ACCEPT_HDR" -H "$VERSION_HDR" \
        "${API}/repos/${OWNER}/${REPO}/contents/${repo_path}" \
        -d "$body" > /dev/null
}

# ─── Step 2 — Broken test file (ZeroDivisionError: division by zero) ─────────
TEST_CONTENT='def test_add():
    assert 1 + 1 == 2

def test_subtract():
    assert 5 - 3 == 2

def test_divide():
    # BUG: divides by zero — raises ZeroDivisionError: division by zero
    result = 10 / 0
    assert result == 5
'
put_file "tests/test_math.py" "$TEST_CONTENT" "Add tests (with intentional bug)"
echo "✓ Created broken test file: tests/test_math.py"

# ─── Step 3 — CI workflow ────────────────────────────────────────────────────
WORKFLOW_CONTENT='name: CI
on: [push]
jobs:
  install:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Install dependencies
        run: pip install pytest
  lint:
    runs-on: ubuntu-latest
    needs: install
    steps:
      - uses: actions/checkout@v4
      - run: echo "lint step (placeholder)"
  test:
    runs-on: ubuntu-latest
    needs: install
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install pytest
      - name: Run tests
        run: pytest tests/ -v
  build:
    runs-on: ubuntu-latest
    needs: [lint, test]
    steps:
      - uses: actions/checkout@v4
      - run: echo "build complete"
  deploy:
    runs-on: ubuntu-latest
    needs: build
    steps:
      - uses: actions/checkout@v4
      - run: echo "deploying..."
'
put_file ".github/workflows/ci.yml" "$WORKFLOW_CONTENT" "Add CI workflow"
echo "✓ Created CI workflow: .github/workflows/ci.yml"

# ─── Step 4 — Register webhook (workflow_run events) ─────────────────────────
WEBHOOK_BODY=$(printf '{"name":"web","active":true,"events":["workflow_run"],"config":{"url":"%s/webhook","content_type":"json","secret":"%s","insecure_ssl":"0"}}' \
    "$NGROK_URL" "$SECRET")
curl -s -X POST -H "$AUTH_HDR" -H "$ACCEPT_HDR" -H "$VERSION_HDR" \
    "${API}/repos/${OWNER}/${REPO}/hooks" -d "$WEBHOOK_BODY" > /dev/null
echo "✓ Webhook registered: ${NGROK_URL}/webhook"

# ─── Step 5 — Trigger the pipeline by touching README ────────────────────────
README_CONTENT="# ${REPO}

Demo repo for the CI/CD Intelligence Agent.

Last updated: $(date -u +%Y-%m-%dT%H:%M:%SZ)
"
put_file "README.md" "$README_CONTENT" "Trigger pipeline run"
echo "✓ Triggered pipeline run — check GitHub Actions tab"

# ─── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════"
echo "  Demo repo ready!"
echo "  → https://github.com/${OWNER}/${REPO}/actions"
echo "  → Watch the pipeline fail, then watch the agent fix it"
echo "══════════════════════════════════════"
echo ""
echo "Next steps:"
echo "  1. Ensure ngrok is running:  ngrok http 8000"
echo "  2. Start the agent:          make dev"
echo "  3. The pipeline will fail in ~60s and the agent will auto-trigger"
