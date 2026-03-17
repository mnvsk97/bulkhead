# AgentBreak Release Notes

## Version 0.2.0 (March 17, 2026)

### MCP Proxy Support (NEW!)

AgentBreak now supports Model Context Protocol (MCP) servers in addition to OpenAI-compatible APIs. The MCP proxy allows you to test the resilience of applications that use MCP tools, resources, and prompts.

**Key Features:**
- Full JSON-RPC 2.0 protocol support
- Three transport types: HTTP, SSE, and stdio
- Mock mode for testing without real MCP servers
- Proxy mode for fault injection on real MCP servers
- MCP-specific fault scenarios and error codes
- Duplicate tool call and loop detection
- MCP scorecard with detailed metrics
- CLI commands: `agentbreak mcp start/test/list-tools/call-tool`

**MCP-Specific Scenarios:**
- `mcp-tool-failures`: Tests tool call retry logic (30% failure rate)
- `mcp-resource-unavailable`: Tests resource read fallback (50% failure rate)
- `mcp-slow-tools`: Tests timeout handling with 90% latency injection
- `mcp-initialization-failure`: Tests initialization retry logic (50% failure rate)
- `mcp-mixed-transient`: General brownout conditions (20% failure, 10% latency)

**New Documentation:**
- [MCP Proxy Guide](docs/mcp-proxy-guide.md): Complete usage documentation
- [MCP Migration Guide](docs/mcp-migration-guide.md): Guide for existing users
- [MCP Architecture](docs/mcp-proxy-architecture.md): Technical design overview
- Updated README.md with MCP section

**New CLI Commands:**
```
agentbreak mcp start      -- Start MCP proxy server
agentbreak mcp test       -- Test MCP server connectivity
agentbreak mcp list-tools -- List available tools from server
agentbreak mcp call-tool   -- Manually invoke a tool
```

**Performance Optimizations:**
- Connection pooling for HTTP/SSE transports
- Request caching for list-style methods (tools/list, resources/list, prompts/list)
- Batch request support for multiple concurrent requests
- Detailed proxy overhead metrics (parse time, cache lookup time, etc.)

**Example Applications:**
- `examples/mcp_client/main.py`: Basic MCP client demonstrating tool calls
- `examples/mcp_client/retry_example.py`: Retry logic with exponential backoff

### Changes from 0.1.1

**Breaking Changes:**
- None (MCP proxy is additive, does not affect existing OpenAI proxy)

**Deprecations:**
- None

### Configuration Changes

The main `agentbreak start` command now supports additional MCP-related flags:
```yaml
mcp_mode: disabled | mock | proxy  # (default: disabled)
mcp_upstream_transport: http | stdio | sse  # (default: http)
mcp_upstream_url: URL  # For http/sse transports
mcp_upstream_command: command  # For stdio transport
mcp_fail_rate: 0.0-1.0  # Fault probability
mcp_error_codes: [code, ...]  # HTTP-style codes to inject
mcp_latency_p: 0.0-1.0  # Latency probability
```

### Testing

- 278 passing tests
- Added comprehensive integration tests for MCP proxy
- Added performance benchmarks comparing proxy vs direct MCP calls
- Added tests for connection pooling, caching, and batch requests
- Added tests for all three transport types (HTTP, SSE, stdio)

### Known Limitations

- The `TestClient` in tests has known limitations with async subprocess timeouts (2 tests skipped)
- Some edge cases with stdio transport may require manual cleanup in test environments

### Upgrade Instructions

**For existing AgentBreak users:**
No changes required. The OpenAI proxy continues to work as before. MCP proxy is an optional feature.

**For new MCP users:**
```bash
pip install agentbreak==0.2.0
agentbreak mcp start --mode mock --scenario mcp-mixed-transient
```

Point your MCP client to `http://localhost:5001/mcp`.

### Migration Guide

See [docs/mcp-migration-guide.md](docs/mcp-migration-guide.md) for detailed migration instructions including:
- Step-by-step setup
- Common migration scenarios
- Troubleshooting guide
- CLI command reference

### Acknowledgments

The MCP proxy implementation follows the [Model Context Protocol specification](https://modelcontextprotocol.io/) and supports JSON-RPC 2.0 as specified.
