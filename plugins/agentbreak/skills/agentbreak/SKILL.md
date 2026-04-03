# AgentBreak -- Chaos Test Your Agent

This skill orchestrates AgentBreak to chaos-test an agent end-to-end.

## Prerequisites

AgentBreak CLI must be installed:

```bash
pip install agentbreak
```

If not installed, help the user install it, then proceed.

## Workflow

Follow these steps in order. **Ask the user to confirm before each major step.**

### Step 1: Initialize

Create `.agentbreak/` config if it doesn't exist:

```bash
agentbreak init
```

### Step 2: Analyze

Scan the codebase to understand the agent. Look for:

- **Provider:** Search for `openai`, `anthropic`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` in code and `.env`
- **Framework:** Search for `langgraph`, `langchain`, `crewai`, `autogen`, `openai` SDK usage
- **MCP tools:** Search for `mcp`, `MCPClient`, `tool_call` patterns
- **Error handling:** Search for `max_retries`, `retry`, `except`, `timeout`, `backoff`

Present findings to the user:

> "Here's what I found:
> - **Provider:** [openai/anthropic]
> - **Framework:** [langgraph/langchain/etc.]
> - **MCP tools:** [detected/none]
> - **Error handling:** [has retry logic / no retry logic]
>
> Does this look correct?"

If the provider is ambiguous or wrong, ask the user which one to use.

### Step 3: Generate Config

Based on the analysis, write `.agentbreak/application.yaml` and `.agentbreak/scenarios.yaml`.

**For LLM-only agents (mock mode, no API key needed):**

```yaml
# application.yaml
llm:
  enabled: true
  mode: mock
mcp:
  enabled: false
serve:
  port: 5005
```

**For LLM proxy mode (real API):**

```yaml
# application.yaml
llm:
  enabled: true
  mode: proxy
  upstream_url: https://api.openai.com  # or anthropic URL
  auth:
    type: bearer
    env: OPENAI_API_KEY
mcp:
  enabled: false
serve:
  port: 5005
```

**For MCP-enabled agents, add:**

```yaml
mcp:
  enabled: true
  upstream_url: http://localhost:8001/mcp
```

Generate scenarios based on what you found. A good starter set:

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
      probability: 0.3

  - name: llm-slow
    summary: LLM takes 3-8 seconds
    target: llm_chat
    fault:
      kind: latency
      min_ms: 3000
      max_ms: 8000
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
      probability: 0.15
```

If the agent has no retry logic, use higher probabilities (0.3-0.5) to surface issues quickly.
If it has retries, use lower probabilities (0.1-0.2) to test realistic intermittent failures.

Present the generated scenarios and ask for confirmation.

### Step 4: Start Proxy

1. Validate config:
   ```bash
   agentbreak validate
   ```
2. If MCP enabled, discover tools:
   ```bash
   agentbreak inspect
   ```
3. Start proxy:
   ```bash
   agentbreak serve -v &
   ```
4. Wait for health check:
   ```bash
   curl -s http://localhost:5005/healthz
   ```

### Step 5: Wire Agent & Run

1. **Back up `.env`:**
   ```bash
   cp .env .env.agentbreak-backup
   ```

2. **Rewrite `.env`** to point at AgentBreak:
   - OpenAI: set base URL var to `http://127.0.0.1:5005/v1`
   - Anthropic: set base URL var to `http://127.0.0.1:5005`
   - MCP: set MCP URL var to `http://127.0.0.1:5005/mcp`
   - If mock mode: set API key to `dummy`

3. Tell the user their agent is now wired to AgentBreak.

4. Start the agent (look for its normal run command in the codebase).

5. Run 3-5 invocations with different prompts.

6. Verify traffic flowed:
   ```bash
   curl -s http://localhost:5005/_agentbreak/scorecard
   ```
   If `requests_seen` is 0, the agent isn't routing through AgentBreak -- check `.env`, restart agent.

### Step 6: Results

1. Fetch full scorecard:
   ```bash
   curl -s http://localhost:5005/_agentbreak/scorecard
   ```

2. **Restore `.env` and stop proxy:**
   ```bash
   cp .env.agentbreak-backup .env && rm .env.agentbreak-backup
   pkill -f "agentbreak serve" || true
   ```

3. Produce the Chaos Test Report:

> ## Chaos Test Report
>
> **Score:** [score]/100 -- [PASS/DEGRADED/FAIL]
>
> ### What Happened
> - **[scenario]:** [Did the agent retry, crash, loop, or succeed?]
>
> ### Issues Found
> | # | Issue | Severity | Evidence |
> |---|-------|----------|----------|
>
> ### Fixes
> For each issue, give a specific code fix referencing actual files from the codebase.
>
> ### Next Steps
> - Issues found: "Want me to apply these fixes?"
> - Score 80+: "Your agent is resilient. Consider adding chaos tests to CI."

## Safety

- **Always restore `.env` when done.** Never leave the user's config pointing at a dead proxy.
- **Always stop the proxy when done.** `pkill -f "agentbreak serve"` as cleanup.
- If something goes wrong mid-run, restore immediately:
  ```bash
  cp .env.agentbreak-backup .env && rm .env.agentbreak-backup
  pkill -f "agentbreak serve" || true
  ```

## Rules

- **Determine one provider** before generating config. Never test both at once.
- **Always confirm** findings and scenarios with the user before proceeding.
- **If you can't start the agent programmatically**, ask the user to trigger it manually.
- **If scorecard shows `requests_seen: 0`**, the agent isn't wired correctly -- debug before continuing.
- **Always restore `.env` and stop proxy** when done.
