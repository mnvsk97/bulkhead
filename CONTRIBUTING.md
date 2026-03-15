# Contributing

Thanks for contributing to AgentBreak.

## Development Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]' -r examples/simple_langchain/requirements.txt
```

## Common Commands

Run tests:

```bash
pytest -q
```

Run AgentBreak locally:

```bash
agentbreak start --mode mock --scenario mixed-transient --fail-rate 0
```

Run the simple example:

```bash
OPENAI_API_KEY=dummy OPENAI_BASE_URL=http://127.0.0.1:5000/v1 python examples/simple_langchain/main.py
```

## Guidelines

- Keep the tool small and focused.
- Prefer simple CLI and config behavior over extra abstraction.
- Add or update tests for behavior changes.
- Update the README when user-facing behavior changes.
- Avoid adding provider-specific logic unless it is required for OpenAI-compatible APIs.

## Pull Requests

- Keep PRs scoped.
- Include a short summary of user-visible changes.
- Mention how you verified the change.
