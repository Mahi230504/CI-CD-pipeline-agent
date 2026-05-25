#!/usr/bin/env bash
# Deploy the CI/CD intelligence agent to Fly.io.
#
# Idempotent: safe to run repeatedly. Reads required secrets from the local
# .env (so the keys you've already set up for local dev push verbatim to Fly).
#
# Usage:
#   ./scripts/deploy.sh                   # build + deploy
#   ./scripts/deploy.sh --setup           # one-time: create app + volume + secrets
#   ./scripts/deploy.sh --status          # show app health post-deploy

set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v fly >/dev/null 2>&1; then
  echo "✗ fly CLI not found. Install: curl -L https://fly.io/install.sh | sh"
  exit 1
fi

GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
APP="$(grep '^app = ' fly.toml | head -1 | sed -E 's/.*"([^"]+)".*/\1/')"
REGION="$(grep '^primary_region = ' fly.toml | head -1 | sed -E 's/.*"([^"]+)".*/\1/')"

cmd="${1:-deploy}"

case "$cmd" in
  --setup)
    echo "→ Creating Fly app: $APP in $REGION"
    fly apps create "$APP" --org personal || echo "  (app may already exist; continuing)"

    echo "→ Creating persistent volume agent_data (1GB) in $REGION"
    fly volumes create agent_data --size 1 --region "$REGION" --app "$APP" || \
      echo "  (volume may already exist; continuing)"

    if [[ ! -f .env ]]; then
      echo "✗ .env not found — copy .env.example and fill in secrets first"
      exit 1
    fi

    echo "→ Pushing secrets from .env"
    # Pass only the keys the agent actually needs. Each line in .env that
    # is FOO=bar (no comments, no blanks) gets set.
    secret_args=()
    while IFS= read -r line; do
      [[ "$line" =~ ^[[:space:]]*# ]] && continue
      [[ -z "${line// }" ]] && continue
      case "$line" in
        GEMINI_API_KEY=*|GITHUB_PERSONAL_ACCESS_TOKEN=*|GITHUB_WEBHOOK_SECRET=*|\
        GITHUB_REPO_OWNER=*|GITHUB_REPO_NAME=*|\
        SLACK_WEBHOOK_URL=*|TELEGRAM_BOT_TOKEN=*|TELEGRAM_CHAT_ID=*|\
        DAILY_COST_CAP_DOLLARS=*|MAX_PATCH_ATTEMPTS=*|SESSION_TIMEOUT_SECONDS=*)
          secret_args+=("$line")
          ;;
      esac
    done < .env
    if (( ${#secret_args[@]} == 0 )); then
      echo "✗ no recognized secrets in .env"
      exit 1
    fi
    fly secrets set --app "$APP" "${secret_args[@]}"
    echo "✓ setup complete. Run ./scripts/deploy.sh to push code."
    ;;
  --status)
    fly status --app "$APP"
    echo
    fly logs --app "$APP" -n 20 || true
    ;;
  deploy|"")
    echo "→ Deploying $APP (sha=$GIT_SHA)"
    fly deploy --app "$APP" \
      --build-arg GIT_SHA="$GIT_SHA" \
      --build-arg BUILD_TIME="$BUILD_TIME" \
      --strategy rolling
    echo
    echo "✓ Deployed. Public URL:"
    fly status --app "$APP" --json 2>/dev/null | \
      python -c "import json,sys; d=json.load(sys.stdin); print('  https://' + d.get('Hostname',''))" \
      || echo "  https://$APP.fly.dev"
    echo
    echo "Register webhook in GitHub repo settings:"
    echo "  https://$APP.fly.dev/webhook"
    ;;
  *)
    echo "Usage: $0 [--setup | --status | deploy]"
    exit 2
    ;;
esac
