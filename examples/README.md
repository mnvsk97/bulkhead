# Examples

## `simple_mcp_server`

MCP server with reporting tools (list sections, fetch KPIs, lookup notes, render briefs). This is the upstream that AgentBreak proxies.

```bash
cd examples/simple_mcp_server
pip install -r requirements.txt
python main.py
```

## `simple_react_agent`

LangGraph ReAct agent that calls an OpenAI-compatible LLM and the MCP server above. Point both URLs at AgentBreak to chaos-test the agent.

```bash
cd examples/simple_react_agent
pip install -r requirements.txt
cp .env.example .env
langgraph dev
```

## `live_harness`

Automated E2E test — boots everything (mock OpenAI, MCP server, AgentBreak, LangGraph agent) and runs traffic through.

```bash
agentbreak verify --live
```
