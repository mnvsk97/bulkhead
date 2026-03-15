# Bulkhead

Minimal chaos proxy for OpenAI-compatible LLM apps.

Bulkhead can run in two modes:

- `proxy`: inject faults, otherwise forward to a real upstream
- `mock`: inject faults, otherwise return a tiny fake completion

It prints a simple resilience scorecard when you stop it.

## Install

```bash
pip install -e .
```

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
OPENAI_API_KEY=... OPENAI_BASE_URL=http://localhost:5000/v1 python main.py
```

More examples: [examples/README.md](/Users/saikrishna/tfy/bulkhead/examples/README.md).
