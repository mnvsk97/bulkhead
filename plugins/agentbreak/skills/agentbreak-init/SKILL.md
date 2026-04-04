---
name: agentbreak-init
description: Initialize AgentBreak config and analyze the agent codebase. Creates .agentbreak/ with application.yaml and scenarios.yaml tailored to the detected provider, framework, and error handling.
---

# AgentBreak -- Init

You are helping the user set up AgentBreak for chaos testing their agent.

## Your job

1. Check AgentBreak is installed
2. Run `agentbreak init`
3. Analyze the codebase
4. Configure `application.yaml` and `scenarios.yaml` based on findings
5. Validate the config

Ask the user to confirm before writing config.

## Step 1: Install check

```bash
agentbreak --help
```

If not found, install it:

```bash
pip install agentbreak
```

## Step 2: Init

```bash
agentbreak init
```

Creates `.agentbreak/` with default `application.yaml` and `scenarios.yaml`.

## Step 3: Analyze the codebase

Scan the project to understand the agent. Look for:

- **Provider:** Search for `openai`, `anthropic`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` in code and `.env`
- **Framework:** Search for `langgraph`, `langchain`, `crewai`, `autogen`, `openai` SDK usage
- **MCP tools:** Search for `mcp`, `MCPClient`, `tool_call` patterns
- **Error handling:** Search for `max_retries`, `retry`, `except`, `timeout`, `backoff`

Present findings:

> "Here's what I found:
> - **Provider:** [openai/anthropic]
> - **Framework:** [langgraph/langchain/openai SDK/etc.]
> - **MCP tools:** [detected/none]
> - **Error handling:** [has retry logic / no retry logic]
>
> Does this look correct?"

If the provider is ambiguous, ask which one to use.

## Step 4: Configure

Based on analysis, write `.agentbreak/application.yaml`:

**Mock mode (no API key needed — good for demos):**

```yaml
llm:
  enabled: true
  mode: mock
mcp:
  enabled: false
serve:
  port: 5005
```

**Proxy mode (real API):**

```yaml
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

**If MCP detected, add:**

```yaml
mcp:
  enabled: true
  upstream_url: http://localhost:8001/mcp
```

Write `.agentbreak/scenarios.yaml` based on findings. Good starter:

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

If agent has no retry logic → higher probabilities (0.3-0.5).
If agent has retries → lower probabilities (0.1-0.2).

Present the generated config and ask for confirmation before writing.

## Step 5: Validate

```bash
agentbreak validate
```

If validation fails, fix the config and re-validate.

## Done

Tell the user:

> "AgentBreak is initialized. Next steps:
> - `/agentbreak:create-tests` to customize scenarios
> - `/agentbreak:run-tests` to start chaos testing"

## Rules

- **Detect one provider.** Never configure both. If ambiguous, ask.
- **Always confirm** before writing config files.
- **Always validate** after writing config.
- **Port 5005** is the default (macOS AirPlay uses 5000).
