# AgentBreak

AgentBreak lets you test how your app behaves when an OpenAI-compatible provider is slow, flaky, or down.

It sits between your app and the provider, then randomly:

- returns errors like `429` or `500`
- adds latency
- lets some requests pass through normally

Use it to check whether your app retries, falls back, or breaks.

PyPI package: [`agentbreak`](https://pypi.org/project/agentbreak/)

## Quick Start

Requirements:

- Python 3.10+

Install from PyPI:

```bash
pip install agentbreak
```

Start AgentBreak in mock mode:

```bash
agentbreak start --mode mock --scenario mixed-transient --fail-rate 0.2
```

Point your app to it:

```bash
export OPENAI_BASE_URL=http://localhost:5000/v1
export OPENAI_API_KEY=dummy
```

That is enough to start testing retry and fallback behavior locally.

## How It Works

Normal path:

```text
your app -> AgentBreak -> provider
```

Mock path:

```text
your app -> AgentBreak -> fake response / injected failure
```

## Real Provider Mode

If you want real upstream calls plus injected failures:

```bash
agentbreak start \
  --mode proxy \
  --upstream-url https://api.openai.com \
  --scenario mixed-transient \
  --fail-rate 0.2
```

Then keep your app pointed at:

```bash
export OPENAI_BASE_URL=http://localhost:5000/v1
```

## What It Supports

- `POST /v1/chat/completions`
- failure injection: `400`, `401`, `403`, `404`, `413`, `429`, `500`, `503`
- latency injection
- duplicate request tracking
- a simple scorecard
- mock mode and proxy mode

## Useful Endpoints

```bash
curl http://localhost:5000/_agentbreak/scorecard
curl http://localhost:5000/_agentbreak/requests
```

Stop the server with `Ctrl+C` to print the final scorecard in the terminal.

## Config File

AgentBreak will load `config.yaml` from the current directory if it exists.

Fastest setup:

```bash
cp config.example.yaml config.yaml
agentbreak start
```

CLI flags override config values.

## Examples

Run the sample LangChain app:

```bash
cd examples/simple_langchain
pip install -r requirements.txt
OPENAI_API_KEY=dummy OPENAI_BASE_URL=http://localhost:5000/v1 python main.py
```

More examples: [examples/README.md](examples/README.md)

## Development

Install locally in editable mode:

```bash
pip install -e .
```

Run tests:

```bash
pip install -e '.[dev]'
pytest -q
```

## Agent Skill

This repo includes a portable Agent Skills skill at [skills/agentbreak-testing/SKILL.md](skills/agentbreak-testing/SKILL.md).

## Links

- [Contributing](CONTRIBUTING.md)
- [Security](SECURITY.md)
- [License](LICENSE)
