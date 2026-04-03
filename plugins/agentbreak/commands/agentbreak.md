---
description: Chaos-test your LLM agent with AgentBreak
allowed-tools: Read, Glob, Grep, Bash, Edit, Write, AskUserQuestion
---

Chaos-test an LLM agent end-to-end using AgentBreak.

Raw slash-command arguments:
`$ARGUMENTS`

Use the `agentbreak` skill for the full workflow. It walks through:

1. Install check (`agentbreak --help`)
2. Init (`.agentbreak/` config)
3. Analyze the codebase for provider, framework, MCP tools, error handling
4. Generate chaos scenarios
5. Start proxy, wire agent, run traffic
6. Scorecard + report with specific fixes

Ask the user to confirm before each major step. Always stop the proxy and restore `.env` when done.
