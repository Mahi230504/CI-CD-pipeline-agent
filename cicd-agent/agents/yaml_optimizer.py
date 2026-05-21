"""
YAML optimizer agent — reduces pipeline runtime by parallelization and caching.

Flow:
1. Reads all .github/workflows/*.yml via run_history.get_workflow_files()
2. Parses each YAML with PyYAML
3. Builds a job dependency graph using networkx (DiGraph)
4. Identifies jobs with no dependency path between them → parallelizable
5. Sends original YAML + graph summary + YAML_OPTIMIZER_PROMPT to Gemini
6. Parses two YAML blocks from response (original + optimized)
7. Estimates time saved: sum(sequential_times) - critical_path_length
8. Opens a SEPARATE PR (never mixed with code patch PR)
9. Returns OptimizationResult

Runs regardless of patch success/failure — these are independent concerns.
Uses PRIMARY_MODEL (gemini-2.5-flash).
"""
