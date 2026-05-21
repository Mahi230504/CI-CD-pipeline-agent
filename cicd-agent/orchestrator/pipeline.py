"""
The main CI/CD agent pipeline.

Single entry point: async run(event: WorkflowFailureEvent) → None

Executes the 6-step agent sequence:
1. Deduplication check (run_registry)
2. Flakiness detection (agents/flakiness_detector)
3. Log analysis + confidence gate (agents/log_analyst)
4. Attempt count check + code patching (agents/code_patcher)
5. YAML optimization (agents/yaml_optimizer)
6. Notification (agents/notifier)

Writes an audit log entry at every step boundary.
Wrapped in asyncio.timeout(SESSION_TIMEOUT_SECONDS) — kills runaway pipelines.
"""
