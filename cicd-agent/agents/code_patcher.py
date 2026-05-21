"""
Code patcher agent — reads the failing file and generates a fix.

Flow:
1. Reads the failing file via github/mcp_client.get_file_contents()
2. Checks BLOCKED_FILE_PATTERNS — hard stop if matched
3. Sends file content + Diagnosis + CODE_PATCHER_PROMPT to Gemini
4. Parses response through response_parser.parse_diff()
5. Validates diff: must be valid unified diff, must not delete >50% of file
6. Calls pr_manager to create branch, apply diff, commit, open PR
7. Returns PatchResult with PR URL and attempt number

Uses PRIMARY_MODEL (gemini-2.5-flash).
Never touches main branch. Never patches secrets or config files.
"""
