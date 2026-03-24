# AgentBreak

Chaos proxy for testing how your agents handle failures. Sits between your agent and the LLM/MCP server, injects faults.

```
Agent  →  AgentBreak (localhost:5005)  →  Real LLM / MCP server
               ↑
          scenarios.yaml
```

## Quick start

```bash
pip install agentbreak
cp config.example.yaml application.yaml    # edit: llm.mode, mcp.enabled
cp scenarios.example.yaml scenarios.yaml   # edit: faults to inject
agentbreak serve --config application.yaml --scenarios scenarios.yaml
```

Point your agent at `http://localhost:5005/v1` instead of the real API. Check results:

```bash
curl localhost:5005/_agentbreak/scorecard
```

## Config

**application.yaml** -- what to proxy:

```yaml
llm:
  enabled: true
  mode: mock           # mock (no API key) or proxy (forwards to upstream)
  upstream_url: https://api.openai.com
mcp:
  enabled: false       # set true + upstream_url for MCP testing
serve:
  port: 5005
```

**scenarios.yaml** -- what faults to inject:

```yaml
version: 1
scenarios:
  - name: slow-llm
    summary: Latency spike on completions
    target: llm_chat           # or mcp_tool
    fault:
      kind: latency
      min_ms: 2000
      max_ms: 5000
    schedule:
      mode: random
      probability: 0.3
```

Or use a preset: `brownout`, `mcp-slow-tools`, `mcp-tool-failures`, `mcp-mixed-transient`.

## Fault kinds

`http_error`, `latency`, `timeout` (MCP only), `empty_response`, `invalid_json`, `schema_violation`, `wrong_content`, `large_response`

## MCP testing

```bash
agentbreak inspect --config application.yaml   # discover tools
agentbreak serve --config application.yaml --scenarios scenarios.yaml
# Agent connects to http://localhost:5005/mcp
```

## CLI

```bash
agentbreak serve      # start proxy
agentbreak validate   # check config
agentbreak inspect    # discover MCP tools
agentbreak verify     # run tests
```

## Claude Code skill

```bash
npx skills add mnvsk97/agentbreak
```

## Examples

See [examples/](examples/) -- ReAct agent, MCP server, E2E harness.
