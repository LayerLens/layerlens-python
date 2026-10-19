"""The mcp SDK surface the MCP suites build, on mcp 1.x and 2.x.

The suites run on the mcp 1.x the shared dev lock installs (crewai and
semantic-kernel cap it below 2) and on the mcp 2.x ``layerlens[mcp]`` installs
today (the ``mcp`` matrix row). mcp 2.0 renamed ``McpError`` to ``MCPError``,
builds it from ``(code, message)`` instead of an ``ErrorData``, and renamed the
``Tool.outputSchema`` attribute to ``output_schema`` (the constructor still takes
``outputSchema=``). The adapter reads none of these: it is duck-typed over the
session.
"""

from __future__ import annotations

from typing import Any

from mcp import types

try:
    from mcp.shared.exceptions import MCPError as MCP_ERROR  # mcp >= 2
except ImportError:
    from mcp.shared.exceptions import McpError as MCP_ERROR  # mcp 1.x


def mcp_error(code: int, message: str) -> Exception:
    """The exception the mcp client raises for a JSON-RPC error response."""
    if MCP_ERROR.__name__ == "MCPError":
        return MCP_ERROR(code, message)
    return MCP_ERROR(types.ErrorData(code=code, message=message))


def output_schema(tool: Any) -> Any:
    """A ``Tool``'s output JSON Schema, as ``ClientSession.list_tools`` caches it."""
    return tool.output_schema if hasattr(tool, "output_schema") else tool.outputSchema
