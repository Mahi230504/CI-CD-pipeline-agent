# Project conventions for Claude Code

These rules apply to anything inside `/Users/mohan/Desktop/CI-CD-pipeline-agent/` — both the agent itself and the `cicd-agent-demo` repo.

## Commits

- **Do NOT add a `Co-Authored-By: Claude …` trailer to commit messages in this project.** Commits should appear single-author (Mahi230504). Skip the trailer entirely — don't replace it with anything else.
- Keep the body content of commit messages as usual: a clear subject line, a blank line, and a body explaining what + why when non-trivial.

## Pull requests

- Body footers (the "🤖 Generated with Claude Code" line at the end of PR descriptions) are fine to keep — they only show in the PR description, not in the git log.
- Continue to open PRs from feature/fix branches; never push directly to `main`.
