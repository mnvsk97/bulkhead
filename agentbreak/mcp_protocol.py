"""Backward-compatibility shim. Protocol definitions have moved to protocols/mcp.py."""

from agentbreak.protocols.mcp import (  # noqa: F401
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    JSONRPC_VERSION,
    MCP_TOOL_ERROR,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    MCPError,
    MCPRequest,
    MCPResponse,
    fingerprint_mcp_request,
)
