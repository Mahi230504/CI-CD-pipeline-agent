# CLAUDE.md — CI/CD Intelligence Agent

## Read this first, every session
This file is your source of truth. Before writing any code, read the relevant section. Never deviate from the architecture described here without a comment explaining why.

## What this project is
A production-grade, multi-agent CI/CD intelligence system that:
1. Detects GitHub Actions pipeline failures via webhook
2. Diagnoses root cause by reading and slicing job logs
3. Patches the failing code and opens a PR (never commits to main)
4. Optimizes the workflow YAML to reduce pipeline runtime
5. Notifies via Slack or Telegram with full report

## LLM provider
- Provider: OpenRouter (OpenAI-compatible /chat/completions over httpx). API key in OPENROUTER_API_KEY.
- Primary model: google/gemini-2.5-flash — for log analyst, code patcher, yaml optimizer
- Light model: google/gemini-2.5-flash-lite — for notifier only
- Models are OpenRouter slugs (PRIMARY_MODEL / LIGHT_MODEL in .env). Swap providers by changing the slug.
- Rate limit: paid tier — small min-gap (RATE_LIMIT_DELAY_SECONDS, default 0.5s) + 429/503 backoff-retry in rate_limiter.py. Daily $ cap via DAILY_COST_CAP_DOLLARS.
- All LLM calls MUST go through llm/gemini_client.py (get_gemini_client().generate) — never call the HTTP API directly from agents. (Module/class keep the historical "gemini" names for call-site stability; provider is OpenRouter.)
- NOTE: native MCP tool-binding (passing a ClientSession into the model) is NOT supported on OpenRouter; agents fetch GitHub data via github/mcp_client and pass it as text.

## GitHub integration
- GitHub MCP server via HTTP transport (PAT authentication)
- Connection command: claude mcp add -s user --transport http github https://api.githubcopilot.com/mcp -H "Authorization: Bearer $GITHUB_PERSONAL_ACCESS_TOKEN"
- All GitHub operations go through github/mcp_client.py — never call GitHub REST API directly
- PAT required permissions: Actions(read), Contents(read+write), Metadata(read), Pull requests(read+write), Workflows(read+write), Checks(read)

## Project structure
```
cicd-agent/
├── CLAUDE.md                    ← you are here
├── main.py                      ← entry point, startup checks, starts server
├── cli.py                       ← manual trigger, status, logs commands
├── test_local.py                ← integration test, no GitHub needed
├── requirements.txt
├── pyproject.toml
├── Makefile
├── .env.example
├── .gitignore
├── config/
│   ├── settings.py              ← Settings dataclass, loads .env, validates on startup
│   ├── constants.py             ← Enums: ErrorType, TaskState, blocked file patterns
│   └── prompts.py               ← All LLM system prompts — one constant per agent
├── models/
│   ├── run.py                   ← WorkflowRun, JobLog, Diagnosis, PatchResult dataclasses
│   ├── events.py                ← WebhookPayload, WorkflowFailureEvent dataclasses
│   └── task.py                  ← AgentTask dataclass, OptimizationResult dataclass
├── llm/
│   ├── rate_limiter.py          ← Semaphore + 7s gap + daily counter + backoff on 429
│   ├── gemini_client.py         ← Wraps google-genai, rate-limited, strips PII
│   └── response_parser.py       ← parse_diagnosis, parse_diff, parse_yaml_blocks, validate_json
├── github/
│   ├── mcp_client.py            ← MCP ClientSession lifecycle, all GitHub tool calls
│   ├── log_fetcher.py           ← Download, decompress, slice logs — key: slice_log()
│   ├── pr_manager.py            ← create_branch, apply_diff, commit_and_push, open_pr
│   └── run_history.py           ← Last N runs, pass rate, workflow YAML reader
├── agents/
│   ├── flakiness_detector.py    ← Checks pass rate + infra error keywords
│   ├── log_analyst.py           ← Log → Gemini → Diagnosis (with confidence gate)
│   ├── code_patcher.py          ← Diagnosis → Gemini → diff → PR
│   ├── yaml_optimizer.py        ← YAML → networkx graph → Gemini → optimized YAML → PR
│   └── notifier.py              ← Slack / Telegram notification, uses flash-lite
├── orchestrator/
│   ├── pipeline.py              ← The main run() function: full 6-step flow
│   ├── run_registry.py          ← JSON store: dedup, attempt counting, escalation
│   └── task_queue.py            ← asyncio.Queue, worker, backpressure, dead letter
├── webhook/
│   ├── server.py                ← FastAPI app, lifespan, all routes
│   ├── validator.py             ← HMAC-SHA256 verification of X-Hub-Signature-256
│   └── router.py                ← Filter events, parse payload, enqueue
├── audit/
│   └── logger.py                ← JSON lines, daily rotation, no secrets in logs
├── tests/
│   ├── unit/
│   │   ├── test_log_slicer.py
│   │   ├── test_run_registry.py
│   │   ├── test_hmac_validator.py
│   │   └── test_yaml_optimizer.py
│   └── fixtures/                ← sample_webhook.json, sample_log.txt, etc.
└── scripts/
    └── setup_demo.sh            ← Creates demo repo, registers webhook, pushes broken workflow
```

## The pipeline flow (implement in orchestrator/pipeline.py)
```
webhook received
  → validator.py: HMAC check — 403 if invalid
  → router.py: filter to workflow_run + conclusion=failure only
  → task_queue.py: enqueue, return 200 immediately
  → pipeline.run(event):
      1. run_registry.is_duplicate() → skip if already processed
      2. flakiness_detector.check() → if flaky: notify + return
      3. log_analyst.diagnose() → get Diagnosis
         └── if confidence < 0.6: escalate, notify, return
      4. run_registry.get_attempt_count(error_hash)
         └── if attempts >= MAX_PATCH_ATTEMPTS: escalate, notify, return
      5. code_patcher.patch() → PatchResult
         └── run_registry.increment_attempt()
      6. yaml_optimizer.optimize() → OptimizationResult (always runs)
      7. notifier.send() → full report
      8. audit every step boundary
```

## Security rules — enforce in EVERY file
1. Zero hardcoded secrets — all from .env via settings.py
2. Never commit to main — all changes via PRs on agent/fix-{run_id} branches
3. BLOCKED_FILE_PATTERNS in constants.py — code_patcher must check before any write
4. MAX_PATCH_ATTEMPTS = 2 — after this, escalate and stop forever for this error hash
5. HMAC validation on every webhook — constant-time comparison only
6. Ignore webhook events from: forks, dependabot, bot actors
7. Never log: raw file contents, API keys, full log text — only metadata
8. rate_limiter.py wraps ALL Gemini calls — no direct SDK calls from agents

## Gemini output contracts (enforce in prompts.py + response_parser.py)
- log_analyst → JSON: {"error_type": str, "file": str, "line": int, "explanation": str, "confidence": float}
- code_patcher → unified diff only, no prose before or after
- yaml_optimizer → two YAML code blocks: first=original, second=optimized, then a JSON summary
- flakiness_detector → JSON: {"is_flaky": bool, "reason": str, "pass_rate": float}
- notifier → plain text, no JSON

## Error handling rules
- All async functions: wrap in try/except, log to audit, re-raise only if fatal
- 429 from Gemini: exponential backoff 2^n seconds, max 3 retries, then raise
- GitHub MCP errors: log + escalate (do not retry indefinitely)
- Diff apply failure: log + open PR with the raw diff as a comment instead
- Webhook with invalid JSON: return 400, log, do not crash server

## What NOT to do
- Do not use google-generativeai (old SDK) — use google-genai only
- Do not call GitHub REST API directly — use MCP client only
- Do not run agents in parallel on free tier — sequential only (rate limiter enforces this)
- Do not merge PRs — agent opens PRs, humans merge
- Do not touch .env, *.pem, *secret*, *password* files ever
- Do not use print() for logging — use audit/logger.py or Python logging module
