"""
Fetches and processes GitHub Actions job logs.

The key challenge: real pipeline logs are 50K–200K tokens of noise
(Docker pulls, npm installs, setup steps). This module finds the signal.

Functions:
- fetch_raw_logs(run_id, job_id, mcp_client) → bytes
- decompress_logs(raw_bytes) → str
- find_failed_step(log_text) → tuple[int, str]   (line_number, step_name)
- slice_log(log_text, error_line, window=30) → str
- token_guard(text, max_tokens=8000) → str        (truncates with warning if needed)

The output of slice_log() is what gets sent to Gemini — not the full log.
"""
