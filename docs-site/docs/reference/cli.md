# CLI Reference

All commands work from your project root (where `.agentbreak/` lives).

## `agentbreak init`

Create `.agentbreak/` directory with default `application.yaml` and `scenarios.yaml`.

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
```

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
| `GET /healthz` | Health check |
