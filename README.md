# Bulkhead

Minimal chaos proxy for OpenAI-compatible LLM apps.

Think of Bulkhead as Toxiproxy for AI: instead of generic TCP toxics, it injects OpenAI-style API failures, latency, and weighted fault scenarios into LLM traffic so you can test retries, fallbacks, and resilience logic.

Bulkhead can run in two modes:

- `proxy`: inject faults, otherwise forward to a real upstream
- `mock`: inject faults, otherwise return a tiny fake completion

It prints a simple resilience scorecard when you stop it.

## Quick Start

No upstream needed:

```bash
pip install -e .
bulkhead start --mode mock --scenario mixed-transient --fail-rate 0.2
```

Then point your app at Bulkhead:

```bash
export OPENAI_BASE_URL=http://localhost:5000/v1
```

## Install

```bash
pip install -e .
```

## Config

Bulkhead will automatically load `config.yaml` from the current directory if it exists.

You can also pass a custom file:

```bash
bulkhead start --config bulkhead.yaml
```

CLI flags override YAML values.

Quick start:

```bash
cp config.example.yaml config.yaml
bulkhead start
```

See [config.example.yaml](/Users/saikrishna/tfy/bulkhead/config.example.yaml).

## Proxy Mode

```bash
bulkhead start --mode proxy --upstream-url https://api.openai.com --scenario mixed-transient --fail-rate 0.2
```

Point your app at Bulkhead:

```bash
export OPENAI_BASE_URL=http://localhost:5000/v1
```

## Mock Mode

```bash
bulkhead start --mode mock --scenario mixed-transient --fail-rate 0.2
```

For SDKs that require an API key even in mock mode, use any dummy value:

```bash
export OPENAI_API_KEY=dummy
```

## Advanced Fault Rates

Inject exact per-code percentages of total requests:

```bash
bulkhead start --mode proxy --upstream-url https://api.openai.com --faults 500=0.30,429=0.45
```

That means:

- `30%` of requests get `500`
- `45%` of requests get `429`
- the rest pass through

- proxies `POST /v1/chat/completions`
- injects `400, 401, 403, 404, 413, 429, 500, 503`
- injects latency
- tracks duplicate requests
- prints a resilience scorecard on shutdown

```bash
curl http://localhost:5000/_bulkhead/scorecard
curl http://localhost:5000/_bulkhead/requests
```

## Scenarios

- `mixed-transient`
- `rate-limited`
- `provider-flaky`
- `non-retryable`
- `brownout`

## Examples

Run the simple LangChain example:

```bash
cd examples/simple_langchain
pip install -r requirements.txt
OPENAI_API_KEY=dummy OPENAI_BASE_URL=http://localhost:5000/v1 python main.py
```

More examples: [examples/README.md](/Users/saikrishna/tfy/bulkhead/examples/README.md).

## Codex Skill

A minimal repo-local skill is included at [skills/bulkhead-testing/SKILL.md](/Users/saikrishna/tfy/bulkhead/skills/bulkhead-testing/SKILL.md) for running Bulkhead in `mock` or `proxy` mode, executing a target app, and checking the scorecard endpoints.
