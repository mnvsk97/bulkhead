"""
Integration tests for MCP proxy end-to-end scenarios.

These tests exercise the full request/response lifecycle through the FastAPI app,
including mock mode, fault injection, scorecard reporting, duplicate/loop detection,
and built-in scenario configuration.  They use FastAPI's TestClient (which runs
synchronously) and reset global state between each test.
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentbreak import mcp_proxy
from agentbreak.mcp_proxy import MCPConfig, MCPStats


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

client = TestClient(mcp_proxy.app)

_MCP_HEADERS = {"content-type": "application/json"}


def _reset(
    mode: str = "mock",
    fail_rate: float = 0.0,
    latency_p: float = 0.0,
    fault_codes: tuple[int, ...] = (429, 500, 503),
    seed: int | None = 0,
) -> None:
    mcp_proxy.mcp_config = MCPConfig(
        mode=mode,
        fail_rate=fail_rate,
        latency_p=latency_p,
        fault_codes=fault_codes,
        seed=seed,
    )
    mcp_proxy.mcp_stats = MCPStats(
        method_counts=defaultdict(int),
        tool_successes_by_name=defaultdict(int),
        tool_failures_by_name=defaultdict(int),
        resource_reads_by_uri=defaultdict(int),
        resource_failures_by_uri=defaultdict(int),
        seen_fingerprints=defaultdict(int),
    )


def _post(method: str, params: dict[str, Any] | None = None, req_id: int = 1) -> Any:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        payload["params"] = params
    resp = client.post("/mcp", content=json.dumps(payload).encode(), headers=_MCP_HEADERS)
    assert resp.status_code == 200
    return resp.json()


# ---------------------------------------------------------------------------
# Scenario 1: Full MCP session in mock mode
# ---------------------------------------------------------------------------

class TestFullMockSession:
    """Simulate a typical MCP client session: init → list → call → read."""

    def setup_method(self) -> None:
        _reset(mode="mock", fail_rate=0.0)

    def test_initialize_returns_capabilities(self) -> None:
        body = _post("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
        assert "error" not in body
        result = body["result"]
        assert "protocolVersion" in result
        assert "capabilities" in result
        assert "serverInfo" in result

    def test_tools_list_returns_tools(self) -> None:
        body = _post("tools/list")
        assert "error" not in body
        tools = body["result"]["tools"]
        assert isinstance(tools, list)
        assert len(tools) >= 1
        tool_names = [t["name"] for t in tools]
        assert "echo" in tool_names

    def test_tools_call_returns_content(self) -> None:
        body = _post("tools/call", {"name": "echo", "arguments": {"text": "hello"}})
        assert "error" not in body
        result = body["result"]
        assert "content" in result
        assert isinstance(result["content"], list)

    def test_resources_list_returns_resources(self) -> None:
        body = _post("resources/list")
        assert "error" not in body
        resources = body["result"]["resources"]
        assert isinstance(resources, list)
        assert len(resources) >= 1
        assert "uri" in resources[0]

    def test_resources_read_returns_contents(self) -> None:
        body = _post("resources/read", {"uri": "file:///example/readme.txt"})
        assert "error" not in body
        result = body["result"]
        assert "contents" in result

    def test_prompts_list_returns_prompts(self) -> None:
        body = _post("prompts/list")
        assert "error" not in body
        prompts = body["result"]["prompts"]
        assert isinstance(prompts, list)
        assert len(prompts) >= 1

    def test_full_session_stats_accurate(self) -> None:
        # Simulate: init, list tools, call echo, read resource
        _post("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}}, req_id=1)
        _post("tools/list", req_id=2)
        _post("tools/call", {"name": "echo", "arguments": {"text": "hi"}}, req_id=3)
        _post("resources/read", {"uri": "file:///example/readme.txt"}, req_id=4)

        stats = mcp_proxy.mcp_stats
        assert stats.total_requests == 4
        assert stats.upstream_successes == 4
        assert stats.upstream_failures == 0
        assert stats.init_requests == 1
        assert stats.tool_calls == 1
        assert stats.resource_reads == 1
        assert stats.injected_faults == 0


# ---------------------------------------------------------------------------
# Scenario 2: Fault injection
# ---------------------------------------------------------------------------

class TestFaultInjection:
    """Verify fault injection fires correctly and updates stats."""

    def setup_method(self) -> None:
        _reset(mode="mock", fail_rate=1.0, fault_codes=(429,), seed=42)

    def test_100_percent_fail_rate_always_injects(self) -> None:
        body = _post("tools/call", {"name": "echo", "arguments": {"text": "x"}})
        assert "error" in body
        assert body["error"]["code"] == mcp_proxy.MCP_TOOL_ERROR  # 429 → -32000

    def test_fault_increments_injected_faults(self) -> None:
        _post("tools/call", {"name": "echo", "arguments": {}})
        assert mcp_proxy.mcp_stats.injected_faults == 1
        # Injected faults are not upstream failures — they are synthetic errors
        # that never reached the upstream. upstream_failures should remain 0.
        assert mcp_proxy.mcp_stats.upstream_failures == 0

    def test_fault_still_records_request(self) -> None:
        _post("tools/call", {"name": "echo", "arguments": {}})
        assert mcp_proxy.mcp_stats.total_requests == 1

    def test_fault_response_preserves_id(self) -> None:
        body = _post("tools/call", {"name": "echo", "arguments": {}}, req_id=77)
        assert body["id"] == 77

    def test_zero_fail_rate_never_injects(self) -> None:
        _reset(mode="mock", fail_rate=0.0)
        for i in range(10):
            body = _post("tools/call", {"name": "echo", "arguments": {}}, req_id=i)
            assert "error" not in body
        assert mcp_proxy.mcp_stats.injected_faults == 0

    def test_partial_fail_rate_injects_some(self) -> None:
        _reset(mode="mock", fail_rate=0.5, fault_codes=(500,), seed=1)
        results = []
        for i in range(20):
            body = _post("tools/list", req_id=i)
            results.append("error" in body)
        faults = sum(results)
        # With seed=1 and p=0.5 over 20 requests, expect some but not all to fail
        assert 0 < faults < 20


# ---------------------------------------------------------------------------
# Scenario 3: Scorecard endpoint
# ---------------------------------------------------------------------------

class TestScorecardEndpoint:
    """Verify the scorecard endpoint reflects accumulated stats."""

    def setup_method(self) -> None:
        _reset(mode="mock", fail_rate=0.0)

    def test_scorecard_returns_200(self) -> None:
        resp = client.get("/_agentbreak/mcp/scorecard")
        assert resp.status_code == 200

    def test_scorecard_initial_state(self) -> None:
        data = client.get("/_agentbreak/mcp/scorecard").json()
        assert data["requests_seen"] == 0
        assert data["injected_faults"] == 0
        assert data["upstream_successes"] == 0
        assert data["run_outcome"] == "PASS"
        assert data["resilience_score"] == 100

    def test_scorecard_reflects_tool_calls(self) -> None:
        _post("tools/call", {"name": "echo", "arguments": {}})
        _post("tools/call", {"name": "get_time", "arguments": {}})
        data = client.get("/_agentbreak/mcp/scorecard").json()
        assert data["tool_calls"] == 2
        assert data["requests_seen"] == 2
        assert data["upstream_successes"] == 2

    def test_scorecard_outcome_pass_when_no_failures(self) -> None:
        _post("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
        _post("tools/list")
        data = client.get("/_agentbreak/mcp/scorecard").json()
        assert data["run_outcome"] == "PASS"

    def test_scorecard_outcome_fail_when_all_fail(self) -> None:
        # FAIL requires actual upstream_failures (not injected faults, which are synthetic)
        _reset(mode="mock", fail_rate=0.0)
        mcp_proxy.mcp_stats.upstream_failures = 1
        data = client.get("/_agentbreak/mcp/scorecard").json()
        assert data["run_outcome"] == "FAIL"

    def test_scorecard_outcome_degraded_when_mixed(self) -> None:
        # Directly set the stats to simulate mixed success/failure
        _reset(mode="mock", fail_rate=0.0)
        mcp_proxy.mcp_stats.upstream_successes = 1
        mcp_proxy.mcp_stats.upstream_failures = 1
        data = client.get("/_agentbreak/mcp/scorecard").json()
        assert data["run_outcome"] == "DEGRADED"

    def test_scorecard_resilience_score_decreases_on_fault(self) -> None:
        _reset(mode="mock", fail_rate=1.0, fault_codes=(500,))
        _post("tools/call", {"name": "echo", "arguments": {}})
        data = client.get("/_agentbreak/mcp/scorecard").json()
        # 1 injected fault = -3 → score = 97
        assert data["resilience_score"] < 100
        assert data["resilience_score"] >= 0

    def test_scorecard_includes_method_counts(self) -> None:
        _post("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
        _post("tools/list")
        _post("tools/call", {"name": "echo", "arguments": {}})
        data = client.get("/_agentbreak/mcp/scorecard").json()
        assert data["method_counts"]["initialize"] == 1
        assert data["method_counts"]["tools/list"] == 1
        assert data["method_counts"]["tools/call"] == 1

    def test_scorecard_includes_per_tool_results(self) -> None:
        _post("tools/call", {"name": "echo", "arguments": {"text": "a"}})
        _post("tools/call", {"name": "get_time", "arguments": {}})
        data = client.get("/_agentbreak/mcp/scorecard").json()
        assert data["tool_successes_by_name"]["echo"] == 1
        assert data["tool_successes_by_name"]["get_time"] == 1

    def test_scorecard_includes_per_uri_results(self) -> None:
        _post("resources/read", {"uri": "file:///example/readme.txt"})
        data = client.get("/_agentbreak/mcp/scorecard").json()
        assert data["resource_reads_by_uri"]["file:///example/readme.txt"] == 1


# ---------------------------------------------------------------------------
# Scenario 4: Tool calls endpoint
# ---------------------------------------------------------------------------

class TestToolCallsEndpoint:
    """Verify the tool-calls endpoint returns recent tool call history."""

    def setup_method(self) -> None:
        _reset(mode="mock", fail_rate=0.0)

    def test_tool_calls_endpoint_returns_200(self) -> None:
        resp = client.get("/_agentbreak/mcp/tool-calls")
        assert resp.status_code == 200

    def test_tool_calls_initially_empty(self) -> None:
        data = client.get("/_agentbreak/mcp/tool-calls").json()
        assert data["recent_tool_calls"] == []

    def test_tool_calls_records_tool_call(self) -> None:
        _post("tools/call", {"name": "echo", "arguments": {"text": "hi"}})
        data = client.get("/_agentbreak/mcp/tool-calls").json()
        assert len(data["recent_tool_calls"]) == 1
        assert data["recent_tool_calls"][0]["method"] == "tools/call"

    def test_tool_calls_does_not_include_other_methods(self) -> None:
        _post("tools/list")
        _post("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
        data = client.get("/_agentbreak/mcp/tool-calls").json()
        assert data["recent_tool_calls"] == []

    def test_tool_calls_includes_fingerprint(self) -> None:
        _post("tools/call", {"name": "echo", "arguments": {"text": "hello"}})
        data = client.get("/_agentbreak/mcp/tool-calls").json()
        record = data["recent_tool_calls"][0]
        assert "fingerprint" in record
        assert len(record["fingerprint"]) == 64  # SHA-256 hex


# ---------------------------------------------------------------------------
# Scenario 5: Duplicate and loop detection
# ---------------------------------------------------------------------------

class TestDuplicateAndLoopDetection:
    """End-to-end verification of duplicate request and loop tracking."""

    def setup_method(self) -> None:
        _reset(mode="mock", fail_rate=0.0)

    def test_single_request_no_duplicate(self) -> None:
        _post("tools/call", {"name": "echo", "arguments": {"x": 1}}, req_id=1)
        assert mcp_proxy.mcp_stats.duplicate_requests == 0
        assert mcp_proxy.mcp_stats.suspected_loops == 0

    def test_two_identical_requests_detect_duplicate(self) -> None:
        _post("tools/call", {"name": "echo", "arguments": {"x": 1}}, req_id=1)
        _post("tools/call", {"name": "echo", "arguments": {"x": 1}}, req_id=2)
        assert mcp_proxy.mcp_stats.duplicate_requests == 1
        assert mcp_proxy.mcp_stats.suspected_loops == 0

    def test_three_identical_requests_detect_loop(self) -> None:
        _post("tools/call", {"name": "echo", "arguments": {"x": 1}}, req_id=1)
        _post("tools/call", {"name": "echo", "arguments": {"x": 1}}, req_id=2)
        _post("tools/call", {"name": "echo", "arguments": {"x": 1}}, req_id=3)
        assert mcp_proxy.mcp_stats.duplicate_requests == 2
        assert mcp_proxy.mcp_stats.suspected_loops == 1

    def test_different_tool_names_no_collision(self) -> None:
        _post("tools/call", {"name": "echo", "arguments": {}}, req_id=1)
        _post("tools/call", {"name": "get_time", "arguments": {}}, req_id=2)
        assert mcp_proxy.mcp_stats.duplicate_requests == 0

    def test_different_arguments_no_collision(self) -> None:
        _post("tools/call", {"name": "echo", "arguments": {"text": "a"}}, req_id=1)
        _post("tools/call", {"name": "echo", "arguments": {"text": "b"}}, req_id=2)
        assert mcp_proxy.mcp_stats.duplicate_requests == 0

    def test_same_fingerprint_different_request_ids(self) -> None:
        # Request ID should be excluded from fingerprint
        args = {"name": "echo", "arguments": {"text": "same"}}
        _post("tools/call", args, req_id=10)
        _post("tools/call", args, req_id=20)
        assert mcp_proxy.mcp_stats.duplicate_requests == 1

    def test_loop_reflected_in_scorecard(self) -> None:
        args = {"name": "looping_tool", "arguments": {}}
        for i in range(4):
            _post("tools/call", args, req_id=i)
        data = client.get("/_agentbreak/mcp/scorecard").json()
        assert data["suspected_loops"] >= 1
        assert data["resilience_score"] < 100


# ---------------------------------------------------------------------------
# Scenario 6: Built-in MCP scenarios
# ---------------------------------------------------------------------------

class TestBuiltInMCPScenarios:
    """Verify that the built-in MCP scenarios are configured with correct parameters."""

    def _get_scenario(self, name: str) -> dict[str, Any]:
        from agentbreak.main import SCENARIOS
        return SCENARIOS[name]

    def test_mcp_tool_failures_scenario_exists(self) -> None:
        s = self._get_scenario("mcp-tool-failures")
        assert s["mcp_fail_rate"] == pytest.approx(0.3)
        assert 429 in s["mcp_error_codes"] or 500 in s["mcp_error_codes"]

    def test_mcp_resource_unavailable_scenario_exists(self) -> None:
        s = self._get_scenario("mcp-resource-unavailable")
        assert s["mcp_fail_rate"] == pytest.approx(0.5)
        assert 404 in s["mcp_error_codes"] or 503 in s["mcp_error_codes"]

    def test_mcp_slow_tools_scenario_exists(self) -> None:
        s = self._get_scenario("mcp-slow-tools")
        assert s.get("mcp_latency_p", 0) > 0

    def test_mcp_initialization_failure_scenario_exists(self) -> None:
        s = self._get_scenario("mcp-initialization-failure")
        assert s["mcp_fail_rate"] == pytest.approx(0.5)

    def test_mcp_mixed_transient_scenario_exists(self) -> None:
        s = self._get_scenario("mcp-mixed-transient")
        assert s["mcp_fail_rate"] == pytest.approx(0.2)
        # Mixed transient should inject multiple different error codes
        assert len(s["mcp_error_codes"]) >= 2


# ---------------------------------------------------------------------------
# Scenario 7: Parse error handling
# ---------------------------------------------------------------------------

class TestParseErrors:
    """Verify malformed requests return proper JSON-RPC parse errors."""

    def setup_method(self) -> None:
        _reset(mode="mock", fail_rate=0.0)

    def test_non_json_body_returns_parse_error(self) -> None:
        resp = client.post("/mcp", content=b"not json at all", headers=_MCP_HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["error"]["code"] == -32700

    def test_missing_method_returns_invalid_request(self) -> None:
        payload = json.dumps({"jsonrpc": "2.0", "id": 1}).encode()
        resp = client.post("/mcp", content=payload, headers=_MCP_HEADERS)
        body = resp.json()
        assert body["error"]["code"] == -32600  # INVALID_REQUEST, not PARSE_ERROR

    def test_wrong_jsonrpc_version_returns_invalid_request(self) -> None:
        payload = json.dumps({"jsonrpc": "1.0", "id": 1, "method": "tools/list"}).encode()
        resp = client.post("/mcp", content=payload, headers=_MCP_HEADERS)
        body = resp.json()
        assert body["error"]["code"] == -32600  # INVALID_REQUEST, not PARSE_ERROR

    def test_parse_error_response_has_null_id(self) -> None:
        resp = client.post("/mcp", content=b"bad", headers=_MCP_HEADERS)
        body = resp.json()
        assert body["id"] is None


# ---------------------------------------------------------------------------
# Scenario 8: Unknown method in mock mode
# ---------------------------------------------------------------------------

class TestUnknownMethod:
    """Verify that unknown methods in mock mode still return a structured response."""

    def setup_method(self) -> None:
        _reset(mode="mock", fail_rate=0.0)

    def test_unknown_method_returns_empty_result(self) -> None:
        body = _post("notifications/initialized")
        # Unknown methods should return a success response with a None/empty result
        assert body["jsonrpc"] == "2.0"
        assert "error" not in body or body.get("error") is None

    def test_custom_method_tracked_in_method_counts(self) -> None:
        _post("custom/method")
        data = client.get("/_agentbreak/mcp/scorecard").json()
        assert "custom/method" in data["method_counts"]


# ---------------------------------------------------------------------------
# Scenario 9: Per-tool failure tracking
# ---------------------------------------------------------------------------

class TestPerToolFailureTracking:
    """Verify per-tool success/failure stats are tracked correctly."""

    def setup_method(self) -> None:
        _reset(mode="mock", fail_rate=0.0)

    def test_successful_tool_call_tracked_by_name(self) -> None:
        _post("tools/call", {"name": "echo", "arguments": {"text": "hi"}})
        data = client.get("/_agentbreak/mcp/scorecard").json()
        assert data["tool_successes_by_name"].get("echo", 0) == 1
        assert data["tool_failures_by_name"].get("echo", 0) == 0

    def test_failed_tool_call_tracked_by_name(self) -> None:
        _reset(mode="mock", fail_rate=1.0, fault_codes=(500,))
        _post("tools/call", {"name": "echo", "arguments": {}})
        data = client.get("/_agentbreak/mcp/scorecard").json()
        assert data["tool_failures_by_name"].get("echo", 0) == 1

    def test_multiple_tools_tracked_independently(self) -> None:
        _post("tools/call", {"name": "echo", "arguments": {"text": "a"}})
        _post("tools/call", {"name": "echo", "arguments": {"text": "b"}})
        _post("tools/call", {"name": "get_time", "arguments": {}})
        data = client.get("/_agentbreak/mcp/scorecard").json()
        assert data["tool_successes_by_name"]["echo"] == 2
        assert data["tool_successes_by_name"]["get_time"] == 1


# ---------------------------------------------------------------------------
# Scenario 10: Per-URI resource read tracking
# ---------------------------------------------------------------------------

class TestPerURIResourceTracking:
    """Verify per-URI resource read stats are tracked correctly."""

    def setup_method(self) -> None:
        _reset(mode="mock", fail_rate=0.0)

    def test_resource_read_tracked_by_uri(self) -> None:
        _post("resources/read", {"uri": "file:///example/readme.txt"})
        data = client.get("/_agentbreak/mcp/scorecard").json()
        assert data["resource_reads_by_uri"].get("file:///example/readme.txt", 0) == 1

    def test_failed_resource_read_tracked_by_uri(self) -> None:
        _reset(mode="mock", fail_rate=1.0, fault_codes=(404,))
        _post("resources/read", {"uri": "file:///example/readme.txt"})
        data = client.get("/_agentbreak/mcp/scorecard").json()
        assert data["resource_failures_by_uri"].get("file:///example/readme.txt", 0) == 1

    def test_multiple_uris_tracked_independently(self) -> None:
        _post("resources/read", {"uri": "file:///a"})
        _post("resources/read", {"uri": "file:///b"})
        _post("resources/read", {"uri": "file:///a"})
        data = client.get("/_agentbreak/mcp/scorecard").json()
        assert data["resource_reads_by_uri"]["file:///a"] == 2
        assert data["resource_reads_by_uri"]["file:///b"] == 1
