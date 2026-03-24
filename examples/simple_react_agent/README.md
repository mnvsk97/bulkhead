# Simple ReAct Agent

LangGraph ReAct agent that uses an OpenAI-compatible LLM and MCP reporting tools.

## Quick start

```bash
cd examples/simple_react_agent
pip install -r requirements.txt
cp .env.example .env    # edit: OPENAI_BASE_URL, REPORT_MCP_URL
langgraph dev
```

## Chaos testing with AgentBreak

Point both URLs at AgentBreak instead of the real backends:

```env
OPENAI_BASE_URL=http://127.0.0.1:5005/v1
REPORT_MCP_URL=http://127.0.0.1:5005/mcp
```
