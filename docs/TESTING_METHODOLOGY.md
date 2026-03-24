# Testing Methodology

How AgentBreak tests agent resilience, and how to design effective chaos tests.

## The model

AgentBreak is a transparent proxy. It sits between your agent and the services it depends on (LLM APIs, MCP servers), intercepting traffic and injecting controlled faults.

```
Agent  →  AgentBreak (localhost:5005)  →  Upstream LLM / MCP
               ↑
         scenarios.yaml (fault rules)
```

The agent talks to AgentBreak as if it were the real service. AgentBreak either forwards to the real upstream (proxy mode) or returns synthetic responses (mock mode), optionally corrupting, delaying, or replacing responses along the way.

## Supported API formats

AgentBreak exposes two LLM endpoints, both backed by the same fault injection engine:

| Endpoint | Format | Upstream path |
|----------|--------|---------------|
| `POST /v1/chat/completions` | OpenAI | `/v1/chat/completions` |
| `POST /v1/messages` | Anthropic Messages | `/v1/messages` |

MCP traffic goes through `POST /mcp` (JSON-RPC over HTTP).

All three endpoints share the same scenario matching, scheduling, and scoring logic.

## What gets tested

AgentBreak focuses on **infrastructure-level** failures, not semantic attacks. The goal is to answer: "Does my agent handle real-world service degradation gracefully?"

### Fault categories

| Category | What it simulates | Example scenario |
|----------|-------------------|------------------|
| **Availability** | Service outages, rate limits | `http_error` with status 503 or 429 |
| **Latency** | Slow responses, network delays | `latency` with min/max milliseconds |
| **Timeouts** | Requests that never complete (MCP) | `timeout` with min/max milliseconds |
| **Corruption** | Broken payloads | `invalid_json`, `empty_response`, `schema_violation` |
| **Content drift** | Unexpected response content | `wrong_content`, `large_response` |

### Behavioral detection

Beyond injected faults, AgentBreak passively monitors:

- **Duplicate requests** -- same payload sent more than once (fingerprint-based)
- **Suspected loops** -- same payload sent 3+ times, indicating the agent is stuck
- **Upstream failures** -- real errors from the upstream service (not injected)

## Designing scenarios

### Start simple

Begin with one fault at a time. A single `http_error 500` on `always` schedule tells you whether the agent retries, crashes, or gives up.

```yaml
scenarios:
  - name: llm-always-500
    summary: Every LLM call fails
    target: llm_chat
    fault:
      kind: http_error
      status_code: 500
    schedule:
      mode: always
```

### Add realism with schedules

Real failures are intermittent. Use `random` with a probability, or `periodic` for burst patterns.

```yaml
schedule:
  mode: random
  probability: 0.3     # 30% of requests fail
```

```yaml
schedule:
  mode: periodic
  every: 5             # every 5th request...
  length: 2            # ...2 consecutive requests fail
```

### Combine faults

Layer multiple scenarios. A brownout might mean slow responses *and* occasional errors:

```yaml
scenarios:
  - name: slow-llm
    summary: LLM latency spikes
    target: llm_chat
    fault:
      kind: latency
      min_ms: 3000
      max_ms: 8000
    schedule:
      mode: random
      probability: 0.4

  - name: llm-rate-limit
    summary: Occasional rate limits
    target: llm_chat
    fault:
      kind: http_error
      status_code: 429
    schedule:
      mode: random
      probability: 0.2
```

### Target specific tools or models

Use the `match` field to scope faults to particular tools or models:

```yaml
match:
  model: gpt-4o          # only affect requests to this model
```

```yaml
match:
  tool_name: search_docs  # only affect this MCP tool
```

```yaml
match:
  tool_name_pattern: "search_*"  # wildcard matching
```

## Reading results

### Scorecard

After a test run, check the scorecard:

```bash
curl localhost:5005/_agentbreak/scorecard
```

Key metrics:

| Metric | Meaning |
|--------|---------|
| `requests_seen` | Total requests the agent made |
| `injected_faults` | How many faults AgentBreak applied |
| `upstream_successes` | Requests that got a valid response |
| `upstream_failures` | Failed requests (injected + real) |
| `duplicate_requests` | Same payload sent twice |
| `suspected_loops` | Same payload sent 3+ times |
| `resilience_score` | 0-100 composite score |
| `run_outcome` | `PASS`, `DEGRADED`, or `FAIL` |

### Run outcome

- **PASS** -- no failures, no loops. The agent handled everything cleanly.
- **DEGRADED** -- some failures occurred, but some requests succeeded. The agent partially recovered.
- **FAIL** -- all requests failed, or the agent got stuck in a loop.

### Resilience score

Starts at 100, with deductions:

- -3 per injected fault
- -12 per upstream failure
- -2 per duplicate request
- -10 per suspected loop

A score of 100 means the agent was never tripped up. Below 50 means serious resilience gaps.

## Presets

Built-in scenario bundles for common patterns:

| Preset | What it does |
|--------|-------------|
| `brownout` | Random LLM latency + rate limits |
| `mcp-slow-tools` | 90% of MCP tool calls are slow |
| `mcp-tool-failures` | 30% of MCP tool calls return 503 |
| `mcp-mixed-transient` | Light MCP latency + errors |

Use in `scenarios.yaml`:

```yaml
preset: brownout
```

## Workflow

1. **`agentbreak init`** -- generate config files
2. **Edit scenarios** -- define what faults to inject
3. **`agentbreak serve`** -- start the proxy
4. **Run your agent** -- point it at `localhost:5005`
5. **Check scorecard** -- review results at `/_agentbreak/scorecard`
6. **Iterate** -- adjust scenarios, re-run, compare scores

For MCP testing, add `agentbreak inspect` before step 3 to discover available tools.
