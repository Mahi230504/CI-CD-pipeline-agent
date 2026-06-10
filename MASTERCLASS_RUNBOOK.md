# Masterclass — End-to-End Bring-Up Runbook

Start everything for the live demo, **managing the codespace entirely from the terminal**, with both the
localhost dashboard *and* the Vercel dashboard showing the agent's live reasoning.

Confirmed live config (2026-06-09): agent on Mac `:8000` (`WEBHOOK_PORT=8000`), codespace
`potential-giggle-px7x7r4x5xwhrq4` (api on codespace `:8000`), `AGENT_SHARED_SECRET` is 64 chars
(matches the codespace), webhook registered to `https://overhighly-overeasy-malakai.ngrok-free.dev/webhook`.

## Why two tunnels
The agent posts its reasoning to the codespace backend; both dashboards read it back over SSE.
- **localhost dashboard** + **the agent's own posting** use a reliable **SSH forward** (`8000→localhost:8001`).
- **Vercel** is HTTPS and remote, so it can only read via the codespace's **public** URL (`gh codespace ports visibility … public`). That path rides GitHub's CDN edge, which has been ~50% flaky — it's the one link we can't force from the terminal.

---

## Step 0 — shared env (paste at the top of EVERY terminal, or `source` it)
```bash
export CS=potential-giggle-px7x7r4x5xwhrq4
export AGENT=/Users/mohan/Desktop/CI-CD-pipeline-agent/cicd-agent
export DEMO=/Users/mohan/Desktop/CI-CD-pipeline-agent/cicd-agent-demo
export PUB="https://$CS-8000.app.github.dev"          # codespace public URL (for Vercel + edge checks)
export HOOK_ID=627988464                               # the registered GitHub webhook
# If you ever recreate the codespace: gh codespace list  → update CS, then re-run Step 1.
```
> Tip: save the block above as `~/mc-env.sh` and run `source ~/mc-env.sh` in each new terminal.

---

## Step 1 — Codespace bring-up (Terminal A — one-time, frees up after)
Wakes the codespace from Shutdown, ensures services, gives port 8000 a **clean** forwarding registration
(the stop→start dance — a plain restart only fixes it ~50% of the time), and makes it public for Vercel.

```bash
source ~/mc-env.sh

# 1a. Wake it + ensure the stack is up (first SSH after Shutdown takes ~30–60s to boot).
gh codespace ssh -c "$CS" -- 'bash /workspaces/cicd-agent-demo/scripts/codespace_start.sh'

# 1b. Clean port re-registration so the public forward isn't stale (cold boot leaves it stale).
gh codespace ssh -c "$CS" -- 'cd /workspaces/cicd-agent-demo && docker compose stop api'
sleep 4
gh codespace ssh -c "$CS" -- 'cd /workspaces/cicd-agent-demo && docker compose start api'

# 1c. Make port 8000 PUBLIC (this is what lets Vercel reach the backend).
gh codespace ports visibility 8000:public -c "$CS"

# 1d. Verify from the Mac: public edge reachable? (200 = great; 404 = edge flaky → Vercel will stutter,
#     localhost path below is unaffected.)
curl -sS -o /dev/null -w "public /health → %{http_code}\n" "$PUB/health"
```

---

## Step 2 — SSH port-forward (Terminal B — long-running, leave open)
Reliable codespace `:8000` → Mac `localhost:8001`. Powers the localhost dashboard AND the agent's posting.
The forward can drop on an SSH hiccup and does **not** auto-reconnect, so wrap it in a gentle supervisor.

```bash
source ~/mc-env.sh
while true; do
  echo "[forward] connecting $CS:8000 → localhost:8001"
  gh codespace ports forward 8000:8001 -c "$CS" || true
  echo "[forward] dropped — reconnecting in 5s (Ctrl-C to stop)"; sleep 5
done
```
Quick check in another shell: `curl -s http://localhost:8001/health` → JSON with `db`/`redis` booleans.

---

## Step 3 — The agent (Terminal C — long-running)
Launched with `BACKEND_BASE_URL` pointed at the **forward** so event-posting and CD health-checks are
reliable regardless of the public edge. (`config/settings.py` uses `load_dotenv(override=False)`, so this
shell value wins over `.env` — no file edit needed.) **Start Terminal B first.**

```bash
source ~/mc-env.sh
cd "$AGENT" && source .venv/bin/activate
BACKEND_BASE_URL=http://localhost:8001 python main.py
```
Expect the banner + `✓ LLM (OpenRouter): connected`, `✓ GitHub PAT: valid`, `Webhook port : 8000`.

---

## Step 4 — ngrok (Terminal D — long-running)
Reclaim the **static** domain the GitHub webhook already points at, so no webhook reconfig is needed.
```bash
source ~/mc-env.sh
ngrok http --url=https://overhighly-overeasy-malakai.ngrok-free.dev 8000
#   (older ngrok: use --domain=overhighly-overeasy-malakai.ngrok-free.dev instead of --url=…)
```
**Fallback** if that domain is no longer reserved to your account (ngrok errors):
```bash
ngrok http 8000          # note the new https URL it prints, then point the webhook at it:
gh api -X PATCH repos/Mahi230504/cicd-agent-demo/hooks/$HOOK_ID \
  -f "config[url]=https://<NEW>.ngrok-free.dev/webhook" \
  -f "config[content_type]=json" \
  -f "config[secret]=$(grep '^GITHUB_WEBHOOK_SECRET=' "$AGENT/.env" | cut -d= -f2-)"
#   (the secret MUST be re-sent — a config PATCH replaces the whole config object.)
```

---

## Step 5 — localhost dashboard (Terminal E — long-running)
Point Vite at the forward, then run the dev server on `:5173`.
```bash
source ~/mc-env.sh
printf 'VITE_API_BASE=http://localhost:8001\n' > "$DEMO/frontend/.env.local"
cd "$DEMO/frontend" && npm run dev
```
Open **http://localhost:5173**.
> **Vercel** (https://cicd-agent-demo.vercel.app) needs no terminal action — its `VITE_API_BASE` is set in
> the Vercel project to the codespace public URL and is unchanged (same codespace). It lights up once
> Step 1c's public port is reachable. If you recreated the codespace, update `VITE_API_BASE` in Vercel and redeploy.

---

## Step 6 — Verify the whole chain BEFORE you present
```bash
source ~/mc-env.sh

# A) Forward alive?
curl -s http://localhost:8001/health | head -c 200; echo

# B) THE critical check — does the agent's secret match the codespace? Post a smoke event.
#    202 / an id  = wired ✓   |   401 = AGENT_SHARED_SECRET mismatch (fix below)
TOKEN=$(grep '^AGENT_SHARED_SECRET=' "$AGENT/.env" | cut -d= -f2-)
curl -s -o /dev/null -w "agent-event → %{http_code}\n" -X POST http://localhost:8001/internal/agent-event \
  -H "X-Agent-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"stage":"setup-smoke","level":"success","message":"wiring check from runbook","metadata":{}}'

# C) Confirm it shows up in the stream (and therefore on BOTH dashboards' timelines):
curl -s -N --max-time 3 http://localhost:8001/agent/events/stream | head -c 400; echo

# D) Agent process healthy?
curl -s http://localhost:8000/status | python3 -m json.tool | head -20
```
**If B returns 401:** the agent's `AGENT_SHARED_SECRET` ≠ the codespace's running value. Read the codespace's
value and copy it into the agent `.env` (don't change the codespace side — that recreates the api container
and re-stales the tunnel):
```bash
gh codespace ssh -c "$CS" -- 'printenv AGENT_SHARED_SECRET'   # copy this into $AGENT/.env, restart Terminal C
```

---

## Step 7 — Trigger the demo run
Pick one (see `MASTERCLASS_BLUEPRINT.md §4` for the staged bug):
```bash
# Most impressive — real webhook via a push to demo main (do this in the demo repo working copy):
cd "$DEMO" && git push origin main          # after committing the pre-staged bug

# No-network alternative (PRODUCTION_MODE=false, so /trigger is enabled):
curl -s -X POST http://localhost:8000/trigger -H 'Content-Type: application/json' -d '{
  "run_id": 0, "repo_owner": "Mahi230504", "repo_name": "cicd-agent-demo",
  "workflow_name": "CI", "branch": "main", "head_sha": "<failing-sha>",
  "html_url": "https://github.com/Mahi230504/cicd-agent-demo/actions"
}'

# Replay a known-good failing delivery (IDs are 19-digit — use python, NOT --jq, which float-rounds them):
gh api "repos/Mahi230504/cicd-agent-demo/hooks/$HOOK_ID/deliveries?per_page=25" \
  | python3 -c "import json,sys;[print(d['id'], d.get('event'), d.get('action')) for d in json.load(sys.stdin)]"
gh api -X POST "repos/Mahi230504/cicd-agent-demo/hooks/$HOOK_ID/deliveries/<DELIVERY_ID>/attempts"
```
Watch: agent terminal (Step 3) logs each stage → both dashboards stream the reasoning timeline → Telegram fires.

---

## Step 8 — Teardown (after the session)
```bash
source ~/mc-env.sh
# Ctrl-C terminals B (forward), C (agent), D (ngrok), E (vite).
gh codespace stop -c "$CS"     # stop the codespace so it doesn't drift / burn hours
```

---

## Gotcha cheat-sheet (hard-won — see reference_codespace_gotchas memory)
- **Cold boot re-stales port 8000** → always do the Step 1b stop→start, not a plain restart.
- **Vercel needs the public edge**, which flaps ~50% GitHub-side and isn't fixable from here. The localhost
  dashboard (via the SSH forward) is your reliable hero screen; treat Vercel as the bonus. If you must make
  Vercel bulletproof, run a *second* ngrok/cloudflared tunnel on the codespace's 8000 and set that HTTPS URL
  as Vercel's `VITE_API_BASE` (a redeploy), bypassing the flaky edge.
- **`RELEASE_WORKFLOW_NAME=Release`** is case-sensitive and already correct — leave it.
- **`/version` can return a cached 404** right after a visibility flip → add `?cb=$RANDOM` to bust it.
- **First `gh codespace ssh` after Shutdown is slow** (~30–60s) — that's the boot, not a hang.
```
