"""
Command-line interface for the CI/CD agent.

Commands:
  python cli.py trigger --run-id <id>   Replay any past GitHub run through the pipeline
  python cli.py status                   Show queue depth, last diagnosis, daily API usage
  python cli.py optimize                 Run the YAML optimizer standalone on the demo repo
  python cli.py logs --tail <n>          Pretty-print last n entries from the audit log
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import aiohttp

from config.settings import get_settings


def _server_url(path: str) -> str:
    settings = get_settings()
    return f"http://localhost:{settings.webhook_port}{path}"


async def cmd_trigger(args: argparse.Namespace) -> int:
    settings = get_settings()
    raw_id = str(args.run_id)
    try:
        run_id_int = int(raw_id)
    except ValueError:
        run_id_int = abs(hash(raw_id)) % (10**9)

    payload = {
        "run_id": run_id_int,
        "repo_owner": settings.github_repo_owner,
        "repo_name": settings.github_repo_name,
        "workflow_name": args.workflow or "CI",
        "branch": args.branch,
        "head_sha": args.sha,
        "html_url": (
            f"https://github.com/{settings.full_repo_name}/actions/runs/{run_id_int}"
        ),
    }

    url = _server_url("/trigger")
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                data = await resp.json(content_type=None)
                print(json.dumps(data, indent=2))
                return 0 if resp.status == 200 else 1
    except Exception as e:
        print(f"trigger failed: {e}", file=sys.stderr)
        return 1


async def cmd_status(_args: argparse.Namespace) -> int:
    url = _server_url("/status")
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                data = await resp.json(content_type=None)
                print(json.dumps(data, indent=2))
                return 0 if resp.status == 200 else 1
    except Exception as e:
        print(f"status failed: {e}", file=sys.stderr)
        return 1


def cmd_optimize(_args: argparse.Namespace) -> int:
    print("Optimization runs as part of the pipeline — trigger a run first.")
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    settings = get_settings()
    log_dir = Path(settings.log_dir)
    if not log_dir.exists():
        print("No logs yet.")
        return 0

    files = sorted(
        log_dir.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not files:
        print("No logs yet.")
        return 0

    latest = files[0]
    text = latest.read_text(encoding="utf-8").strip()
    if not text:
        print("No logs yet.")
        return 0

    lines = text.splitlines()
    tail = max(1, int(args.tail))
    for line in lines[-tail:]:
        try:
            obj = json.loads(line)
            print(json.dumps(obj, indent=2))
        except json.JSONDecodeError:
            print(line)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="cli.py", description="CI/CD agent CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_trigger = sub.add_parser("trigger", help="Manually trigger a pipeline run")
    p_trigger.add_argument("--run-id", required=True, help="GitHub workflow run ID")
    p_trigger.add_argument("--branch", default="main")
    p_trigger.add_argument("--sha", default="0" * 40)
    p_trigger.add_argument("--workflow", default="CI")

    p_status = sub.add_parser("status", help="Show queue depth + Gemini usage")

    p_optimize = sub.add_parser(
        "optimize", help="Standalone YAML optimization (not yet supported via CLI)"
    )

    p_logs = sub.add_parser("logs", help="Tail the most recent audit log file")
    p_logs.add_argument("--tail", type=int, default=50)

    args = parser.parse_args()

    if args.command == "trigger":
        sys.exit(asyncio.run(cmd_trigger(args)))
    elif args.command == "status":
        sys.exit(asyncio.run(cmd_status(args)))
    elif args.command == "optimize":
        sys.exit(cmd_optimize(args))
    elif args.command == "logs":
        sys.exit(cmd_logs(args))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
