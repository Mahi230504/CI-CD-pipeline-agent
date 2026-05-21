"""
Flakiness detector agent.

Before the log analyst runs, this agent checks if the failing job is
genuinely broken or just intermittently flaky.

Logic:
1. Fetch last 5 runs of the same workflow via run_history.py
2. Compute pass rate — if >= 0.4 (passed 2+ of last 5), mark as flaky
3. Separately: scan the current log for known infra error keywords
   (network timeout, rate limit, docker pull, runner: no space left)
   These are never code bugs and should never trigger a patch.

Returns: FlakinessVerdict(is_flaky, reason, pass_rate, error_category)

If is_flaky=True, the orchestrator skips patching entirely.
Uses ZERO Gemini calls — this is a GitHub API + keyword check only.
"""
