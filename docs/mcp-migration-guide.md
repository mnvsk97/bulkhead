# MCP Proxy Migration Guide

This guide helps existing AgentBreak users add MCP (Model Context Protocol) proxy testing to their workflow.

## What's New in AgentBreak 0.2.0

AgentBreak 0.2.0 adds MCP proxy support, allowing you to test resilience of applications that use MCP servers. The MCP proxy works similarly to the existing OpenAI proxy but handles JSON-RPC 2.0 requests instead of chat completions.

## Quick Overview

Before: AgentBreak tested OpenAI-compatible LLM APIs.
After: AgentBreak can now test both OpenAI APIs AND MCP servers.

## MCP Proxy Basics

MCP is a protocol used by Claude Code and other AI tools to interact with external services through:
- Tools (functions that can be called)
- Resources (files, data that can be read)
- Prompts (pre-defined message templates)

The MCP proxy sits between MCP clients and MCP servers, injecting faults and tracking behavior.

### Modes

- **mock mode**: Returns fake responses without a real MCP server. Great for initial testing.
- **proxy mode**: Forwards to a real MCP server while injecting faults.

### Transports

- **HTTP**: MCP server exposes an HTTP endpoint
- **SSE**: MCP server uses Server-Sent Events
- **stdio**: MCP server communicates via stdin/stdout (subprocess)

## Migration Steps

### Step 1: Understand Your MCP Setup

Identify which MCP servers your application uses:
- Are they local services (stdio transport)?
- Are they network services (HTTP/SSE transport)?
- What tools and resources do they expose?

### Step 2: Update Your Test Configuration

Create or update your `config.yaml` to include MCP settings:

```yaml
# Existing OpenAI proxy configuration
mode: proxy
upstream_url: https://api.openai.com
scenario: mixed-transient
fail_rate: 0.2

# NEW: MCP proxy configuration
mcp_mode: mock  # or "proxy"
mcp_upstream_transport: http  # or "stdio" or "sse"
mcp_upstream_url: http://localhost:8080  # Required for http/sse
# mcp_upstream_command: python my_mcp_server.py  # Required for stdio
mcp_fail_rate: 0.1
mcp_error_codes: [429, 500, 503]
```

### Step 3: Start the MCP Proxy

Run the MCP proxy (separate from the OpenAI proxy):

```bash
# Start in mock mode (no real MCP server needed)
agentbreak mcp start --mode mock --scenario mcp-mixed-transient

# Start in proxy mode (forwards to real MCP server)
agentbreak mcp start \
  --mode proxy \
  --upstream-url http://localhost:8080 \
  --scenario mcp-tool-failures \
  --fail-rate 0.3
```

The MCP proxy runs on port 5001 by default. Your MCP client should point to `http://localhost:5001/mcp`.

### Step 4: Update Your MCP Client Configuration

Configure your MCP client to use the AgentBreak proxy URL:

**Example: Claude Code MCP Configuration**
```json
{
  "mcpServers": {
    "my-server": {
      "url": "http://localhost:5001/mcp",
      "transport": "http"
    }
  }
}
```

**Example: Python MCP Client**
```python
from mcp import Client

client = Client("http://localhost:5001/mcp")
```

### Step 5: Run Your Tests

Your application should now be testing against the MCP proxy instead of the real MCP server. The proxy will:
- Inject faults according to your scenario
- Add latency to test timeout handling
- Track tool calls, resource reads, and duplicate requests
- Detect potential retry loops

## MCP Scenarios

AgentBreak includes built-in MCP fault scenarios:

| Scenario | What it Tests |
|----------|---------------|
| `mcp-tool-failures` | Tool call retry and backoff logic (30% of calls fail) |
| `mcp-resource-unavailable` | Resource read fallback handling (50% of reads fail) |
| `mcp-slow-tools` | Timeout handling for slow tool backends (90% get latency) |
| `mcp-initialization-failure` | Initialization retry logic (50% of init calls fail) |
| `mcp-mixed-transient` | General resilience in brownout conditions (20% failure, 10% latency) |

Use scenarios with `--scenario` flag:
```bash
agentbreak mcp start --scenario mcp-tool-failures
```

## Checking MCP Test Results

### View the Scorecard

```bash
curl http://localhost:5001/_agentbreak/mcp/scorecard
```

The scorecard includes:
- `tool_calls`: Total number of tool calls made
- `resource_reads`: Total number of resource reads
- `injected_faults`: Number of faults injected by AgentBreak
- `upstream_successes` / `upstream_failures`: Real MCP server interaction results
- `duplicate_requests`: Tool calls that were repeated (retry behavior)
- `suspected_loops`: Same tool call made 3+ times (potential infinite loop)
- `tool_successes_by_name`: Success/failure counts per tool
- `resource_reads_by_uri`: Success/failure counts per resource URI
- `resilience_score`: Overall score (0-100, higher is better)

### View Recent Tool Calls

```bash
curl http://localhost:5001/_agentbreak/mcp/tool-calls
```

Shows the last 20 tool calls with their fingerprints and full request bodies.

## Using Both Proxies Simultaneously

You can run both OpenAI and MCP proxies at the same time:

```bash
# Terminal 1: OpenAI proxy (port 5000)
agentbreak start --mode proxy --upstream-url https://api.openai.com --scenario mixed-transient

# Terminal 2: MCP proxy (port 5001)
agentbreak mcp start --mode proxy --upstream-url http://localhost:8080 --scenario mcp-tool-failures
```

Your application can use both:
- OpenAI API calls → `http://localhost:5000/v1`
- MCP tool calls → `http://localhost:5001/mcp`

## Common Migration Scenarios

### Scenario 1: Adding MCP Tests to Existing LLM Tests

You already test your LLM resilience with AgentBreak. Now you want to add MCP testing.

1. Keep your existing `config.yaml` for OpenAI proxy testing
2. Add MCP proxy configuration to the same file or create `config.mcp.yaml`
3. Run both proxies in parallel

### Scenario 2: Migrating from Direct MCP Testing

You currently test by manually failing MCP servers or using flaky test servers.

1. Replace manual failure injection with AgentBreak's controlled scenarios
2. Use mock mode for faster, more deterministic tests
3. Use the scorecard for standardized metrics

### Scenario 3: Testing MCP-Only Applications

Your application uses MCP but not OpenAI APIs.

1. Only run the MCP proxy (no OpenAI proxy needed)
2. Configure your MCP client to use `http://localhost:5001/mcp`
3. Use MCP-specific scenarios

## Troubleshooting

### Issue: "mode must be 'proxy' or 'mock'"

This error occurs when using the `agentbreak start` command with `--mcp-mode` flag incorrectly. Use the dedicated MCP command:

```bash
# Instead of:
agentbreak start --mcp-mode proxy

# Use:
agentbreak mcp start --mode proxy
```

### Issue: "upstream-url is required for http and sse transports"

When using `--mode proxy` with HTTP or SSE transport, provide the MCP server URL:

```bash
agentbreak mcp start --mode proxy --upstream-url http://localhost:8080
```

### Issue: "upstream-command is required for stdio transport"

When using stdio transport, provide the command to start the MCP server:

```bash
agentbreak mcp start --mode proxy --upstream-transport stdio --upstream-command 'python server.py'
```

### Issue: My MCP client can't connect

Verify:
1. The MCP proxy is running: `curl http://localhost:5001/healthz`
2. Your client is using the correct URL: `http://localhost:5001/mcp` (include `/mcp` path)
3. No firewall is blocking port 5001

### Issue: No faults are being injected

Check:
1. The `--fail-rate` or scenario is set correctly
2. Your client is actually pointed at the proxy, not directly at the MCP server
3. The proxy mode is "proxy" (not "mock", which always succeeds)

### Issue: Tests are flaky/inconsistent

Use deterministic runs for reproducibility:

```bash
agentbreak mcp start --mode mock --scenario mcp-tool-failures --seed 42
```

The `--seed` flag makes fault injection deterministic, useful for CI/CD.

## CLI Commands Reference

### Start MCP Proxy

```bash
agentbreak mcp start [OPTIONS]

Options:
  --mode TEXT              proxy | mock (default: mock)
  --upstream-url TEXT      Base URL of MCP server (proxy mode)
  --upstream-transport TEXT  http | sse | stdio (default: http)
  --upstream-command TEXT    Command for stdio transport
  --upstream-timeout FLOAT Timeout in seconds (default: 30)
  --scenario TEXT           Built-in MCP scenario name
  --fail-rate FLOAT         Probability of injecting a fault (0.0-1.0)
  --latency-p FLOAT         Probability of injecting latency
  --latency-min FLOAT      Min delay in seconds (default: 5)
  --latency-max FLOAT      Max delay in seconds (default: 15)
  --seed INT              Fix random seed for deterministic runs
  --port INT              Port to bind on (default: 5001)
```

### Test MCP Server

```bash
agentbreak mcp test [OPTIONS]

Options:
  --url TEXT        URL of MCP server (default: http://localhost:5001)
  --transport TEXT  http | sse | stdio (default: http)
  --command TEXT    Command for stdio transport
  --timeout FLOAT  Request timeout in seconds (default: 30)
```

### List Tools

```bash
agentbreak mcp list-tools [OPTIONS]

Options:
  --url TEXT        URL of MCP server (default: http://localhost:5001)
  --transport TEXT  http | sse | stdio (default: http)
  --command TEXT    Command for stdio transport
  --timeout FLOAT  Request timeout in seconds (default: 30)
```

### Call Tool

```bash
agentbreak mcp call-tool TOOL_NAME [OPTIONS]

Options:
  --args TEXT       JSON-encoded arguments dict (default: {})
  --url TEXT        URL of MCP server (default: http://localhost:5001)
  --transport TEXT  http | sse | stdio (default: http)
  --command TEXT    Command for stdio transport
  --timeout FLOAT  Request timeout in seconds (default: 30)
```

## Next Steps

1. Read the [MCP Proxy Guide](mcp-proxy-guide.md) for detailed usage
2. Check the [examples/mcp_client](../examples/mcp_client/) directory for example implementations
3. Review the [MCP Architecture](mcp-proxy-architecture.md) for technical details
4. Run the full test suite: `pytest tests/`

## Support

For issues or questions:
- Check the [FAQ](mcp-proxy-guide.md#faq) section
- Review [existing issues](https://github.com/mnvsk97/agentbreak/issues)
- Open a new issue if you encounter unexpected behavior
