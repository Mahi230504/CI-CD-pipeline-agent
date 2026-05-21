"""
GitHub MCP client — manages the MCP ClientSession lifecycle.

Connects to the GitHub MCP server at https://api.githubcopilot.com/mcp
using the GITHUB_PERSONAL_ACCESS_TOKEN from settings.

Exposes async context manager: async with GitHubMCPClient() as client
Inside the context, all tool call methods are available.

Methods (all async):
- get_workflow_run(run_id) → dict
- list_jobs(run_id) → list[dict]
- get_file_contents(path, ref) → str
- list_workflow_files() → list[str]
- get_workflow_yaml(filename) → str

Session is created once per pipeline run, reused across all agent calls.
"""
