"""Integration tests for Phase 3 service implementations."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agentbreak.config.models import (
    FaultConfig,
    LatencyConfig,
    MCPServiceConfig,
    OpenAIServiceConfig,
    ServiceType,
)
from agentbreak.core.statistics import StatisticsTracker
from agentbreak.protocols.mcp import MCPError, MCPRequest, MCPResponse
from agentbreak.services.mcp import MCPProxy, MCPService
from agentbreak.services.openai import OpenAIProxy, OpenAIService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_openai_config(**kwargs) -> OpenAIServiceConfig:
    defaults = dict(
        name="test-openai",
        port=5000,
        mode="mock",
        fault=FaultConfig(enabled=False),
        latency=LatencyConfig(enabled=False),
    )
    defaults.update(kwargs)
    return OpenAIServiceConfig(**defaults)


def _make_mcp_config(**kwargs) -> MCPServiceConfig:
    defaults = dict(
        name="test-mcp",
        port=5001,
        mode="mock",
        fault=FaultConfig(enabled=False),
        latency=LatencyConfig(enabled=False),
    )
    defaults.update(kwargs)
    return MCPServiceConfig(**defaults)


def _mcp_body(method: str, req_id: int = 1, params: dict | None = None) -> bytes:
    payload: dict = {"jsonrpc": "2.0", "method": method, "id": req_id}
    if params is not None:
        payload["params"] = params
    return json.dumps(payload).encode()


# ---------------------------------------------------------------------------
# protocols/mcp import and basic functionality
# ---------------------------------------------------------------------------


def test_protocols_mcp_import() -> None:
    from agentbreak.protocols.mcp import (
        INTERNAL_ERROR,
        INVALID_REQUEST,
        JSONRPC_VERSION,
        PARSE_ERROR,
        MCPError,
        MCPRequest,
        MCPResponse,
        fingerprint_mcp_request,
    )
    assert JSONRPC_VERSION == "2.0"
    assert PARSE_ERROR == -32700
    assert INVALID_REQUEST == -32600
    assert INTERNAL_ERROR == -32603


def test_protocols_package_import() -> None:
    from agentbreak.protocols import MCPRequest, MCPResponse, fingerprint_mcp_request

    req = MCPRequest(method="tools/list", id=1)
    fp = fingerprint_mcp_request(req)
    assert isinstance(fp, str) and len(fp) == 64


def test_mcp_protocol_shim_backward_compat() -> None:
    # mcp_protocol.py now re-exports from protocols/mcp
    from agentbreak.mcp_protocol import MCPRequest as Req
    from agentbreak.protocols.mcp import MCPRequest as CanonicalReq

    assert Req is CanonicalReq


def test_mcp_response_with_mcp_error() -> None:
    resp = MCPResponse(id=1, error=MCPError(code=-32600, message="bad request"))
    d = resp.to_dict()
    assert d["error"]["code"] == -32600
    assert d["error"]["message"] == "bad request"
    assert "result" not in d


# ---------------------------------------------------------------------------
# OpenAI service: mock mode
# ---------------------------------------------------------------------------


def test_openai_service_mock_response() -> None:
    config = _make_openai_config()
    stats = StatisticsTracker()
    svc = OpenAIService(config, stats)
    svc.setup_routes()
    client = TestClient(svc.get_app())

    resp = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "chatcmpl-agentbreak-mock"
    assert data["choices"][0]["message"]["content"] == "AgentBreak mock response."


def test_openai_service_health_check() -> None:
    config = _make_openai_config()
    stats = StatisticsTracker()
    svc = OpenAIService(config, stats)
    svc.setup_routes()
    client = TestClient(svc.get_app())

    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["service"] == "test-openai"


def test_openai_service_scorecard() -> None:
    config = _make_openai_config()
    stats = StatisticsTracker()
    svc = OpenAIService(config, stats)
    svc.setup_routes()
    client = TestClient(svc.get_app())

    # Make a request first
    client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]},
    )

    resp = client.get("/_agentbreak/scorecard")
    assert resp.status_code == 200
    data = resp.json()
    assert data["requests_seen"] == 1
    assert data["upstream_successes"] == 1


def test_openai_service_recent_requests() -> None:
    config = _make_openai_config()
    stats = StatisticsTracker()
    svc = OpenAIService(config, stats)
    svc.setup_routes()
    client = TestClient(svc.get_app())

    client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]},
    )

    resp = client.get("/_agentbreak/requests")
    assert resp.status_code == 200
    assert len(resp.json()["recent_requests"]) == 1


def test_openai_service_fault_injection() -> None:
    config = _make_openai_config(
        fault=FaultConfig(
            enabled=True,
            overall_rate=1.0,
            per_error_rates={},
            available_codes=(429,),
        )
    )
    stats = StatisticsTracker()
    svc = OpenAIService(config, stats)
    svc.setup_routes()
    client = TestClient(svc.get_app())

    resp = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 429
    data = resp.json()
    assert "error" in data
    assert data["error"]["code"] == 429


def test_openai_service_stats_track_faults() -> None:
    config = _make_openai_config(
        fault=FaultConfig(enabled=True, overall_rate=1.0, per_error_rates={}, available_codes=(500,))
    )
    stats = StatisticsTracker()
    svc = OpenAIService(config, stats)
    svc.setup_routes()
    client = TestClient(svc.get_app())

    client.post("/v1/chat/completions", json={"model": "gpt-4", "messages": []})

    scorecard = client.get("/_agentbreak/scorecard").json()
    assert scorecard["injected_faults"] == 1
    assert scorecard["upstream_failures"] == 1


# ---------------------------------------------------------------------------
# MCP service: mock mode
# ---------------------------------------------------------------------------


def test_mcp_service_initialize() -> None:
    config = _make_mcp_config()
    stats = StatisticsTracker()
    svc = MCPService(config, stats)
    svc.setup_routes()
    client = TestClient(svc.get_app())

    resp = client.post("/mcp", content=_mcp_body("initialize", 1))
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == 1
    assert "result" in data
    assert data["result"]["protocolVersion"] == "2024-11-05"
    assert data["result"]["serverInfo"]["name"] == "agentbreak-mock"


def test_mcp_service_tools_list() -> None:
    config = _make_mcp_config(
        mock_tools=[{"name": "search", "description": "Search the web"}]
    )
    stats = StatisticsTracker()
    svc = MCPService(config, stats)
    svc.setup_routes()
    client = TestClient(svc.get_app())

    resp = client.post("/mcp", content=_mcp_body("tools/list", 2))
    assert resp.status_code == 200
    data = resp.json()
    assert data["result"]["tools"][0]["name"] == "search"


def test_mcp_service_tools_call() -> None:
    config = _make_mcp_config()
    stats = StatisticsTracker()
    svc = MCPService(config, stats)
    svc.setup_routes()
    client = TestClient(svc.get_app())

    resp = client.post(
        "/mcp",
        content=_mcp_body("tools/call", 3, {"name": "my_tool", "arguments": {}}),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "Mock result for tool: my_tool" in data["result"]["content"][0]["text"]


def test_mcp_service_resources_list() -> None:
    config = _make_mcp_config(
        mock_resources=[{"uri": "file://test.txt", "name": "test"}]
    )
    stats = StatisticsTracker()
    svc = MCPService(config, stats)
    svc.setup_routes()
    client = TestClient(svc.get_app())

    resp = client.post("/mcp", content=_mcp_body("resources/list", 4))
    assert resp.status_code == 200
    data = resp.json()
    assert data["result"]["resources"][0]["uri"] == "file://test.txt"


def test_mcp_service_resources_read() -> None:
    config = _make_mcp_config()
    stats = StatisticsTracker()
    svc = MCPService(config, stats)
    svc.setup_routes()
    client = TestClient(svc.get_app())

    resp = client.post(
        "/mcp",
        content=_mcp_body("resources/read", 5, {"uri": "file://example.txt"}),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["result"]["contents"][0]["uri"] == "file://example.txt"
    assert "Mock content for resource" in data["result"]["contents"][0]["text"]


def test_mcp_service_prompts_list() -> None:
    config = _make_mcp_config(
        mock_prompts=[{"name": "summarize", "description": "Summarize text"}]
    )
    stats = StatisticsTracker()
    svc = MCPService(config, stats)
    svc.setup_routes()
    client = TestClient(svc.get_app())

    resp = client.post("/mcp", content=_mcp_body("prompts/list", 6))
    assert resp.status_code == 200
    data = resp.json()
    assert data["result"]["prompts"][0]["name"] == "summarize"


def test_mcp_service_prompts_get() -> None:
    config = _make_mcp_config()
    stats = StatisticsTracker()
    svc = MCPService(config, stats)
    svc.setup_routes()
    client = TestClient(svc.get_app())

    resp = client.post(
        "/mcp",
        content=_mcp_body("prompts/get", 7, {"name": "my_prompt"}),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "Mock prompt for: my_prompt" in data["result"]["messages"][0]["content"]["text"]


def test_mcp_service_unknown_method_returns_empty() -> None:
    config = _make_mcp_config()
    stats = StatisticsTracker()
    svc = MCPService(config, stats)
    svc.setup_routes()
    client = TestClient(svc.get_app())

    resp = client.post("/mcp", content=_mcp_body("unknown/method", 8))
    assert resp.status_code == 200
    data = resp.json()
    assert data["result"] == {}


def test_mcp_service_health_check() -> None:
    config = _make_mcp_config()
    stats = StatisticsTracker()
    svc = MCPService(config, stats)
    svc.setup_routes()
    client = TestClient(svc.get_app())

    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["service"] == "test-mcp"


def test_mcp_service_fault_injection() -> None:
    config = _make_mcp_config(
        fault=FaultConfig(
            enabled=True,
            overall_rate=1.0,
            per_error_rates={},
            available_codes=(429,),
        )
    )
    stats = StatisticsTracker()
    svc = MCPService(config, stats)
    svc.setup_routes()
    client = TestClient(svc.get_app())

    resp = client.post("/mcp", content=_mcp_body("tools/call", 9, {"name": "foo"}))
    assert resp.status_code == 200  # MCP faults are 200 with error body
    data = resp.json()
    assert "error" in data
    assert data["error"]["code"] == -32000  # 429 maps to -32000


def test_mcp_service_fault_preserves_request_id() -> None:
    config = _make_mcp_config(
        fault=FaultConfig(
            enabled=True,
            overall_rate=1.0,
            per_error_rates={},
            available_codes=(500,),
        )
    )
    stats = StatisticsTracker()
    svc = MCPService(config, stats)
    svc.setup_routes()
    client = TestClient(svc.get_app())

    resp = client.post("/mcp", content=_mcp_body("initialize", req_id=42))
    data = resp.json()
    assert data["id"] == 42


def test_mcp_service_invalid_body_parse_error() -> None:
    config = _make_mcp_config()
    stats = StatisticsTracker()
    svc = MCPService(config, stats)
    svc.setup_routes()
    client = TestClient(svc.get_app())

    resp = client.post("/mcp", content=b"not valid json")
    assert resp.status_code == 200
    data = resp.json()
    # In mock mode, invalid JSON should return parse error
    assert "error" in data
    assert data["error"]["code"] == -32700


def test_mcp_service_scorecard() -> None:
    config = _make_mcp_config()
    stats = StatisticsTracker()
    svc = MCPService(config, stats)
    svc.setup_routes()
    client = TestClient(svc.get_app())

    client.post("/mcp", content=_mcp_body("initialize", 1))

    resp = client.get("/_agentbreak/scorecard")
    assert resp.status_code == 200
    data = resp.json()
    assert data["requests_seen"] == 1


# ---------------------------------------------------------------------------
# get_app / factory
# ---------------------------------------------------------------------------


def test_openai_service_get_app_returns_fastapi() -> None:
    from fastapi import FastAPI

    config = _make_openai_config()
    svc = OpenAIService(config, StatisticsTracker())
    assert isinstance(svc.get_app(), FastAPI)


def test_mcp_service_get_app_returns_fastapi() -> None:
    from fastapi import FastAPI

    config = _make_mcp_config()
    svc = MCPService(config, StatisticsTracker())
    assert isinstance(svc.get_app(), FastAPI)
