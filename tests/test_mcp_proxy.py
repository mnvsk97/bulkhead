from __future__ import annotations

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
    MCPResponse,
)


def reset_state(
    mode: str = "mock",
    upstream_url: str = "http://upstream.example",
    fail_rate: float = 0.0,
    latency_p: float = 0.0,
) -> None:
    mcp_proxy.mcp_config = mcp_proxy.MCPConfig(
        mode=mode,
        upstream_url=upstream_url,
        fail_rate=fail_rate,
        latency_p=latency_p,
    )
    mcp_proxy.mcp_stats = mcp_proxy.MCPStats()


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


def test_missing_method_returns_parse_error() -> None:
    reset_state()
    payload = json.dumps({"jsonrpc": "2.0", "id": 1}).encode()
    resp = client.post("/mcp", content=payload, headers={"content-type": "application/json"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["error"]["code"] == -32700


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
    assert mcp_proxy.mcp_stats.suspected_loops >= 1


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
    assert mcp_proxy.mcp_stats.upstream_failures == 1


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
    reset_state(fail_rate=1.0)
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    client.post("/mcp", content=payload, headers={"content-type": "application/json"})
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
