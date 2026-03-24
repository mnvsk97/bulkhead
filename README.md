# AgentBreak

A local chaos proxy for testing how your agents handle failures.

```
Your Agent  -->  AgentBreak (localhost:5000)  -->  Real LLM / MCP server
                      |
                 injects faults from
                 scenarios.yaml
```

It sits between your agent and two surfaces:

- OpenAI-compatible `POST /v1/chat/completions`
- MCP servers over streamable HTTP (`POST /mcp`)

and injects configurable faults: HTTP errors, latency, invalid JSON, empty bodies, corrupt tool-call shapes, timeouts, oversized responses, and more.

## Claude Code Skill

Install the AgentBreak skill into any project with [skills.sh](https://skills.sh):

```bash
npx skills add mnvsk97/agentbreak
```

Then ask Claude Code to run chaos tests -- it knows the full workflow.

## Usage

### 1. Install

```bash
pip install agentbreak
```

### 2. Configure

```bash
cp config.example.yaml application.yaml
cp scenarios.example.yaml scenarios.yaml
```

**application.yaml** -- where to proxy traffic:

```yaml
llm:
  enabled: true
  mode: proxy              # "proxy" forwards to real LLM, "mock" fakes responses
  upstream_url: https://api.openai.com
  auth:
    type: bearer
    env: OPENAI_API_KEY    # reads token from this env var

mcp:
  enabled: false           # set true if testing MCP tools too

serve:
  host: 0.0.0.0
  port: 5000
```

**scenarios.yaml** -- what faults to inject:

```yaml
version: 1
scenarios:
  - name: slow-llm
    target: llm_chat
    fault:
      kind: latency
      min_ms: 5000
      max_ms: 15000
    schedule:
      mode: random
      probability: 0.2    # 20% of requests get 5-15s delay
```

Or use a one-liner preset:

```yaml
version: 1
preset: brownout
```

### 3. Start the proxy

```bash
agentbreak serve --config application.yaml --scenarios scenarios.yaml
```

### 4. Point your agent at AgentBreak

Change your agent's base URL from the real API to AgentBreak:

```python
# Before
client = OpenAI()

# After
client = OpenAI(base_url="http://localhost:5000/v1")
```

Your agent thinks it's talking to OpenAI. AgentBreak forwards requests upstream and injects faults along the way.

### 5. Check the scorecard

While your agent runs:

```bash
curl http://localhost:5000/_agentbreak/scorecard
```

```json
{
  "requests_seen": 12,
  "injected_faults": 3,
  "upstream_successes": 9,
  "duplicate_requests": 0,
  "suspected_loops": 0,
  "run_outcome": "PASS",
  "resilience_score": 91
}
```

When you Ctrl+C the server, the full scorecard prints to stderr.

### 6. MCP testing (optional)

If your agent uses MCP tools, enable MCP and discover the upstream server:

```bash
# Edit application.yaml: set mcp.enabled: true, mcp.upstream_url
agentbreak inspect --config application.yaml   # discovers tools, writes registry
agentbreak serve --config application.yaml --scenarios scenarios.yaml
```

Point your agent's MCP client at `http://localhost:5000/mcp`. AgentBreak mirrors the real server's tools/resources/prompts and injects faults per your scenarios.

### In CI

```bash
pip install agentbreak
agentbreak serve --config application.yaml --scenarios scenarios.yaml &
pytest your_agent_tests/
curl -s localhost:5000/_agentbreak/scorecard | jq .resilience_score
```

### Mock mode (no API key needed)

Set `llm.mode: mock` in application.yaml. AgentBreak returns fake OpenAI responses but still applies all faults. Good for CI or testing fault handling without burning tokens.

## Scenario Reference

Each scenario is one fault applied to one target:

```yaml
version: 1
scenarios:
  - name: docs-tool-invalid-schema
    summary: Corrupt one MCP tool result
    target: mcp_tool            # llm_chat or mcp_tool
    match:
      tool_name: search_docs    # optional filter
    fault:
      kind: schema_violation
    schedule:
      mode: random
      probability: 0.3
    tags: [mcp, schema]         # optional labels
```

**Targets:** `llm_chat`, `mcp_tool`

**Match fields** (all optional, all must match if set):

| Field | Example | Notes |
|-------|---------|-------|
| `tool_name` | `search_docs` | Exact match |
| `tool_name_pattern` | `search_*` | Glob pattern |
| `route` | `/v1/chat/completions` | Request path |
| `method` | `tools/call` | MCP method or HTTP method |
| `model` | `gpt-4o` | LLM model name |

**Fault kinds:**

| Kind | Extra fields | Notes |
|------|-------------|-------|
| `http_error` | `status_code` (required) | |
| `latency` | `min_ms`, `max_ms` (required) | |
| `timeout` | `min_ms`, `max_ms` (required) | MCP only |
| `empty_response` | | |
| `invalid_json` | | |
| `schema_violation` | | Corrupts tool_calls (LLM) or result shape (MCP) |
| `wrong_content` | `body` (optional) | |
| `large_response` | `size_bytes` (required, > 0) | |

**Schedule modes:**

| Mode | Fields | Behavior |
|------|--------|----------|
| `always` | | Every matching request |
| `random` | `probability` (0.0-1.0) | Fire with given probability |
| `periodic` | `every`, `length` | Fire for `length` out of every `every` requests |

**Presets** (expand into multiple scenarios):

| Preset | What it does |
|--------|-------------|
| `brownout` | Random latency + HTTP errors on LLM |
| `mcp-slow-tools` | Latency on MCP tool calls |
| `mcp-tool-failures` | HTTP errors on MCP tool calls |
| `mcp-mixed-transient` | Mix of latency, errors, and timeouts on MCP |

## Endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /healthz` | Health check |
| `GET /_agentbreak/scorecard` | LLM scorecard |
| `GET /_agentbreak/requests` | LLM recent requests |
| `GET /_agentbreak/llm-scorecard` | LLM scorecard (explicit) |
| `GET /_agentbreak/llm-requests` | LLM recent requests (explicit) |
| `GET /_agentbreak/mcp-scorecard` | MCP scorecard (method counts, per-tool stats) |
| `GET /_agentbreak/mcp-requests` | MCP recent requests |

## Built-in Detection

These are always on, no configuration needed:

- **Duplicate detection** -- flags when the same request body is seen twice
- **Loop detection** -- flags 3+ identical requests (agent is probably stuck)
- **Session recovery** -- re-initializes upstream MCP session on expiry
- **Resilience scoring** -- 0-100 score based on faults, failures, and loops

## CLI Commands

```bash
agentbreak serve     # start the proxy
agentbreak validate  # check config without starting
agentbreak inspect   # discover MCP tools and write registry
agentbreak verify    # run test suite (--live for full E2E harness)
```

All commands accept `--config`, `--scenarios`, and `--registry` flags. `serve` also accepts `--verbose / -v`.

## Examples

- [Simple LangChain client](examples/simple_langchain)
- [Simple MCP server](examples/simple_mcp_server)
- [LangGraph report agent](examples/langgraph_report_agent)
- [Reporting MCP server](examples/reporting_mcp_server)

## Development

```bash
pip install -e '.[dev]'
agentbreak verify          # run pytest
agentbreak verify --live   # pytest + live LangGraph E2E harness
```

## More

- [Contributing](CONTRIBUTING.md)
- [Failure Modes](docs/FAILURE_MODES.md)
- [Deferred Targets](docs/TODO_SCENARIOS.md)
- [Live Testing](docs/live-testing.md)
- [Docs Index](docs/README.md)
