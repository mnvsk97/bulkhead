# CLI Reference

All commands work from your project root (where `.agentbreak/` lives).

## `agentbreak init`

Create `.agentbreak/` directory with default `application.yaml` and `scenarios.yaml`. Auto-detects your provider (OpenAI/Anthropic), framework, and MCP usage. Generates `scenarios.yaml` with a standard preset based on what's enabled.

```bash
agentbreak init
```

## `agentbreak serve`

Start the chaos proxy.

```bash
agentbreak serve              # start on default port (5005)
agentbreak serve -v           # verbose logging
agentbreak serve --label "description"   # label for run history
```

## `agentbreak validate`

Check your config files for errors.

```bash
agentbreak validate
agentbreak validate --test-connection   # also test upstream auth (proxy mode)
```

The `--test-connection` flag makes a lightweight request to each proxy-mode upstream to verify connectivity and auth. Results:

- **OK** — upstream reachable, auth works
- **AUTH FAILED (401)** — bad API key or token
- **FORBIDDEN (403)** — key valid but insufficient permissions
- **CONNECTION FAILED** — wrong URL or upstream is down
- **TIMEOUT** — upstream too slow to respond

## `agentbreak inspect`

Connect to your MCP server and discover available tools, resources, and prompts. Writes `.agentbreak/registry.json`.

```bash
agentbreak inspect
```

## `agentbreak verify`

Run the test suite (pytest).

```bash
agentbreak verify
```

## `agentbreak mcp-server`

Start AgentBreak as an MCP server (for advanced use). Most users should use the [Claude Code plugin](../getting-started/plugin.md) instead.

```bash
agentbreak mcp-server    # requires: pip install mcp
```

## `agentbreak history`

List past runs (requires `history.enabled: true` in `application.yaml`).

```bash
agentbreak history
agentbreak history show <id>
agentbreak history compare <a> <b>
```

## Endpoints

When the proxy is running:

| Endpoint | Description |
|----------|-------------|
| `POST /v1/chat/completions` | OpenAI proxy |
| `POST /v1/messages` | Anthropic proxy |
| `POST /mcp` | MCP proxy |
| `GET /_agentbreak/scorecard` | LLM scorecard |
| `GET /_agentbreak/mcp-scorecard` | MCP scorecard |
| `GET /_agentbreak/requests` | Recent LLM requests |
| `GET /_agentbreak/mcp-requests` | Recent MCP requests |
| `POST /_agentbreak/reset` | Reset all runtime counters |
| `GET /_agentbreak/history` | List past runs (if history enabled) |
| `GET /_agentbreak/history/{id}` | Get details for a specific run |
| `GET /healthz` | Health check |
