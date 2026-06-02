# CI/CD Intelligence Agent — End-to-End

A multi-agent system that watches a GitHub repository's CI/CD. When a pipeline **fails**, it autonomously
diagnoses the root cause, writes a fix, opens a pull request, and **verifies the fix against the PR's own CI**
before claiming success. When a fix is merged and a **release** succeeds, it **deploys** the new image,
**health-checks** it, and **rolls back** on failure. Every reasoning step is streamed live to a dashboard.

- **`cicd-agent/`** — the agent itself (Python / FastAPI). This is the brain.
- **`cicd-agent-demo/`** — the target app it operates on (FastAPI inventory service + Vite/React dashboard).

The agent talks to GitHub through the **GitHub MCP server** (+ a few REST helpers), reasons with
**Gemini via OpenRouter**, and posts its reasoning to the demo backend, which relays it to the dashboard over SSE.

---

## 1. The big picture

```
                          GitHub (cicd-agent-demo)
                                   │  workflow_run webhook (HMAC-signed)
                                   ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │ INGRESS            webhook/server.py  (FastAPI :8010, via ngrok)       │
   │   validator.py  → HMAC verify (403 on bad signature)                   │
   │   dedup.py      → drop duplicate X-GitHub-Delivery                     │
   │   router.py     → classify: CI-failure | release-success | ignore      │
   └───────────────┬───────────────────────────────────────┬──────────────┘
                   │ failure                                │ release success
                   ▼                                        ▼
   ┌───────────────────────────┐            ┌───────────────────────────────┐
   │ task_queue + event_store  │            │ task_queue + event_store      │
   │ (persist → enqueue → 200) │            │ (persist → enqueue → 200)     │
   └───────────────┬───────────┘            └───────────────┬───────────────┘
                   ▼                                         ▼
   ┌───────────────────────────┐            ┌───────────────────────────────┐
   │ orchestrator/pipeline.py  │            │ orchestrator/cd_pipeline.py   │
   │  (CI: fail → fix → verify)│            │  (CD: deploy → health → roll) │
   └───────────────┬───────────┘            └───────────────┬───────────────┘
                   │ calls agents/* in sequence              │ calls agents/* in sequence
                   ▼                                         ▼
        diagnose → patch → verify → PR             deploy_guard → deploy → health → rollback?
                   │                                         │
                   └──────────── event_publisher ────────────┘
                                   │  POST /internal/agent-event (X-Agent-Token)
                                   ▼
                       demo backend  ──SSE /agent/events/stream──►  dashboard (:5173)
                                   │
                                Telegram / Slack  (notifier)
```

Both pipelines run as **async tasks off a serialized queue**, are wrapped in a **session timeout**, and emit
**audit logs** + **Prometheus metrics** at every step.

---

## 2. End-to-end flow, process by process

### 2A. CI failure → verified fix PR  (`orchestrator/pipeline.py`)

1. **A developer pushes a bug** to `main` → GitHub Actions CI runs and **fails**.
2. GitHub fires a `workflow_run` webhook → **`webhook/server.py`** receives it.
   - **`validator.py`** verifies the `X-Hub-Signature-256` HMAC (constant-time). Bad sig → `403`.
   - **`dedup.py`** drops redeliveries already seen (in-memory LRU on `X-GitHub-Delivery`).
   - **`router.py`** parses + filters: it acts **only** on `workflow_run` with `conclusion=failure`, ignores
     successes, `skipped`, bot/forks, and **the agent's own `agent/*` branches** (so it never self-triggers).
3. **`event_store.py`** persists the event (sqlite WAL outbox), **`task_queue.py`** enqueues it, and the
   server returns `200` immediately (webhook stays fast). The outbox replays unfinished events on restart.
4. The queue worker runs **`pipeline.run_pipeline(event)`**, which steps through:
   1. **Dedup** (`run_registry.py`) — skip if this `run_id` was already processed.
   2. **Flakiness** (`flakiness_detector.py`) — compares against recent run history (`run_history.py`); if the
      workflow is normally green and this is a one-off (or matches infra-noise keywords), it's flaky → notify + stop.
   3. **Fetch logs** (`log_fetcher.py` via `mcp_client.py`) — download, decompress, and **slice** the failed
      job log to the error window.
   4. **Diagnose** (`log_analyst.py`) — sends the sliced log (+ the failing **test file** as context) to Gemini →
      a `Diagnosis` (`error_type`, `file`, `line`, `explanation`, `confidence`). **Confidence gate**: < 0.6 → escalate.
   5. **Attempt gate** (`run_registry.py`) — error-hash dedup (comment on the existing open PR instead of opening
      another) and a hard `MAX_PATCH_ATTEMPTS` cap so a recurring failure can't loop forever.
   6. **Patch + verify** (`code_patcher.patch_and_verify`) — see §3. Produces a fix on the rolling `agent/fixes`
      branch, opens/updates a PR (`pr_manager.py`, with risk score + labels from `pr_risk.py`), then **watches
      that PR's own CI** (`ci_verifier.py`) and only reports `[FIXED]` when it goes green.
   7. **YAML optimize** (`yaml_optimizer.py`) — independently proposes a faster workflow (job parallelization +
      dependency caching) as a separate PR.
   8. **Notify** (`notifier.py`) — Slack/Telegram report with the honest outcome.
5. A human reviews and **merges the fix PR**.

### 2B. Merge → release → deploy  (`orchestrator/cd_pipeline.py`)

6. Merge to `main` → CI passes → the **Release** workflow builds + pushes a container image tagged with the
   merge SHA → fires a `workflow_run` success for the release workflow.
7. **`router.py`** classifies it as **release-success** (matches `RELEASE_WORKFLOW_NAME`) → enqueues the CD pipeline.
8. **`cd_pipeline.run`** steps through:
   1. **Deploy guard** (`deploy_guard.py`) — an LLM reads the merged diff and returns a go/no-go + risk; a
      destructive migration, auth change, or low confidence **blocks** the deploy (fail-safe).
   2. **Deploy** (`deployer.py`) — SSHes into the GitHub Codespace target, pins `API_IMAGE` + `VERSION` in its
      `.env`, and `docker compose pull && up -d` to run the new image.
   3. **Health check** (`health_monitor.py`) — probes `BACKEND_BASE_URL` `/health` + `/version` until the
      reported commit matches the deployed SHA (or a timeout).
   4. **Rollback** (`rollback.py`) — on health failure, re-deploys the previously captured image tag.
   5. **Notify** (`notifier.py`) — final deploy outcome.
9. The dashboard's `/version` flips to the new SHA → the deploy is live.

### 2C. Live reasoning (runs alongside both pipelines)

At every meaningful step, the pipelines call **`event_publisher.publish_safe(stage, message, …)`**, which
`POST`s a reasoning event to `{BACKEND_BASE_URL}/internal/agent-event` with an `X-Agent-Token` header. The demo
backend authenticates the token, stores the event, and broadcasts it over **SSE on `/agent/events/stream`**. The
Vite/React dashboard subscribes via `EventSource` and renders the timeline + pipeline stage tracker live. Publishing
is **fire-and-forget** — it never blocks or fails a pipeline, and silently no-ops when unconfigured.

---

## 3. The patch → verify loop (the core of the CI fix)

`code_patcher.patch_and_verify()` is what makes fixes trustworthy rather than hopeful:

1. **Gather context** — fetch the failing file **plus** the failing test file **plus** the definitions of the
   first-party modules the file imports (so the model sees *both ends* of a mismatch — e.g. a type declared in
   `models.py` vs. the value assigned in `api/items.py` — instead of guessing).
2. **Generate** — Gemini returns the full corrected file (system prompt enforces minimal, targeted changes).
3. **Deterministic autofix** — strip any import the fix left unused (LLMs reliably leave dangling imports when
   they switch approaches; a tool is perfect at this where the model isn't).
4. **Apply atomically** — synthesize a unified diff locally, dry-run syntax-check it, enforce a removal cap and
   blocked-file rules, and commit all files in one atomic commit to `agent/fixes` (`pr_manager.py`).
5. **Open / update the PR** — comment on the existing PR if the same error recurred.
6. **Verify against CI** (`ci_verifier.py`) — poll the fix PR's **own** CI run until it reaches a verdict.
   - **Green** → report `[FIXED]`.
   - **Red** → re-patch using the new failing output as feedback, **against the fix branch** (so the feedback is
     coherent), up to `PATCH_VERIFY_MAX_ITERATIONS` extra tries.
   - **Inconclusive / timeout** → report **`[PATCH OPENED — CI not confirmed]`**, never a false `[FIXED]`.

**Honest labelling:** only a green CI earns `[FIXED]`; otherwise the run is flagged `[PATCH NEEDS REVIEW]` or
`[PATCH OPENED]`. The agent never claims a fix it hasn't proven.

---

## 4. Components by layer (what each does)

### Ingress — `webhook/`
| Module | Function |
|---|---|
| `server.py` | FastAPI app on `:8010`; entry point for GitHub webhooks; also exposes `/status`, `/version`, `/metrics`. |
| `validator.py` | HMAC-SHA256 verification of `X-Hub-Signature-256` (constant-time). |
| `router.py` | Filters + normalizes events into CI-failure / release-success / ignore; drops bots, forks, and `agent/*` branches. |
| `dedup.py` | In-memory LRU keyed on `X-GitHub-Delivery` to drop duplicate deliveries. |

### Orchestration — `orchestrator/`
| Module | Function |
|---|---|
| `pipeline.py` | The CI pipeline: dedup → flakiness → diagnose → gate → patch+verify → YAML optimize → notify, under a session timeout. |
| `cd_pipeline.py` | The CD pipeline: deploy_guard → deploy → health → rollback → notify. |
| `task_queue.py` | Async queue that serializes webhook events (no parallel runs) with backpressure + dead-letter. |
| `event_store.py` | Persistent sqlite-WAL outbox; events are durably stored before enqueue and replayed on restart. |
| `run_registry.py` | Persistent JSON store: run dedup, per-error-hash attempt counting, escalation, open-PR tracking. |

### Agents — `agents/` (each is a single-responsibility step)
| Module | Function |
|---|---|
| `flakiness_detector.py` | Decides flaky vs. real failure from recent pass-rate + infra-error keywords. |
| `log_analyst.py` | Log → `Diagnosis` (error type, file, line, explanation, confidence) via Gemini. |
| `code_patcher.py` | Diagnosis → fix → diff → PR; adds import context, deterministic autofix, and the verify-retry loop. |
| `ci_verifier.py` | Watches the fix PR's own CI run; returns verified / failed (+ new log) / inconclusive. |
| `yaml_optimizer.py` | Proposes faster workflow YAML (parallelization + caching) as a separate PR. |
| `deploy_guard.py` | LLM-judged go/no-go for promoting a merged PR (blocks risky/destructive changes). |
| `deployer.py` | Deploys a new image tag to the Codespace target (pins env, `docker compose up`). |
| `health_monitor.py` | Post-deploy probe of `/health` + `/version` until the SHA matches or it times out. |
| `rollback.py` | Re-deploys the previously captured image tag on health failure. |
| `notifier.py` | Sends the full report to Slack/Telegram (uses the light model for formatting). |
| `event_publisher.py` | Fire-and-forget POST of reasoning events to the dashboard backend. |

### GitHub integration — `github/`
| Module | Function |
|---|---|
| `mcp_client.py` | Manages the GitHub MCP `ClientSession`; all GitHub tool calls go through here. |
| `log_fetcher.py` | Downloads, decompresses, and slices job logs to the error window. |
| `pr_manager.py` | Rolling fix branch, atomic multi-file commits (Git Database API), PR open/update, dry-run syntax checks. |
| `pr_risk.py` | Risk scoring (LOW/MED/HIGH) + auto-labels for agent PRs. |
| `rest_api.py` | Thin REST helpers for operations where the MCP tool shape is awkward (branch SHAs, merges, labels, comments). |
| `run_history.py` | Recent-run history + pass-rate computation for flakiness. |

### LLM — `llm/`
| Module | Function |
|---|---|
| `gemini_client.py` | OpenRouter (OpenAI-compatible) chat completions; the single choke point for all model calls. |
| `rate_limiter.py` | Min-gap pacing, 429/503 exponential backoff, and a daily **cost cap** short-circuit. |
| `response_parser.py` | Parses structured agent outputs (diagnosis JSON, diffs, YAML blocks). |

### Supporting — `config/`, `models/`, `audit/`, `metrics/`
| Module | Function |
|---|---|
| `config/settings.py` | Loads + validates all configuration from `.env` (single `get_settings()`). |
| `config/constants.py` | Enums (`ErrorType`, `TaskState`, `PipelineStep`), blocked-file patterns, thresholds, branch prefixes. |
| `config/prompts.py` | All LLM system prompts (one per agent), with output contracts. |
| `models/run.py` | `WorkflowRun`, `JobLog`, `Diagnosis`, `PatchResult` (incl. `verified` / `head_sha`). |
| `models/events.py` | `WebhookPayload`, `WorkflowFailureEvent`. |
| `models/task.py` | `AgentTask` lifecycle, `NotificationPayload` (incl. honest `summary_line`). |
| `models/cd.py` | CD-side dataclasses (release event, deploy/health/rollback results). |
| `audit/` | Structured per-task logging via contextvars; every action recorded, no secrets. |
| `metrics/` | Prometheus metrics + per-model cost/token pricing. |

---

## 5. Key design rules

- **Verify before claiming fixed** — a fix is only `[FIXED]` once the PR's own CI is green.
- **Two-ended context** — the patcher sees the failing file *and* the declarations it depends on.
- **Deterministic where it can be** — unused-import cleanup and diff synthesis are done by tools, not the model.
- **Never self-trigger** — the router drops the agent's own `agent/*` branch events.
- **Never commit to `main`** — all changes land on `agent/fixes` / `agent/optimize-*` via PRs; humans merge.
- **Fail safe** — low diagnosis confidence escalates; a risky diff blocks deploy; a failed health check rolls back.
- **Bounded** — `MAX_PATCH_ATTEMPTS`, session timeout, daily cost cap, removal caps, and blocked-file patterns.
- **Observable** — audit log + Prometheus metrics + a live reasoning dashboard for every run.

---

## 6. Configuration & running (quickstart)

```bash
# Agent
cd cicd-agent && source .venv/bin/activate
WEBHOOK_PORT=8010 python main.py        # FastAPI webhook server
ngrok http --domain=<static> 8010       # public URL; register as the repo webhook (/webhook)

# Dashboard (target repo)
cd cicd-agent-demo/frontend && npm run dev   # Vite dev server (:5173), VITE_API_BASE → backend
```

Key `.env` settings (see `cicd-agent/config/settings.py`): `OPENROUTER_API_KEY`, `GITHUB_PERSONAL_ACCESS_TOKEN`,
`GITHUB_WEBHOOK_SECRET`, `GITHUB_REPO_OWNER/NAME`, `TELEGRAM_*`, and for CD/dashboard:
`BACKEND_BASE_URL`, `AGENT_SHARED_SECRET` (must match the demo backend), `CODESPACE_NAME`,
`DEPLOY_IMAGE_REPOSITORY`, `RELEASE_WORKFLOW_NAME`, and the `PATCH_VERIFY_*` knobs.

**Trigger a run:** push any commit to the demo repo's `main`, or redeliver a `workflow_run` webhook.

> Deeper conventions and the module map live in `cicd-agent/CLAUDE.md`.
