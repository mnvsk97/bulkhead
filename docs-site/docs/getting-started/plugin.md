# Claude Code Plugin

AgentBreak works as a plugin for Claude Code, giving Claude structured commands to run chaos tests. It handles `.env` backup/restore, proxy lifecycle, and produces actionable resilience reports.

## Install

```bash
pip install agentbreak
```

## Configure

In Claude Code:

```
/plugin marketplace add mnvsk97/agentbreak
/plugin install agentbreak@mnvsk97-agentbreak
/reload-plugins
```

## Commands

| Command | What it does |
|---------|-------------|
| `/agentbreak:init` | Initialize AgentBreak, analyze your agent codebase |
| `/agentbreak:create-tests` | Generate tailored chaos scenarios |
| `/agentbreak:run-tests` | Run tests and produce a resilience report |

### `/agentbreak:init`

Sets up `.agentbreak/` config and scans your codebase for provider, framework, MCP tools, and error handling patterns. Run this first.

### `/agentbreak:create-tests`

Generate `scenarios.yaml` entries based on the analysis from init. Claude understands the full scenario schema (8 fault kinds, 3 schedule modes, match filters) and writes scenarios targeting your agent's specific failure modes.

### `/agentbreak:run-tests`

Step-by-step test execution: validate, serve, wire your agent, send traffic, collect scorecard, produce a Chaos Test Report with specific fixes.

## Safety

- **`.env` is always restored.** The plugin backs up your `.env` before wiring and restores it when done.
- **Proxy is always stopped.** Cleanup runs even if something goes wrong mid-test.
- If something goes wrong, you can always restore manually:

```bash
cp .env.agentbreak-backup .env
pkill -f "agentbreak serve"
```

## Plugin vs CLI

| | CLI | Plugin |
|---|-----|--------|
| Install | `pip install agentbreak` | `pip install agentbreak` + `/plugin install` in Claude Code |
| Usage | Manual commands | Claude runs commands automatically |
| .env handling | Manual backup/restore | Automatic backup + restore on stop |
| Scorecard | `curl` output | Structured report in Claude's context |
| Best for | CI, scripts, manual testing | Interactive development with Claude |
