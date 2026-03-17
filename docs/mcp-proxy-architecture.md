# MCP Proxy Architecture for AgentBreak

## Overview

This document describes the architecture for adding Model Context Protocol (MCP) proxy support to AgentBreak. The MCP proxy intercepts JSON-RPC 2.0 messages between MCP clients and servers, enabling fault injection, latency simulation, and duplicate/loop detection for AI agent resilience testing.

## MCP Protocol: JSON-RPC 2.0 Structure

MCP uses JSON-RPC 2.0 as its wire format. All messages are JSON objects.

### Request
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "read_file",
    "arguments": {"path": "/etc/hosts"}
  }
}
```

### Successful Response
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "content": [{"type": "text", "text": "...file contents..."}],
    "isError": false
  }
}
```

### Error Response
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "error": {
    "code": -32603,
    "message": "Internal error",
    "data": {"details": "Tool execution failed"}
  }
}
```

### Notification (no id, no response expected)
```json
{
  "jsonrpc": "2.0",
  "method": "notifications/initialized",
  "params": {}
}
```

## MCP Transport Types

### 1. stdio
- MCP server is a subprocess. Client writes JSON-RPC messages to stdin (newline-delimited), reads responses from stdout.
- Most common transport for local tools (e.g. filesystem server, database server).
- Proxy strategy: spawn subprocess, pipe stdin/stdout through the proxy layer.

### 2. SSE over HTTP (legacy)
- Server runs an HTTP server with two endpoints:
  - `POST /message` (or similar): client sends requests
  - `GET /sse`: server pushes responses/notifications as Server-Sent Events
- Used by older MCP servers and some hosted integrations.
- Proxy strategy: intercept POST requests and SSE stream, inject faults on POST handling.

### 3. Streamable HTTP (modern)
- Single HTTP endpoint accepting POST requests with JSON-RPC payloads.
- Responses can be plain JSON or SSE streams (for streaming tool results).
- Response type indicated by Content-Type: application/json vs text/event-stream.
- This is the preferred transport for HTTP-based MCP servers.
- Proxy strategy: intercept POST, conditionally return JSON error or stream fault.

## Key MCP Methods to Proxy

| Method | Direction | Description |
|--------|-----------|-------------|
| `initialize` | client → server | Handshake, exchange capabilities |
| `notifications/initialized` | client → server | Notify server initialization complete (no response) |
| `ping` | either → either | Keep-alive check |
| `tools/list` | client → server | List available tools |
| `tools/call` | client → server | Execute a named tool |
| `resources/list` | client → server | List available resources |
| `resources/read` | client → server | Read resource by URI |
| `resources/subscribe` | client → server | Subscribe to resource updates |
| `resources/unsubscribe` | client → server | Unsubscribe from resource |
| `prompts/list` | client → server | List available prompt templates |
| `prompts/get` | client → server | Get a prompt template by name |
| `logging/setLevel` | client → server | Set server log verbosity |
| `completion/complete` | client → server | Get argument completion suggestions |

Primary targets for fault injection: `tools/call`, `resources/read`, `initialize`.
Lower-risk methods: `tools/list`, `resources/list`, `prompts/list`, `prompts/get`.

## Error Injection Strategy

### JSON-RPC Error Codes

Standard JSON-RPC 2.0 error codes to inject:

| Code | Name | When to Inject |
|------|------|----------------|
| -32700 | Parse error | Simulate malformed server responses |
| -32600 | Invalid Request | Simulate server rejecting request |
| -32601 | Method not found | Simulate capability not available |
| -32602 | Invalid params | Simulate bad parameter handling |
| -32603 | Internal error | General server-side failure (most useful) |
| -32000 | Server error (MCP-defined) | Tool execution failure |

### Injection Strategy

The proxy will use the same probability-based injection as the OpenAI proxy, but return JSON-RPC error responses instead of HTTP error status codes:

1. On each `tools/call` or `resources/read`, check `should_inject(fail_rate)`.
2. If injecting, pick error code from configured `mcp_error_codes` (similar to `error_codes`).
3. Return a JSON-RPC error response with `id` from request, appropriate error object.
4. Track the injection in `Stats.injected_faults`.

For `initialize` failures, return error with code -32603 to simulate server unavailability.

HTTP-level faults (connection refused, timeout) can also be simulated for proxy mode.

### MCP vs HTTP Error Distinction

- MCP errors are always HTTP 200 with JSON-RPC error body (correct behavior)
- Connection failures / timeouts are HTTP-level (502/504 from proxy)
- The proxy returns HTTP 200 with JSON-RPC error bodies for protocol-level faults
- The proxy returns HTTP 502/504 for transport-level faults

## Tool Call Fingerprinting for Duplicate Detection

A tool call fingerprint is derived from:
- `method` (e.g. `tools/call`)
- `params.name` (tool name, e.g. `read_file`)
- `params.arguments` (serialized, sorted keys for consistency)

```python
import hashlib, json

def fingerprint_mcp_request(request: dict) -> str:
    method = request.get("method", "")
    params = request.get("params", {})
    # Normalize: sort keys for consistent hashing
    normalized = json.dumps({"method": method, "params": params}, sort_keys=True)
    return hashlib.sha256(normalized.encode()).hexdigest()
```

For `resources/read`, fingerprint is derived from `params.uri`.
For `tools/list` / `resources/list`, fingerprint includes method only (no params).

The `id` field is excluded from fingerprinting because the same logical call may have different ids across retries. This allows detection of repeated identical tool calls even when the request id changes.

## Architecture Decision: Separate vs Extended FastAPI App

Decision: extend the existing FastAPI app with new routes under a separate router prefix.

Rationale:
- Avoids duplicating uvicorn startup, config loading, signal handling
- MCP proxy and OpenAI proxy can run side-by-side (different ports or path prefixes)
- Shared `Stats`, `Config`, and helper functions (should_inject, maybe_delay)
- Separate module `agentbreak/mcp_proxy.py` keeps code organized
- New router mounted at `/mcp` prefix (e.g., `POST /mcp/message`)
- Optional: run as standalone via `python -m agentbreak.mcp_proxy`

## Module Structure

```
agentbreak/
  main.py              # Existing OpenAI proxy (unchanged)
  mcp_protocol.py      # MCP message types (MCPRequest, MCPResponse, MCPError)
  mcp_proxy.py         # MCP FastAPI router + proxy logic
  mcp_transport.py     # Transport abstractions (stdio, SSE, HTTP)
  mcp_mock.py          # Mock response generators
  __init__.py

tests/
  test_main.py         # Existing OpenAI proxy tests (unchanged)
  test_mcp_protocol.py # MCP message parsing tests
  test_mcp_proxy.py    # MCP proxy logic tests
  test_mcp_transport.py # Transport tests
```

## Config Extensions

New fields in the `Config` dataclass:

```python
mcp_mode: str = "disabled"          # "disabled", "mock", "proxy"
mcp_upstream_transport: str = "http" # "stdio", "sse", "http"
mcp_upstream_command: str = ""       # Command for stdio servers
mcp_upstream_url: str = ""           # URL for HTTP/SSE servers
mcp_fail_rate: float = 0.0           # MCP-specific fail rate
mcp_error_codes: tuple = (-32603, -32000)  # MCP error codes to inject
```

## Stats Extensions

New fields in the `Stats` dataclass:

```python
mcp_initialize_requests: int = 0
mcp_tools_list_requests: int = 0
mcp_tool_calls: int = 0
mcp_tool_successes: int = 0
mcp_tool_failures: int = 0
mcp_resource_reads: int = 0
mcp_resource_successes: int = 0
mcp_resource_failures: int = 0
mcp_injected_faults: int = 0
mcp_duplicate_tool_calls: int = 0
mcp_suspected_loops: int = 0
```

## Scorecard Endpoint

`GET /_agentbreak/mcp/scorecard` returns:
- Total tool calls, success/failure breakdown
- Injected faults count
- Duplicate tool call count, suspected loop count
- Per-tool success/failure breakdown
- Score (0-100) with same deduction logic as OpenAI scorecard

`GET /_agentbreak/mcp/tool-calls` returns:
- Last 20 tool calls with method, tool name, args fingerprint, result, timing

## Proxy Flow for HTTP Transport

```
MCP Client
    │
    │  POST /mcp/message  (JSON-RPC request)
    ▼
AgentBreak MCP Proxy
    │
    ├── 1. Parse JSON-RPC request (validate structure)
    ├── 2. Fingerprint request (exclude id field)
    ├── 3. Track in stats (increment method counter)
    ├── 4. should_inject(mcp_fail_rate)?
    │       YES → return JSON-RPC error response (HTTP 200)
    │       NO  → continue
    ├── 5. maybe_delay(latency_p, latency_min, latency_max)
    ├── 6. mode == "mock"?
    │       YES → return mock response for method
    │       NO  → forward to upstream MCP server
    ├── 7. Handle upstream response
    │       success → track mcp_tool_successes
    │       error   → track mcp_tool_failures
    └── 8. Return response to client
```
