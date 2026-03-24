---
name: agentbreak-run-tests
description: Run chaos tests against an OpenAI-compatible app or MCP server using AgentBreak. Uses application.yaml + scenarios.yaml, agentbreak serve, optional MCP inspect, and scorecard endpoints.
---

# AgentBreak -- Run Chaos Tests

You are helping the user run chaos tests against their agent using AgentBreak. AgentBreak is a local proxy that sits between an agent and its LLM/MCP backends, injecting faults defined in `scenarios.yaml`.

```
Agent  →  AgentBreak (localhost)  →  Real LLM / MCP server (or mock)
               ↑
          scenarios.yaml defines faults
```

## Your job

Walk the user through the full workflow: configure, inspect (if MCP), validate, serve, send traffic, read the scorecard. Do not skip steps. If something fails, diagnose and fix it before moving on.

## Step-by-step instructions

### Step 1: Install AgentBreak

Check if `agentbreak` CLI is available. If not, install it:

```bash
pip install agentbreak
# Or from repo root:
pip install -e '.[dev]'
```

Verify with `agentbreak --help`.

### Step 2: Create configuration files

If `application.yaml` and `scenarios.yaml` don't already exist in the project root, create them from the examples:

```bash
cp config.example.yaml application.yaml
cp scenarios.example.yaml scenarios.yaml
```

### Step 3: Configure application.yaml

Ask the user what they want to test. Based on their answer, edit `application.yaml`:

**LLM-only testing (no API key needed):**

```yaml
llm:
  enabled: true
  mode: mock              # returns synthetic completions, no API key needed
  upstream_url: https://api.openai.com
mcp:
  enabled: false
serve:
  host: 0.0.0.0
  port: 5005
```

**LLM proxy testing (real API):**

```yaml
llm:
  enabled: true
  mode: proxy             # forwards to real LLM
  upstream_url: https://api.openai.com
  auth:
    type: bearer
    env: OPENAI_API_KEY   # reads token from this env var
mcp:
  enabled: false
serve:
  host: 0.0.0.0
  port: 5005
```

**LLM + MCP testing:**

```yaml
llm:
  enabled: true
  mode: mock
  upstream_url: https://api.openai.com
mcp:
  enabled: true
  upstream_url: http://127.0.0.1:8001/mcp    # the real MCP server
  transport: streamable_http
  auth:
    type: bearer
    env: MCP_API_KEY
serve:
  host: 0.0.0.0
  port: 5005
```

IMPORTANT: On macOS, port 5000 is often taken by AirPlay Receiver. Use port 5005 or another free port.

### Step 4: Configure scenarios.yaml

If the user doesn't have specific scenarios in mind, start with something visible for a demo:

```yaml
version: 1
scenarios:
  - name: llm-latency
    summary: Random 2-3 second delays on LLM calls
    target: llm_chat
    fault:
      kind: latency
      min_ms: 2000
      max_ms: 3000
    schedule:
      mode: random
      probability: 0.5

  - name: llm-500-errors
    summary: Random server errors on LLM calls
    target: llm_chat
    fault:
      kind: http_error
      status_code: 500
    schedule:
      mode: random
      probability: 0.3
```

Or use a preset for one-liner setup:

```yaml
version: 1
preset: brownout
```

Available presets: `brownout`, `mcp-slow-tools`, `mcp-tool-failures`, `mcp-mixed-transient`.

For the full scenario schema, use the `agentbreak-create-tests` skill.

### Step 5: If MCP is enabled, run inspect

This discovers the upstream MCP server's tools, resources, and prompts and writes `.agentbreak/registry.json`:

```bash
agentbreak inspect --config application.yaml
```

Expected output:
```
Discovered N MCP tools
Wrote registry: .agentbreak/registry.json
```

If this fails:
- Check that `mcp.upstream_url` is correct and the MCP server is running
- Check auth configuration if the server requires authentication
- Ensure the server speaks MCP over streamable HTTP

### Step 6: Validate configuration

Always validate before serving:

```bash
agentbreak validate --config application.yaml --scenarios scenarios.yaml
```

Expected output:
```
Config valid: llm_enabled=True mcp_enabled=True scenarios=3 tools=3
```

If validation fails, it will tell you exactly what's wrong (missing fields, invalid fault kinds, unsupported targets, etc.). Fix the issue and re-validate.

### Step 7: Start the chaos proxy

```bash
agentbreak serve --config application.yaml --scenarios scenarios.yaml -v
```

The `-v` flag enables verbose logging so you can see each request and fault injection in real time. The server logs will show:
```
INFO [agentbreak] starting on 0.0.0.0:5005
INFO [agentbreak] llm=proxy mcp=on scenarios=3
```

Leave this running in its own terminal.

### Step 8: Send traffic through the proxy

The user's agent should point at AgentBreak instead of the real API:

```python
# Python (OpenAI SDK)
client = OpenAI(base_url="http://localhost:5005/v1")
```

```bash
# Environment variables
export OPENAI_BASE_URL=http://127.0.0.1:5005/v1
export OPENAI_API_KEY=dummy   # any value works in mock mode
```

For MCP, point the agent's MCP client at `http://localhost:5005/mcp`.

To quickly test without an agent, use curl:

```bash
# Single LLM request
curl -s http://localhost:5005/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"Hello"}]}'

# Multiple requests (use unique content to avoid loop detection)
for i in {1..10}; do
  curl -s -w " [HTTP %{http_code}]\n" http://localhost:5005/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"gpt-4o\",\"messages\":[{\"role\":\"user\",\"content\":\"Request $i: hello\"}]}"
done
```

### Step 9: Check the scorecard

```bash
# LLM scorecard
curl -s http://localhost:5005/_agentbreak/scorecard | python3 -m json.tool

# MCP scorecard (if MCP enabled)
curl -s http://localhost:5005/_agentbreak/mcp-scorecard | python3 -m json.tool

# Recent requests log
curl -s http://localhost:5005/_agentbreak/requests | python3 -m json.tool
curl -s http://localhost:5005/_agentbreak/mcp-requests | python3 -m json.tool
```

### Step 10: Interpret results

**Scorecard fields:**

| Field | Meaning |
|-------|---------|
| `requests_seen` | Total requests proxied |
| `injected_faults` | How many faults AgentBreak injected |
| `latency_injections` | How many latency delays were added |
| `upstream_successes` | Requests that succeeded (with or without faults) |
| `upstream_failures` | Requests that failed (injected errors or real upstream errors) |
| `duplicate_requests` | Same request body seen more than once |
| `suspected_loops` | Same request body seen 3+ times (agent may be stuck) |
| `run_outcome` | PASS, DEGRADED, or FAIL |
| `resilience_score` | 0-100 score |

**MCP scorecard** also includes: `tool_calls`, `method_counts`, `tool_call_counts`, `tool_successes_by_name`, `tool_failures_by_name`, `response_mutations`.

**Resilience score interpretation:**

| Score | Meaning |
|-------|---------|
| 80-100 | Resilient -- agent handles faults well |
| 50-79 | Degraded -- some failures but partially functional |
| 0-49 | Fragile -- agent struggles with faults |

Duplicate and loop counters are signals, not necessarily bugs. Some frameworks legitimately retry or repeat completions.

### Step 11: Stop and review

Ctrl+C the `agentbreak serve` process. It prints the final scorecard to stderr.

## All available endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /healthz` | Health check |
| `GET /_agentbreak/scorecard` | LLM scorecard |
| `GET /_agentbreak/requests` | LLM recent requests |
| `GET /_agentbreak/llm-scorecard` | LLM scorecard (explicit) |
| `GET /_agentbreak/llm-requests` | LLM recent requests (explicit) |
| `GET /_agentbreak/mcp-scorecard` | MCP scorecard |
| `GET /_agentbreak/mcp-requests` | MCP recent requests |

## All CLI commands

| Command | Purpose |
|---------|---------|
| `agentbreak serve` | Start the chaos proxy. Flags: `--config`, `--scenarios`, `--registry`, `-v` |
| `agentbreak validate` | Check configs without starting. Flags: `--config`, `--scenarios`, `--registry` |
| `agentbreak inspect` | Discover MCP tools, write registry. Flags: `--config`, `--registry` |
| `agentbreak verify` | Run test suite. Flag: `--live` for full E2E harness |

## Common issues

- **Port 5000 in use on macOS**: AirPlay Receiver uses it. Use port 5005 or disable AirPlay in System Settings.
- **`agentbreak: command not found`**: Activate the venv first (`source .venv/bin/activate`) or install globally (`pip install agentbreak`).
- **No faults firing**: Check scenario probability. At `probability: 0.1` you need ~10+ requests to see one. Increase to 0.5 for demos.
- **MCP inspect fails**: Ensure the upstream MCP server is running and `mcp.upstream_url` is correct.
- **`FileNotFoundError`**: `application.yaml` must exist. Copy from `config.example.yaml`.
- **Registry not found**: Run `agentbreak inspect` before `serve` when `mcp.enabled: true`.

## CI usage

```bash
pip install agentbreak
agentbreak serve --config application.yaml --scenarios scenarios.yaml &
sleep 2
pytest your_agent_tests/
SCORE=$(curl -s localhost:5005/_agentbreak/scorecard | python3 -c "import sys,json; print(json.load(sys.stdin)['resilience_score'])")
echo "Resilience score: $SCORE"
[ "$SCORE" -ge 70 ] || exit 1
```
