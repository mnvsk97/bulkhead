# AgentBreak

Your agent works great — until the LLM times out, returns garbage, or an MCP tool fails. AgentBreak lets you test for that *before* production.

It's a chaos proxy that sits between your agent and the real API, injecting faults like latency spikes, HTTP errors, and malformed responses so you can see how your agent actually handles failure.

```
Agent  -->  AgentBreak (localhost:5005)  -->  Real LLM / MCP server
                     ^
          injects faults based on your scenarios
```

## Get started

```bash
pip install agentbreak
agentbreak init       # creates .agentbreak/ with default configs
agentbreak serve      # start the chaos proxy on port 5005
```

Point your agent at `http://localhost:5005` instead of the real API:

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

That's it. No code changes needed — just swap the base URL.

## How it works

AgentBreak reads two files from `.agentbreak/`:

- **`application.yaml`** — what to proxy (LLM mode, MCP upstream, port)
- **`scenarios.yaml`** — what faults to inject

A scenario is just a target + a fault + a schedule:

```yaml
scenarios:
  - name: slow-llm
    summary: Latency spike on completions
    target: llm_chat          # what to hit (llm_chat or mcp_tool)
    fault:
      kind: latency           # what goes wrong
      min_ms: 2000
      max_ms: 5000
    schedule:
      mode: random            # when it happens
      probability: 0.3
```

Don't want to write YAML? Use a preset:

```yaml
preset: brownout
```

Available presets: `standard`, `standard-mcp`, `standard-all`, `brownout`, `mcp-slow-tools`, `mcp-tool-failures`, `mcp-mixed-transient`.

## MCP testing

```bash
agentbreak inspect    # discover tools from your MCP server
agentbreak serve      # proxy both LLM and MCP traffic
```

## Track resilience over time

```yaml
# in .agentbreak/application.yaml
history:
  enabled: true
```

```bash
agentbreak serve --label "added retry logic"
agentbreak history compare 1 2    # diff two runs
```

## Claude Code

AgentBreak works as a plugin for [Claude Code](https://docs.anthropic.com/en/docs/claude-code):

```bash
pip install agentbreak
```

Then in Claude Code:

```
/plugin marketplace add mnvsk97/agentbreak
/plugin install agentbreak@mnvsk97-agentbreak
/reload-plugins
```

Now use the three commands — `/agentbreak:init`, `/agentbreak:create-tests`, and `/agentbreak:run-tests` — and Claude walks you through codebase analysis, scenario generation, and resilience reporting.

## Full reference

For the full list of fault kinds, schedule modes, match filters, and config options, see the [documentation](https://mnvsk97.github.io/agentbreak).

## Examples

See [examples/](examples/) for sample agents and MCP servers you can test against.
