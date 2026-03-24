---
name: agentbreak-create-tests
description: Analyze the user's codebase and generate tailored AgentBreak chaos test configuration in .agentbreak/
---

# AgentBreak -- Create Chaos Scenarios

## Your job

Scan the user's codebase to detect their agent framework, LLM provider, MCP tools, and error handling patterns. Then generate tailored `.agentbreak/application.yaml` and `.agentbreak/scenarios.yaml` that target the specific failure modes most likely to affect their agent.

## Step-by-step

### 1. Check prerequisites

```bash
which agentbreak || pip install agentbreak
```

### 2. Scan the codebase

Search for these patterns across the project:

**Agent framework** -- check imports:
- `langgraph`, `langchain`, `langchain_openai`, `langchain_anthropic`
- `openai` (OpenAI SDK)
- `anthropic` (Anthropic SDK)
- `crewai`
- `autogen`, `ag2`
- `llama_index`
- `smolagents`

**LLM provider** -- determine if the agent uses OpenAI or Anthropic (or both):
- OpenAI: `from openai`, `ChatOpenAI`, `api.openai.com`, `OPENAI_API_KEY`, `model="gpt-*"`
- Anthropic: `from anthropic`, `ChatAnthropic`, `api.anthropic.com`, `ANTHROPIC_API_KEY`, `model="claude-*"`
- If both are found, ask the user which one to test.
- If neither is clear, **ask the user**: "Which LLM provider does your agent use — OpenAI or Anthropic?"
- This determines the endpoint: OpenAI uses `/v1/chat/completions`, Anthropic uses `/v1/messages`.

**MCP tool usage** -- look for:
- `mcp`, `MCPClient`, `MCPServerSse`, `MCPServerStreamableHttp`
- `tools/call`, `tools/list` in strings
- Tool name definitions, `@tool` decorators
- MCP server URLs (`/mcp`, `/sse`)

**API key env vars** -- look for:
- `os.getenv("OPENAI_API_KEY")`, `os.environ[`, `env:` in YAML
- `.env` files with `*_API_KEY`, `*_SECRET` patterns

**Error handling** -- look for:
- `try`/`except` around LLM or tool calls
- `retry`, `tenacity`, `backoff` imports
- `max_retries`, `timeout=` parameters
- Fallback logic, circuit breaker patterns

Record what you find — especially the LLM provider. This drives the config generation. You MUST determine a single provider (OpenAI or Anthropic) before proceeding. If ambiguous, ask.

### 3. Initialize .agentbreak/

```bash
[ -d .agentbreak ] || agentbreak init
```

This creates `.agentbreak/` with default `application.yaml` and `scenarios.yaml`.

### 4. Generate .agentbreak/application.yaml

Based on scan findings:

- **LLM mode**: Set `proxy` if a real upstream URL was found, `mock` otherwise.
- **upstream_url**: Use the detected provider URL (e.g., `https://api.openai.com`).
- **auth**: Set `env:` to the detected API key env var name.
- **MCP**: Enable if MCP usage was detected. Set `upstream_url` to the detected MCP server URL.
- **Port**: Default to 5005 (port 5000 is taken by AirPlay on macOS).

Write the file to `.agentbreak/application.yaml`.

### 5. Generate .agentbreak/scenarios.yaml

Based on scan findings:

- **No retry logic found** -- prioritize `http_error` scenarios (429, 500, 503). The agent likely crashes on these.
- **No timeout handling found** -- prioritize `latency` scenarios with high delays (10-30s).
- **MCP tools found** -- add per-tool fault scenarios using `match.tool_name` for each discovered tool.
- **Specific model found** -- use `match.model` to target it.
- Use `probability: 0.2-0.3` for realistic intermittent failures.
- Always include at least one error scenario, one latency scenario, and one response mutation scenario.
- Name scenarios descriptively: `{tool-or-target}-{fault-type}` (e.g., `search-rate-limited`, `llm-slow-response`).

Write the file to `.agentbreak/scenarios.yaml`.

### 6. Validate

```bash
agentbreak validate
```

Fix any validation errors and re-validate until it passes.

### 7. Summary

Tell the user what was generated and why. Suggest they run the `agentbreak-run-tests` skill to start testing.

## Schema reference

### Scenario fields

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `name` | string | yes | Unique, kebab-case |
| `summary` | string | yes | One-line description |
| `target` | string | yes | `llm_chat` or `mcp_tool` |
| `match` | MatchSpec | no | Filters -- defaults to match all |
| `fault` | FaultSpec | yes | What fault to inject |
| `schedule` | ScheduleSpec | no | Defaults to `always` |
| `tags` | list[string] | no | Optional labels |

### MatchSpec

All fields optional. If set, ALL must match for the scenario to fire.

| Field | Type | How it matches |
|-------|------|----------------|
| `tool_name` | string | Exact match on MCP tool name or LLM function call name |
| `tool_name_pattern` | string | Glob pattern (`fnmatch`) |
| `route` | string | Exact match on request path |
| `method` | string | MCP JSON-RPC method or HTTP method |
| `model` | string | Exact match on `model` field in request body |

### FaultSpec

| Field | Type | Required when | Notes |
|-------|------|---------------|-------|
| `kind` | string | always | One of the 8 kinds below |
| `status_code` | int | `http_error` | e.g., 429, 500, 503 |
| `min_ms` | int | `latency`, `timeout` | >= 0, <= `max_ms` |
| `max_ms` | int | `latency`, `timeout` | >= 0, >= `min_ms` |
| `size_bytes` | int | `large_response` | > 0 |
| `body` | string | optional for `wrong_content` | Custom response body |

### Fault kinds

| Kind | Effect | Extra fields | Target restriction |
|------|--------|--------------|-------------------|
| `http_error` | Returns HTTP error status | `status_code` (required) | llm_chat, mcp_tool |
| `latency` | Adds delay, then proxies normally | `min_ms`, `max_ms` | llm_chat, mcp_tool |
| `timeout` | Adds delay, then returns 504 | `min_ms`, `max_ms` | **mcp_tool only** |
| `empty_response` | Returns 200 with empty body | none | llm_chat, mcp_tool |
| `invalid_json` | Returns 200 with unparseable JSON | none | llm_chat, mcp_tool |
| `schema_violation` | Returns 200 with corrupted structure | none | llm_chat, mcp_tool |
| `wrong_content` | Returns 200 with replaced content | `body` (optional) | llm_chat, mcp_tool |
| `large_response` | Returns 200 with oversized body | `size_bytes` (required) | llm_chat, mcp_tool |

### ScheduleSpec

| Mode | Fields | Behavior |
|------|--------|----------|
| `always` | none | Every matching request is faulted |
| `random` | `probability` (0.0-1.0) | Each request faulted with given probability |
| `periodic` | `every`, `length` | `length` out of every `every` requests are faulted |

### Presets

One-liner alternatives to writing individual scenarios:

```yaml
version: 1
preset: brownout
```

| Preset | Target | Expands to |
|--------|--------|------------|
| `brownout` | llm_chat | latency 5-15s at p=0.2 + HTTP 429 at p=0.3 |
| `mcp-slow-tools` | mcp_tool | latency 5-15s at p=0.9 |
| `mcp-tool-failures` | mcp_tool | HTTP 503 at p=0.3 |
| `mcp-mixed-transient` | mcp_tool | latency 5-15s at p=0.1 + HTTP 503 at p=0.2 |

Presets can be combined with explicit scenarios. Use `presets: [brownout, mcp-mixed-transient]` for multiple.

## Config-scenario relationship

- `target: llm_chat` requires `llm.enabled: true` in application.yaml.
- `target: mcp_tool` requires `mcp.enabled: true` and `mcp.upstream_url`. Also requires `agentbreak inspect` to generate the tool registry.
- MCP-targeted scenarios are silently skipped when `mcp.enabled: false`.
