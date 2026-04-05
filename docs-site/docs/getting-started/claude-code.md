# Claude Code Workflow

The plugin splits chaos testing into three commands. Run them in order, or jump to the one you need.

## Prerequisites

AgentBreak must be installed and the plugin configured — see [Plugin](plugin.md).

## Commands

### `/agentbreak:init` — Initialize & Analyze

Sets up AgentBreak and scans your codebase to detect:

- **LLM provider** — OpenAI or Anthropic
- **Agent framework** — LangGraph, LangChain, CrewAI, raw SDK, etc.
- **MCP tools** — tool names, server URLs
- **Error handling** — retry logic, timeouts, try/except patterns

Creates `.agentbreak/` if needed and confirms findings with you before proceeding.

### `/agentbreak:create-tests` — Generate Scenarios

Generates tailored scenarios in `scenarios.yaml` based on the analysis from init. Scenarios target gaps — if your agent has no retry logic, it'll prioritize error scenarios.

You review and adjust the scenarios before proceeding.

If you'd rather skip the analysis and use a preset, just tell Claude:

```yaml
preset: brownout
```

### `/agentbreak:run-tests` — Run Tests & Report

1. Runs `agentbreak inspect` (if MCP), validates config, starts the chaos proxy
2. Rewires your agent's `.env` to point at AgentBreak, runs your agent, sends traffic through the proxy
3. Restores the original `.env` and stops the proxy
4. Reads the scorecard and produces a structured **Chaos Test Report** with:

    - Traffic summary and resilience score
    - What happened for each fault that fired
    - Numbered issues with severity and evidence
    - Specific, copy-pasteable code fixes referencing your actual files
    - If history is enabled, comparison with previous runs

You can then ask Claude to apply the fixes directly.

## What makes it useful

The plugin does what you'd otherwise do manually — but it picks the *right* scenarios for your codebase. An agent with no retry logic gets rate limit errors. An agent with MCP tools gets per-tool fault injection. The report ties failures back to specific lines in your code.
