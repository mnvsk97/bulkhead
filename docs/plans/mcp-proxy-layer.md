# Plan: Implement MCP Server Proxy Layer

## Overview

Extend AgentBreak to support Model Context Protocol (MCP) servers in addition to OpenAI-compatible APIs. This will allow testing the resilience of MCP-based applications by injecting faults, latency, and tracking duplicate/looping tool calls.

MCP is a JSON-RPC 2.0 protocol used by Claude Code and other AI tools to interact with external services through tools, resources, and prompts. This proxy layer will sit between MCP clients and MCP servers, similar to how AgentBreak currently proxies OpenAI API calls.

## Validation Commands

- `pytest -q tests/`
- `python -m agentbreak.mcp_proxy --help`
- `ruff check agentbreak/`

## Success Criteria

- MCP proxy can intercept JSON-RPC 2.0 requests from MCP clients
- Supports stdio, SSE, and HTTP transports for MCP servers
- Can inject faults (errors, timeouts) into tool calls and resource reads
- Can inject latency before forwarding requests
- Tracks duplicate tool calls and suspected loops
- Provides scorecard endpoint similar to OpenAI proxy
- Works with both mock mode (fake responses) and proxy mode (real MCP servers)
- Configuration via YAML similar to existing AgentBreak config

### Task 1: Research and design MCP proxy architecture

- [x] Read MCP specification documentation to understand JSON-RPC 2.0 structure
- [x] Document MCP transport types: stdio, SSE over HTTP, HTTP with SSE
- [x] Identify key MCP methods to proxy: initialize, tools/list, tools/call, resources/list, resources/read, prompts/list, prompts/get
- [x] Design error injection strategy for MCP errors vs JSON-RPC errors
- [x] Design how to fingerprint MCP tool calls for duplicate detection
- [x] Create architecture document in `docs/mcp-proxy-architecture.md`
- [x] Decide on whether to use separate FastAPI app or extend existing one

### Task 2: Implement MCP message parsing and fingerprinting

- [x] Create `agentbreak/mcp_protocol.py` with MCP JSON-RPC 2.0 message types
- [x] Implement `MCPRequest` dataclass with id, method, params
- [x] Implement `MCPResponse` dataclass with id, result, error
- [x] Implement `MCPError` dataclass with code, message, data
- [x] Create fingerprint function for MCP tool calls based on method + params
- [x] Add unit tests in `tests/test_mcp_protocol.py`
- [x] Validate JSON-RPC 2.0 message structure parsing

### Task 3: Implement MCP proxy core logic

- [x] Create `agentbreak/mcp_proxy.py` with MCP proxy FastAPI app
- [x] Implement POST endpoint for MCP JSON-RPC 2.0 requests
- [x] Add request recording with fingerprinting similar to OpenAI proxy
- [x] Implement fault injection for MCP tool calls
- [x] Map HTTP error codes to MCP error codes (-32600 to -32603, -32000 for tools)
- [x] Add latency injection using existing `maybe_delay()` pattern
- [x] Track MCP-specific stats: tool calls, resource reads, initialization requests
- [x] Add unit tests in `tests/test_mcp_proxy.py`

### Task 4: Implement MCP mock mode

- [x] Create mock response generators for common MCP methods
- [x] Mock `initialize` to return fake server capabilities
- [x] Mock `tools/list` to return sample tool definitions
- [x] Mock `tools/call` to return fake tool results
- [x] Mock `resources/list` to return sample resource URIs
- [x] Mock `resources/read` to return fake resource content
- [x] Mock `prompts/list` to return sample prompt templates
- [x] Add configuration for which tools/resources to mock

### Task 5: Implement MCP proxy mode with upstream forwarding

- [x] Add support for stdio transport to upstream MCP server
- [x] Add support for SSE transport to upstream MCP server
- [x] Implement request forwarding with header filtering
- [x] Handle streaming responses for tools/call results
- [x] Track upstream successes and failures
- [x] Add timeout handling for slow upstream servers
- [x] Add integration tests with real MCP server

### Task 6: Add MCP configuration support

- [x] Extend `Config` dataclass with MCP-specific fields
- [x] Add `mcp_mode` field: "disabled", "mock", "proxy"
- [x] Add `mcp_upstream_transport` field: "stdio", "sse", "http"
- [x] Add `mcp_upstream_command` for stdio servers
- [x] Add `mcp_upstream_url` for HTTP/SSE servers
- [x] Add `mcp_fail_rate` and `mcp_error_codes` configuration
- [x] Update `config.example.yaml` with MCP examples
- [x] Update CLI with `--mcp-mode` and related flags

### Task 7: Implement MCP scorecard and observability

- [x] Extend `Stats` dataclass with MCP-specific fields
- [x] Track MCP method call counts (initialize, tools/list, tools/call, etc.)
- [x] Track tool call successes/failures by tool name
- [x] Track resource read successes/failures by URI pattern
- [x] Add duplicate tool call detection
- [x] Add suspected loop detection for repeated tool calls
- [x] Create `/_agentbreak/mcp/scorecard` endpoint
- [x] Create `/_agentbreak/mcp/tool-calls` endpoint for recent tool call history
- [x] Update `print_scorecard()` to include MCP stats

### Task 8: Add MCP-specific fault scenarios

- [x] Create `mcp-tool-failures` scenario: tools/call returns errors
- [x] Create `mcp-resource-unavailable` scenario: resources/read fails
- [x] Create `mcp-slow-tools` scenario: high latency for tool calls
- [x] Create `mcp-initialization-failure` scenario: initialize fails intermittently
- [x] Create `mcp-mixed-transient` scenario: combination of MCP failures
- [x] Update `SCENARIOS` dict with MCP scenarios
- [x] Document MCP scenarios in README

### Task 9: Add MCP transport layer abstraction

- [x] Create `agentbreak/mcp_transport.py` with transport interface
- [x] Implement `StdioTransport` for stdio-based MCP servers
- [x] Implement `SSETransport` for SSE-based MCP servers
- [x] Implement `HTTPTransport` for HTTP-based MCP servers
- [x] Handle connection lifecycle: start, stop, reconnect
- [x] Add timeout and error handling for each transport
- [x] Add integration tests for each transport type

### Task 10: Create MCP proxy CLI commands

- [x] Add `agentbreak mcp start` subcommand
- [x] Add `agentbreak mcp test` subcommand to test MCP server connectivity
- [x] Add `agentbreak mcp list-tools` to query available tools from upstream
- [x] Add `agentbreak mcp call-tool` to manually invoke a tool through proxy
- [x] Update help text and documentation
- [x] Add examples for each command in README

### Task 11: Create example MCP applications

- [x] Create `examples/mcp_client/` with simple MCP client
- [x] Create example using `mcp` Python package
- [x] Demonstrate tool calling through proxy
- [x] Demonstrate resource reading through proxy
- [x] Add README with setup instructions
- [x] Add example showing retry logic on MCP errors

### Task 12: Update documentation and tests

- [x] Update main README.md with MCP proxy section
- [x] Create `docs/mcp-proxy-guide.md` with detailed usage
- [x] Document MCP error codes and mapping
- [x] Add FAQ section for MCP-specific questions
- [x] Create integration test suite for end-to-end MCP scenarios
- [x] Update `.claude-plugin/commands/agentbreak.md` with MCP commands
- [x] Add MCP examples to `skills/agentbreak-testing/SKILL.md`

### Task 13: Add MCP proxy performance optimizations

- [x] Add connection pooling for HTTP/SSE transports
- [x] Implement request batching for multiple tool calls
- [x] Add caching layer for resources/list responses
- [x] Add metrics for proxy overhead measurement
- [x] Profile and optimize JSON-RPC parsing
- [x] Add benchmarks comparing proxy vs direct MCP calls

### Task 14: Final integration and polish

- [ ] Test MCP proxy with multiple real MCP servers (filesystem, database, web)
- [ ] Verify compatibility with Claude Code MCP integration
- [ ] Add comprehensive error messages for common misconfigurations
- [ ] Update version number in `pyproject.toml`
- [ ] Add MCP proxy to PyPI release notes
- [ ] Create migration guide for users adding MCP testing
- [ ] Run full test suite and fix any regressions
