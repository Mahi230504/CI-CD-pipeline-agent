"""
Async task queue — serializes incoming webhook events.

Uses asyncio.Queue to ensure only one pipeline run executes at a time.
This is required on the Gemini free tier (rate limits prevent parallelism).

Features:
- Max queue depth: 10 (rejects new events if exceeded — returns 429)
- Worker coroutine: started on server boot via FastAPI lifespan
- Graceful drain: on SIGTERM, finishes current task then stops
- Dead letter queue: failed tasks written to logs/dead_letter.jsonl
- queue_depth() → int: exposed for /status endpoint
"""
