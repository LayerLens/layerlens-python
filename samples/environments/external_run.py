"""
Environments in CI -- LayerLens Python SDK
==========================================
Creates (or reuses) an environment, runs an agent once per task of a JSONL task file,
each attempt on its own copy of the world, and exits 10 when the pass rate is below a
threshold: the same gate as `layerlens env run exec`, from Python.

Prerequisites:
    uv pip install --extra-index-url https://sdk.layerlens.ai/package layerlens httpx
    export LAYERLENS_API_KEY="an Automation key"

Usage:
    python samples/environments/external_run.py --tasks-file ci/tasks.jsonl --min-pass-rate 0.9
"""

from __future__ import annotations

import sys
import argparse
from typing import Any, Dict, List

import httpx

from layerlens import LayerLens, PermissionDeniedError


def run_agent(prompt: str, base_url: str, token: str) -> str:
    """Your agent goes here. This one only reads the tools and asks for the first one.

    The attempt's address serves GET /tools and POST /call, and the token works as
    x-api-key; it reaches this attempt's own copy of the world and nothing else.
    """
    env = httpx.Client(base_url=base_url, headers={"x-api-key": token}, timeout=60)
    tools: List[Dict[str, Any]] = env.get("/tools").json()["data"]["tools"]
    if not tools:
        return "no tools"
    reply = env.post("/call", json={"tool": tools[0]["name"], "args": {}}).json()["data"]
    return f"{prompt[:40]}... {tools[0]['name']} answered with status {reply['status']}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", default="support-desk")
    parser.add_argument("--type", default="zendesk")
    parser.add_argument("--description", default="A small outdoor-gear retailer's support desk: 40 open tickets.")
    parser.add_argument("--tasks-file", required=True, help="One task per line: name, prompt and grader")
    parser.add_argument("--agent-name", default="sample-agent")
    parser.add_argument("--min-pass-rate", type=float, default=0.9)
    args = parser.parse_args()

    try:
        client = LayerLens()
        client.organization_id  # checks the key and URL now: building the client makes no request
    except Exception as exc:
        print(f"ERROR: Failed to initialize LayerLens client: {exc}")
        sys.exit(1)
    try:
        env = client.environments.create(args.name, args.type, description=args.description, if_not_exists=True)
    except PermissionDeniedError as exc:
        print(f"ERROR: this sample needs an API key with Automation access: {exc}")
        sys.exit(1)
    ready = client.environments.wait_until_ready(env.id, timeout_seconds=1800)
    print(f"{ready.name}: version {ready.environment_version}, {ready.tool_count} tools")

    with client.environments.external_run(ready.id, tasks_file=args.tasks_file, agent_name=args.agent_name) as run:
        for attempt in run.attempts:
            try:
                answer = run_agent(attempt.prompt, attempt.base_url or "", attempt.token)
            except Exception as err:  # the agent failing is a verdict, not a broken pipeline
                attempt.submit_error(f"the agent raised {type(err).__name__}: {err}")
            else:
                attempt.submit(answer)

    result = run.wait()
    result.to_junit("results.xml")
    print(result.to_markdown())
    if result.status != "success":
        sys.exit(11)
    if (result.pass_rate or 0.0) < args.min_pass_rate:
        sys.exit(10)


if __name__ == "__main__":
    main()
