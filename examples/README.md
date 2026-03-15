# Examples

Two tiny examples are included so you can point real agent code at Bulkhead immediately.

Both examples can send more than one request. Set `request_count` in [config.example.yaml](/Users/saikrishna/tfy/bulkhead/config.example.yaml) via your local `config.yaml`, or override it with `BULKHEAD_REQUEST_COUNT`.

If port `5000` is already in use, start Bulkhead on another port and set `OPENAI_BASE_URL` to match, for example `http://127.0.0.1:5050/v1`.

## 1. Simple LangChain agent

```bash
cd examples/simple_langchain
pip install -r requirements.txt
OPENAI_API_KEY=dummy OPENAI_BASE_URL=http://localhost:5000/v1 python main.py
```

## 2. Deep Agents example

```bash
cd examples/deepagents
pip install -r requirements.txt
OPENAI_API_KEY=dummy OPENAI_BASE_URL=http://localhost:5000/v1 python main.py
```

## Full flow

In one terminal:

```bash
bulkhead start --mode proxy --upstream-url https://api.openai.com --scenario mixed-transient --fail-rate 0.2
```

In another terminal, run either example. For repeatable scorecards, you can also set:

```bash
export BULKHEAD_REQUEST_COUNT=10
```
