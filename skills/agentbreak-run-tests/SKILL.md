---
name: agentbreak-run-tests
description: Run chaos tests against an OpenAI-compatible app or MCP server using AgentBreak.
---

# AgentBreak -- Run Tests

## Workflow

1. Set up configs:

   ```bash
   cp config.example.yaml application.yaml   # edit: llm.mode, mcp.enabled
   cp scenarios.example.yaml scenarios.yaml   # edit: faults to inject
   pip install -e '.[dev]'
   ```

2. If MCP is enabled, discover tools:

   ```bash
   agentbreak inspect --config application.yaml
   ```

3. Validate and serve:

   ```bash
   agentbreak validate --config application.yaml --scenarios scenarios.yaml
   agentbreak serve --config application.yaml --scenarios scenarios.yaml -v
   ```

4. Point agent at AgentBreak:

   ```bash
   export OPENAI_BASE_URL=http://127.0.0.1:5005/v1
   ```

5. Check results:

   ```bash
   curl http://127.0.0.1:5005/_agentbreak/scorecard
   curl http://127.0.0.1:5005/_agentbreak/mcp-scorecard   # if MCP enabled
   ```

## Scorecard

| Score | Meaning |
|-------|---------|
| 80-100 | Resilient |
| 50-79 | Degraded |
| 0-49 | Fragile |

## Notes

- `llm.mode: mock` needs no API key. `llm.mode: proxy` forwards to real LLM.
- Presets: `brownout`, `mcp-slow-tools`, `mcp-tool-failures`, `mcp-mixed-transient`.
- Ctrl+C prints final scorecard to stderr.
