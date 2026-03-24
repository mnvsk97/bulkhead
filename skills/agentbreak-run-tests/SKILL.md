---
name: agentbreak-run-tests
description: Start the AgentBreak chaos proxy and guide the user through testing their agent against injected faults.
---

# AgentBreak -- Run Chaos Tests

## Your job

Start the AgentBreak chaos proxy and guide the user through testing their agent. You cannot run the user's agent directly -- you start the proxy, tell the user how to point their agent at it, and interpret the results.

```
Agent  -->  AgentBreak proxy (localhost)  -->  Real LLM / MCP server (or mock)
                     ^
            .agentbreak/scenarios.yaml defines faults
```

## Step-by-step

### 1. Check prerequisites

```bash
which agentbreak || pip install agentbreak
```

### 2. Check .agentbreak/ exists

If `.agentbreak/` does not exist, tell the user:

> No `.agentbreak/` directory found. Run the **agentbreak-create-tests** skill first to analyze your codebase and generate config, or run `agentbreak init` for defaults.

Do not proceed without `.agentbreak/application.yaml` and `.agentbreak/scenarios.yaml`.

### 3. Read the config and detect the provider

Read `.agentbreak/application.yaml` to understand what is enabled (LLM mode, MCP, port, upstream URLs).

Also determine which LLM provider the user's agent uses. Check:
- `.agentbreak/application.yaml` `upstream_url` — does it point to `openai.com` or `anthropic.com`?
- Scan the codebase for `from openai` vs `from anthropic` imports
- If still unclear, **ask the user**: "Does your agent use OpenAI (`/v1/chat/completions`) or Anthropic (`/v1/messages`)?"

You need this to give the correct endpoint and env vars in step 7.

### 4. If MCP is enabled, run inspect

```bash
agentbreak inspect
```

This discovers upstream MCP tools and writes the registry. If it fails, check that the MCP server is running and `mcp.upstream_url` is correct.

### 5. Validate

```bash
agentbreak validate
```

Fix any errors before proceeding.

### 6. Start the proxy

```bash
agentbreak serve -v &
```

Run it in the background. Wait for the startup log line confirming the port.

### 7. Tell the user how to connect

Based on the detected provider and config, tell the user the **exact** env vars needed. Only show the relevant provider — do not show both.

**If OpenAI** (read the port from config, default 5005):
```bash
export OPENAI_BASE_URL=http://127.0.0.1:{port}/v1
export OPENAI_API_KEY=dummy   # any value works in mock mode
```

**If Anthropic**:
```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:{port}
export ANTHROPIC_API_KEY=dummy   # any value works in mock mode
```

**If MCP is also enabled**:
```
Point your MCP client at http://127.0.0.1:{port}/mcp
```

Tell the user to run their agent now and exercise the workflows they want to test.

### 8. Wait for user to run their agent

Ask the user to confirm when they are done running their agent.

### 9. Read and interpret the scorecard

```bash
curl -s http://localhost:{port}/_agentbreak/scorecard | python3 -m json.tool
```

If MCP is enabled:
```bash
curl -s http://localhost:{port}/_agentbreak/mcp-scorecard | python3 -m json.tool
```

Interpret the results for the user (see scorecard interpretation below).

### 10. Stop the proxy

```bash
kill %1   # or kill the background agentbreak process
```

The final scorecard is also printed to stderr on shutdown.

## Scorecard interpretation

**Fields:**

| Field | Meaning |
|-------|---------|
| `requests_seen` | Total requests proxied |
| `injected_faults` | Faults AgentBreak injected |
| `latency_injections` | Latency delays added |
| `upstream_successes` | Requests that succeeded |
| `upstream_failures` | Requests that failed (injected or real) |
| `duplicate_requests` | Same request body seen 2+ times |
| `suspected_loops` | Same request body seen 3+ times (agent may be stuck) |
| `run_outcome` | PASS, DEGRADED, or FAIL |
| `resilience_score` | 0-100 score |

MCP scorecard adds: `tool_calls`, `method_counts`, `tool_call_counts`, `tool_successes_by_name`, `tool_failures_by_name`, `response_mutations`.

**Score ranges:**

| Score | Meaning |
|-------|---------|
| 80-100 | Resilient -- agent handles faults well |
| 50-79 | Degraded -- partial failures, needs improvement |
| 0-49 | Fragile -- agent struggles with faults |

Duplicate and loop counters are signals, not necessarily bugs. Some frameworks legitimately retry.

## Endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /healthz` | Health check |
| `GET /_agentbreak/scorecard` | LLM scorecard |
| `GET /_agentbreak/requests` | LLM recent requests |
| `GET /_agentbreak/mcp-scorecard` | MCP scorecard |
| `GET /_agentbreak/mcp-requests` | MCP recent requests |
| `GET /_agentbreak/history` | All recorded runs (if `history.enabled: true`) |
| `GET /_agentbreak/history/{run_id}` | Single run details |

## Common issues

- **Port 5000 in use on macOS**: AirPlay Receiver uses it. Use port 5005 or change in application.yaml.
- **`agentbreak: command not found`**: `pip install agentbreak` or activate the correct venv.
- **No faults firing**: Check scenario probability. At `0.1` you need ~10+ requests. Increase to `0.5` for demos.
- **MCP inspect fails**: Ensure upstream MCP server is running and `mcp.upstream_url` is correct.
- **Registry not found**: Run `agentbreak inspect` before `serve` when MCP is enabled.
