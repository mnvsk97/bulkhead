---
name: agentbreak-create-tests
description: Generate chaos test scenarios for AgentBreak.
---

# AgentBreak -- Create Tests

Generate `scenarios.yaml` entries for AgentBreak chaos testing.

## Scenario structure

```yaml
version: 1
scenarios:
  - name: flaky-llm              # unique, kebab-case
    summary: Random 500s          # one-line description
    target: llm_chat              # llm_chat or mcp_tool
    match:                        # all optional, all must match if set
      tool_name: search_docs      # exact match
      model: gpt-4o               # LLM model
    fault:
      kind: http_error
      status_code: 500
    schedule:
      mode: random
      probability: 0.3
    tags: [llm, error]            # optional
```

## Fault kinds

| Kind | Required fields | Notes |
|------|----------------|-------|
| `http_error` | `status_code` | |
| `latency` | `min_ms`, `max_ms` | |
| `timeout` | `min_ms`, `max_ms` | MCP only |
| `empty_response` | | |
| `invalid_json` | | |
| `schema_violation` | | |
| `wrong_content` | `body` (optional) | |
| `large_response` | `size_bytes` (> 0) | |

## Schedule modes

- `always` -- every matching request
- `random` -- `probability` (0.0-1.0)
- `periodic` -- `every` + `length`

## Presets

```yaml
version: 1
preset: brownout   # or: mcp-slow-tools, mcp-tool-failures, mcp-mixed-transient
```

## After creating scenarios

```bash
agentbreak validate --config application.yaml --scenarios scenarios.yaml
agentbreak serve --config application.yaml --scenarios scenarios.yaml
```
