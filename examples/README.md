# Examples

Two tiny examples are included so you can point real agent code at Bulkhead immediately.

## 1. Simple LangChain agent

```bash
cd examples/simple_langchain
pip install -r requirements.txt
OPENAI_API_KEY=... OPENAI_BASE_URL=http://localhost:5000/v1 python main.py
```

## 2. Deep Agents example

```bash
cd examples/deepagents
pip install -r requirements.txt
OPENAI_API_KEY=... OPENAI_BASE_URL=http://localhost:5000/v1 python main.py
```

## Full flow

In one terminal:

```bash
bulkhead start --mode proxy --upstream-url https://api.openai.com --scenario mixed-transient --fail-rate 0.2
```

In another terminal, run either example.
