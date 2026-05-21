"""
Fetches historical run data for flakiness detection.

Functions:
- get_last_n_runs(workflow_name, n, mcp_client) → list[dict]
- compute_pass_rate(runs) → float
- get_workflow_files(mcp_client) → list[str]

Results are cached in-memory per pipeline execution (not persisted)
to avoid redundant MCP calls within the same agent task.
"""
