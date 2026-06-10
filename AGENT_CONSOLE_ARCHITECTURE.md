# Agent Console — MVP Architecture

> Final, signed-off-ready architecture for the **Agent Console** MVP. Synthesizes five slice
> designs and resolves every `must_fix_before_build` and `contract_conflict` from the adversarial
> critique. Decisions are **made**, not deferred; over-engineering is cut into §8.
>
> Grounding note: every file path, signature, column, and literal string below was read in the
> live tree (`cicd-agent/`, `cicd-agent-demo/`). Verified facts that drove keystone decisions:
> - The demo backend (`cicd-agent-demo/`) **already** depends on `sqlalchemy>=2.0.30, asyncpg, alembic, redis>=5.0, sse-starlette` (its `pyproject.toml`). The agent (`cicd-agent/requirements.txt`) has **none** of these and **no SSE route / no Redis client** — its `event_publisher.py` only POSTs to the demo backend.
> - The only SSE endpoint + Redis broker live in `cicd-agent-demo/app/api/agent_events.py`. `AgentEvent` is frozen at `{stage(≤64), level∈{info,warn,error,success}, message(≤2000), metadata:dict, timestamp}`; `metadata` is `json.dumps`'d into a single Redis stream field.
> - Demo ORM uses `Mapped[int] = mapped_column(Integer, primary_key=True)` for every PK.
> - The demo repo's CI workflow is literally `name: CI`; release is `name: Release`.
> - `github/pr_manager.apply_patch_set(diagnosis, diff, run_id, head_sha, mcp_client, base_ref=None)` **hardcodes** `ROLLING_PATCH_BRANCH = "agent/fixes"` (no branch param).
> - `agents/ci_verifier._select_run` filters `r["head_branch"] == ROLLING_PATCH_BRANCH and r["name"] == workflow_name`.
> - **No merge capability exists** anywhere in `github/rest_api.py` or `github/mcp_client.py`.

---

## 0. Executive summary

**What we're building this milestone:** a new standalone **Agent Console** — a chat-first operator
surface where a user types a request ("add a low-stock endpoint and deploy it") and watches the
existing CI/CD agent diagnose, write code, open a **CI-verified** PR, and (on approval or AUTO)
merge + deploy to a live URL — narrated live, step by step, with a running cost/ROI ticker.

**One-sentence pitch:** *Type a sentence, watch an autonomous agent ship it — diagnosed, patched,
CI-verified, deployed, and priced — live.*

**The core loop (the only thing this milestone ships):**
`chat → classify intent → read files → generate edit → diff preview → open PR → verify the PR's own CI → AUTO-gate (or human approve in chat) → merge → deploy → live URL`, every step streamed over the existing SSE channel and persisted for replay.

**This is a conversational orchestration layer over machinery that already exists.** We reuse
`code_patcher`, `ci_verifier`, `pr_risk`, `deploy_guard`, `deployer`/`health_monitor`/`rollback`,
`pr_manager`, `gemini_client`, the `task_queue`, and the Redis-backed SSE fan-out. The genuinely
new code is: a `ChatOrchestrator`, a `chat_editor` (instruction→diff), an `autonomy_policy` pure
function, **one** new privileged GitHub action (`merge_pull_request`), a per-turn chat branch, a
unified SQLAlchemy data layer, a thin set of `/api/console/*` routes, and a new React SPA.

### The three keystone decisions (resolving the critique's blockers)

1. **WHERE IT RUNS — RESOLVED.** The console API + SSE are hosted on the **demo backend
   (`cicd-agent-demo/app`)**, which already owns Redis, SQLAlchemy, Alembic, async sessions, and
   the live `/agent/events/stream`. The **agent (`cicd-agent/`) stays a pure worker** that POSTs
   events. This consciously **overrides the locked "extend `webhook/server.py`" decision** because
   that decision predates the discovery that the SSE broker lives in a different process; rebuilding
   Redis + SSE + SQLAlchemy inside the agent to honor the letter of the lock is pure duplication.
   *See §7 R1 — this is the one lock we ask the user to ratify.* The agent's `webhook/server.py` is
   still extended for one thing only: it keeps emitting events via `event_publisher`.

2. **ONE DATA LAYER — RESOLVED.** Slices 2 and 3 proposed two incompatible schemas. We adopt **one**
   SQLAlchemy 2.0 + Alembic model set living in the **demo backend** (`cicd-agent-demo/app/models/console.py`),
   using **integer surrogate PKs** (demo convention) with **opaque string public IDs** derived from
   them (`cnv_<id>`, `msg_<id>`, …) at the API boundary. Legacy `run_registry.json`/JSONL migration
   and `cost_ledger` are **deferred** (§8) — the core loop does not read them.

3. **THE AUTO-SHIP DEMO IS REAL — RESOLVED.** `pr_risk` forces `HIGH` on `*main.py`/`*config.py`
   edits, which "add an endpoint" touches — so the canonical demo would **never** auto-ship.
   Decision: **(a)** the flagship demo is scripted around the **human-approve "Ship it" click**
   (one tap → ShipMoment), and **(b)** chat-authored edits target a **new endpoint in a dedicated
   router file** (`app/api/console_demo.py`-style) so realistic features can be `LOW` risk and a
   second demo prompt *does* auto-ship. We do **not** weaken the global sensitive-path list.

### Explicitly deferred (see §8)
Multi-step agent-mode planner; proactive war-room push-into-chat; ask-your-pipeline "why" queries;
per-token live cost narration (degraded to end-of-run count-up for MVP); GCP Cloud Run provider;
per-tenant rate-buckets / cost-caps / Postgres RLS; legacy `run_registry.json`+JSONL migration;
`cost_ledger`; `DeploymentProvider`/`LLMProvider` as runtime-selected classes (kept as typing seams only).

---

## 1. System architecture

### 1.1 Component diagram (ASCII)

```
                          ┌──────────────────────────────────────────────────┐
   Browser (Agent Console SPA, Vite/React/TS/Tailwind, its own Vercel project) │
   - Chat panel · Pipeline stepper · ROI/war-room · AUTO toggle               │
                          └───────────────┬───────────────────┬──────────────┘
                            POST /api/console/* (REST)         │ GET /agent/events/stream (SSE)
                                           │                   │
            ┌──────────────────────────────▼───────────────────▼───────────────────────────┐
            │  DEMO BACKEND  (cicd-agent-demo/app)  ── hosts API + SSE + DB + Redis          │
            │                                                                                │
            │   app/api/console/        app/api/agent_events.py        app/db.py (async SA)  │
            │   ├ chat.py  (POST turn)  ├ POST /internal/agent-event ──┐   Postgres / SQLite │
            │   ├ stream.py(SSE relay)  └ GET  /agent/events/stream     │   (DATABASE_URL)    │
            │   ├ approvals.py                       │ XADD             │                     │
            │   ├ runs.py / metrics.py        ┌──────▼──────┐    ┌──────▼──────────────────┐ │
            │   └ config.py                   │ Redis stream│    │ console tables (§3)     │ │
            │        │ enqueue                │ agent:events│    │ conversations, messages,│ │
            │        ▼                        └──────┬──────┘    │ turns, runs, run_events,│ │
            │   app/worker/consumer.py (Redis Streams group)     │ tenants, repos          │ │
            └────────┬───────────────────────────────┬──────────┴─────────────────────────┘ │
                     │ ChatTaskEvent (Redis Stream)   │ SSE tail (XREAD)                       │
                     ▼                                                                          
   ┌────────────────────────────────────────────────────────────────────────────────────────┐
   │  AGENT  (cicd-agent/)  ── pure worker; no SSE route, no DB-of-record for console          │
   │                                                                                            │
   │  orchestrator/task_queue.py  ──dispatch──►  agents/chat_orchestrator.ChatOrchestrator      │
   │     QueueEvent = WorkflowFailureEvent | ReleaseSuccessEvent | ChatTaskEvent  (3rd branch)  │
   │                                                                                            │
   │  ChatOrchestrator.handle_turn():                                                           │
   │    classify_intent (LLM) → chat_editor.generate_edit (LLM) → apply_patch_set(branch=…)     │
   │      → ci_verifier.verify_patch_ci → autonomy_policy.should_ship_unattended                │
   │      → [merge_pull_request] → deployer.deploy → health_monitor.check → live URL            │
   │                                                                                            │
   │    every step → event_publisher.publish_safe ──POST X-Agent-Token──► demo /internal/...    │
   │    reuses: code_patcher helpers · pr_manager · pr_risk · deploy_guard · gemini_client       │
   └────────────────────────────────────────────────────────────────────────────────────────┘
```

**Two processes, one Redis, one DB.** The browser talks to the demo backend for both REST and SSE
(single origin → no two-origin CORS problem; critique gap closed). The agent is a stateless worker
fed by a Redis Stream and emitting events back through the existing `/internal/agent-event` POST.
Webhook-driven CI/CD keeps working exactly as today (the agent's own `webhook/server.py` is untouched
except that pipelines now also stamp `conversation_id`/`run_id` into event metadata — see §1.3).

### 1.2 Request / turn data-flow for the core loop

```
1. SPA  POST /api/console/conversations/{cid}/turns
        body { message, idempotency_key, autonomy }                        → 202 {turn_id, run_id, stream_url}
2. demo backend: INSERT message(role=user) + turn(status=QUEUED);
        XADD agent:tasks  ChatTaskEvent{tenant_id, conversation_id, turn_id, run_id, message, autonomy, kind}
3. SPA  opens GET /agent/events/stream?conversation_id={cid}  (SSE; filtered server-side by tenant+conv)
4. agent worker (consumer group) picks up ChatTaskEvent → ChatOrchestrator.handle_turn()
        a. resolve TenantContext(tenant_id)  → repo, ci_workflow_name="CI", default_branch
        b. classify_intent(message)          → FEATURE | BUGFIX | DEPLOY | QUESTION | APPROVE | REJECT
        c. (FEATURE) read target files via MCP → chat_editor.generate_edit → EditProposal{diff, files}
        d. emit stage=diff_preview  (TRUNCATED preview in metadata + full diff persisted as a message)
        e. apply_patch_set(synthetic_diagnosis, diff, run_id, head_sha, mcp, branch=f"agent/chat-{turn_id}")
                                              → PatchResult{pr_number, pr_url, head_sha}
        f. verify_patch_ci(patch_result, synthetic_event(workflow_name="CI", branch=chat_branch), mcp)
                                              → VerifyResult{verified: True|False|None}
        g. risk    = pr_risk.assess_risk(synthetic_diagnosis, patch_set.paths)
           verdict = deploy_guard.judge(pr_number, title, body, files, diff_summary, head_sha)
           decision= autonomy_policy.should_ship_unattended(verify, risk, verdict, autonomy)
        h. if decision.ship:  merge_pull_request(pr_number) → deploy → health → emit live_url → turn=DONE
           else: turn=AWAITING_APPROVAL (resume_token); emit stage=awaiting_approval; RETURN (worker freed)
5. (paused path) SPA POST /api/console/conversations/{cid}/turns {message:"approve", ...}  OR a future
        explicit /turns with kind=approve  → demo backend XADD ChatTaskEvent{kind:"approve", turn_id}
        → worker _resume_turn: assert AWAITING_APPROVAL → merge → deploy → live URL → DONE
6. throughout: each emitted event is (i) POSTed to /internal/agent-event → Redis XADD → SSE to SPA,
        and (ii) INSERTed into run_events by the ingest route → durable replay on reconnect.
```

### 1.3 How it reuses existing agent modules (verbatim unless noted)

| Existing module | Reused for | Change required |
|---|---|---|
| `agents/code_patcher.py` helpers (`_format_with_line_numbers`, `_extract_full_file`, `_strip_unused_imports`, `_synthesize_diff`, import-context fetch) | `chat_editor.generate_edit` builds an `EditProposal` from an **instruction** (no `Diagnosis`) | none — called as helpers |
| `agents/ci_verifier.verify_patch_ci(patch_result, event, mcp, settings)` | verify the chat PR's own CI | **none to the fn**, but it filters `head_branch == ROLLING_PATCH_BRANCH` — see §1.4 branch fix |
| `github/pr_manager.apply_patch_set(...)` | commit + open/append PR; enforces `BLOCKED_FILE_PATTERNS` on every path | **add a `branch:` param** (currently hardcodes `ROLLING_PATCH_BRANCH`) — see §1.4 |
| `github/pr_risk.assess_risk(diagnosis, paths)` | one leg of the AUTO gate | none — fed a synthetic `Diagnosis` |
| `agents/deploy_guard.judge(...)` | second AUTO leg (always returns a verdict, never raises) | none |
| `agents/deployer` / `health_monitor` / `rollback` | deploy step → live URL → auto-rollback | none — called directly (no provider class for MVP) |
| `llm/gemini_client.get_gemini_client().generate(...)` | intent classify + edit generation | none — used with `agent='chat_intent'` / `'chat_editor'` |
| `orchestrator/task_queue.py` | serialize chat turns with CI/CD work | **add 3rd dispatch branch** for `ChatTaskEvent` (see §1.4) |
| `agents/event_publisher.publish_safe(...)` | stream every step | none — chat just adds correlation keys to `metadata` |
| `webhook/router.py` / `pipeline.py` / `cd_pipeline.py` | webhook CI/CD path unchanged | pipelines stamp `conversation_id`/`run_id` into `_publish` metadata (already stamp `run_id`) |

### 1.4 The three small but load-bearing agent changes (each owned, none assumed)

1. **`github/rest_api.merge_pull_request(pr_number, *, merge_method="squash", sha=None) -> tuple[bool, str]`**
   — REST `PUT /repos/{owner}/{name}/pulls/{n}/merge`. The **only** new privileged write. Owned by
   the orchestrator slice. **Degrade rule:** if the PAT lacks merge rights or branch protection
   refuses (403/405/409), it returns `(False, reason)` and the turn flips to `AWAITING_APPROVAL`
   with that reason — it **never** raises or silently fails. Verified pre-build against the live repo
   (§6 build order step 0; §7 R5).

2. **`apply_patch_set` gains `branch: str = ROLLING_PATCH_BRANCH`** (back-compatible default keeps the
   CI path identical). `build_patch_set` and the PR-open path thread `branch` through. Chat passes
   `branch=f"agent/chat-{turn_id}"` so a chat feature **never** collides with the CI auto-fix PR on
   `agent/fixes`. `ci_verifier._select_run` similarly gains a `head_branch` parameter (default
   `ROLLING_PATCH_BRANCH`) so it can match the chat branch's run. *This is the fix for critique
   "single rolling branch collision".*

3. **`orchestrator/task_queue.py`**: `QueueEvent = WorkflowFailureEvent | ReleaseSuccessEvent | ChatTaskEvent`
   and `_worker` gets an **explicit third branch checked first**:
   ```python
   if isinstance(event, ChatTaskEvent):
       await chat_orchestrator.handle_turn(event)        # NOT through event_store outbox
   elif isinstance(event, ReleaseSuccessEvent):
       await run_cd_pipeline(event)
   else:
       await run_pipeline(event)
   ```
   Chat turns are durable in the `turns` table (status machine), **not** in the integer-`run_id`
   `queued_events` outbox — no UNIQUE collision. *Resolves the name conflict (`ChatActionEvent` vs
   `ChatTaskEvent`) → **canonical name: `ChatTaskEvent`**, defined in `cicd-agent/models/chat.py`.*

### 1.5 The provider + deployment seams (typing-only for MVP)

```python
# cicd-agent/llm/provider.py — pure typing Protocol; GeminiClient already satisfies it. FREE.
class LLMProvider(Protocol):
    async def generate(self, prompt: str, system_prompt: str, agent: str,
                       use_light_model: bool = False, temperature: float = 0.1,
                       strip_pii: bool = False) -> str: ...
def get_llm_provider() -> LLMProvider: return get_gemini_client()
```
The **`DeploymentProvider` class is NOT built** for MVP (critique over-engineering). The orchestrator
calls `deployer.deploy` / `health_monitor.check` / `rollback.rollback_to` **directly**. The seam is
documented in §8 as a one-class refactor when GCP Cloud Run lands. The `repos.deploy_provider` column
exists (seam) but only `"codespace"` is read.

### 1.6 The AUTO-toggle decision flow (one pure function — the single source of truth)

```python
# cicd-agent/agents/autonomy_policy.py
@dataclass(frozen=True)
class ShipDecision:
    ship: bool
    reason: str
    gate: Literal["auto", "manual"]

def should_ship_unattended(verify: VerifyResult, risk: RiskAssessment,
                           verdict: DeployVerdict, autonomy: str) -> ShipDecision:
    if autonomy != "auto":
        return ShipDecision(False, "Autonomy is MANUAL — pausing for approval.", "manual")
    if verify.verified is not True:
        return ShipDecision(False, f"CI not green ({verify.detail}) — needs human review.", "manual")
    if risk.level != RiskLevel.LOW:
        return ShipDecision(False, f"Risk is {risk.level.value} — needs human review.", "manual")
    if not verdict.approve:
        return ShipDecision(False, f"deploy_guard blocked: {verdict.reason}", "manual")
    if not verdict.is_high_confidence:                      # confidence >= 0.6
        return ShipDecision(False, f"deploy_guard low confidence ({verdict.confidence:.2f}).", "manual")
    return ShipDecision(True, "Verified-green + low-risk + guard-approved — shipping unattended.", "auto")
```

| autonomy | verify.verified | risk.level | verdict.approve | conf≥0.6 | → ship |
|---|---|---|---|---|---|
| auto | True | low | True | True | **YES** |
| auto | True | medium/high | * | * | no → pause |
| auto | False/None | * | * | * | no → pause |
| auto | True | low | False | * | no → pause |
| manual | * | * | * | * | no → pause |

**Fail-closed by construction.** A degraded LLM or ambiguous diff makes `deploy_guard` block and
`pr_risk` escalate to HIGH → the run pauses, never ships. **Authority of the AUTO toggle** (resolving
the slice-4-vs-slice-2/3/5 conflict): the **stored `repos.autonomy_mode` is authoritative**; the
SPA's per-message `autonomy` field is a *convenience override for that one turn only* and the backend
clamps it (a turn may not be MORE autonomous than the repo allows is **not** enforced — the per-turn
value simply wins for that turn, and the toggle PATCH persists the default). The `ShipDecision` is
computed on the agent side with the value carried in `ChatTaskEvent.autonomy`.

---

## 2. File structure

### 2.1 Backend — agent worker (`cicd-agent/`) — NEW and CHANGED

```
cicd-agent/
  agents/
    chat_orchestrator.py        NEW  turn lifecycle engine; handle_turn(); never raises
    chat_editor.py              NEW  instruction → EditProposal (reuses code_patcher helpers)
    autonomy_policy.py          NEW  should_ship_unattended() pure decision (§1.6)
  llm/
    provider.py                 NEW  LLMProvider Protocol + get_llm_provider() (typing seam)
  models/
    chat.py                     NEW  ChatTaskEvent, EditProposal, ChatIntent, TurnStatus, ShipDecision
  github/
    rest_api.py                 CHG  + merge_pull_request(pr_number, *, merge_method, sha) -> (bool, str)
    pr_manager.py               CHG  apply_patch_set/build_patch_set gain branch=ROLLING_PATCH_BRANCH param
  agents/
    ci_verifier.py              CHG  _select_run gains head_branch=ROLLING_PATCH_BRANCH param
  orchestrator/
    task_queue.py               CHG  QueueEvent += ChatTaskEvent; explicit 3rd dispatch branch
    pipeline.py                 CHG  _publish() stamps conversation_id (when run is chat-initiated)
    cd_pipeline.py              CHG  _publish() stamps conversation_id
  config/
    prompts.py                  CHG  + CHAT_INTENT_SYSTEM_PROMPT, CHAT_EDITOR_SYSTEM_PROMPT
    settings.py                 CHG  + chat_enabled, chat_task_stream_key, conversation_api_base
  webhook/
    server.py                   CHG  lifespan inits ChatOrchestrator singleton + a Redis consumer task
```
> No `task_planner.py` (intent classify IS the plan for MVP; §8). No `deploy_provider.py` class.
> No `store/` package in the agent — the data layer lives in the demo backend (keystone #1).

### 2.2 Backend — console host (`cicd-agent-demo/app/`) — NEW and CHANGED

```
cicd-agent-demo/app/
  models/
    console.py                  NEW  SQLAlchemy ORM: Tenant, Repo, Conversation, Message, Turn,
                                     Run, RunEvent (the ONE data layer — §3)
  api/
    console/
      __init__.py               NEW  APIRouter(prefix="/api/console"); mounts the routers below
      deps.py                   NEW  get_current_tenant() -> Tenant (returns "default" now), pagination
      schemas.py                NEW  Pydantic request/response models (§4); public-id (de)serialization
      chat.py                   NEW  POST /conversations, POST /conversations/{cid}/turns, GETs
      stream.py                 NEW  GET /conversations/{cid}/events (SSE relay, filtered)
      approvals.py              NEW  POST /conversations/{cid}/turns (kind=approve|reject) handler logic
      runs.py                   NEW  GET /runs/{rid} (stepper resync fallback)
      metrics.py                NEW  GET /metrics/roi (single endpoint; MTTR folded in)
      config.py                 NEW  GET/PATCH /config (repo selector + AUTO toggle)
      enqueue.py                NEW  enqueue_chat_task(): XADD to agent:tasks Redis stream
    agent_events.py             CHG  ingest also INSERTs run_events when metadata.run_id present;
                                     metadata size cap mirrors the 1800-char message clip
  repositories/
    console_repo.py             NEW  async DAL: conversations/messages/turns/runs/run_events CRUD
  main.py                       CHG  include console router; CORS already covers the SPA origin
  config.py                     CHG  + agent_tasks_stream_key, console_origin
alembic/versions/
  0003_console_tables.py        NEW  creates the 7 console tables + indexes + seeds default tenant/repo
app/worker/
  consumer.py                   CHG  (agent side reads agent:tasks; demo consumer unchanged for items)
```

### 2.3 Frontend — NEW standalone SPA (`cicd-agent-demo/console/`)

```
cicd-agent-demo/console/
  index.html                    NEW  title "Agent Console"
  package.json                  NEW  name "agent-console"; same deps/scripts as frontend/
  vite.config.ts                NEW  [react(), tailwindcss()]; server.port 5174
  tsconfig.json                 NEW  copied verbatim from frontend/tsconfig.json
  vercel.json                   NEW  SPA rewrite all → /index.html
  .env.example                  NEW  VITE_API_BASE=...
  src/
    main.tsx                    NEW  StrictMode + QueryClientProvider
    App.tsx                     NEW  renders <Console/>; useHashConversation for #/c/<id> resume
    index.css                   NEW  frontend/index.css verbatim + 2 keyframes (flow, ship-glow)
    lib/
      api.ts                    NEW  get/post + console endpoints (§4 paths, exact)
      queryClient.ts            NEW  copied verbatim from frontend/
      ids.ts                    NEW  publicId helpers (parse "cnv_123" ⇄ 123) — matches API boundary
      format.ts                 NEW  formatTime/usd/relTokens
    hooks/
      useEventSource.ts         NEW  copied verbatim from frontend/
      useConsoleStream.ts       NEW  wraps useEventSource, scopes by conversation_id
      useChat.ts                NEW  useReducer merging history + streamed turns (REPLACE convention)
      useRun.ts                 NEW  RunView projection from events + GET /runs/{id} resync
      useAutonomy.ts            NEW  AUTO toggle state; reads RepoConfig default, PATCHes /config
    types/
      console.ts                NEW  ALL TS contracts mirroring §4 Pydantic (string ids)
    pages/Console.tsx           NEW  3-zone layout; owns the single SSE subscription
    components/
      TopBar.tsx                NEW  repo selector + AUTO toggle + ROI badges
      RepoSelector.tsx          NEW
      AutonomyToggle.tsx        NEW
      SavingsBadges.tsx         NEW
      chat/{ChatPanel,MessageList,ChatMessage,UserBubble,AgentTurn,DiffCard,
            ApprovalCard,LiveUrlCard,ThinkingIndicator,Composer}.tsx        NEW
      pipeline/{PipelinePanel,PipelineStepper,StepNode,FlowConnector,
                TokenCostTicker,ShipMoment}.tsx                              NEW
      war_room/{RoiPanel,StatTile,IncidentsFeed}.tsx                         NEW
```

---

## 3. Database schema

**ONE data layer.** SQLAlchemy 2.0 (async) + Alembic, in the **demo backend** (which already ships
these deps), cloning `cicd-agent-demo/app/models.py` conventions verbatim:
`Mapped[int] = mapped_column(Integer, primary_key=True)`, `DateTime(timezone=True), server_default=func.now()`,
`ForeignKey(..., ondelete=...)`, enums as `String(32)` + Python `StrEnum` (portable; not native DB enums).
**SQLite↔Postgres is one env var** (`DATABASE_URL`); the demo's `app/db.py` async engine and
`alembic/env.py` (reads `database_url_sync`) are reused unchanged.

**MVP table set = 7** (the core loop only). `pr_actions`, `deployments`, `cost_ledger`, `audit_log`,
`error_attempts`, and legacy-store migration are **deferred** (§8). The columns the loop needs are
folded onto `runs` (pr_number, verified, live_url, etc.).

**ID strategy (resolves slice 2/3/4 conflict):** integer surrogate PK internally; the API serializes
to **opaque prefixed strings** (`cnv_<id>`, `msg_<id>`, `turn_<id>`, `run_<id>`). `run_id` semantics
are pinned in §3.8.

### 3.1 `tenants` — the multi-tenant seam (one row now)
| col | type | notes |
|---|---|---|
| id | Integer PK | |
| slug | String(64) UNIQUE NOT NULL | `'default'` |
| name | String(200) NOT NULL | `'Default Workspace'` |
| created_at | TIMESTAMPTZ NOT NULL server_default now() | |

Seed: `(1, 'default', 'Default Workspace')`.

### 3.2 `repos` — per-tenant config; repo is NEVER hardcoded in business logic
| col | type | notes |
|---|---|---|
| id | Integer PK | |
| tenant_id | Integer FK→tenants(id) ON DELETE CASCADE, **indexed, NOT NULL, first col** | the seam |
| owner | String(100) NOT NULL | `'Mahi230504'` |
| name | String(200) NOT NULL | `'cicd-agent-demo'` |
| default_branch | String(100) NOT NULL DEFAULT `'main'` | |
| ci_workflow_name | String(200) NOT NULL DEFAULT `'CI'` | **verified literal** — `ci_verifier` matches on it |
| release_workflow_name | String(200) NOT NULL DEFAULT `'Release'` | case-sensitive (memory note) |
| autonomy_mode | String(16) NOT NULL DEFAULT `'manual'` CHECK in (`'auto'`,`'manual'`) | **the AUTO toggle** |
| deploy_provider | String(32) NOT NULL DEFAULT `'codespace'` | seam only |
| deploy_config | JSON | `{codespace_name, image_repo, base_url, workdir}` |
| live_url | String(500) NULL | current deployed URL |
| created_at / updated_at | TIMESTAMPTZ | |

Index `(tenant_id)`, UNIQUE `(tenant_id, owner, name)`. Seed one row from the agent's current env
(`GITHUB_REPO_OWNER/NAME`, `'CI'`, `'Release'`).

> **MCP caveat — resolved honestly (critique R "tenant seam is cosmetic"):** `GitHubMCPClient.__aenter__`,
> `rest_api._repo_path`, and `run_history` still read `owner/name` from `Settings`. For **single-tenant
> MVP this is acceptable and intentional**: the one `repos` row is seeded to **equal** the env repo, so
> DB and Settings agree. We do **not** claim true repo-from-DB I/O. Threading repo through MCPClient is
> scoped as a fast-follow (§8) — the column is the seam, the refactor is later.

### 3.3 `conversations`
| col | type | notes |
|---|---|---|
| id | Integer PK | public `cnv_<id>` |
| tenant_id | Integer FK→tenants, indexed NOT NULL | |
| repo_id | Integer FK→repos ON DELETE CASCADE | |
| title | String(300) NULL | first user message, truncated |
| status | String(16) NOT NULL DEFAULT `'active'` CHECK in (`'active'`,`'paused'`,`'archived'`) | |
| created_at / updated_at | TIMESTAMPTZ | |

Index `(tenant_id, repo_id, updated_at DESC)`.

### 3.4 `messages` — chat log incl. diff/approval payloads; **idempotency lives here**
| col | type | notes |
|---|---|---|
| id | Integer PK | public `msg_<id>` |
| tenant_id | Integer FK, indexed NOT NULL | |
| conversation_id | Integer FK→conversations ON DELETE CASCADE | |
| seq | Integer NOT NULL | monotonic per conversation → ordered replay |
| role | String(16) NOT NULL CHECK in (`'user'`,`'assistant'`,`'system'`) | |
| kind | String(20) NOT NULL DEFAULT `'text'` CHECK in (`'text'`,`'step'`,`'diff'`,`'approval'`,`'live_url'`,`'status'`) | |
| content | TEXT NOT NULL DEFAULT `''` | full prose / **full diff text lives here, NOT in SSE** |
| payload | JSON NULL | diff files list / approval fields / live_url |
| run_id | Integer FK→runs ON DELETE SET NULL | links the assistant turn to its run |
| idempotency_key | String(64) NULL | client UUID per user turn |
| created_at | TIMESTAMPTZ | |

Index `(conversation_id, seq)`. **UNIQUE `(tenant_id, conversation_id, idempotency_key)`** → a retried
POST is a no-op (mirrors `event_store`'s `INSERT OR IGNORE` on `run_id`).

### 3.5 `turns` — the chat-turn state machine (pause/resume lives here, NOT a separate approvals table)
*(Resolves the slice-1-vs-slice-3 pause/resume conflict: pause = a DB row state, worker freed; resume
= a re-enqueued event. There is **no** held coroutine and **no** `asyncio.Event` in the worker.)*

| col | type | notes |
|---|---|---|
| id | Integer PK | public `turn_<id>` |
| tenant_id | Integer FK, indexed NOT NULL | |
| conversation_id | Integer FK→conversations ON DELETE CASCADE | |
| run_id | Integer FK→runs ON DELETE SET NULL | |
| intent | String(16) NULL | classified: feature/bugfix/deploy/question/approve/reject |
| status | String(20) NOT NULL DEFAULT `'queued'` CHECK in (`'queued'`,`'running'`,`'awaiting_approval'`,`'merging'`,`'deploying'`,`'done'`,`'failed'`,`'rejected'`) | the **canonical turn vocabulary** |
| pr_number | Integer NULL | |
| pr_url | String(500) NULL | |
| resume_token | String(64) NULL | UUID set on pause; cleared first on resume (idempotent double-click) |
| error | String(1000) NULL | |
| autonomy | String(16) NOT NULL | the per-turn value used for the ShipDecision |
| created_at / updated_at | TIMESTAMPTZ | |

Index `(conversation_id, created_at)`, `(status)`.

### 3.6 `runs` — the CI/CD run spine (one name, one PK; resolves `runs` vs `pipeline_runs`)
| col | type | notes |
|---|---|---|
| id | Integer PK | **the public `run_id` = `run_<id>`** (NOT the GitHub id) |
| tenant_id | Integer FK, indexed NOT NULL | |
| repo_id | Integer FK→repos | |
| conversation_id | Integer FK→conversations ON DELETE SET NULL | NULL for webhook-triggered |
| kind | String(8) NOT NULL CHECK in (`'ci'`,`'cd'`) | |
| trigger | String(16) NOT NULL DEFAULT `'webhook'` CHECK in (`'webhook'`,`'chat'`) | |
| gh_run_id | BIGINT NULL | GitHub Actions run id (external; dedup key for webhook path) |
| workflow_name / branch / head_sha / html_url | String | |
| status | String(16) NOT NULL DEFAULT `'running'` CHECK in (`'running'`,`'verified'`,`'escalated'`,`'failed'`,`'deployed'`,`'rolled_back'`,`'timed_out'`,`'deduped'`,`'flaky'`) | maps 1:1 to `pipeline_outcomes_total` + CD terminals |
| pr_number / pr_url | Integer / String NULL | |
| verified | Boolean NULL | `PatchResult.verified` (True green / False red / NULL unknown) |
| verification_detail | String(500) NULL | |
| live_url | String(500) NULL | |
| duration_seconds | Numeric(10,3) NULL | |
| started_at | TIMESTAMPTZ NOT NULL | finished_at TIMESTAMPTZ NULL |

Index `(tenant_id, started_at DESC)`, `(repo_id, status)`. UNIQUE `(tenant_id, gh_run_id, kind)` **WHERE
gh_run_id IS NOT NULL** (partial — chat runs have NULL gh_run_id and don't dedup on it).

### 3.7 `run_events` — the durable SSE mirror (replay survives Redis flush; backs ROI/MTTR)
| col | type | notes |
|---|---|---|
| id | Integer PK | also the SSE resume cursor when run-scoped |
| tenant_id | Integer FK, indexed NOT NULL | |
| run_id | Integer FK→runs ON DELETE CASCADE NULL | |
| conversation_id | Integer FK→conversations ON DELETE SET NULL | |
| seq | Integer NOT NULL | monotonic per run |
| stage | String(64) NOT NULL | exactly `event_publisher` stage (≤64, verified) |
| level | String(8) NOT NULL DEFAULT `'info'` CHECK in (`'info'`,`'warn'`,`'error'`,`'success'`) | |
| message | String(2000) NOT NULL | matches `AgentEvent.message` max |
| metadata | JSON | **no full diffs/files** (see §4.6 size cap) |
| occurred_at | TIMESTAMPTZ NOT NULL | |

Index `(run_id, seq)`, `(tenant_id, occurred_at DESC)`. **MVP retention:** a 7-day prune on
`occurred_at` (mirrors the existing `event_store.cleanup_done` cadence).

### 3.8 `run_id` semantics — pinned across all slices (resolves the ambiguity)
- **Public `run_id`** (API + SPA + chat) = `run_<runs.id>` (the surrogate PK string). This is what the
  frontend types as `run_id: string` and what `run_events.run_id` FKs to.
- **`runs.gh_run_id`** = the GitHub Actions run id (BIGINT, external). Used only for webhook dedup.
- **The agent does NOT mint synthetic GitHub run_ids** (kills the slice-1 collision risk + the
  `error_hash` collision). A chat turn `INSERT`s a `runs` row (NULL `gh_run_id`) and uses its PK.
- **Chat turns never touch `error_attempts`/attempt-budget machinery** (a CI-retry concept). The
  synthetic `Diagnosis(error_type=UNKNOWN, line_number=None)` is built **only** to satisfy
  `apply_patch_set` (PR body/labels) and `assess_risk` — `increment_attempt`/`record_open_pr` are
  **not** called for chat. *Resolves the `error_hash` collision critique.*

### 3.9 SQLite ↔ Postgres switch & migration path
- **Now:** `DATABASE_URL=sqlite+aiosqlite:///./console.sqlite3` (tests + local). Alembic uses
  `DATABASE_URL_SYNC=sqlite:///./console.sqlite3`.
- **Postgres:** swap to `postgresql+asyncpg://…` / `postgresql+psycopg2://…`. The demo already runs
  Postgres in prod and CI (its `pyproject` deps), so this path is proven, not hypothetical.
- **Legacy migration (`run_registry.json`, `logs/*.jsonl`) is DEFERRED (§8).** The core loop reads none
  of it; the agent keeps its existing JSON/JSONL stores for the webhook path untouched. `queued_events`
  (the agent's SQLite outbox) stays as-is — it is webhook-replay infra, not console business data.

---

## 4. API endpoints

All under **`/api/console`** (single prefix; resolves the `/console` vs `/api/console` conflict — **`/api/console` wins**). Hosted on the **demo backend**. Every route: `tenant: Tenant = Depends(get_current_tenant)` (returns the seeded `default` tenant now; maps a session/JWT → tenant later — body change only). All IDs are **opaque strings** on the wire.

### 4.1 Chat
```
POST /api/console/conversations
POST /api/console/conversations/{conversation_id}/turns
```

`POST /conversations` → `201`:
```jsonc
{ "id": "cnv_12", "tenant_id": "default", "repo_id": "repo_1",
  "title": null, "status": "active", "created_at": "...", "updated_at": "..." }
```

`POST /conversations/{conversation_id}/turns` request (**the unified chat-send body** — resolves the
slice-1/3/4 body conflict):
```jsonc
{
  "message": "add a low-stock endpoint and deploy it",   // field name: "message" (NOT "text")
  "idempotency_key": "client-uuid",                       // REQUIRED — frontend MUST send it
  "autonomy": "auto" | "manual" | null,                   // null → use repo default
  "kind": "chat" | "approve" | "reject"                   // default "chat"; approve/reject resume a pause
}
```
→ `202 Accepted`:
```jsonc
{ "conversation_id": "cnv_12", "turn_id": "turn_88",
  "user_message_id": "msg_201", "run_id": "run_55" | null,
  "stream_url": "/api/console/conversations/cnv_12/events" }
```
Status codes: `202` accepted; `409` idempotency replay (returns the original 202 body) or already-resolved approve/reject; `422` validation; `429` Redis task stream backpressure (depth ≥ cap); `404` unknown conversation.

> **Approve/reject is the SAME endpoint** with `kind:"approve"|"reject"` (resolves the
> `/approvals/{id}/decision` vs `/chat/{turn_id}/approve` conflict — **one endpoint, `kind`
> discriminator**). The backend finds the conversation's latest `AWAITING_APPROVAL` turn, validates
> its `resume_token`, and XADDs a `ChatTaskEvent{kind:"approve"}`. A natural-language "yes ship it"
> also works: intent-classify returns APPROVE and routes identically. There is **no separate
> approvals table and no `approval_id`/`action_id`** — the unit of approval is the **turn**
> (`turn_id`). The SPA's `ApprovalCard` carries `turn_id`.

### 4.2 Stream a conversation's events (SSE)
```
GET /api/console/conversations/{conversation_id}/events       (Accept: text/event-stream)
```
Emits the **byte-identical** demo frames (so `useEventSource` works unchanged):
```
event: agent_event
id: <resume-cursor>
data: {"id","stage","level","message","timestamp","metadata"}

event: ping
data: keepalive          ← every 15s
```
- Server-side filter: `metadata.tenant_id == tenant.id AND metadata.conversation_id == {conversation_id}`.
- Honors `Last-Event-ID`. `?backfill=20` (default 20, max 200) replays from `run_events` oldest-first
  before live-tailing the Redis stream — durable replay even after a Redis flush.
- Unauthenticated GET (EventSource can't send headers) — acceptable single-tenant; signed
  `?stream_token=` is a documented fast-follow before signups (§7 R3, §8).

### 4.3 List / read
```
GET /api/console/conversations?limit=20&cursor=<id>&status=active|paused|archived
GET /api/console/conversations/{conversation_id}
GET /api/console/conversations/{conversation_id}/messages?limit=50&cursor=<id>
```
`Conversation`: `{ id, tenant_id, repo_id, title, status, autonomy_effective, last_message_preview,
run_ids:[…], pending_turn_id: "turn_88"|null, created_at, updated_at }`.
`Message`: `{ id, conversation_id, role, kind, content, payload, run_id, created_at }`.
**Pagination:** keyset on monotonic id, `{ items, next_cursor }`. No offset.

### 4.4 Run resync (stepper fallback)
```
GET /api/console/runs/{run_id}            → RunView (§5 types) — used on SSE reconnect to resync
```

### 4.5 ROI + config
```
GET  /api/console/metrics/roi?window=7d   → RoiMetrics (MTTR folded in; single endpoint — /mttr deferred)
GET  /api/console/config                  → tenant config (repo selector + autonomy + cd_enabled + live_url)
PATCH /api/console/config                 → { autonomy_mode?, repo_id? }  (the AUTO toggle persists here)
```
`RoiMetrics` (derived from `runs` + `run_events`; cost from the agent's existing `/status`
`gemini.cost_today_dollars` until `cost_ledger` lands in §8):
```jsonc
{ "tenant_id":"default","window":"7d",
  "runs_total":42,"auto_merged":31,"escalated":4,"failed":3,
  "incidents_resolved":38,"deploys_total":12,"rollbacks":1,
  "mttr_seconds":188.0,"p50_seconds":62.8,
  "engineer_minutes_saved":1860,            // incidents_resolved × MANUAL_FIX_MINUTES (config constant)
  "dollars_saved":3100.0,                   // saved-minutes × BLENDED_RATE − agent_spend  (ESTIMATE, labeled)
  "agent_spend_usd":0.84, "updated_at":"..." }
```
> `dollars_saved` is an **honest estimate** built from two named config constants
> (`MANUAL_FIX_MINUTES`, `BLENDED_RATE_USD` in `config/constants.py`), surfaced with a methodology
> tooltip in the UI (consistent with the project's verify-honesty ethos).

### 4.6 SSE event envelope — the frozen contract (resolves the metadata-bloat critique)
The envelope is the **existing** `AgentEvent` shape, unchanged. The console only standardizes
`metadata` correlation keys and enforces a size rule:

```jsonc
"metadata": {
  "tenant_id": "default",
  "conversation_id": "cnv_12",     // NEW — routes to the chat thread + stepper
  "run_id": "run_55",              // NEW — the surrogate-PK public id (NOT gh_run_id)
  "turn_id": "turn_88",            // NEW
  "step_index": 0,                 // seam for agent-mode (always 0 in MVP)
  "repo": "Mahi230504/cicd-agent-demo", "branch": "agent/chat-88", "sha": "abc1234",
  // small stage-specific keys only: pr_number, pr_url, risk_level, verified, live_url, diff_preview
}
```
**HARD RULE (resolves "large diffs bloat Redis"):** full diffs and file contents are **NEVER** put in
an SSE event. They are persisted as a `messages` row (`kind:"diff"`, `content`=full diff). The
`diff_preview` event carries only a **truncated** preview (≤ ~1KB) plus the `message_id`/`pr_url`
pointer. `event_publisher` gains a **metadata JSON size cap** mirroring its existing 1800-char message
clip; oversized metadata is dropped with a warning, never sent. `run_events.metadata` stores the same
capped dict.

**Canonical `stage` vocabulary** (grounded in pipeline/cd_pipeline + chat-new):
`chat_received, intent, reading_files, generating_edit, diff_preview, opening_pr, verifying_ci,
auto_gate, awaiting_approval, merging, deploying, health_check, live_url, done, error` plus the existing
CD stages `cd_start, deploy_guard, deploy, rollback, cd_done`.

### 4.7 Streaming convention — PINNED (resolves slice-4 blocking open question)
Agent prose streams as **whole-message REPLACEMENT**, not token deltas. Each `chat`/`step` event
carries the full current text for that `message_id` in `message` (≤1800 chars); `useChat` overwrites
the matching `ChatMessage.text`. (Rationale: simplest reliable reducer; no delta-ordering hazards; the
narration is short step-level prose, not long token streams.)

### 4.8 Idempotency / pagination / backpressure (summary)
- **Idempotency:** `messages.UNIQUE(tenant_id, conversation_id, idempotency_key)` for turns; resume is
  idempotent on `turns.resume_token` (cleared first). Frontend **must** generate+resend a stable UUID
  (§5 `SendMessageRequest.idempotency_key`).
- **Pagination:** keyset everywhere (`cursor`+`limit`, `{items,next_cursor}`).
- **Backpressure:** Redis `agent:tasks` stream is bounded; over the cap → `429`. SSE bounded by Redis
  `MAXLEN ~1000` → slow clients reconnect + backfill from `run_events`.

---

## 5. UI architecture

A new standalone SPA at **`cicd-agent-demo/console/`** — a **sibling** to `frontend/` (which is the
inventory app the agent *deploys to*; per the locked "don't bolt on" rule). Same stack verbatim:
Vite 6 + React 19 + TS 5.7 + Tailwind v4 (`@tailwindcss/vite`, no config file). Its own Vercel project,
its own `VITE_API_BASE` (pointing at the demo backend — the single origin that serves both REST and
SSE, so **no two-origin CORS**). Shared code (`useEventSource`, `index.css` tokens, tone records) is
**copied**, matching the repo's existing per-app self-containment.

### 5.1 Component tree (paths)
```
App.tsx → Console.tsx
  TopBar  (RepoSelector · AutonomyToggle · SavingsBadges)
  ├ chat/ChatPanel
  │   MessageList → ChatMessage → { UserBubble | AgentTurn | DiffCard | ApprovalCard | LiveUrlCard }
  │   Composer (textarea + send; suggestion chips)
  ├ pipeline/PipelinePanel
  │   PipelineStepper → StepNode + FlowConnector;  TokenCostTicker;  ShipMoment (overlay)
  └ war_room/{ RoiPanel → StatTile×4 ;  IncidentsFeed }
```

### 5.2 State management
- **React Query** for server state (conversation history, runs resync, repos, ROI) with the demo's
  shared `queryClient` config copied verbatim (5s refetch, 2s stale, keep-last-good).
- **One page-level SSE subscription** in `Console.tsx` (`useConsoleStream` → reused `useEventSource`),
  threaded down — exactly the `Dashboard.tsx` "one SSE, shared" pattern. No second connection.
- **`useReducer` in `useChat`** merges history (React Query) + live events (SSE) keyed on
  `message_id`, applying the **REPLACE** convention (§4.7). Optimistic `UserBubble` on send via the
  `InventoryTable` `onMutate`/rollback pattern; server echo reconciles by `idempotency_key`.
- No Redux/Zustand.

### 5.3 Streaming layer (reuses useEventSource)
`useConsoleStream(streamUrl, { conversationId })` filters the buffer to the active conversation
(`metadata.conversation_id`). On `status: connecting→open` after a blip, `useRun` refetches
`GET /api/console/runs/{run_id}` to resync the stepper (closes the reconnect-gap risk). Chat input is
plain `POST .../turns`; approve/reject is the same POST with `kind`.

### 5.4 Routing
**No router library** (matches `frontend/App.tsx`). `conversation_id` lives in React state mirrored to
URL hash `#/c/<id>` (a 10-line `useHashConversation`) for deep-link/refresh resume. `react-router` is a
documented fast-follow.

### 5.5 TS types (mirror §4 Pydantic; string IDs; hand-synced per repo convention)
```ts
export type { AgentEvent, ConnectionStatus } from '../hooks/useEventSource'  // reused verbatim
export type Autonomy = 'auto' | 'manual'
export type RiskLevel = 'low' | 'medium' | 'high'
export type TurnStatus =
  | 'queued' | 'running' | 'awaiting_approval' | 'merging' | 'deploying'
  | 'done' | 'failed' | 'rejected'                       // the ONE turn vocabulary (matches §3.5)
export type MessageRole = 'user' | 'assistant' | 'system'
export type MessageKind = 'text' | 'step' | 'diff' | 'approval' | 'live_url' | 'status'

export interface DiffFile { path: string; additions: number; deletions: number; patch: string }

export interface ApprovalPayload {                       // carried on a kind:'approval' message
  turn_id: string                                        // approval unit = the turn (no approval_id)
  title: string                                          // "Merge PR #42 and deploy"
  risk_level: RiskLevel; risk_reasons: string[]
  verified: boolean | null; verification_detail: string | null
  pr_number: number | null; pr_url: string | null
  files: DiffFile[]
}
export interface ChatMessage {
  id: string; conversation_id: string
  role: MessageRole; kind: MessageKind
  text: string                                           // REPLACE convention (§4.7) grows the text
  streaming: boolean; created_at: string
  diff?: { files: DiffFile[]; summary: string }
  approval?: ApprovalPayload
  live_url?: { url: string; commit: string; deployed_at: string }
  run_id?: string                                        // "run_55"
}
export interface SendMessageRequest {
  message: string                                        // field name matches §4.1
  idempotency_key: string                                // REQUIRED — frontend generates a stable UUID
  autonomy: Autonomy | null
  kind?: 'chat' | 'approve' | 'reject'
}
export interface SendMessageResponse {
  conversation_id: string; turn_id: string
  user_message_id: string; run_id: string | null; stream_url: string
}
export type RunPhase = 'diagnose' | 'patch' | 'verify' | 'deploy' | 'done' | 'failed'
export type StepState = 'pending' | 'active' | 'done' | 'warn' | 'error'
export interface RunStep { key: RunPhase; label: string; state: StepState; detail: string | null
  started_at: string | null; ended_at: string | null }
export interface RunView {
  run_id: string | null; phase: RunPhase; steps: RunStep[]
  pr_url: string | null; live_url: string | null; verified: boolean | null
  status: string | null; tokens: number; cost_usd: number
}
export interface RepoConfig {
  id: string; full_name: string; default_branch: string
  live_url: string | null; autonomy_default: Autonomy; cd_enabled: boolean
}
export interface RoiMetrics {
  dollars_saved: number; engineer_minutes_saved: number
  runs_total: number; auto_merged: number; deploys_total: number; rollbacks: number
  mttr_seconds: number; agent_spend_usd: number
}
export interface Incident {
  id: string; kind: 'ci_failure' | 'rollback' | 'escalation' | 'deploy'
  run_id: string | null; title: string; level: AgentEvent['level']
  created_at: string; href: string | null
}
```
`lib/api.ts` (exact paths — resolves slice-3/4 path conflict):
```ts
export const api = {
  repos:    () => get<RepoConfig[]>('/api/console/config').then(c => c.available_repos),
  config:   () => get<TenantConfigResp>('/api/console/config'),
  patchCfg: (b: {autonomy_mode?: Autonomy; repo_id?: string}) => patch('/api/console/config', b),
  metrics:  () => get<RoiMetrics>('/api/console/metrics/roi?window=7d'),
  history:  (cid: string) => get<ChatMessage[]>(`/api/console/conversations/${cid}/messages`),
  run:      (rid: string) => get<RunView>(`/api/console/runs/${rid}`),
  newConv:  () => post('/api/console/conversations', {}),
  sendTurn: (cid: string, b: SendMessageRequest) =>
              post<SendMessageResponse>(`/api/console/conversations/${cid}/turns`, b),
}
// SSE: `${VITE_API_BASE}/api/console/conversations/${cid}/events`
```

### 5.6 The lucrative visualization (three calibrated moments)
1. **Live animated pipeline** (`PipelineStepper` + `FlowConnector`): vertical `diagnose → patch →
   verify → deploy → live URL`. Active node pulses (`animate-pulse-soft`, already in `index.css`); the
   connector above it shows a traveling-gradient (`@keyframes flow`). Completed steps snap emerald with
   ✓ — same glyph language as the existing `PipelineStageTracker.STATE_STYLES`. Pure projection over
   the §4.6 stage keys + `metadata.run_id` scoping (grounded, not invented).
2. **Real-time cost ticker** (`TokenCostTicker`): a monospace count-up. **MVP reality (resolves the
   live-per-token open question):** the agent emits an explicit `cost` field on each step event using
   the per-call cost `record_gemini_call` returns; if absent, it **degrades gracefully** to a single
   rAF count-up to the end-of-run total from `GET /metrics/roi`. Either way the audience sees "this fix
   cost $0.018."
3. **The "it shipped itself" moment** (`ShipMoment` + `LiveUrlCard`): when a turn reaches verified +
   low-risk + AUTO (or after the human taps **Ship it** in `ApprovalCard`), the deploy node ignites and
   `LiveUrlCard` drops into chat with a big clickable URL ("Shipped to https://… · commit a1b2c3d"). A
   one-shot `@keyframes ship-glow` flourish plays. **Per the §0 keystone #3 decision, the flagship demo
   is scripted around the human-approve tap** (one click → ShipMoment), with a second LOW-risk prompt
   (new endpoint in a fresh router file) demonstrating true unattended auto-ship.

All conditional Tailwind classes use **baked `Record<State,string>` lookups** (purge-safe), never
template literals. `prefers-reduced-motion` disables `animate-flow`/`animate-ship`/`animate-pulse-soft`.

---

## 6. Production-ready code plan

### 6.1 Key interfaces / signatures to implement
```python
# cicd-agent/agents/chat_orchestrator.py
class ChatOrchestrator:
    def __init__(self, repo_client: "ConsoleApiClient", llm: LLMProvider = get_llm_provider()): ...
    async def handle_turn(self, event: ChatTaskEvent) -> None: ...        # never raises; routes by kind
    async def _run_chat_turn(self, event, tenant) -> None: ...
    async def _resume_turn(self, event, tenant) -> None: ...
    async def _classify_intent(self, message: str, history: list[dict]) -> ChatIntent: ...
    async def _open_and_verify(self, proposal, ctx) -> tuple[PatchResult, VerifyResult]: ...
    async def _ship_or_pause(self, proposal, patch, verify, ctx) -> None: ...
    async def _deploy(self, merged_sha: str, ctx) -> str: ...             # returns live URL
    async def _emit(self, ctx, stage, message, *, level="info", meta=None) -> None: ...

# cicd-agent/agents/chat_editor.py
async def generate_edit(instruction: str, target_files: dict[str, str],
                        import_context: str, mcp: GitHubMCPClient) -> EditProposal: ...

# cicd-agent/github/rest_api.py  (NEW — the one privileged write)
async def merge_pull_request(pr_number: int, *, merge_method: str = "squash",
                             sha: str | None = None) -> tuple[bool, str]: ...

# cicd-agent/github/pr_manager.py  (CHG signatures)
async def apply_patch_set(diagnosis, diff, run_id, head_sha, mcp_client,
                          base_ref=None, branch: str = ROLLING_PATCH_BRANCH) -> PatchResult: ...

# cicd-agent/agents/ci_verifier.py  (CHG)
def _select_run(runs, head_sha, since, workflow_name,
                head_branch: str = ROLLING_PATCH_BRANCH) -> dict | None: ...

# cicd-agent/agents/autonomy_policy.py  → should_ship_unattended(...) -> ShipDecision   (§1.6)
```
`ConsoleApiClient` is a thin async HTTP client (agent → demo backend) that creates/updates `turns` and
`messages` and reads `repos` config, so the agent never opens the console DB directly (clean process
boundary). It POSTs events through the existing `event_publisher` path.

### 6.2 Build order — vertical slices for the core loop (each demoable)
0. **Pre-flight (BLOCKER):** verify `merge_pull_request` against the live repo — confirm PAT merge
   rights + `main`/branch-protection on `agent/chat-*`; wire the degrade-to-`AWAITING_APPROVAL` path.
   Seed the `default` tenant/repo row (`ci_workflow_name="CI"`, `release_workflow_name="Release"`).
1. **Data layer:** `console.py` models + Alembic `0003`; `console_repo.py` DAL; SQLite local.
2. **API skeleton:** `/api/console` routers (chat, stream, config, runs, metrics) returning canned
   data; SSE relay filtered by conversation; CORS for the SPA origin.
3. **Queue wiring:** `ChatTaskEvent` + 3rd dispatch branch; `enqueue_chat_task` XADD; agent consumer.
4. **Chat orchestrator — FEATURE branch:** intent classify → `chat_editor` → `apply_patch_set(branch=
   agent/chat-{turn})` → `verify_patch_ci(head_branch=…)` → `should_ship_unattended` → pause/merge/deploy.
5. **Frontend:** scaffold SPA; chat + SSE stream + pipeline stepper + approval card + ROI panel; wire
   the ShipMoment.
6. **BUGFIX-from-chat (smaller):** add `github/run_history.get_latest_failing_run(workflow_name)` then
   route through existing `log_analyst.diagnose → code_patcher.patch_and_verify` into `_ship_or_pause`.

### 6.3 Testing strategy (per memory: import-check per phase + full functional at end)
- **Per phase:** after each build step, an **import-check** (`python -c "import agents.chat_orchestrator"`
  etc.) + targeted unit tests. Pure functions get exhaustive units: `should_ship_unattended` (the §1.6
  truth table), the public-id (de)serializer, the metadata size-cap clip.
- **Contract tests:** a single shared fixture asserts TS types ⇄ Pydantic field parity for
  `ChatMessage`, `ApprovalPayload`, `RunView`, `RepoConfig`, `RoiMetrics` (names + nullability).
- **DB-isolation hazard (memory):** tests set `DATABASE_URL=sqlite+aiosqlite:///:memory:` and use
  `setenv("","")` not `delenv` so the dev `.env` can't leak a live codespace/SSH target.
- **End:** full functional run on SQLite — a fake-MCP harness drives `handle_turn` through FEATURE +
  approve + deploy, asserting the `turns` state machine, `run_events` ordering, and the SSE replay.

### 6.4 Scalability-to-millions growth path (seams now, build later)
- **Stateless workers:** chat durability is in `turns`/`messages` (DB), not process memory; the agent
  consumer is restart-safe via the Redis Stream consumer-group offset. N workers = scale out.
- **Postgres:** flip `DATABASE_URL` (demo already runs PG in prod/CI).
- **Redis SSE fan-out:** already in place; multi-tenant = namespace the stream key `agent:events:{tenant}`
  and filter the subscription (the `tenant_id` metadata is the seam).
- **Queue:** Redis Streams now (the demo's `app/worker/consumer.py` `XREADGROUP` pattern is the
  template) → SQS/Cloud Tasks later behind the same `enqueue_chat_task` surface.
- **Rate/cost controls:** the existing global `GeminiRateLimiter` + daily cost cap stay as the provider
  floor; per-tenant token-buckets/caps are a fast-follow (§8). The first real bottleneck is the
  single-worker `verify_patch_ci` blocking wait (≤240s) — flagged for per-tenant workers/job-runner.

---

## 7. Risks & open questions (the few that still want the user's input)

- **R1 — Lock override to ratify (HIGH):** We host the console API + SSE on the **demo backend**, not
  the agent's `webhook/server.py`, because the Redis broker, SSE route, SQLAlchemy, and Alembic already
  live there and rebuilding them in the agent is pure duplication. This is the one explicit deviation
  from a locked decision. **Need: a yes to host on the demo backend.** (If the lock must hold, we add
  `redis` + an SSE route + SQLAlchemy to the agent — ~1 extra build slice of duplication.)
- **R2 — Merge capability (HIGH, build-blocker):** `merge_pull_request` is new and the demo repo's PAT
  merge rights + branch protection are unverified. Pre-flight step 0 verifies this; the degrade path
  (→ `AWAITING_APPROVAL` with reason) is specified so the loop never silently fails. **Confirm the PAT
  can merge** (or that we accept always-pause until it can).
- **R3 — Unauthenticated SSE (MEDIUM):** EventSource can't send auth headers, so the stream GET is open.
  Fine for single-tenant demo; a signed `?stream_token=` is required before signups (§8).
- **R4 — Merge→deploy timing (MEDIUM):** After merge, `release.yml` fires its own `workflow_run` that the
  existing `cd_pipeline` already handles. **Decision for MVP: the chat deploy step calls `deployer.deploy`
  directly** (immediate demoable live URL) and we **suppress the release webhook for `agent/chat-*`/the
  squash-merge** to avoid a double-deploy. **Confirm** this is acceptable vs. waiting for the release CD path.
- **R5 — ROI $ constants:** `MANUAL_FIX_MINUTES` and `BLENDED_RATE_USD` need a product number so
  `dollars_saved` is impressive **and** defensible (shown with a methodology tooltip). **Need values.**
- **R6 — Demo script (confirm):** Keystone #3 makes the flagship demo a human-approve tap; a second
  prompt (endpoint in a fresh router file) shows unattended auto-ship. **Confirm** this two-prompt script.

---

## 8. Deferred / fast-follows

Designed-for (seams in place), explicitly **not built** this milestone:

- **Agent-mode multi-step planner** — MVP collapses planning into the single intent-classify call. The
  `step_index` metadata key + the per-turn executor shape are the seam; enabling it later = a
  `task_planner.plan()` returning N steps and a per-step re-entry into `_ship_or_pause`. (No
  `task_planner.py`, no `chat_planner` LLM round-trip in MVP — cut as over-engineering.)
- **Proactive war-room push** — the `IncidentsFeed` renders from existing error/rollback/escalation SSE
  stages today; the *unprompted* "agent posts into chat" behavior is later (data + transport are ready).
- **Ask-your-pipeline "why" queries** — over `run_events`/audit; needs Postgres JSONB indexing.
- **Live per-token cost narration** — MVP degrades to end-of-run count-up; threading per-call cost +
  run-context into a `cost` SSE frame is the fast-follow.
- **`DeploymentProvider` runtime class + GCP Cloud Run** (Terraform + Cloud Build, plan-review gate,
  self-healing provision_doctor) — MVP calls `deployer`/`health_monitor`/`rollback` directly; the
  `repos.deploy_provider` column + a one-class refactor are the seam.
- **`LLMProvider` as a runtime switch** — the typing Protocol exists (free); a `ClaudeProvider` + an
  `LLM_PROVIDER` env switch are later.
- **Multi-tenant scale machinery** — per-tenant token-buckets, per-tenant cost caps, Postgres RLS,
  per-tenant SSE stream-key namespacing, Prometheus tenant-label cardinality caps. The `tenant_id`
  column + a `get_current_tenant()` returning `"default"` is the entire seam now.
- **True repo-from-DB GitHub I/O** — refactor `GitHubMCPClient.__aenter__`, `rest_api._repo_path`, and
  `run_history` to accept a repo instead of reading `Settings`. MVP seeds the one `repos` row to equal
  the env repo.
- **Legacy data migration** — importing `run_registry.json` + `logs/*.jsonl` into tables, plus the
  `cost_ledger`, `pr_actions`, `deployments`, `audit_log`, and `error_attempts` tables. The core loop
  reads none of them; the agent keeps its existing JSON/JSONL stores for the webhook path untouched.
- **`react-router`** — MVP uses `#/c/<id>` hash; the seam makes `/c/:id`, `/runs/:id`, `/settings`
  mechanical.
- **Shared frontend package** — `useEventSource`/tokens are copied per-app (the repo's existing
  convention); a shared workspace package is later.
