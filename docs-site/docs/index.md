# AgentBreak

**Chaos proxy for testing how your agents handle failures.**

Your agent works great — until the LLM times out, returns garbage, or an MCP tool fails. AgentBreak lets you test for that *before* production.

It sits between your agent and the real API, injecting faults like latency spikes, HTTP errors, and malformed responses so you can see how your agent actually handles failure.

```
Agent  -->  AgentBreak (localhost:5005)  -->  Real LLM / MCP server
                     ^
          injects faults based on your scenarios
```

## Install

```bash
pip install agentbreak
```

## 30-second demo

```bash
agentbreak init       # creates .agentbreak/ with default configs
agentbreak serve      # start the chaos proxy on port 5005
```

Point your agent at `http://localhost:5005`:

```bash
# OpenAI
export OPENAI_BASE_URL=http://localhost:5005/v1

# Anthropic
export ANTHROPIC_BASE_URL=http://localhost:5005
```

Run your agent, then check how it did:

```bash
curl localhost:5005/_agentbreak/scorecard
```

No code changes needed — just swap the base URL.

## What can it do?

- **Simulate failures** — HTTP errors, latency spikes, timeouts, malformed JSON, schema violations
- **Target specific things** — scope faults to a model (`gpt-4o`) or MCP tool (`search_docs`)
- **Control timing** — faults on every request, randomly, or in periodic bursts
- **Score resilience** — get a 0-100 score with pass/degraded/fail outcome
- **Track over time** — compare runs to see if your agent is getting more resilient
- **Test MCP** — proxy and fault-inject MCP tool calls, resource reads, and prompt gets

## Claude Code

If you use [Claude Code](https://docs.anthropic.com/en/docs/claude-code), AgentBreak has a plugin:

```
/plugin marketplace add mnvsk97/agentbreak
/plugin install agentbreak@mnvsk97-agentbreak
```

Then use `/agentbreak:init`, `/agentbreak:create-tests`, and `/agentbreak:run-tests` — Claude walks you through codebase analysis, scenario generation, and resilience reporting.

## Next steps

- [Quickstart](getting-started/quickstart.md) — full walkthrough
- [Scenarios reference](reference/scenarios.md) — all fault kinds, schedules, and match filters
- [Testing methodology](guides/testing-methodology.md) — how to design effective chaos tests
