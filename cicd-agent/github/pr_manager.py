"""
Creates branches, commits diffs, and opens PRs on GitHub via MCP.

Safety rules enforced here (not just in orchestrator):
- Branch names MUST match: agent/fix-{run_id} or agent/optimize-{run_id}
- Never writes to main, master, or any protected branch
- Checks BLOCKED_FILE_PATTERNS before applying any diff
- If diff apply fails, opens PR with raw diff as a comment

Functions:
- create_branch(run_id, branch_type, mcp_client) → str   (branch name)
- apply_diff(original_content, diff_text) → str           (new file content)
- commit_file(path, content, branch, message, mcp_client) → bool
- open_pr(branch, title, body, mcp_client) → str          (PR URL)
- post_comment(pr_number, body, mcp_client) → bool
"""
