# Contributing

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'   # includes pytest; use '.' for core only
```

## Commands

```bash
agentbreak verify                                                    # run pytest
agentbreak verify --live                                             # pytest + live LangGraph harness
agentbreak validate --config application.yaml --scenarios scenarios.yaml  # check config
agentbreak inspect --config application.yaml                         # discover MCP tools
agentbreak serve --config application.yaml --scenarios scenarios.yaml     # start proxy
```

## Repo Layout

| Path | Role |
|------|------|
| `agentbreak/main.py` | CLI (`serve`, `validate`, `inspect`, `verify`), FastAPI app, proxy logic |
| `agentbreak/config.py` | `application.yaml` Pydantic models, registry I/O |
| `agentbreak/scenarios.py` | `scenarios.yaml` schema and validation |
| `agentbreak/behaviors.py` | Response mutation helpers |
| `agentbreak/discovery/mcp.py` | MCP server inspection |
| `tests/` | Pytest suite (`agentbreak verify` runs these) |
| `examples/` | LangChain, LangGraph, MCP servers, live harness |

## Guidelines

- Keep the product surface small.
- Prefer one clear path over several aliases or modes.
- Keep scenarios explicit and typed.
- Preserve test isolation so `agentbreak verify` stays deterministic.
- Update docs when config shape or fault types change.

See also [AGENTS.md](AGENTS.md) and [docs/README.md](docs/README.md).
