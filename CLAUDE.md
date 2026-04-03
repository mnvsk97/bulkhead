# Claude Code — AgentBreak

## What this repo is

A Python package that proxies LLM (OpenAI + Anthropic) and MCP traffic, injecting configurable faults. Config lives in `.agentbreak/`, created by `agentbreak init`.

## Quick commands

```bash
pip install -e '.[dev]'
agentbreak init
agentbreak validate
agentbreak serve
```

For MCP: `agentbreak inspect` then `serve`.

## Plugin

Install in Claude Code via `/plugin marketplace add mnvsk97/agentbreak`, then use `/agentbreak`.

## Verification

Before suggesting a change is done: run **`agentbreak verify`**.
