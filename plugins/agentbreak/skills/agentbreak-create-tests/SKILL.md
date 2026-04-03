---
name: agentbreak-create-tests
description: Generate chaos test scenarios for AgentBreak. Produces scenarios.yaml entries that conform to the Pydantic schema, target LLM chat completions and MCP tool calls, and integrate with application.yaml.
---

# AgentBreak -- Create Chaos Scenarios

You are helping the user create `scenarios.yaml` for AgentBreak chaos testing. Each scenario defines one fault injected into one target (LLM or MCP) on a schedule. The file is validated at load time against Pydantic models in `agentbreak/scenarios.py`.

## Your job

1. Understand what the user's agent does: what LLM provider, what MCP tools, what breaks in production
2. Write scenarios targeting those specific failure modes
3. Write or update `scenarios.yaml`
4. Validate it: `agentbreak validate --config application.yaml --scenarios scenarios.yaml`
5. Explain what each scenario tests and why

## File format

```yaml
version: 1                # always 1
scenarios:
  - name: scenario-name   # unique, kebab-case
    summary: One-line description of the failure
    target: llm_chat       # or mcp_tool
    match: {}              # optional filters
    fault:
      kind: http_error
      status_code: 500
    schedule:
      mode: random
      probability: 0.3
    tags: [optional, labels]
```

## Complete schema reference

These are the exact Pydantic models from `agentbreak/scenarios.py`. Every scenario you write MUST conform to these.

### Scenario (required fields marked with *)

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `name`* | string | -- | Unique identifier, use kebab-case (e.g., `search-tool-timeout`) |
| `summary`* | string | -- | One-line human description of the failure being simulated |
| `target`* | string | -- | `llm_chat` or `mcp_tool`. Only these two are implemented. Others (`queue`, `state`, `memory`, `artifact_store`, `approval`, `browser_worker`, `multi_agent`, `telemetry`) are recognized but will fail validation. |
| `match` | MatchSpec | match all | Optional filters -- see below |
| `fault`* | FaultSpec | -- | What fault to inject -- see below |
| `schedule` | ScheduleSpec | always | When to fire -- see below |
| `tags` | list[string] | [] | Optional labels for organizing scenarios |

### MatchSpec

All fields are optional. If set, ALL specified fields must match for the scenario to fire. If omitted or `{}`, the scenario matches every request of the target type.

| Field | Type | Example | How it matches |
|-------|------|---------|---------------|
| `tool_name` | string | `search_docs` | Exact match on MCP tool name or LLM function call name |
| `tool_name_pattern` | string | `search_*` | Glob pattern via Python's `fnmatch` |
| `route` | string | `/v1/chat/completions` | Exact match on request path |
| `method` | string | `tools/call` | MCP JSON-RPC method or HTTP method |
| `model` | string | `gpt-4o` | Exact match on the `model` field in the LLM request body |

### FaultSpec

| Field | Type | Required when | Validation rules |
|-------|------|--------------|-----------------|
| `kind`* | string | always | Must be one of the 8 kinds listed below |
| `status_code` | int | `http_error` | Must be provided for `http_error`, ignored otherwise |
| `min_ms` | int | `latency`, `timeout` | Must be >= 0, must be <= `max_ms` |
| `max_ms` | int | `latency`, `timeout` | Must be >= 0, must be >= `min_ms` |
| `size_bytes` | int | `large_response` | Must be > 0 |
| `body` | string | optional for `wrong_content` | Custom response body text |

**All 8 fault kinds explained:**

| Kind | What it does | Extra fields | Target restrictions |
|------|-------------|-------------|-------------------|
| `http_error` | Returns an HTTP error status code instead of proxying | `status_code` (required). Common values: 429 (rate limit), 500 (server error), 503 (unavailable) | llm_chat, mcp_tool |
| `latency` | Adds a random delay between `min_ms` and `max_ms` before proxying. The request still goes through after the delay. | `min_ms`, `max_ms` (both required) | llm_chat, mcp_tool |
| `timeout` | Adds a delay like `latency`, then returns a 504 timeout error. The request does NOT go through. | `min_ms`, `max_ms` (both required) | **mcp_tool ONLY**. Using with `llm_chat` will fail validation. |
| `empty_response` | Returns a 200 with an empty body | none | llm_chat, mcp_tool |
| `invalid_json` | Returns a 200 with unparseable JSON (`{not valid`) | none | llm_chat, mcp_tool |
| `schema_violation` | Returns a 200 with valid JSON but corrupted structure. For LLM: sets `tool_calls` to `"INVALID"`. For MCP: sets `content`/`contents`/`messages` to `"INVALID"`. | none | llm_chat, mcp_tool |
| `wrong_content` | Returns a 200 with replaced content text | `body` (optional, defaults to a generic wrong-content message) | llm_chat, mcp_tool |
| `large_response` | Returns a 200 with a very large response body | `size_bytes` (required, > 0) | llm_chat, mcp_tool |

### ScheduleSpec

| Field | Type | Required when | Validation rules |
|-------|------|--------------|-----------------|
| `mode` | string | always | `always`, `random`, or `periodic` |
| `probability` | float | `random` | Must be between 0.0 and 1.0 inclusive |
| `every` | int | `periodic` | Must be > 0 |
| `length` | int | `periodic` | Must be > 0, must be <= `every` |

**Schedule modes explained:**

- **`always`**: Every matching request triggers the fault. Good for debugging a specific failure path.
- **`random`**: Each matching request fires with the given `probability`. Use 0.1-0.3 for realistic intermittent failures. Use 0.5+ for demos where you want faults to be visible quickly.
- **`periodic`**: Fires for `length` requests out of every `every` requests. Example: `every: 10, length: 2` means 2 out of every 10 matching requests get faulted.

## Connecting scenarios to application.yaml

Scenarios don't run in isolation. They need a matching `application.yaml`:

- **`target: llm_chat`** scenarios require `llm.enabled: true`. Set `llm.mode: mock` for testing without an API key, or `llm.mode: proxy` with `llm.upstream_url` to test against a real provider.
- **`target: mcp_tool`** scenarios require `mcp.enabled: true` and `mcp.upstream_url`. Also requires running `agentbreak inspect` first to generate `.agentbreak/registry.json`.
- Both targets can be enabled simultaneously for mixed LLM + MCP testing.
- MCP-targeted scenarios are silently skipped when `mcp.enabled: false`. If you write `target: mcp_tool` scenarios, make sure MCP is enabled or they will never fire.

**Minimal application.yaml for LLM-only:**

```yaml
llm:
  enabled: true
  mode: mock
mcp:
  enabled: false
serve:
  port: 5005
```

**Minimal application.yaml for MCP testing:**

```yaml
llm:
  enabled: false
mcp:
  enabled: true
  upstream_url: http://localhost:8001/mcp
serve:
  port: 5005
```

## Presets

For quick coverage without writing individual scenarios, use a preset:

```yaml
version: 1
preset: brownout
```

| Preset | Target | What it expands to |
|--------|--------|--------------------|
| `brownout` | llm_chat | `brownout-latency`: latency 5-15s at p=0.2 + `brownout-errors`: HTTP 429 at p=0.3 |
| `mcp-slow-tools` | mcp_tool | `mcp-slow-tools`: latency 5-15s at p=0.9 |
| `mcp-tool-failures` | mcp_tool | `mcp-tool-failures`: HTTP 503 at p=0.3 |
| `mcp-mixed-transient` | mcp_tool | `mcp-mixed-transient-latency`: latency 5-15s at p=0.1 + `mcp-mixed-transient-errors`: HTTP 503 at p=0.2 |

Presets can be combined with explicit scenarios. Preset scenarios come first in evaluation order:

```yaml
version: 1
preset: brownout
scenarios:
  - name: search-timeout
    summary: Search tool times out
    target: mcp_tool
    match:
      tool_name: search_docs
    fault:
      kind: timeout
      min_ms: 30000
      max_ms: 60000
    schedule:
      mode: random
      probability: 0.2
```

Multiple presets:

```yaml
version: 1
presets:
  - brownout
  - mcp-mixed-transient
```

## How to write good scenarios

1. **Ask what the user worries about.** "What fails in production?" or "What would break your agent?" Then target those exact surfaces.

2. **Target specific tools/models when possible.** `match: {}` is fine for broad chaos, but `tool_name: search_docs` or `model: gpt-4o` catches issues in specific integrations.

3. **Use realistic probabilities for testing.** 0.1-0.3 simulates real intermittent failures. Use `mode: always` only when debugging a specific fault path. Use 0.5+ for demos where you want faults visible quickly.

4. **Cover multiple fault kinds.** A good test suite includes at least:
   - One HTTP error (429 or 500) to test error handling
   - One latency spike to test timeout handling
   - One response mutation (invalid_json or schema_violation) to test parsing resilience

5. **Name scenarios clearly.** `search-tool-timeout` is better than `test-1`. Names appear in logs and scorecards.

6. **Remember: `timeout` is MCP only.** Using `kind: timeout` with `target: llm_chat` will fail validation. For LLM timeout simulation, use `kind: latency` with high values.

## Example scenarios for common agent types

### RAG agent with search and fetch MCP tools

```yaml
version: 1
scenarios:
  - name: search-rate-limited
    summary: Search API returns 429
    target: mcp_tool
    match:
      tool_name: search_docs
    fault:
      kind: http_error
      status_code: 429
    schedule:
      mode: random
      probability: 0.2
    tags: [mcp, error]

  - name: search-slow
    summary: Search takes 10-30 seconds
    target: mcp_tool
    match:
      tool_name: search_docs
    fault:
      kind: latency
      min_ms: 10000
      max_ms: 30000
    schedule:
      mode: random
      probability: 0.3
    tags: [mcp, latency]

  - name: fetch-returns-garbage
    summary: Page fetch returns invalid JSON
    target: mcp_tool
    match:
      tool_name: fetch_page
    fault:
      kind: invalid_json
    schedule:
      mode: random
      probability: 0.15
    tags: [mcp, corruption]

  - name: llm-server-error
    summary: LLM returns 500 intermittently
    target: llm_chat
    fault:
      kind: http_error
      status_code: 500
    schedule:
      mode: random
      probability: 0.1
    tags: [llm, error]
```

### Simple chat agent (LLM only)

```yaml
version: 1
scenarios:
  - name: llm-rate-limited
    summary: LLM returns 429 rate limit
    target: llm_chat
    fault:
      kind: http_error
      status_code: 429
    schedule:
      mode: random
      probability: 0.2

  - name: llm-slow
    summary: LLM takes 5-10 seconds
    target: llm_chat
    fault:
      kind: latency
      min_ms: 5000
      max_ms: 10000
    schedule:
      mode: random
      probability: 0.3

  - name: llm-bad-json
    summary: LLM returns unparseable response
    target: llm_chat
    fault:
      kind: invalid_json
    schedule:
      mode: random
      probability: 0.1
```

### Demo-friendly (high probability, visible faults)

```yaml
version: 1
scenarios:
  - name: demo-latency
    summary: Visible 2-3s delays
    target: llm_chat
    fault:
      kind: latency
      min_ms: 2000
      max_ms: 3000
    schedule:
      mode: random
      probability: 0.5

  - name: demo-errors
    summary: Visible 500 errors
    target: llm_chat
    fault:
      kind: http_error
      status_code: 500
    schedule:
      mode: random
      probability: 0.3
```

## Validation

Always validate after writing scenarios:

```bash
agentbreak validate --config application.yaml --scenarios scenarios.yaml
```

This catches:
- Invalid fault kinds or missing required fields (e.g., `http_error` without `status_code`)
- Unsupported targets (`queue`, `state`, etc. are recognized but not yet implemented)
- `timeout` used with `llm_chat` (not implemented, will be rejected)
- Schedule constraint violations (`probability` out of range, `length` > `every`)
- Missing `llm.upstream_url` when `llm.mode: proxy`
- Missing MCP registry when `mcp.enabled: true`

## After creating scenarios

Tell the user to run:

```bash
agentbreak validate
agentbreak serve -v
# Point agent at http://localhost:5005/v1 (OpenAI) or http://localhost:5005 (Anthropic)
# For MCP: http://localhost:5005/mcp
# Check results: curl http://localhost:5005/_agentbreak/scorecard
```

**IMPORTANT:** If the agent uses `langgraph dev`, `dotenv`, or similar tools that read `.env` at startup, inline env var overrides may be ignored. In that case, **temporarily edit the `.env` file** to point at AgentBreak. Remember to restore it after testing.

Or suggest they use the `agentbreak-run-tests` skill for the full run workflow.
