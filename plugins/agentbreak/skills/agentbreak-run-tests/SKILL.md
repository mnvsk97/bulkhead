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

If `.agentbreak/application.yaml` and `.agentbreak/scenarios.yaml` don't already exist, initialize them:

```bash
agentbreak init
```

This creates the `.agentbreak/` directory with default `application.yaml` and `scenarios.yaml`.

### Step 3: Configure application.yaml

Ask the user what they want to test. Based on their answer, edit `.agentbreak/application.yaml`:

**LLM-only testing (no API key needed):**

```yaml
llm:
  enabled: true
  mode: mock              # returns synthetic completions, no API key needed
mcp:
  enabled: false
serve:
  port: 5005
```

**LLM proxy testing (real API):**

```yaml
llm:
  enabled: true
  mode: proxy             # forwards to real LLM
  upstream_url: https://api.openai.com  # or any OpenAI-compatible gateway
  auth:
    type: bearer
    env: OPENAI_API_KEY   # reads token from this env var
mcp:
  enabled: false
serve:
  port: 5005
```

**LLM + MCP testing:**

```yaml
llm:
  enabled: true
  mode: mock
mcp:
  enabled: true
  upstream_url: http://127.0.0.1:8001/mcp    # the real MCP server
  auth:
    type: bearer
    env: MCP_API_KEY
serve:
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
agentbreak validate
```

Expected output:
```
Config valid: llm_enabled=True mcp_enabled=True scenarios=3 tools=3
```

If validation fails, it will tell you exactly what's wrong (missing fields, invalid fault kinds, unsupported targets, etc.). Fix the issue and re-validate.

### Step 7: Start the chaos proxy

```bash
agentbreak serve -v
```

The `-v` flag enables verbose logging so you can see each request and fault injection in real time. The server logs will show:
```
INFO [agentbreak] starting on 0.0.0.0:5005
INFO [agentbreak] llm=proxy mcp=on scenarios=3
```

Leave this running in its own terminal.

### Step 8: Wire the agent to AgentBreak

The agent must send its LLM/MCP traffic through AgentBreak instead of the real API. **Do not just tell the user to do this — actually do the wiring.**

#### 8a. Find the LLM connection config

Search the codebase for how the agent connects to its LLM:
- `.env` files: look for `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY`, or custom vars like `TFY_GATEWAY_URL`, `LLM_BASE_URL`
- Code: `base_url=` in SDK constructors, `ChatOpenAI(base_url=...)`, `Anthropic(base_url=...)`
- Config files: YAML/JSON with endpoint URLs

#### 8b. Back up and modify `.env`

```bash
cp .env .env.backup
```

Edit `.env` to replace the LLM URL with the AgentBreak proxy:

**If OpenAI / OpenAI-compatible:**
- Change the base URL var (e.g. `OPENAI_BASE_URL`, `TFY_GATEWAY_URL`) to `http://127.0.0.1:5005/v1`
- If mock mode: set the API key to `dummy` (or leave real key — mock mode ignores it)

**If Anthropic:**
- Change the base URL var to `http://127.0.0.1:5005`
- If mock mode: set the API key to `dummy`

**If MCP is also enabled:**
- Change the MCP URL var to `http://127.0.0.1:5005/mcp`

**IMPORTANT:** Many tools (`langgraph dev`, `dotenv`, subprocess launchers) ignore inline env var overrides and only read `.env` files. Always edit the actual `.env` file.

#### 8c. Start the agent

Start the agent using its normal run command:

```bash
# LangGraph
langgraph dev --port 8888 --no-browser &

# Direct Python
python agent.py &

# Other
python -m my_agent &
```

Wait for it to be ready (check its health endpoint or logs).

#### 8d. Trigger runs through the agent

Send real requests so LLM/MCP traffic flows through AgentBreak. Run **3-5 invocations** with different prompts to avoid loop detection.

**LangGraph agents:**
```bash
for i in 1 2 3; do
  THREAD=$(curl -s -X POST http://127.0.0.1:8888/threads -H "Content-Type: application/json" -d '{}')
  THREAD_ID=$(echo "$THREAD" | python3 -c "import sys,json; print(json.load(sys.stdin)['thread_id'])")
  curl -s -X POST "http://127.0.0.1:8888/threads/$THREAD_ID/runs/wait" \
    -H "Content-Type: application/json" \
    -d "{\"assistant_id\": \"agent\", \"input\": {\"messages\": [{\"role\": \"user\", \"content\": \"Run $i: your prompt here\"}]}}"
done
```

**CLI agents:** just run the script multiple times.

**If you can't trigger programmatically:** use curl directly against AgentBreak as a smoke test:
```bash
for i in {1..10}; do
  curl -s -w " [HTTP %{http_code}]\n" http://localhost:5005/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"gpt-4o\",\"messages\":[{\"role\":\"user\",\"content\":\"Request $i: hello\"}]}"
done
```

#### 8e. Verify traffic and restore config

Check that requests flowed through:
```bash
curl -s http://localhost:5005/_agentbreak/scorecard | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'Requests: {d[\"requests_seen\"]}, Faults: {d[\"injected_faults\"]}')"
```

If `requests_seen` is 0, the agent isn't routing through AgentBreak — check `.env` edit, restart the agent.

**Restore the original config and stop the agent:**
```bash
cp .env.backup .env && rm .env.backup
# Stop the agent process
kill %2   # or pkill -f "langgraph dev" etc.
```

### Step 9: Collect results & produce report

Fetch all scorecard data:

```bash
curl -s http://localhost:5005/_agentbreak/scorecard
curl -s http://localhost:5005/_agentbreak/requests
# If MCP enabled:
curl -s http://localhost:5005/_agentbreak/mcp-scorecard
curl -s http://localhost:5005/_agentbreak/mcp-requests
```

Stop the proxy:
```bash
kill %1   # or pkill -f "agentbreak serve"
```

If history is enabled, compare with previous run:
```bash
agentbreak history
agentbreak history compare <old_id> <new_id>
```

**Now produce the Chaos Test Report.** This is the most important output. Follow this structure:

---

> ## Chaos Test Report
>
> **Agent:** [name/path]
> **Score:** [score]/100 — [PASS/DEGRADED/FAIL]
>
> ### Traffic Summary
>
> | Metric | Value |
> |--------|-------|
> | Requests proxied | [N] |
> | Faults injected | [N] ([percentage]%) |
> | Latency injections | [N] |
> | Response mutations | [N] |
> | Upstream successes | [N] |
> | Upstream failures | [N] |
> | Duplicate requests | [N] |
> | Suspected loops | [N] |
>
> *If MCP, add per-tool success/failure breakdown*
>
> ### What Happened
>
> For each fault that fired, describe the agent's behavior:
> - **[scenario-name] ([fault kind]):** [Crashed / retried and recovered / succeeded / looped — with evidence]
>
> Cross-reference proxy logs and agent run results (which runs succeeded vs failed).
>
> ### Issues Found
>
> | # | Issue | Severity | Evidence |
> |---|-------|----------|----------|
> | 1 | [description] | High/Medium/Low | [what happened] |
>
> Only list issues that **actually manifested**. If score is 80+: "No resilience issues detected."
>
> ### Fixes
>
> For each issue, provide a **specific, code-level fix** referencing the actual codebase:
>
> **Issue 1: [description]**
> - **File:** `[path/to/file.py]:[line]`
> - **Current code:** `[relevant snippet]`
> - **Fix:** `[exact change]`
> - **Why:** [one sentence]
>
> Common fix patterns:
> - **No retry on errors** → `ChatOpenAI(max_retries=3)` or `OpenAI(max_retries=3)`
> - **No timeout** → `ChatOpenAI(request_timeout=30)` or `OpenAI(timeout=30)`
> - **Crashes on malformed responses** → `max_retries` handles at SDK level
> - **Unbounded retries / loops** → cap with `max_retries`, add exponential backoff
> - **MCP tool failures crash agent** → error handling around tool call results
>
> ### Comparison with Previous Run
>
> *If history has a previous run:*
> - **Score:** [old] → [new] ([+/-delta])
> - **What changed:** [summary]
>
> ### Next Steps
>
> - If issues found: "Want me to apply these fixes now?"
> - If score improved: "Score improved [old] → [new] after [what changed]."
> - If score 80+: "Agent is resilient. Consider adding to CI."
> - If still low: "Re-run after fixes to verify."

---

**IMPORTANT:** This report must be detailed enough that:
1. A **user** can understand exactly what broke and how to fix it
2. **Claude Code** can pick up where this skill left off and apply the fixes directly without re-reading the codebase

If the user asks to apply fixes, edit the code files directly using the information from this report.

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
| `agentbreak init` | Create `.agentbreak/` directory with default config files |
| `agentbreak serve` | Start the chaos proxy. Flags: `--config`, `--scenarios`, `--registry`, `-v`, `--label` |
| `agentbreak validate` | Check configs without starting. Flags: `--config`, `--scenarios`, `--registry` |
| `agentbreak inspect` | Discover MCP tools, write registry. Flags: `--config`, `--registry` |
| `agentbreak verify` | Run test suite. Flag: `--live` for full E2E harness |
| `agentbreak history` | List past runs. Subcommands: `show <id>`, `compare <a> <b>` |

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
agentbreak init  # if .agentbreak/ not checked in
agentbreak serve -v &
sleep 2
pytest your_agent_tests/
SCORE=$(curl -s localhost:5005/_agentbreak/scorecard | python3 -c "import sys,json; print(json.load(sys.stdin)['resilience_score'])")
echo "Resilience score: $SCORE"
[ "$SCORE" -ge 70 ] || exit 1
```
