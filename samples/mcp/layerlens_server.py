"""LayerLens MCP Server -- powered by the LayerLens Python SDK.

An MCP (Model Context Protocol) server that exposes LayerLens trace
inspection, evaluation, and judge management capabilities as tools that
any MCP-compatible AI assistant can invoke.

Prerequisites
-------------
    uv pip install --extra-index-url https://sdk.layerlens.ai/package layerlens mcp

It runs on mcp 1.x (1.2 or later) and 2.x, through the SDK's high-level server: mcp 2
renamed ``FastMCP`` to ``MCPServer`` and dropped the low-level decorators.

Environment
-----------
    LAYERLENS_API_KEY   API key used by the SDK (picked up
                                automatically by ``LayerLens()``).

Usage
-----
    python layerlens_server.py
"""

from __future__ import annotations

import os
import sys
import json
import asyncio
import threading
from typing import Any, Optional

import mcp.server
from mcp.types import TextContent

from layerlens import LayerLens, LayerLensError, NotFoundError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from _helpers import create_judge as _create_judge_helper

# ---------------------------------------------------------------------------
# Lazy-initialised SDK client
# ---------------------------------------------------------------------------

_client_lock = threading.Lock()
_client: Optional[LayerLens] = None


def _get_client() -> LayerLens:
    """Return the module-level LayerLens client, creating it on first use."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:  # double-check after acquiring lock
                _client = LayerLens()
    return _client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _obj_to_text(obj: Any) -> str:
    """Convert a Pydantic model, dict, or list to a readable string."""
    if obj is None:
        return "(no data)"
    if hasattr(obj, "model_dump"):
        return json.dumps(obj.model_dump(), indent=2, default=str)
    if isinstance(obj, (dict, list)):
        return json.dumps(obj, indent=2, default=str)
    return str(obj)


def _error_text(exc: Exception) -> str:
    """Format an exception into a user-friendly error message."""
    if isinstance(exc, NotFoundError):
        return f"Not found: {exc}"
    if isinstance(exc, LayerLensError):
        return f"LayerLens API error: {exc}"
    return f"Error: {exc}"


# mcp 2.x calls the high-level server ``MCPServer``; mcp 1.x (1.2 or later) ``FastMCP``.
MCPServer = getattr(mcp.server, "MCPServer", None) or mcp.server.FastMCP


# ---------------------------------------------------------------------------
# MCP server factory
# ---------------------------------------------------------------------------


def create_server() -> Any:
    """Create and return the MCP server with its six tools."""

    server = MCPServer("layerlens")

    @server.tool()
    async def list_traces(limit: int = 20) -> str:
        """List traces stored in LayerLens, the most recent first: at most 100 (default 20)."""
        return await _call(_handle_list_traces, {"limit": limit})

    @server.tool()
    async def get_trace(trace_id: str) -> str:
        """Retrieve the full details of a single trace by its ID."""
        return await _call(_handle_get_trace, {"trace_id": trace_id})

    @server.tool()
    async def run_evaluation(trace_id: str, judge_id: str) -> str:
        """Run a judge evaluation against a trace. Returns the created trace-evaluation
        object (initially in pending state)."""
        return await _call(_handle_run_evaluation, {"trace_id": trace_id, "judge_id": judge_id})

    @server.tool()
    async def get_evaluation(evaluation_id: str) -> str:
        """Get the status and results of a trace evaluation. If the evaluation has
        completed, the results are included."""
        return await _call(_handle_get_evaluation, {"evaluation_id": evaluation_id})

    @server.tool()
    async def create_judge(name: str, goal: str) -> str:
        """Create a new evaluation judge with a name and goal (the criteria it assesses)."""
        return await _call(_handle_create_judge, {"name": name, "goal": goal})

    @server.tool()
    async def list_judges() -> str:
        """List the judges configured in the current project."""
        return await _call(_handle_list_judges, {})

    return server


async def _call(handler: Any, arguments: dict) -> str:
    """Run a tool's handler. An error is answered as text, and the server keeps serving."""
    try:
        contents = await handler(_get_client(), arguments)
    except Exception as exc:
        return _error_text(exc)
    return "\n".join(c.text for c in contents)


# ---------------------------------------------------------------------------
# Individual tool handlers
#
# Each handler wraps synchronous SDK calls in asyncio.to_thread() so they
# do not block the async MCP event loop.
# ---------------------------------------------------------------------------


async def _handle_list_traces(client: LayerLens, arguments: dict) -> list[TextContent]:
    limit = arguments.get("limit", 20)
    resp = await asyncio.to_thread(
        client.traces.get_many, page_size=limit, sort_by="created_at", sort_order="desc"
    )
    if resp is None:
        return [TextContent(type="text", text="No traces found.")]

    lines: list[str] = [f"Traces (showing {resp.count} of {resp.total_count}):"]
    for t in resp.traces:
        eval_info = ""
        if t.evaluations_count:
            eval_info = f" | {t.evaluations_count} evaluation(s)"
        lines.append(
            f"  - {t.id}  created={t.created_at}  file={t.filename}{eval_info}"
        )
    return [TextContent(type="text", text="\n".join(lines))]


async def _handle_get_trace(client: LayerLens, arguments: dict) -> list[TextContent]:
    trace_id: str = arguments["trace_id"]
    trace = await asyncio.to_thread(client.traces.get, trace_id)
    if trace is None:
        return [TextContent(type="text", text=f"Trace {trace_id} not found.")]
    return [TextContent(type="text", text=_obj_to_text(trace))]


async def _handle_run_evaluation(client: LayerLens, arguments: dict) -> list[TextContent]:
    trace_id: str = arguments["trace_id"]
    judge_id: str = arguments["judge_id"]
    evaluation = await asyncio.to_thread(
        client.trace_evaluations.create, trace_id=trace_id, judge_id=judge_id
    )
    if evaluation is None:
        return [TextContent(type="text", text="Failed to create evaluation.")]
    return [
        TextContent(
            type="text",
            text=(
                f"Evaluation created.\n"
                f"  ID:      {evaluation.id}\n"
                f"  Status:  {evaluation.status}\n"
                f"  Trace:   {trace_id}\n"
                f"  Judge:   {judge_id}"
            ),
        )
    ]


async def _handle_get_evaluation(client: LayerLens, arguments: dict) -> list[TextContent]:
    eid: str = arguments["evaluation_id"]
    evaluation = await asyncio.to_thread(client.trace_evaluations.get, eid)
    if evaluation is None:
        return [TextContent(type="text", text=f"Evaluation {eid} not found.")]

    parts: list[str] = [
        f"Evaluation {eid}:",
        f"  Status:  {evaluation.status}",
    ]

    # If the evaluation finished, fetch and append results.
    if (
        hasattr(evaluation.status, "value")
        and evaluation.status.value == "success"
        or str(evaluation.status) == "success"
    ):
        results_resp = await asyncio.to_thread(
            client.trace_evaluations.get_results, id=eid
        )
        if results_resp and results_resp.score is not None:
            r = results_resp
            parts.append("")
            parts.append(f"  Result:")
            parts.append(f"    Score:     {r.score}")
            parts.append(f"    Passed:    {r.passed}")
            parts.append(f"    Reasoning: {r.reasoning}")
            parts.append(f"    Latency:   {r.latency_ms} ms")
            parts.append(f"    Cost:      {r.total_cost}")

    return [TextContent(type="text", text="\n".join(parts))]


async def _handle_create_judge(client: LayerLens, arguments: dict) -> list[TextContent]:
    name: str = arguments["name"]
    goal: str = arguments["goal"]
    judge = await asyncio.to_thread(
        _create_judge_helper, client, name=name, evaluation_goal=goal
    )
    if judge is None:
        return [TextContent(type="text", text="Failed to create judge.")]
    return [
        TextContent(
            type="text",
            text=(f"Judge created.\n  ID:      {judge.id}\n  Name:    {judge.name}"),
        )
    ]


async def _handle_list_judges(client: LayerLens, arguments: dict) -> list[TextContent]:
    resp = await asyncio.to_thread(client.judges.get_many)
    if resp is None or not resp.judges:
        return [TextContent(type="text", text="No judges found.")]

    lines: list[str] = [f"Judges ({len(resp.judges)}):"]
    for j in resp.judges:
        lines.append(f"  - {j.id}  name={j.name!r}")
    return [TextContent(type="text", text="\n".join(lines))]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the MCP server over stdio."""
    create_server().run()


if __name__ == "__main__":
    main()
