# Demo Script (2 minutes)

Pre-open 3 terminal tabs, all starting with:

```bash
cd ~/tfy/agentbreak && source .venv/bin/activate
```

---

## Tab 1 — MCP Server

```bash
python examples/mcp_servers/no_auth/main.py
```

---

## Tab 2 — AgentBreak

```bash
agentbreak inspect --config application.yaml
```

```bash
cat scenarios.yaml
```

```bash
agentbreak serve --config application.yaml --scenarios scenarios.yaml -v
```

---

## Tab 3 — Be the agent

**LLM calls:**

```bash
for i in {1..8}; do
  echo "--- Request $i ---"
  START=$(python3 -c "import time; print(time.time())")
  RESP=$(curl -s -w "\n%{http_code}" http://localhost:5005/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"gpt-4o\",\"messages\":[{\"role\":\"user\",\"content\":\"Generate QBR for acct-acme, request $i\"}]}")
  CODE=$(echo "$RESP" | tail -1)
  END=$(python3 -c "import time; print(time.time())")
  ELAPSED=$(python3 -c "print(f'{$END - $START:.1f}s')")
  echo "  HTTP $CODE  (${ELAPSED})"
done
```

**MCP tool calls:**

```bash
curl -s http://localhost:5005/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-protocol-version: 2024-11-05" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"clientInfo":{"name":"demo","version":"0.1"}}}' > /dev/null

echo "--- fetch_kpi_snapshot (4 calls, 50% will be corrupted) ---"
for i in {1..4}; do
  RESULT=$(curl -s http://localhost:5005/mcp \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "mcp-protocol-version: 2024-11-05" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":$i,\"method\":\"tools/call\",\"params\":{\"name\":\"fetch_kpi_snapshot\",\"arguments\":{\"metric_names\":[\"arr\",\"nrr\"],\"as_of\":\"2026-03-24\"}}}")
  HAS_INVALID=$(echo "$RESULT" | grep -c '"INVALID"')
  if [ "$HAS_INVALID" -gt 0 ]; then
    echo "  Call $i: CORRUPTED (schema_violation injected)"
  else
    echo "  Call $i: OK (real data)"
  fi
done
```

**Scorecard:**

```bash
curl -s http://localhost:5005/_agentbreak/scorecard | python3 -m json.tool
```

```bash
curl -s http://localhost:5005/_agentbreak/mcp-scorecard | python3 -m json.tool
```

---

## Talk track

1. **Tab 1**: "Here's an MCP server with reporting tools — KPIs, account notes, report briefs."
2. **Tab 2 inspect**: "AgentBreak discovers all 4 tools automatically."
3. **Tab 2 cat scenarios**: "We're injecting 3 faults: 50% latency on LLM, 30% random 500s on LLM, 50% schema corruption on one MCP tool."
4. **Tab 2 serve**: "Start the proxy. One port, both LLM and MCP."
5. **Tab 3 LLM**: "Agent sends 8 requests. Watch — some are slow, some get 500s."
6. **Tab 3 MCP**: "Agent calls fetch_kpi_snapshot 4 times. Some return real data, some return corrupted results."
7. **Tab 3 scorecard**: "Resilience score, faults injected, duplicates detected — all automatic. Zero code changes to the agent, just swap the URL."
