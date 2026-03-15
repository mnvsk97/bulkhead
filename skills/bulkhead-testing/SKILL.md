---
name: bulkhead-testing
description: Use when testing an LLM app or agent with Bulkhead in this repo. Starts Bulkhead in mock or proxy mode, chooses a scenario or weighted faults, points the target app at OPENAI_BASE_URL, runs the target command, and checks the scorecard endpoints.
---

# Bulkhead Testing

Use this skill when the user wants to run chaos tests against an OpenAI-compatible app with Bulkhead.

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
4. Start Bulkhead from the repo root:

```bash
source .venv/bin/activate && bulkhead start --mode mock --scenario mixed-transient --fail-rate 0.2 --port 5000
```

Or:

```bash
source .venv/bin/activate && bulkhead start --mode proxy --upstream-url https://api.openai.com --scenario mixed-transient --fail-rate 0.2 --port 5000
```

5. Point the target app at Bulkhead:

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
curl http://127.0.0.1:5000/_bulkhead/scorecard
curl http://127.0.0.1:5000/_bulkhead/requests
```

8. Summarize:
   - requests seen
   - injected faults
   - duplicate requests
   - suspected loops
   - run outcome
   - resilience score

## Notes

- If `config.yaml` exists, `bulkhead start` will load it automatically.
- CLI flags override `config.yaml`.
- For first-time users, prefer `mock` mode because it avoids upstream setup.
- For end-to-end resilience testing, prefer `proxy` mode.
