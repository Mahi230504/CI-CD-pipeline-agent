"""
Webhook event router — filters and normalizes GitHub events.

Only passes through:
- Event type: workflow_run
- Action: completed
- Conclusion: failure
- Actor: NOT dependabot, NOT [bot] suffix, NOT fork PRs

Everything else is acknowledged (200) and discarded silently.

Converts the raw GitHub payload into a WorkflowFailureEvent dataclass
and enqueues it via task_queue. Returns 200 immediately — processing is async.
"""
