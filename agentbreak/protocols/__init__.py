"""Protocol definitions for AgentBreak."""

from agentbreak.protocols.mcp import (
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

__all__ = [
    "JSONRPC_VERSION",
    "PARSE_ERROR",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
    "INVALID_PARAMS",
    "INTERNAL_ERROR",
    "MCP_TOOL_ERROR",
    "MCPError",
    "MCPRequest",
    "MCPResponse",
    "fingerprint_mcp_request",
]
