# Claude Code (and similar) — AgentBreak

## What this repo is

A Python package that proxies **LLM chat completions** and **MCP** traffic and injects **configurable faults** from `scenarios.yaml`. Configuration is **`.agentbreak/application.yaml`** + **`.agentbreak/scenarios.yaml`**, created by `agentbreak init`.

## Quick commands

```bash
pip install -e '.[dev]'
agentbreak init
# Edit .agentbreak/application.yaml: llm.mode mock|proxy, mcp.enabled true|false

agentbreak validate
agentbreak serve
```

For MCP mirroring: `agentbreak inspect` then `serve`.

## Skill

Bundled workflow under **`skills/`** (install via `npx skills add mnvsk97/agentbreak`):

| Skill | Path |
|-------|------|
| Chaos test your agent | `skills/agentbreak/SKILL.md` |

## Docs map

| File | Purpose |
|------|---------|
| `README.md` | User-facing overview, scenario format, examples |
| `CONTRIBUTING.md` | Dev setup, verify, validate, inspect |
| `AGENTS.md` | Agent/coding guidelines for this repo |
| `docs/README.md` | Index of extra docs |
| `docs/FAILURE_MODES.md` | Scope of simulated failures |
| `docs/TODO_SCENARIOS.md` | Deferred scenario targets |

## Verification

Before suggesting a change is done: run **`agentbreak verify`**.
