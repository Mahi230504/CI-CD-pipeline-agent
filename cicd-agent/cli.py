"""
Command-line interface for the CI/CD agent.

Commands:
  python cli.py trigger --run-id <id>   Replay any past GitHub run through the pipeline
  python cli.py status                   Show queue depth, last diagnosis, daily API usage
  python cli.py optimize                 Run the YAML optimizer standalone on the demo repo
  python cli.py logs --tail <n>          Pretty-print last n entries from the audit log
"""
