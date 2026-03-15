# Bulkhead

Bulkhead helps you test what your app does when an AI provider is slow, flaky, or down.

In simple terms:

- put Bulkhead between your app and OpenAI-compatible APIs
- tell Bulkhead to randomly fail some requests, slow some down, or return rate limits
- see whether your app retries, falls back, or breaks

This is useful because provider outages are normal, not rare.

```mermaid
flowchart LR
    subgraph Mock["Mock Mode"]
        A1["Your app"] --> B1["Bulkhead"]
        B1 --> C1["Fake AI response<br/>or injected failure"]
    end

    subgraph Proxy["Proxy Mode"]
        A2["Your app"] --> B2["Bulkhead"]
        B2 -->|usually| C2["Real AI provider"]
        B2 -->|sometimes| D2["Injected failure<br/>slowdown / 429 / 500"]
    end
```

Provider outages, slowdowns, rate limits, and partial failures happen regularly. That is the reason this tool exists: you should be able to test them before your users find them for you.

If you know Toxiproxy, Bulkhead is the same basic idea for LLM APIs: it injects OpenAI-style failures, latency, and weighted fault scenarios so you can test retries, fallbacks, and resilience logic.

Bulkhead can run in two modes:

- `proxy`: send requests to the real provider, but occasionally simulate failures on the way
- `mock`: do not call a real provider; return a tiny fake response unless a failure is injected

When you stop it, Bulkhead prints a simple scorecard showing how your app handled those failures.

## Quick Start

Fastest way to try it, with no real provider needed:

```bash
pip install -e .
bulkhead start --mode mock --scenario mixed-transient --fail-rate 0.2
```

Then point your app at Bulkhead instead of directly at OpenAI:

```bash
export OPENAI_BASE_URL=http://localhost:5000/v1
```

## Install

```bash
pip install -e .
```

Run it with:

```bash
bulkhead start --mode mock --scenario mixed-transient --fail-rate 0.2
```

Run tests with:

```bash
pip install -e '.[dev]'
pytest -q
```

## Config

Bulkhead will automatically load `config.yaml` from the current directory if it exists.

You can also pass a custom file:

```bash
bulkhead start --config bulkhead.yaml
```

CLI flags override YAML values.

You can also set `request_count` in `config.yaml` for the included example apps. They will send that many requests to Bulkhead unless `BULKHEAD_REQUEST_COUNT` is set.

Quick start:

```bash
cp config.example.yaml config.yaml
bulkhead start
```

See [config.example.yaml](/Users/saikrishna/tfy/bulkhead/config.example.yaml).

## Real Provider Mode

```bash
bulkhead start --mode proxy --upstream-url https://api.openai.com --scenario mixed-transient --fail-rate 0.2
```

This forwards traffic to the real provider, but injects failures along the way.

Point your app at Bulkhead:

```bash
export OPENAI_BASE_URL=http://localhost:5000/v1
```

## Fake Provider Mode

```bash
bulkhead start --mode mock --scenario mixed-transient --fail-rate 0.2
```

This never calls a real provider. It is useful for local testing, demos, and retry logic checks.

For SDKs that require an API key even in mock mode, use any dummy value:

```bash
export OPENAI_API_KEY=dummy
```

## Exact Failure Mix

If you want a very specific test, you can choose the exact mix of failures:

```bash
bulkhead start --mode proxy --upstream-url https://api.openai.com --faults 500=0.30,429=0.45
```

That means:

- `30%` of requests get `500`
- `45%` of requests get `429`
- the rest pass through normally

Bulkhead currently:

- handles `POST /v1/chat/completions`
- can inject `400, 401, 403, 404, 413, 429, 500, 503`
- can inject latency
- tracks duplicate requests
- prints a resilience scorecard on shutdown

```bash
curl http://localhost:5000/_bulkhead/scorecard
curl http://localhost:5000/_bulkhead/requests
```

## Reading The Scorecard

The scorecard is a quick signal, not a perfect pass/fail oracle.

- `duplicate_requests` means Bulkhead saw the same request body more than once
- `suspected_loops` means Bulkhead saw the same request body at least three times

That can indicate a real problem such as:

- a retry loop with no backoff
- an app resending the same request after an error
- a framework getting stuck and replaying work

But it can also happen during normal agent execution. Some agent frameworks make repeated underlying completion calls while planning, using tools, or recovering from intermediate steps.

So:

- treat high duplicate counts as a clue to inspect
- use `/_bulkhead/requests` to see what was repeated
- do not assume every duplicate is a bug

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

A repo-local Codex skill is included at [skills/bulkhead-testing/SKILL.md](/Users/saikrishna/tfy/bulkhead/skills/bulkhead-testing/SKILL.md).

## Install The Skill

If you already cloned this repo, install the skill into Codex by copying it into your local skills directory:

```bash
mkdir -p ~/.codex/skills/bulkhead-testing
cp skills/bulkhead-testing/SKILL.md ~/.codex/skills/bulkhead-testing/SKILL.md
```

Then restart Codex so it reloads local skills.

## Use The Skill

Ask for it in plain English by name. For example:

```text
Use the bulkhead-testing skill to run the simple_langchain example in mock mode with request_count 10.
```

Or:

```text
Use the bulkhead-testing skill to run proxy mode against https://api.openai.com and summarize the scorecard.
```

You can also ask more generally:

- `Use the bulkhead-testing skill to test my app against rate limits.`
- `Use the bulkhead-testing skill to run Bulkhead in mock mode and inspect the scorecard.`
- `Use the bulkhead-testing skill to run the deepagents example with request_count 10.`

What the skill does:

- starts Bulkhead in `mock` or `proxy` mode
- points your app at `OPENAI_BASE_URL`
- runs your target command or example app
- checks `/_bulkhead/scorecard` and `/_bulkhead/requests`
- summarizes resilience signals like retries, duplicates, and failures

This repo is a local single-skill repo, not a published multi-skill registry like [truefoundry/tfy-agent-skills](https://github.com/truefoundry/tfy-agent-skills). So the install flow here is a local copy, not `npx skills add ...`.

Get started locally:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]' -r examples/simple_langchain/requirements.txt -r examples/deepagents/requirements.txt
cp config.example.yaml config.yaml
bulkhead start --mode mock --scenario mixed-transient --fail-rate 0
```

## Project Status

Bulkhead is currently an early-stage developer tool. Expect the API surface and scorecard heuristics to evolve.

## Contributing

See [CONTRIBUTING.md](/Users/saikrishna/tfy/bulkhead/CONTRIBUTING.md).

## Security

See [SECURITY.md](/Users/saikrishna/tfy/bulkhead/SECURITY.md).

## License

MIT. See [LICENSE](/Users/saikrishna/tfy/bulkhead/LICENSE).
