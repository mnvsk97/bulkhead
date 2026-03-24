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
agentbreak init                    # creates .agentbreak/ with default configs
# edit .agentbreak/application.yaml and .agentbreak/scenarios.yaml
agentbreak serve
```

Point your agent at `http://localhost:5005` instead of the real API. Works with both OpenAI (`/v1/chat/completions`) and Anthropic (`/v1/messages`) formats. Check results:

```bash
curl localhost:5005/_agentbreak/scorecard
```

## Config

**`.agentbreak/application.yaml`** -- what to proxy:

```yaml
llm:
  enabled: true
  mode: mock           # mock (no API key needed) or proxy (forwards to upstream)
mcp:
  enabled: false       # set true + upstream_url for MCP testing
serve:
  port: 5005
```

**`.agentbreak/scenarios.yaml`** -- what faults to inject:

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
agentbreak inspect    # discover tools
agentbreak serve
# Agent connects to http://localhost:5005/mcp
```

## CLI

```bash
agentbreak init       # create .agentbreak/ config
agentbreak serve      # start proxy
agentbreak validate   # check config
agentbreak inspect    # discover MCP tools
agentbreak verify     # run tests
```

## Claude Code skill

```bash
npx skills add mnvsk97/agentbreak
```

## Docs

- [Testing Methodology](docs/TESTING_METHODOLOGY.md) -- how to design chaos tests, read results, and iterate
- [Failure Modes](docs/FAILURE_MODES.md) -- what AgentBreak simulates and what is out of scope

## Examples

See [examples/](examples/) -- agents (ReAct, DeepAgents) and MCP servers (no auth, bearer, basic, OAuth2).
