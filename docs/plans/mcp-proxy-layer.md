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

- [ ] Create `agentbreak/mcp_proxy.py` with MCP proxy FastAPI app
- [ ] Implement POST endpoint for MCP JSON-RPC 2.0 requests
- [ ] Add request recording with fingerprinting similar to OpenAI proxy
- [ ] Implement fault injection for MCP tool calls
- [ ] Map HTTP error codes to MCP error codes (-32600 to -32603, -32000 for tools)
- [ ] Add latency injection using existing `maybe_delay()` pattern
- [ ] Track MCP-specific stats: tool calls, resource reads, initialization requests
- [ ] Add unit tests in `tests/test_mcp_proxy.py`

### Task 4: Implement MCP mock mode

- [ ] Create mock response generators for common MCP methods
- [ ] Mock `initialize` to return fake server capabilities
- [ ] Mock `tools/list` to return sample tool definitions
- [ ] Mock `tools/call` to return fake tool results
- [ ] Mock `resources/list` to return sample resource URIs
- [ ] Mock `resources/read` to return fake resource content
- [ ] Mock `prompts/list` to return sample prompt templates
- [ ] Add configuration for which tools/resources to mock

### Task 5: Implement MCP proxy mode with upstream forwarding

- [ ] Add support for stdio transport to upstream MCP server
- [ ] Add support for SSE transport to upstream MCP server
- [ ] Implement request forwarding with header filtering
- [ ] Handle streaming responses for tools/call results
- [ ] Track upstream successes and failures
- [ ] Add timeout handling for slow upstream servers
- [ ] Add integration tests with real MCP server

### Task 6: Add MCP configuration support

- [ ] Extend `Config` dataclass with MCP-specific fields
- [ ] Add `mcp_mode` field: "disabled", "mock", "proxy"
- [ ] Add `mcp_upstream_transport` field: "stdio", "sse", "http"
- [ ] Add `mcp_upstream_command` for stdio servers
- [ ] Add `mcp_upstream_url` for HTTP/SSE servers
- [ ] Add `mcp_fail_rate` and `mcp_error_codes` configuration
- [ ] Update `config.example.yaml` with MCP examples
- [ ] Update CLI with `--mcp-mode` and related flags

### Task 7: Implement MCP scorecard and observability

- [ ] Extend `Stats` dataclass with MCP-specific fields
- [ ] Track MCP method call counts (initialize, tools/list, tools/call, etc.)
- [ ] Track tool call successes/failures by tool name
- [ ] Track resource read successes/failures by URI pattern
- [ ] Add duplicate tool call detection
- [ ] Add suspected loop detection for repeated tool calls
- [ ] Create `/_agentbreak/mcp/scorecard` endpoint
- [ ] Create `/_agentbreak/mcp/tool-calls` endpoint for recent tool call history
- [ ] Update `print_scorecard()` to include MCP stats

### Task 8: Add MCP-specific fault scenarios

- [ ] Create `mcp-tool-failures` scenario: tools/call returns errors
- [ ] Create `mcp-resource-unavailable` scenario: resources/read fails
- [ ] Create `mcp-slow-tools` scenario: high latency for tool calls
- [ ] Create `mcp-initialization-failure` scenario: initialize fails intermittently
- [ ] Create `mcp-mixed-transient` scenario: combination of MCP failures
- [ ] Update `SCENARIOS` dict with MCP scenarios
- [ ] Document MCP scenarios in README

### Task 9: Add MCP transport layer abstraction

- [ ] Create `agentbreak/mcp_transport.py` with transport interface
- [ ] Implement `StdioTransport` for stdio-based MCP servers
- [ ] Implement `SSETransport` for SSE-based MCP servers
- [ ] Implement `HTTPTransport` for HTTP-based MCP servers
- [ ] Handle connection lifecycle: start, stop, reconnect
- [ ] Add timeout and error handling for each transport
- [ ] Add integration tests for each transport type

### Task 10: Create MCP proxy CLI commands

- [ ] Add `agentbreak mcp start` subcommand
- [ ] Add `agentbreak mcp test` subcommand to test MCP server connectivity
- [ ] Add `agentbreak mcp list-tools` to query available tools from upstream
- [ ] Add `agentbreak mcp call-tool` to manually invoke a tool through proxy
- [ ] Update help text and documentation
- [ ] Add examples for each command in README

### Task 11: Create example MCP applications

- [ ] Create `examples/mcp_client/` with simple MCP client
- [ ] Create example using `mcp` Python package
- [ ] Demonstrate tool calling through proxy
- [ ] Demonstrate resource reading through proxy
- [ ] Add README with setup instructions
- [ ] Add example showing retry logic on MCP errors

### Task 12: Update documentation and tests

- [ ] Update main README.md with MCP proxy section
- [ ] Create `docs/mcp-proxy-guide.md` with detailed usage
- [ ] Document MCP error codes and mapping
- [ ] Add FAQ section for MCP-specific questions
- [ ] Create integration test suite for end-to-end MCP scenarios
- [ ] Update `.claude-plugin/commands/agentbreak.md` with MCP commands
- [ ] Add MCP examples to `skills/agentbreak-testing/SKILL.md`

### Task 13: Add MCP proxy performance optimizations

- [ ] Add connection pooling for HTTP/SSE transports
- [ ] Implement request batching for multiple tool calls
- [ ] Add caching layer for resources/list responses
- [ ] Add metrics for proxy overhead measurement
- [ ] Profile and optimize JSON-RPC parsing
- [ ] Add benchmarks comparing proxy vs direct MCP calls

### Task 14: Final integration and polish

- [ ] Test MCP proxy with multiple real MCP servers (filesystem, database, web)
- [ ] Verify compatibility with Claude Code MCP integration
- [ ] Add comprehensive error messages for common misconfigurations
- [ ] Update version number in `pyproject.toml`
- [ ] Add MCP proxy to PyPI release notes
- [ ] Create migration guide for users adding MCP testing
- [ ] Run full test suite and fix any regressions
