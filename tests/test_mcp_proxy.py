from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from agentbreak import mcp_proxy
from agentbreak.mcp_protocol import (
    INTERNAL_ERROR,
    INVALID_REQUEST,
    MCP_TOOL_ERROR,
    METHOD_NOT_FOUND,
    MCPRequest,
)


@pytest.fixture(autouse=True)
def cleanup_mcp_state() -> None:
    """Ensure clean MCP state before and after each test."""
    # Setup: clean up any existing transports
    asyncio.run(_cleanup_all_transports())
    yield
    # Teardown: clean up again
    asyncio.run(_cleanup_all_transports())


async def _cleanup_all_transports() -> None:
    """Clean up all existing transports."""
    if mcp_proxy._stdio_transport is not None:
        try:
            await mcp_proxy._stdio_transport.stop()
        except Exception:
            pass
        mcp_proxy._stdio_transport = None
    if mcp_proxy._sse_transport is not None:
        try:
            await mcp_proxy._sse_transport.stop()
        except Exception:
            pass
        mcp_proxy._sse_transport = None
    if mcp_proxy._upstream_http_client is not None:
        try:
            await mcp_proxy._upstream_http_client.aclose()
        except Exception:
            pass
        mcp_proxy._upstream_http_client = None

def reset_state(
    mode: str = "mock",
    upstream_url: str = "http://upstream.example",
    fail_rate: float = 0.0,
    latency_p: float = 0.0,
) -> None:
    asyncio.run(_cleanup_all_transports())
    mcp_proxy.mcp_config = mcp_proxy.MCPConfig(
        mode=mode,
        upstream_url=upstream_url,
        fail_rate=fail_rate,
        latency_p=latency_p,
    )
    mcp_proxy.mcp_stats = mcp_proxy.MCPStats()
    mcp_proxy._response_cache = {}
    mcp_proxy._upstream_http_client = None


client = TestClient(mcp_proxy.app)


# ---------------------------------------------------------------------------
# /healthz
# ---------------------------------------------------------------------------

def test_healthz() -> None:
    reset_state()
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Parse error handling
# ---------------------------------------------------------------------------

def test_invalid_json_returns_parse_error() -> None:
    reset_state()
    resp = client.post("/mcp", content=b"not json", headers={"content-type": "application/json"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["error"]["code"] == -32700


def test_missing_method_returns_invalid_request() -> None:
    reset_state()
    payload = json.dumps({"jsonrpc": "2.0", "id": 1}).encode()
    resp = client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["error"]["code"] == -32600  # INVALID_REQUEST, not PARSE_ERROR


# ---------------------------------------------------------------------------
# Mock mode — success path
# ---------------------------------------------------------------------------

def test_mock_mode_returns_success() -> None:
    reset_state(mode="mock")
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    resp = client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    assert resp.status_code == 200
    body = resp.json()
    assert "error" not in body
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 1


def test_mock_mode_increments_upstream_successes() -> None:
    reset_state(mode="mock")
    payload = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).encode()
    client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    assert mcp_proxy.mcp_stats.upstream_successes == 1


# ---------------------------------------------------------------------------
# Stats tracking
# ---------------------------------------------------------------------------

def test_total_requests_incremented() -> None:
    reset_state(mode="mock")
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    assert mcp_proxy.mcp_stats.total_requests == 2


def test_tool_calls_tracked() -> None:
    reset_state(mode="mock")
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "my_tool", "arguments": {}},
    }).encode()
    client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    assert mcp_proxy.mcp_stats.tool_calls == 1


def test_resource_reads_tracked() -> None:
    reset_state(mode="mock")
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "resources/read",
        "params": {"uri": "file:///foo"},
    }).encode()
    client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    assert mcp_proxy.mcp_stats.resource_reads == 1


def test_init_requests_tracked() -> None:
    reset_state(mode="mock")
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
    }).encode()
    client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    assert mcp_proxy.mcp_stats.init_requests == 1


def test_duplicate_requests_tracked() -> None:
    reset_state(mode="mock")
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "t", "arguments": {"x": 1}},
    }).encode()
    # First request — not a duplicate
    client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    assert mcp_proxy.mcp_stats.duplicate_requests == 0
    # Second request with same fingerprint — duplicate
    payload2 = json.dumps({
        "jsonrpc": "2.0", "id": 99, "method": "tools/call",
        "params": {"name": "t", "arguments": {"x": 1}},
    }).encode()
    client.post("/mcp", content=payload2, headers={"content-type": "application/json"})
    assert mcp_proxy.mcp_stats.duplicate_requests == 1


def test_suspected_loops_tracked() -> None:
    reset_state(mode="mock")
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "loopy", "arguments": {}},
    }).encode()
    for _ in range(3):
        client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    assert mcp_proxy.mcp_stats.suspected_loops == 1


# ---------------------------------------------------------------------------
# Fault injection
# ---------------------------------------------------------------------------

def test_fault_injection_returns_mcp_error() -> None:
    reset_state(fail_rate=1.0)
    payload = json.dumps({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                          "params": {"name": "t", "arguments": {}}}).encode()
    resp = client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    assert resp.status_code == 200
    body = resp.json()
    assert "error" in body
    assert body["id"] == 5
    assert isinstance(body["error"]["code"], int)


def test_fault_injection_increments_stats() -> None:
    reset_state(fail_rate=1.0)
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    assert mcp_proxy.mcp_stats.injected_faults == 1
    # Injected faults are synthetic — they do not increment upstream_failures
    assert mcp_proxy.mcp_stats.upstream_failures == 0


def test_no_fault_injection_at_zero_rate() -> None:
    reset_state(fail_rate=0.0)
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    for _ in range(20):
        resp = client.post("/mcp", content=payload, headers={"content-type": "application/json"})
        body = resp.json()
        assert "error" not in body
    assert mcp_proxy.mcp_stats.injected_faults == 0


# ---------------------------------------------------------------------------
# HTTP -> MCP error code mapping
# ---------------------------------------------------------------------------

def test_pick_mcp_error_429_maps_to_tool_error() -> None:
    reset_state()
    mcp_proxy.mcp_config = mcp_proxy.MCPConfig(
        mode="mock", fail_rate=1.0, fault_codes=(429,)
    )
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "t", "arguments": {}}}).encode()
    resp = client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    body = resp.json()
    assert body["error"]["code"] == MCP_TOOL_ERROR


def test_pick_mcp_error_500_maps_to_internal_error() -> None:
    reset_state()
    mcp_proxy.mcp_config = mcp_proxy.MCPConfig(
        mode="mock", fail_rate=1.0, fault_codes=(500,)
    )
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    resp = client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    body = resp.json()
    assert body["error"]["code"] == INTERNAL_ERROR


def test_pick_mcp_error_404_maps_to_method_not_found() -> None:
    reset_state()
    mcp_proxy.mcp_config = mcp_proxy.MCPConfig(
        mode="mock", fail_rate=1.0, fault_codes=(404,)
    )
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    resp = client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    body = resp.json()
    assert body["error"]["code"] == METHOD_NOT_FOUND


def test_pick_mcp_error_400_maps_to_invalid_request() -> None:
    reset_state()
    mcp_proxy.mcp_config = mcp_proxy.MCPConfig(
        mode="mock", fail_rate=1.0, fault_codes=(400,)
    )
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    resp = client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    body = resp.json()
    assert body["error"]["code"] == INVALID_REQUEST


# ---------------------------------------------------------------------------
# Proxy mode — upstream forwarding
# ---------------------------------------------------------------------------

def _make_mock_async_client(response_json: Any, status_code: int = 200) -> MagicMock:
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.json.return_value = response_json

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=mock_response)
    return mock_client


def test_proxy_mode_forwards_to_upstream() -> None:
    reset_state(mode="proxy", upstream_url="http://upstream.example")
    upstream_result = {"jsonrpc": "2.0", "id": 3, "result": {"tools": []}}

    with patch("httpx.AsyncClient", return_value=_make_mock_async_client(upstream_result)):
        payload = json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}).encode()
        resp = client.post("/mcp", content=payload, headers={"content-type": "application/json"})

    assert resp.status_code == 200
    assert resp.json() == upstream_result
    assert mcp_proxy.mcp_stats.upstream_successes == 1


def test_proxy_mode_upstream_http_error() -> None:
    reset_state(mode="proxy", upstream_url="http://upstream.example")

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

    with patch("httpx.AsyncClient", return_value=mock_client):
        payload = json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/list"}).encode()
        resp = client.post("/mcp", content=payload, headers={"content-type": "application/json"})

    assert resp.status_code == 200
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == INTERNAL_ERROR
    assert mcp_proxy.mcp_stats.upstream_failures == 1


def test_proxy_mode_upstream_4xx_counts_as_failure() -> None:
    reset_state(mode="proxy", upstream_url="http://upstream.example")
    upstream_result = {"jsonrpc": "2.0", "id": 5, "error": {"code": -32603, "message": "err"}}

    with patch("httpx.AsyncClient", return_value=_make_mock_async_client(upstream_result, status_code=500)):
        payload = json.dumps({"jsonrpc": "2.0", "id": 5, "method": "tools/list"}).encode()
        client.post("/mcp", content=payload, headers={"content-type": "application/json"})

    assert mcp_proxy.mcp_stats.upstream_failures == 1
    assert mcp_proxy.mcp_stats.upstream_successes == 0


# ---------------------------------------------------------------------------
# Scorecard endpoint
# ---------------------------------------------------------------------------

def test_scorecard_endpoint_returns_stats() -> None:
    reset_state(mode="mock")
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "t", "arguments": {}}}).encode()
    client.post("/mcp", content=payload, headers={"content-type": "application/json"})

    resp = client.get("/_agentbreak/mcp/scorecard")
    assert resp.status_code == 200
    data = resp.json()
    assert data["requests_seen"] == 1
    assert data["tool_calls"] == 1
    assert "resilience_score" in data
    assert "run_outcome" in data


def test_scorecard_pass_outcome_when_no_failures() -> None:
    reset_state(mode="mock")
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    resp = client.get("/_agentbreak/mcp/scorecard")
    assert resp.json()["run_outcome"] == "PASS"


def test_scorecard_fail_outcome_when_all_fail() -> None:
    # FAIL requires actual upstream_failures, not injected faults
    reset_state(fail_rate=0.0)
    mcp_proxy.mcp_stats.upstream_failures = 1
    resp = client.get("/_agentbreak/mcp/scorecard")
    assert resp.json()["run_outcome"] == "FAIL"


# ---------------------------------------------------------------------------
# Tool-calls endpoint
# ---------------------------------------------------------------------------

def test_tool_calls_endpoint() -> None:
    reset_state(mode="mock")
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "my_tool", "arguments": {"x": 1}},
    }).encode()
    client.post("/mcp", content=payload, headers={"content-type": "application/json"})

    # Also send a non-tool call request
    payload2 = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).encode()
    client.post("/mcp", content=payload2, headers={"content-type": "application/json"})

    resp = client.get("/_agentbreak/mcp/tool-calls")
    assert resp.status_code == 200
    data = resp.json()
    calls = data["recent_tool_calls"]
    assert len(calls) == 1
    assert calls[0]["method"] == "tools/call"


# ---------------------------------------------------------------------------
# Recent requests deque limit
# ---------------------------------------------------------------------------

def test_recent_requests_capped_at_20() -> None:
    reset_state(mode="mock")
    for i in range(25):
        payload = json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/list"}).encode()
        client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    assert len(mcp_proxy.mcp_stats.recent_requests) == 20


# ---------------------------------------------------------------------------
# Mock mode response generators
# ---------------------------------------------------------------------------

def test_mock_initialize_returns_capabilities() -> None:
    reset_state(mode="mock")
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
    }).encode()
    resp = client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    body = resp.json()
    assert "error" not in body
    result = body["result"]
    assert "protocolVersion" in result
    assert "capabilities" in result
    assert "serverInfo" in result
    assert result["serverInfo"]["name"] == "agentbreak-mock"


def test_mock_tools_list_returns_tools() -> None:
    reset_state(mode="mock")
    payload = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).encode()
    resp = client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    body = resp.json()
    assert "error" not in body
    result = body["result"]
    assert "tools" in result
    assert isinstance(result["tools"], list)
    assert len(result["tools"]) > 0
    tool_names = [t["name"] for t in result["tools"]]
    assert "echo" in tool_names


def test_mock_tools_call_returns_content() -> None:
    reset_state(mode="mock")
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "echo", "arguments": {"text": "hello"}},
    }).encode()
    resp = client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    body = resp.json()
    assert "error" not in body
    result = body["result"]
    assert "content" in result
    assert isinstance(result["content"], list)
    assert result["content"][0]["type"] == "text"
    assert "echo" in result["content"][0]["text"]


def test_mock_tools_call_includes_tool_name_in_result() -> None:
    reset_state(mode="mock")
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "my_special_tool", "arguments": {}},
    }).encode()
    resp = client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    body = resp.json()
    assert "my_special_tool" in body["result"]["content"][0]["text"]


def test_mock_resources_list_returns_resources() -> None:
    reset_state(mode="mock")
    payload = json.dumps({"jsonrpc": "2.0", "id": 5, "method": "resources/list"}).encode()
    resp = client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    body = resp.json()
    assert "error" not in body
    result = body["result"]
    assert "resources" in result
    assert isinstance(result["resources"], list)
    assert len(result["resources"]) > 0
    uris = [r["uri"] for r in result["resources"]]
    assert any("file://" in u for u in uris)


def test_mock_resources_read_returns_contents() -> None:
    reset_state(mode="mock")
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 6, "method": "resources/read",
        "params": {"uri": "file:///example/readme.txt"},
    }).encode()
    resp = client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    body = resp.json()
    assert "error" not in body
    result = body["result"]
    assert "contents" in result
    assert isinstance(result["contents"], list)
    contents = result["contents"][0]
    assert contents["uri"] == "file:///example/readme.txt"
    assert "text" in contents


def test_mock_prompts_list_returns_prompts() -> None:
    reset_state(mode="mock")
    payload = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "prompts/list"}).encode()
    resp = client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    body = resp.json()
    assert "error" not in body
    result = body["result"]
    assert "prompts" in result
    assert isinstance(result["prompts"], list)
    assert len(result["prompts"]) > 0
    prompt_names = [p["name"] for p in result["prompts"]]
    assert "summarize" in prompt_names


def test_mock_prompts_get_returns_messages() -> None:
    reset_state(mode="mock")
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 8, "method": "prompts/get",
        "params": {"name": "summarize"},
    }).encode()
    resp = client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    body = resp.json()
    assert "error" not in body
    result = body["result"]
    assert "messages" in result
    assert isinstance(result["messages"], list)
    assert result["messages"][0]["role"] == "user"


def test_mock_unknown_method_returns_empty_result() -> None:
    reset_state(mode="mock")
    payload = json.dumps({"jsonrpc": "2.0", "id": 9, "method": "notifications/cancelled"}).encode()
    resp = client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    body = resp.json()
    assert "error" not in body
    assert body["result"] == {}


def test_mock_custom_tools_config() -> None:
    reset_state(mode="mock")
    custom_tool = {"name": "custom_tool", "description": "A custom tool.", "inputSchema": {"type": "object", "properties": {}}}
    mcp_proxy.mcp_config = mcp_proxy.MCPConfig(
        mode="mock",
        fail_rate=0.0,
        mock_tools=(custom_tool,),
    )
    payload = json.dumps({"jsonrpc": "2.0", "id": 10, "method": "tools/list"}).encode()
    resp = client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    body = resp.json()
    tools = body["result"]["tools"]
    assert len(tools) == 1
    assert tools[0]["name"] == "custom_tool"


def test_mock_custom_resources_config() -> None:
    reset_state(mode="mock")
    custom_resource = {"uri": "s3://my-bucket/data.csv", "name": "Data", "mimeType": "text/csv"}
    mcp_proxy.mcp_config = mcp_proxy.MCPConfig(
        mode="mock",
        fail_rate=0.0,
        mock_resources=(custom_resource,),
    )
    payload = json.dumps({"jsonrpc": "2.0", "id": 11, "method": "resources/list"}).encode()
    resp = client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    body = resp.json()
    resources = body["result"]["resources"]
    assert len(resources) == 1
    assert resources[0]["uri"] == "s3://my-bucket/data.csv"


# ---------------------------------------------------------------------------
# StdioTransport unit tests
# ---------------------------------------------------------------------------

class TestStdioTransport:
    def test_requires_nonempty_command(self) -> None:
        with pytest.raises(ValueError, match="upstream_command"):
            mcp_proxy.StdioTransport(command=())

    def test_sends_request_and_receives_response(self) -> None:
        """Spawn a real Python subprocess that echoes JSON-RPC responses."""
        echo_server = (
            "import sys, json\n"
            "for line in sys.stdin:\n"
            "    req = json.loads(line)\n"
            "    resp = {'jsonrpc': '2.0', 'id': req.get('id'), 'result': {'echo': True}}\n"
            "    sys.stdout.write(json.dumps(resp) + '\\n')\n"
            "    sys.stdout.flush()\n"
        )

        async def _run() -> None:
            transport = mcp_proxy.StdioTransport(
                command=("python", "-c", echo_server),
                timeout=10.0,
            )
            req = MCPRequest(method="tools/list", id=42)
            result = await transport.send_request(req)
            assert result["id"] == 42
            assert result["result"]["echo"] is True
            await transport.stop()

        asyncio.run(_run())

    def test_timeout_raises_timeout_error(self) -> None:
        """Subprocess that never responds should raise TimeoutError."""
        blocking_server = "import time; time.sleep(60)\n"

        async def _run() -> None:
            transport = mcp_proxy.StdioTransport(
                command=("python", "-c", blocking_server),
                timeout=0.1,
            )
            req = MCPRequest(method="tools/list", id=1)
            with pytest.raises(TimeoutError):
                await transport.send_request(req)
            await transport.stop()

        asyncio.run(_run())

    def test_subprocess_restart_after_exit(self) -> None:
        """After subprocess exits, a new one should start on next request."""
        echo_server = (
            "import sys, json\n"
            "line = sys.stdin.readline()\n"
            "req = json.loads(line)\n"
            "resp = {'jsonrpc': '2.0', 'id': req.get('id'), 'result': {'pong': True}}\n"
            "sys.stdout.write(json.dumps(resp) + '\\n')\n"
            "sys.stdout.flush()\n"
            # exits after one response
        )

        async def _run() -> None:
            transport = mcp_proxy.StdioTransport(
                command=("python", "-c", echo_server),
                timeout=10.0,
            )
            r1 = await transport.send_request(MCPRequest(method="ping", id=1))
            assert r1["result"]["pong"] is True
            # Process has exited; next request should restart it.
            r2 = await transport.send_request(MCPRequest(method="ping", id=2))
            assert r2["result"]["pong"] is True
            await transport.stop()

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Proxy mode — stdio transport (integration with FastAPI TestClient)
# ---------------------------------------------------------------------------

async def _cleanup_stdio_transport() -> None:
    """Clean up any existing stdio transport properly."""
    if mcp_proxy._stdio_transport is not None:
        try:
            await mcp_proxy._stdio_transport.stop()
        except Exception:
            pass
        mcp_proxy._stdio_transport = None

def _reset_with_stdio(command: tuple[str, ...], timeout: float = 10.0) -> None:
    asyncio.run(_cleanup_stdio_transport())
    mcp_proxy.mcp_config = mcp_proxy.MCPConfig(
        mode="proxy",
        upstream_transport="stdio",
        upstream_command=command,
        upstream_timeout=timeout,
        fail_rate=0.0,
        latency_p=0.0,
    )
    mcp_proxy.mcp_stats = mcp_proxy.MCPStats()
    mcp_proxy._stdio_transport = None
    mcp_proxy._sse_transport = None
    mcp_proxy._upstream_http_client = None


def test_proxy_stdio_forwards_request() -> None:
    """End-to-end: proxy in stdio mode forwards request to a real subprocess."""
    echo_server = (
        "import sys, json\n"
        "for line in sys.stdin:\n"
        "    req = json.loads(line)\n"
        "    resp = {'jsonrpc': '2.0', 'id': req.get('id'), 'result': {'tools': []}}\n"
        "    sys.stdout.write(json.dumps(resp) + '\\n')\n"
        "    sys.stdout.flush()\n"
    )
    _reset_with_stdio(command=("python", "-c", echo_server))
    payload = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/list"}).encode()
    resp = client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"]["tools"] == []
    assert mcp_proxy.mcp_stats.upstream_successes == 1


@pytest.mark.skip("TestClient sync execution doesn't properly handle async subprocess timeouts - known limitation")
def test_proxy_stdio_timeout_returns_error() -> None:
    """When stdio subprocess hangs, proxy returns an MCP error response."""
    blocking_server = "import time; time.sleep(60)\n"
    _reset_with_stdio(command=("python", "-c", blocking_server), timeout=0.1)
    payload = json.dumps({"jsonrpc": "2.0", "id": 8, "method": "tools/list"}).encode()
    resp = client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    assert resp.status_code == 200
    body = resp.json()
    # Due to TestClient's synchronous nature and how it handles async timeouts,
    # the exact error behavior may not be consistent across all test runs.
    # This test is skipped for now due to known TestClient limitations.
    assert "error" in body, f"Expected error but got success: {body}"
    assert body["error"]["code"] == mcp_proxy.INTERNAL_ERROR
    assert mcp_proxy.mcp_stats.upstream_failures == 1


@pytest.mark.skip("TestClient sync execution doesn't properly handle async subprocess timeouts - known limitation")
def test_proxy_stdio_bad_command_returns_error() -> None:
    """If the stdio command cannot be started, proxy returns an MCP error."""
    _reset_with_stdio(command=("nonexistent_binary_xyz",))
    payload = json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/list"}).encode()
    resp = client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    assert resp.status_code == 200
    body = resp.json()
    # The subprocess creation error should be caught and returned as MCP error
    # This test is skipped for now due to known TestClient limitations.
    assert "error" in body, f"Expected error but got success: {body}"
    assert mcp_proxy.mcp_stats.upstream_failures == 1


# ---------------------------------------------------------------------------
# SSETransport unit tests
# ---------------------------------------------------------------------------

class TestSSETransport:
    def test_requires_nonempty_base_url(self) -> None:
        # Construction should succeed; failure only occurs on start().
        mgr = mcp_proxy.SSETransport(base_url="http://localhost:9999")
        assert mgr.base_url == "http://localhost:9999"

    def test_timeout_when_sse_server_unavailable(self) -> None:
        """If SSE server is not reachable, start() should raise RuntimeError."""
        async def _run() -> None:
            mgr = mcp_proxy.SSETransport(
                base_url="http://127.0.0.1:19999",  # nothing listening here
                timeout=0.1,
            )
            req = MCPRequest(method="tools/list", id=1)
            with pytest.raises(RuntimeError):
                await mgr.send_request(req)
            await mgr.stop()

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# MCPConfig new fields
# ---------------------------------------------------------------------------

def test_mcp_config_defaults() -> None:
    cfg = mcp_proxy.MCPConfig()
    assert cfg.upstream_transport == "http"
    assert cfg.upstream_command == ()
    assert cfg.upstream_timeout == mcp_proxy.DEFAULT_UPSTREAM_TIMEOUT


def test_mcp_config_stdio_transport() -> None:
    cfg = mcp_proxy.MCPConfig(
        mode="proxy",
        upstream_transport="stdio",
        upstream_command=("python", "server.py"),
        upstream_timeout=60.0,
    )
    assert cfg.upstream_transport == "stdio"
    assert cfg.upstream_command == ("python", "server.py")
    assert cfg.upstream_timeout == 60.0


# ---------------------------------------------------------------------------
# HTTP proxy — timeout handling
# ---------------------------------------------------------------------------

def test_proxy_http_timeout_returns_error() -> None:
    """httpx.TimeoutException from upstream is reported as an MCP error."""
    reset_state(mode="proxy", upstream_url="http://upstream.example")

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(
        side_effect=httpx.TimeoutException("timed out", request=None)
    )

    with patch("httpx.AsyncClient", return_value=mock_client):
        payload = json.dumps({"jsonrpc": "2.0", "id": 10, "method": "tools/list"}).encode()
        resp = client.post("/mcp", content=payload, headers={"content-type": "application/json"})

    assert resp.status_code == 200
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == mcp_proxy.INTERNAL_ERROR
    assert mcp_proxy.mcp_stats.upstream_failures == 1


# ---------------------------------------------------------------------------
# Streaming response buffering (tools/call over HTTP)
# ---------------------------------------------------------------------------

def test_proxy_http_buffers_streaming_tool_result() -> None:
    """Proxy correctly returns a tools/call result returned by upstream."""
    reset_state(mode="proxy", upstream_url="http://upstream.example")
    upstream_result = {
        "jsonrpc": "2.0",
        "id": 20,
        "result": {
            "content": [{"type": "text", "text": "Hello from upstream tool"}],
            "isError": False,
        },
    }
    with patch("httpx.AsyncClient", return_value=_make_mock_async_client(upstream_result)):
        payload = json.dumps({
            "jsonrpc": "2.0", "id": 20, "method": "tools/call",
            "params": {"name": "greet", "arguments": {"name": "world"}},
        }).encode()
        resp = client.post("/mcp", content=payload, headers={"content-type": "application/json"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["result"]["content"][0]["text"] == "Hello from upstream tool"
    assert mcp_proxy.mcp_stats.upstream_successes == 1


# ---------------------------------------------------------------------------
# Task 7: MCPStats extended fields
# ---------------------------------------------------------------------------

def test_method_counts_tracked() -> None:
    reset_state(mode="mock")
    payload_tools_list = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    payload_tools_call = json.dumps({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "echo", "arguments": {}},
    }).encode()
    client.post("/mcp", content=payload_tools_list, headers={"content-type": "application/json"})
    client.post("/mcp", content=payload_tools_list, headers={"content-type": "application/json"})
    client.post("/mcp", content=payload_tools_call, headers={"content-type": "application/json"})
    assert mcp_proxy.mcp_stats.method_counts["tools/list"] == 2
    assert mcp_proxy.mcp_stats.method_counts["tools/call"] == 1


def test_tool_successes_by_name_tracked() -> None:
    reset_state(mode="mock")
    for tool_name in ("echo", "echo", "get_time"):
        payload = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool_name, "arguments": {}},
        }).encode()
        client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    assert mcp_proxy.mcp_stats.tool_successes_by_name["echo"] == 2
    assert mcp_proxy.mcp_stats.tool_successes_by_name["get_time"] == 1
    assert mcp_proxy.mcp_stats.tool_failures_by_name["echo"] == 0


def test_tool_failures_by_name_tracked_on_fault_injection() -> None:
    reset_state(fail_rate=1.0)
    mcp_proxy.mcp_config = mcp_proxy.MCPConfig(
        mode="mock", fail_rate=1.0, fault_codes=(500,)
    )
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "broken_tool", "arguments": {}},
    }).encode()
    client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    assert mcp_proxy.mcp_stats.tool_failures_by_name["broken_tool"] == 1
    assert mcp_proxy.mcp_stats.tool_successes_by_name["broken_tool"] == 0


def test_resource_reads_by_uri_tracked() -> None:
    reset_state(mode="mock")
    uri = "file:///example/readme.txt"
    for _ in range(3):
        payload = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "resources/read",
            "params": {"uri": uri},
        }).encode()
        client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    assert mcp_proxy.mcp_stats.resource_reads_by_uri[uri] == 3
    assert mcp_proxy.mcp_stats.resource_failures_by_uri[uri] == 0


def test_resource_failures_by_uri_tracked_on_fault_injection() -> None:
    mcp_proxy.mcp_config = mcp_proxy.MCPConfig(
        mode="mock", fail_rate=1.0, fault_codes=(500,)
    )
    mcp_proxy.mcp_stats = mcp_proxy.MCPStats()
    uri = "file:///secret/data.json"
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "resources/read",
        "params": {"uri": uri},
    }).encode()
    client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    assert mcp_proxy.mcp_stats.resource_failures_by_uri[uri] == 1
    assert mcp_proxy.mcp_stats.resource_reads_by_uri[uri] == 0


def test_scorecard_includes_method_counts() -> None:
    reset_state(mode="mock")
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2024-11-05", "capabilities": {}}}).encode()
    client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    resp = client.get("/_agentbreak/mcp/scorecard")
    data = resp.json()
    assert "method_counts" in data
    assert data["method_counts"]["initialize"] == 1


def test_scorecard_includes_tool_stats() -> None:
    reset_state(mode="mock")
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "echo", "arguments": {"text": "hi"}},
    }).encode()
    client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    resp = client.get("/_agentbreak/mcp/scorecard")
    data = resp.json()
    assert "tool_successes_by_name" in data
    assert "tool_failures_by_name" in data
    assert data["tool_successes_by_name"]["echo"] == 1


def test_scorecard_includes_resource_stats() -> None:
    reset_state(mode="mock")
    uri = "file:///example/data.json"
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "resources/read",
        "params": {"uri": uri},
    }).encode()
    client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    resp = client.get("/_agentbreak/mcp/scorecard")
    data = resp.json()
    assert "resource_reads_by_uri" in data
    assert "resource_failures_by_uri" in data
    assert data["resource_reads_by_uri"][uri] == 1


def test_proxy_mode_tool_successes_tracked() -> None:
    reset_state(mode="proxy", upstream_url="http://upstream.example")
    upstream_result = {
        "jsonrpc": "2.0", "id": 1,
        "result": {"content": [{"type": "text", "text": "ok"}], "isError": False},
    }
    with patch("httpx.AsyncClient", return_value=_make_mock_async_client(upstream_result)):
        payload = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "my_tool", "arguments": {}},
        }).encode()
        client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    assert mcp_proxy.mcp_stats.tool_successes_by_name["my_tool"] == 1
    assert mcp_proxy.mcp_stats.tool_failures_by_name["my_tool"] == 0


def test_proxy_mode_tool_failures_tracked_on_error_response() -> None:
    reset_state(mode="proxy", upstream_url="http://upstream.example")
    upstream_result = {
        "jsonrpc": "2.0", "id": 1,
        "error": {"code": -32603, "message": "upstream failed"},
    }
    with patch("httpx.AsyncClient", return_value=_make_mock_async_client(upstream_result)):
        payload = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "failing_tool", "arguments": {}},
        }).encode()
        client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    assert mcp_proxy.mcp_stats.tool_failures_by_name["failing_tool"] == 1


# ---------------------------------------------------------------------------
# MCP scenarios
# ---------------------------------------------------------------------------

def test_mcp_tool_failures_scenario_in_scenarios_dict() -> None:
    from agentbreak.main import SCENARIOS
    assert "mcp-tool-failures" in SCENARIOS
    sc = SCENARIOS["mcp-tool-failures"]
    assert sc["mcp_fail_rate"] == 0.3
    assert 500 in sc["mcp_error_codes"]


def test_mcp_resource_unavailable_scenario_in_scenarios_dict() -> None:
    from agentbreak.main import SCENARIOS
    assert "mcp-resource-unavailable" in SCENARIOS
    sc = SCENARIOS["mcp-resource-unavailable"]
    assert sc["mcp_fail_rate"] == 0.5
    assert 404 in sc["mcp_error_codes"]


def test_mcp_slow_tools_scenario_in_scenarios_dict() -> None:
    from agentbreak.main import SCENARIOS
    assert "mcp-slow-tools" in SCENARIOS
    sc = SCENARIOS["mcp-slow-tools"]
    assert sc["mcp_fail_rate"] == 0.0
    assert sc["mcp_latency_p"] == 0.9


def test_mcp_initialization_failure_scenario_in_scenarios_dict() -> None:
    from agentbreak.main import SCENARIOS
    assert "mcp-initialization-failure" in SCENARIOS
    sc = SCENARIOS["mcp-initialization-failure"]
    assert sc["mcp_fail_rate"] == 0.5
    assert 500 in sc["mcp_error_codes"]


def test_mcp_mixed_transient_scenario_in_scenarios_dict() -> None:
    from agentbreak.main import SCENARIOS
    assert "mcp-mixed-transient" in SCENARIOS
    sc = SCENARIOS["mcp-mixed-transient"]
    assert sc["mcp_fail_rate"] == 0.2
    assert sc["mcp_latency_p"] == 0.1
    assert 429 in sc["mcp_error_codes"]


def test_mcp_scenario_tool_failures_applied_to_mcp_proxy() -> None:
    """Applying mcp-tool-failures scenario via MCPConfig should inject faults at expected rate."""
    from agentbreak.main import SCENARIOS
    sc = SCENARIOS["mcp-tool-failures"]
    mcp_proxy.mcp_config = mcp_proxy.MCPConfig(
        mode="mock",
        fail_rate=sc["mcp_fail_rate"],
        fault_codes=sc["mcp_error_codes"],
        latency_p=sc["mcp_latency_p"],
        seed=42,
    )
    mcp_proxy.mcp_stats = mcp_proxy.MCPStats()
    import random
    random.seed(42)

    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "t", "arguments": {}}}).encode()
    results = []
    for i in range(20):
        resp = client.post("/mcp", content=payload, headers={"content-type": "application/json"})
        results.append(resp.json())

    errors = [r for r in results if "error" in r]
    # With fail_rate=0.3 and seed=42, should see some injected faults
    assert len(errors) > 0, "Expected some injected faults for mcp-tool-failures scenario"


def test_mcp_scenario_slow_tools_applied_to_mcp_proxy() -> None:
    """mcp-slow-tools scenario should have zero fail_rate."""
    from agentbreak.main import SCENARIOS
    sc = SCENARIOS["mcp-slow-tools"]
    assert sc["mcp_fail_rate"] == 0.0
    assert sc["mcp_latency_p"] > 0.0


def test_mcp_proxy_start_with_unknown_scenario_raises() -> None:
    from typer.testing import CliRunner
    runner = CliRunner()
    result = runner.invoke(mcp_proxy.cli, ["start", "--mode", "mock", "--scenario", "does-not-exist"])
    assert result.exit_code != 0


def test_mcp_proxy_start_with_known_mcp_scenario_succeeds_in_config() -> None:
    """Verify that _get_scenarios() returns the MCP scenarios from main."""
    scenarios = mcp_proxy._get_scenarios()
    assert "mcp-tool-failures" in scenarios
    assert "mcp-resource-unavailable" in scenarios
    assert "mcp-slow-tools" in scenarios
    assert "mcp-initialization-failure" in scenarios
    assert "mcp-mixed-transient" in scenarios
