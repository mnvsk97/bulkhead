---
name: agentbreak-testing
description: Use when testing an LLM app or agent with AgentBreak in this repo. Starts AgentBreak in mock or proxy mode, chooses a scenario or weighted faults, points the target app at OPENAI_BASE_URL, runs the target command, and checks the scorecard endpoints.
---

# AgentBreak Testing

Use this skill when the user wants to run chaos tests against an OpenAI-compatible app with AgentBreak.

## Workflow

1. Decide mode:
   - `mock` for zero-upstream local testing
   - `proxy` for fault injection in front of a real upstream
2. Prefer scenarios first:
   - `mixed-transient`
   - `rate-limited`
   - `provider-flaky`
   - `non-retryable`
   - `brownout`
3. Use weighted faults only when the user asks for exact percentages such as `500=0.3,429=0.2`.
4. Start AgentBreak from the repo root:

```bash
source .venv/bin/activate && agentbreak start --mode mock --scenario mixed-transient --fail-rate 0.2 --port 5000
```

Or:

```bash
source .venv/bin/activate && agentbreak start --mode proxy --upstream-url https://api.openai.com --scenario mixed-transient --fail-rate 0.2 --port 5000
```

5. Point the target app at AgentBreak:

```bash
export OPENAI_BASE_URL=http://127.0.0.1:5000/v1
export OPENAI_API_KEY=dummy
```

Use a real API key only when proxying to a real upstream.

6. Run the target command or one of the examples:

```bash
source .venv/bin/activate && OPENAI_API_KEY=dummy OPENAI_BASE_URL=http://127.0.0.1:5000/v1 python examples/simple_langchain/main.py
```

7. Inspect the result:

```bash
curl http://127.0.0.1:5000/_agentbreak/scorecard
curl http://127.0.0.1:5000/_agentbreak/requests
```

8. Summarize:
   - requests seen
   - injected faults
   - duplicate requests
   - suspected loops
   - run outcome
   - resilience score

When reporting duplicate requests or suspected loops, note that some agent frameworks legitimately issue repeated underlying completions. Treat those counters as investigation signals, not automatic proof of a bug.

## Install

Copy this skill into your local Codex skills directory:

```bash
mkdir -p ~/.codex/skills/agentbreak-testing
cp skills/agentbreak-testing/SKILL.md ~/.codex/skills/agentbreak-testing/SKILL.md
```

Then restart Codex so it reloads local skills.

## Invoke

Ask for the skill by name, for example:

- `Use the agentbreak-testing skill to run the simple_langchain example in mock mode.`
- `Use the agentbreak-testing skill to run proxy mode against https://api.openai.com and report the scorecard.`
- `Use the agentbreak-testing skill to run the simple_langchain example with AGENTBREAK_REQUEST_COUNT=10.`

## Notes

- If `config.yaml` exists, `agentbreak start` will load it automatically.
- CLI flags override `config.yaml`.
- The examples also read `request_count` from `config.yaml`, or `AGENTBREAK_REQUEST_COUNT` if it is set.
- For first-time users, prefer `mock` mode because it avoids upstream setup.
- For end-to-end resilience testing, prefer `proxy` mode.
