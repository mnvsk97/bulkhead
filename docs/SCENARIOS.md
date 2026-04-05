# Scenario Reference

Each scenario in `.agentbreak/scenarios.yaml` has a **target**, a **fault**, and a **schedule**. You can add as many as you want.

## Targets

| Target | What it hits |
|--------|-------------|
| `llm_chat` | OpenAI `/v1/chat/completions` and Anthropic `/v1/messages` |
| `mcp_tool` | MCP tool calls, resource reads, prompt gets |

## Fault kinds

| Fault | What it does | Required fields |
|-------|-------------|-----------------|
| `http_error` | Returns an HTTP error | `status_code` |
| `latency` | Adds a random delay | `min_ms`, `max_ms` |
| `timeout` | Delay + 504 (MCP only) | `min_ms`, `max_ms` |
| `empty_response` | Returns empty body | -- |
| `invalid_json` | Returns unparseable JSON | -- |
| `schema_violation` | Corrupts response structure | -- |
| `wrong_content` | Replaces response content | `body` (optional) |
| `large_response` | Returns oversized response | `size_bytes` |

## Schedules

| Mode | Fields | Behavior |
|------|--------|----------|
| `always` | -- | Every matching request |
| `random` | `probability` (0.0-1.0) | Probabilistic |
| `periodic` | `every`, `length` | `length` faults every `every` requests |

## Match fields

Use the `match` field to scope faults to specific requests:

| Field | Applies to | Description |
|-------|-----------|-------------|
| `model` | `llm_chat` | Match a specific model name |
| `tool_name` | `mcp_tool` | Match an exact tool/resource/prompt name |
| `tool_name_pattern` | `mcp_tool` | Wildcard match (e.g. `search_*`) |
| `route` | both | Match a specific request path |
| `method` | both | Match HTTP method or MCP method |

```yaml
# Only affect GPT-4o requests
- name: gpt4o-errors
  summary: Errors on GPT-4o only
  target: llm_chat
  match:
    model: gpt-4o
  fault:
    kind: http_error
    status_code: 429
  schedule:
    mode: random
    probability: 0.3

# Only affect a specific MCP tool
- name: search-timeout
  summary: search_docs times out
  target: mcp_tool
  match:
    tool_name: search_docs
  fault:
    kind: timeout
    min_ms: 5000
    max_ms: 10000
  schedule:
    mode: always

# Wildcard match on tool names
- name: search-tools-slow
  summary: All search_* tools are slow
  target: mcp_tool
  match:
    tool_name_pattern: "search_*"
  fault:
    kind: latency
    min_ms: 3000
    max_ms: 8000
  schedule:
    mode: random
    probability: 0.5
```

## Presets

Skip manual config and use a built-in bundle:

```yaml
preset: brownout
```

Or combine a preset with custom scenarios:

```yaml
preset: brownout
scenarios:
  - name: custom-fault
    summary: My extra fault
    target: mcp_tool
    fault:
      kind: http_error
      status_code: 503
    schedule:
      mode: random
      probability: 0.2
```

Available presets:

| Preset | What it does |
|--------|-------------|
| `standard` | Baseline LLM faults — rate limit, server error, latency, invalid JSON, empty response, schema violation (6 scenarios) |
| `standard-mcp` | Baseline MCP faults — 503, timeout, latency, empty response, invalid JSON, schema violation, wrong content (7 scenarios) |
| `standard-all` | Both LLM + MCP baselines combined (13 scenarios) |
| `brownout` | Random LLM latency + rate limits |
| `mcp-slow-tools` | 90% of MCP tool calls are slow |
| `mcp-tool-failures` | 30% of MCP tool calls return 503 |
| `mcp-mixed-transient` | Light MCP latency + errors |
