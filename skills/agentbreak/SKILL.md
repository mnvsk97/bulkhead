---
name: agentbreak
description: >
  Chaos-test any LLM agent using AgentBreak. Analyzes the codebase to detect framework, LLM provider,
  and MCP tools, generates tailored fault scenarios, starts a chaos proxy, guides the user through
  testing, and interprets results. Use when the user wants to test agent resilience, generate chaos
  scenarios, or run AgentBreak.
---

# AgentBreak — Chaos Test Your Agent

This skill runs the full AgentBreak workflow: analyze the codebase, generate chaos config, start the proxy, guide the user through testing, and interpret the scorecard.

## Workflow

Execute the steps in order. **Do not start the next step until the current step's checkpoint is complete and the user has confirmed.**

**Before starting each step**, tell the user what is about to happen:

> "I'm about to start **Step N: [Name]**. This will [brief description]. Here's what to expect: [what the user will need to review or answer]."

### Step 1: Setup

**What it does:** Installs AgentBreak and initializes the `.agentbreak/` config directory.

**Execute:**

1. Check if `agentbreak` CLI is available:
   ```bash
   which agentbreak || pip install agentbreak
   ```
2. If `.agentbreak/` does not exist, run:
   ```bash
   agentbreak init
   ```

No checkpoint needed — proceed to Step 2.

### Step 2: Analyze Codebase

**What it does:** Scans the user's codebase to detect their agent framework, LLM provider, MCP tools, and error handling patterns. This drives the config generation.

**Execute:** Use subagents to search in parallel for:

**LLM provider** (you MUST determine exactly one):
- OpenAI: `from openai`, `ChatOpenAI`, `langchain_openai`, `api.openai.com`, `OPENAI_API_KEY`, `model="gpt-*"`
- Anthropic: `from anthropic`, `ChatAnthropic`, `langchain_anthropic`, `api.anthropic.com`, `ANTHROPIC_API_KEY`, `model="claude-*"`

**Agent framework:** `langgraph`, `langchain`, `crewai`, `autogen`, `llama_index`, `smolagents`, or raw SDK usage

**MCP tool usage:** `MCPClient`, `tools/call`, `@mcp.tool`, `@tool` decorators, MCP server URLs

**API key env vars:** `os.getenv("*_API_KEY")`, `.env` files

**Error handling:** `try`/`except` around LLM calls, `retry`, `tenacity`, `backoff` imports, `max_retries`, `timeout=`

**Review checkpoint:**
Present findings to the user:

> "Here's what I found in your codebase:
>
> - **LLM Provider:** [OpenAI / Anthropic]
> - **Framework:** [LangGraph / OpenAI SDK / etc.]
> - **MCP tools:** [list tool names, or "none detected"]
> - **Error handling:** [has retry logic / no retry logic found]
>
> **Why this matters:** The provider determines which endpoint AgentBreak proxies (`/v1/chat/completions` for OpenAI, `/v1/messages` for Anthropic). The error handling patterns determine which fault scenarios are most valuable — if your agent has no retry logic, rate limit errors (429) will be especially revealing."

**Then use `AskUserQuestion`** with options like: "Looks correct, proceed", "Wrong provider — I use [other]", "I want to add/change something".

If the provider is ambiguous (both found, or neither), **ask the user directly** which provider to test.

### Step 3: Generate Config

**What it does:** Creates tailored `.agentbreak/application.yaml` and `.agentbreak/scenarios.yaml` based on the scan findings.

**Execute:**

**application.yaml:**
- `llm.mode`: `mock` (default — no API key needed for chaos testing) or `proxy` if user wants to test against real provider
- `mcp.enabled`: `true` if MCP tools were detected, with `upstream_url` from the codebase
- `serve.port`: 5005

**scenarios.yaml** — generate scenarios based on findings:
- **No retry logic found** → prioritize `http_error` scenarios (429, 500). The agent likely crashes on these.
- **No timeout handling** → prioritize `latency` scenarios with high delays.
- **MCP tools found** → add per-tool fault scenarios using `match.tool_name` for each tool.
- **Specific model found** → use `match.model` to target it.
- Always include at least: one error scenario, one latency scenario, one response mutation.
- Use `probability: 0.2-0.3` for realistic testing.

Write both files to `.agentbreak/`.

**Review checkpoint:**
Present the generated scenarios:

> "Here are the chaos scenarios I generated:
>
> | Scenario | Target | Fault | Probability |
> |----------|--------|-------|-------------|
> | [name] | [llm_chat/mcp_tool] | [kind] | [prob] |
> | ... | ... | ... | ... |
>
> **What each tests:**
> - [scenario name]: [one-line explanation of what it catches]
> - ...
>
> **What to check:** Are there specific failure modes you've seen in production that aren't covered? Any tools that should be excluded from fault injection?"

**Then use `AskUserQuestion`** with options like: "Looks good, proceed", "Add more scenarios", "Remove a scenario", "Change probabilities".

### Step 4: Start the Proxy

**What it does:** Runs MCP inspect (if needed), validates config, and starts the AgentBreak chaos proxy.

**Execute:**

1. If MCP is enabled and the upstream MCP server is running:
   ```bash
   agentbreak inspect
   ```
   If inspect fails, tell the user to start their MCP server first.

2. Validate:
   ```bash
   agentbreak validate
   ```
   Fix any errors before proceeding.

3. Check if history is enabled in `.agentbreak/application.yaml`. If not, ask the user if they want to enable it for run comparison. If yes, add `history: {enabled: true}` to the config.

4. Start the proxy. If the user provided context about what changed, pass it as a label:
   ```bash
   agentbreak serve -v --label "description of what changed" &
   ```
   If no label context was given:
   ```bash
   agentbreak serve -v &
   ```
   Wait for the health check to confirm it's running:
   ```bash
   curl -s http://localhost:{port}/healthz
   ```

**Review checkpoint:**
Tell the user exactly how to connect their agent. Show ONLY the relevant provider:

**If OpenAI:**
> "AgentBreak is running on port {port}. To connect your agent, set these env vars:
> ```bash
> export OPENAI_BASE_URL=http://127.0.0.1:{port}/v1
> export OPENAI_API_KEY=dummy
> ```
> Then run your agent as normal. AgentBreak will intercept all LLM calls and inject faults according to the scenarios."

**If Anthropic:**
> "AgentBreak is running on port {port}. To connect your agent, set these env vars:
> ```bash
> export ANTHROPIC_BASE_URL=http://127.0.0.1:{port}
> export ANTHROPIC_API_KEY=dummy
> ```
> Then run your agent as normal."

**If MCP is also enabled**, add:
> "For MCP, point your MCP client at `http://127.0.0.1:{port}/mcp`"

**Then use `AskUserQuestion`**: "Run your agent now and tell me when you're done."

### Step 5: Results

**What it does:** Reads the scorecard and interprets the results.

**Execute:**

1. Fetch the scorecard:
   ```bash
   curl -s http://localhost:{port}/_agentbreak/scorecard
   ```
   If MCP was enabled:
   ```bash
   curl -s http://localhost:{port}/_agentbreak/mcp-scorecard
   ```

2. Stop the proxy:
   ```bash
   kill %1
   ```

3. Check for previous runs:
   ```bash
   agentbreak history
   ```
   If there are previous runs, compare with the most recent one:
   ```bash
   agentbreak history compare {previous_id} {current_id}
   ```

   Present the comparison:
   > "Compared to your previous run:
   >
   > **LLM Score:** [old] → [new] ([+/-delta])
   > **MCP Score:** [old] → [new] ([+/-delta])
   >
   > [interpretation: what improved, what got worse, and why based on the scenarios]"

**Present results:**

> "Here are your chaos test results:
>
> **LLM Resilience:** [score]/100 — [PASS/DEGRADED/FAIL]
> - Requests: [N], Faults injected: [N], Failures: [N]
> - [interpretation based on specific numbers]
>
> **MCP Resilience:** [score]/100 — [PASS/DEGRADED/FAIL] *(if applicable)*
> - Tool calls: [N], Faults injected: [N], Failures: [N]
> - [per-tool breakdown if interesting]
>
> **What this means:**
> - 80-100: Your agent handles faults well
> - 50-79: Some failures — consider adding retry logic or error handling for [specific faults]
> - 0-49: Your agent is fragile — [specific recommendations based on what failed]"

## Scorecard fields reference

| Field | Meaning |
|-------|---------|
| `requests_seen` | Total requests proxied |
| `injected_faults` | Faults AgentBreak injected |
| `latency_injections` | Latency delays added |
| `upstream_successes` | Requests that succeeded |
| `upstream_failures` | Requests that failed |
| `duplicate_requests` | Same request body seen 2+ times |
| `suspected_loops` | Same body 3+ times (agent may be stuck) |
| `run_outcome` | PASS, DEGRADED, or FAIL |
| `resilience_score` | 0-100 |

MCP scorecard adds: `tool_calls`, `tool_call_counts`, `tool_successes_by_name`, `tool_failures_by_name`.

## Scenario schema reference

### Fault kinds

| Kind | Effect | Target |
|------|--------|--------|
| `http_error` | Returns HTTP error (needs `status_code`) | llm_chat, mcp_tool |
| `latency` | Adds delay (needs `min_ms`, `max_ms`) | llm_chat, mcp_tool |
| `timeout` | Delay + 504 error | **mcp_tool only** |
| `empty_response` | 200 with empty body | llm_chat, mcp_tool |
| `invalid_json` | 200 with unparseable JSON | llm_chat, mcp_tool |
| `schema_violation` | 200 with corrupted structure | llm_chat, mcp_tool |
| `wrong_content` | 200 with replaced content | llm_chat, mcp_tool |
| `large_response` | 200 with oversized body (needs `size_bytes`) | llm_chat, mcp_tool |

### Schedule modes

| Mode | Fields | Behavior |
|------|--------|----------|
| `always` | none | Every matching request faulted |
| `random` | `probability` (0-1) | Probabilistic |
| `periodic` | `every`, `length` | `length` out of every `every` requests |

### Presets

```yaml
preset: brownout              # latency + 429 on LLM
preset: mcp-slow-tools        # latency on MCP
preset: mcp-tool-failures     # 503 on MCP
preset: mcp-mixed-transient   # latency + 503 on MCP
```

## Rules

- **Always determine the LLM provider before generating config.** If ambiguous, ask. Never generate scenarios for both providers — pick one.
- **Never skip review checkpoints.** Use `AskUserQuestion` at every checkpoint so the user gets an interactive prompt. Wrong provider detection leads to wrong endpoints. Wrong scenarios lead to untested failure modes.
- **Complete steps in order.** Each step depends on the previous step's output.
- **Use subagents for codebase scanning** in Step 2. Serial grep is too slow on large codebases.
- **Only show the relevant provider's env vars** in Step 4. If the user uses OpenAI, don't mention Anthropic.
- **In mock mode, no API keys are needed.** Don't ask the user for keys unless they want proxy mode.
- **If history is enabled, always compare with the previous run in Step 5.** This helps users track whether their changes improved resilience.

## Common issues

- **Port 5000 in use on macOS**: AirPlay Receiver. Use 5005.
- **MCP inspect fails**: Upstream MCP server must be running first.
- **No faults firing**: Probability too low. Use 0.3+ for visible results.
- **Registry not found**: Run `agentbreak inspect` before `serve` when MCP enabled.
- **No history found**: Enable `history.enabled: true` in `.agentbreak/application.yaml` and re-run.
