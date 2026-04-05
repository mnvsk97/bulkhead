# Claude Code Plugin

AgentBreak works as a plugin for Claude Code, giving Claude structured commands to run chaos tests. It handles `.env` backup/restore, proxy lifecycle, and produces actionable resilience reports.

## Install

1. Install the package:

```bash
pip install agentbreak
```

2. Add the plugin in Claude Code:

```
/plugin marketplace add mnvsk97/agentbreak
/plugin install agentbreak@mnvsk97-agentbreak
/reload-plugins
```

To update after a new release:

```
/reload-plugins
```

## Commands

| Command | What it does |
|---------|-------------|
| `/agentbreak:init` | Initialize AgentBreak, analyze your agent codebase |
| `/agentbreak:create-tests` | Generate project-specific chaos scenarios |
| `/agentbreak:run-tests` | Run tests and produce a resilience report |

### `/agentbreak:init`

Full setup flow:

1. Checks AgentBreak is installed
2. Runs `agentbreak init` to create `.agentbreak/`
3. Analyzes your codebase (provider, framework, MCP tools, error handling)
4. Asks: **mock or proxy mode?**
    - **Mock** — no API keys needed, synthetic responses
    - **Proxy** — real API traffic, requires valid keys
5. Writes `application.yaml` and `scenarios.yaml` (with standard preset)
6. Validates config (+ `--test-connection` if proxy mode)
7. Offers to generate project-specific scenarios

### `/agentbreak:create-tests`

Generates project-specific scenarios on top of the standard preset. Analyzes your codebase to find specific MCP tools, models, and integrations, then writes targeted fault scenarios. Can be run anytime to add more scenarios.

Standard baseline scenarios are always included via the preset — this command focuses on what's unique to your agent.

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
