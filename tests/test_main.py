from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agentbreak import main
from agentbreak.config import MCPPrompt, MCPRegistry, MCPResource, MCPTool
from agentbreak.discovery import mcp as mcp_discovery
from agentbreak.scenarios import ScenarioFile, load_scenarios
from tests.helpers import DummyAsyncClient, DummyResponse


runner = CliRunner()


def write_application(path: Path, *, llm: dict | None = None, mcp: dict | None = None) -> None:
    payload = {
        "llm": {"enabled": True, "mode": "proxy", "upstream_url": "https://upstream.example"},
        "mcp": {"enabled": False},
        "serve": {"host": "127.0.0.1", "port": 5000},
    }
    if llm is not None:
        payload["llm"].update(llm)
    if mcp is not None:
        payload["mcp"].update(mcp)
    path.write_text(__import__("yaml").safe_dump(payload), encoding="utf-8")


def write_scenarios(path: Path, scenarios: list[dict]) -> None:
    path.write_text(__import__("yaml").safe_dump({"version": 1, "scenarios": scenarios}), encoding="utf-8")


def test_openai_error_shape() -> None:
    payload = main.openai_error(429)
    assert payload["error"]["type"] == "rate_limit_error"
    assert payload["error"]["code"] == 429


def test_openai_error_preserves_unknown_status_code() -> None:
    payload = main.openai_error(418)
    assert payload["error"]["type"] == "server_error"
    assert payload["error"]["code"] == 418


def test_load_scenarios_parses_llm_and_mcp_targets(tmp_path: Path) -> None:
    scenarios_path = tmp_path / "scenarios.yaml"
    write_scenarios(
        scenarios_path,
        [
            {
                "name": "brownout",
                "summary": "Inject latency",
                "target": "llm_chat",
                "match": {},
                "fault": {"kind": "latency", "min_ms": 5, "max_ms": 10},
                "schedule": {"mode": "random", "probability": 0.2},
            },
            {
                "name": "tool-bad-schema",
                "summary": "Break tool result",
                "target": "mcp_tool",
                "match": {"tool_name": "search_docs"},
                "fault": {"kind": "schema_violation"},
                "schedule": {"mode": "always"},
            },
        ],
    )

    scenario_file = load_scenarios(str(scenarios_path))
    assert len(scenario_file.scenarios) == 2
    assert scenario_file.scenarios[0].target == "llm_chat"
    assert scenario_file.scenarios[1].match.tool_name == "search_docs"


def test_load_scenarios_expands_presets(tmp_path: Path) -> None:
    scenarios_path = tmp_path / "scenarios.yaml"
    scenarios_path.write_text(__import__("yaml").safe_dump({"version": 1, "preset": "mcp-mixed-transient"}), encoding="utf-8")

    scenario_file = load_scenarios(str(scenarios_path))

    assert len(scenario_file.scenarios) == 2
    assert {scenario.target for scenario in scenario_file.scenarios} == {"mcp_tool"}


def test_validate_rejects_unimplemented_targets(tmp_path: Path) -> None:
    application_path = tmp_path / "application.yaml"
    scenarios_path = tmp_path / "scenarios.yaml"
    write_application(application_path)
    write_scenarios(
        scenarios_path,
        [
            {
                "name": "memory-poison",
                "summary": "Reserved target",
                "target": "memory",
                "match": {},
                "fault": {"kind": "wrong_content", "body": "poison"},
                "schedule": {"mode": "always"},
            }
        ],
    )

    result = runner.invoke(main.cli, ["validate", "--config", str(application_path), "--scenarios", str(scenarios_path)])
    assert result.exit_code != 0
    assert "Recognized but unimplemented scenario targets" in str(result.exception)


def test_validate_rejects_llm_timeout_fault(tmp_path: Path) -> None:
    application_path = tmp_path / "application.yaml"
    scenarios_path = tmp_path / "scenarios.yaml"
    write_application(application_path)
    write_scenarios(
        scenarios_path,
        [
            {
                "name": "llm-timeout",
                "summary": "Unsupported timeout",
                "target": "llm_chat",
                "match": {},
                "fault": {"kind": "timeout", "min_ms": 1, "max_ms": 2},
                "schedule": {"mode": "always"},
            }
        ],
    )

    result = runner.invoke(main.cli, ["validate", "--config", str(application_path), "--scenarios", str(scenarios_path)])
    assert result.exit_code != 0
    assert "llm_chat timeout faults are not implemented" in str(result.exception)


def test_mcp_only_mock_config_is_valid() -> None:
    config = main.ApplicationConfig.model_validate(
        {
            "llm": {"enabled": False},
            "mcp": {"enabled": True},
            "serve": {"host": "127.0.0.1", "port": 5000},
        }
    )
    assert config.mcp.enabled is True
    assert config.mcp.upstream_url == ""


def test_serve_loads_split_config_without_crashing(monkeypatch, tmp_path: Path) -> None:
    application_path = tmp_path / "application.yaml"
    scenarios_path = tmp_path / "scenarios.yaml"
    registry_path = tmp_path / "registry.json"
    write_application(application_path, llm={"mode": "mock"})
    write_scenarios(
        scenarios_path,
        [
            {
                "name": "bad-json",
                "summary": "Break chat JSON",
                "target": "llm_chat",
                "match": {},
                "fault": {"kind": "invalid_json"},
                "schedule": {"mode": "always"},
            }
        ],
    )
    registry_path.write_text(json.dumps({"version": 1, "tools": []}), encoding="utf-8")
    captured: dict[str, object] = {}

    monkeypatch.setattr(main, "install_signal_handlers", lambda: None)
    monkeypatch.setattr(main, "print_scorecard", lambda: None)

    def fake_run(app, host: str, port: int, log_level: str) -> None:
        captured["state"] = main.service_state

    monkeypatch.setattr(main.uvicorn, "run", fake_run)
    result = runner.invoke(
        main.cli,
        ["serve", "--config", str(application_path), "--scenarios", str(scenarios_path), "--registry", str(registry_path)],
    )

    assert result.exit_code == 0
    state = captured["state"]
    assert state is not None
    assert isinstance(state, main.ServiceState)
    assert state.llm_runtime is not None
    assert len(state.scenarios.scenarios) == 1


def test_inspect_writes_registry(monkeypatch, tmp_path: Path) -> None:
    application_path = tmp_path / "application.yaml"
    registry_path = tmp_path / "registry.json"
    write_application(application_path, mcp={"enabled": True, "upstream_url": "https://mcp.example.com"})

    async def fake_inspect(_config):
        return MCPRegistry(tools=[MCPTool(name="search_docs", description="Search docs", inputSchema={"type": "object"})])

    monkeypatch.setattr(main, "inspect_mcp_server", fake_inspect)
    result = runner.invoke(main.cli, ["inspect", "--config", str(application_path), "--registry", str(registry_path)])

    assert result.exit_code == 0
    assert "Discovered 1 MCP tools" in result.stdout
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    assert payload["tools"][0]["name"] == "search_docs"


def test_inspect_collects_paginated_tools_resources_and_prompts(monkeypatch) -> None:
    DummyAsyncClient.responses = [
        DummyResponse(
            json_data={"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": mcp_discovery.MCP_PROTOCOL_VERSION}},
            headers={"content-type": "application/json", "mcp-session-id": "sess-1"},
        ),
        DummyResponse(status_code=202, content=b""),
        DummyResponse(
            json_data={
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "tools": [{"name": "search_docs", "description": "Search docs", "inputSchema": {"type": "object"}}],
                    "nextCursor": "next-tools",
                },
            }
        ),
        DummyResponse(
            json_data={
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "tools": [{"name": "fetch_docs", "description": "Fetch docs", "inputSchema": {"type": "object"}}],
                },
            }
        ),
        DummyResponse(
            json_data={
                "jsonrpc": "2.0",
                "id": 100,
                "result": {
                    "resources": [{"uri": "file:///playbook.md", "name": "playbook", "mimeType": "text/markdown"}],
                },
            }
        ),
        DummyResponse(
            json_data={
                "jsonrpc": "2.0",
                "id": 200,
                "result": {
                    "prompts": [{"name": "summary_prompt", "description": "Prompt", "arguments": []}],
                },
            }
        ),
    ]
    monkeypatch.setattr(mcp_discovery.httpx, "AsyncClient", DummyAsyncClient)

    registry = __import__("asyncio").run(
        mcp_discovery.inspect_mcp_server(
            main.ApplicationConfig.model_validate(
                {
                    "llm": {"enabled": False},
                    "mcp": {"enabled": True, "upstream_url": "https://mcp.example.com"},
                    "serve": {"host": "127.0.0.1", "port": 5000},
                }
            ).mcp
        )
    )

    assert [tool.name for tool in registry.tools] == ["search_docs", "fetch_docs"]
    assert registry.resources[0].uri == "file:///playbook.md"
    assert registry.prompts[0].name == "summary_prompt"


def test_validate_rejects_missing_registry_when_mcp_enabled(tmp_path: Path) -> None:
    application_path = tmp_path / "application.yaml"
    registry_path = tmp_path / "nonexistent" / "registry.json"
    write_application(application_path, mcp={"enabled": True, "upstream_url": "https://mcp.example.com"})

    result = runner.invoke(main.cli, ["validate", "--config", str(application_path), "--registry", str(registry_path)])
    assert result.exit_code != 0
    assert "MCP registry not found" in str(result.exception)


def test_llm_proxy_successful_forwarding(monkeypatch) -> None:
    DummyAsyncClient.response = DummyResponse(
        status_code=200,
        content=b'{"id":"cmpl_123","object":"chat.completion"}',
        headers={"content-type": "application/json", "x-request-id": "req_123"},
    )
    monkeypatch.setattr(main, "service_state", main.service_state)
    monkeypatch.setattr(main.httpx, "AsyncClient", DummyAsyncClient)
    client = TestClient(main.app)

    response = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hello"}]},
        headers={"authorization": "Bearer test"},
    )

    assert response.status_code == 200
    assert response.json()["id"] == "cmpl_123"
    assert response.headers["x-request-id"] == "req_123"
    assert DummyAsyncClient.calls[0]["url"] == "https://upstream.example/v1/chat/completions"
    assert DummyAsyncClient.calls[0]["headers"]["authorization"] == "Bearer test"


def test_llm_http_error_scenario_injects_fault() -> None:
    main.service_state.llm_runtime = main.LLMRuntime(
        mode="mock",
        upstream_url="",
        auth_headers={},
        scenarios=ScenarioFile.model_validate(
            {
                "scenarios": [
                    {
                        "name": "llm-500",
                        "summary": "Inject 500",
                        "target": "llm_chat",
                        "match": {},
                        "fault": {"kind": "http_error", "status_code": 500},
                        "schedule": {"mode": "always"},
                    }
                ]
            }
        ).scenarios,
    )
    client = TestClient(main.app)

    response = client.post("/v1/chat/completions", json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]})

    assert response.status_code == 500
    assert response.json()["error"]["code"] == 500


def test_llm_unknown_http_error_scenario_preserves_status_code() -> None:
    main.service_state.llm_runtime = main.LLMRuntime(
        mode="mock",
        upstream_url="",
        auth_headers={},
        scenarios=ScenarioFile.model_validate(
            {
                "scenarios": [
                    {
                        "name": "llm-418",
                        "summary": "Inject 418",
                        "target": "llm_chat",
                        "match": {},
                        "fault": {"kind": "http_error", "status_code": 418},
                        "schedule": {"mode": "always"},
                    }
                ]
            }
        ).scenarios,
    )
    client = TestClient(main.app)

    response = client.post("/v1/chat/completions", json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]})

    assert response.status_code == 418
    assert response.json()["error"]["code"] == 418


def test_llm_invalid_json_scenario_mutates_success_response() -> None:
    main.service_state.llm_runtime = main.LLMRuntime(
        mode="mock",
        upstream_url="",
        auth_headers={},
        scenarios=ScenarioFile.model_validate(
            {
                "scenarios": [
                    {
                        "name": "llm-bad-json",
                        "summary": "Break JSON",
                        "target": "llm_chat",
                        "match": {},
                        "fault": {"kind": "invalid_json"},
                        "schedule": {"mode": "always"},
                    }
                ]
            }
        ).scenarios,
    )
    client = TestClient(main.app)

    response = client.post("/v1/chat/completions", json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 200
    assert response.content == b"{not valid"


def test_llm_malformed_request_returns_400_instead_of_crashing() -> None:
    main.service_state.llm_runtime = main.LLMRuntime(
        mode="mock",
        upstream_url="",
        auth_headers={},
        scenarios=[],
    )
    client = TestClient(main.app)

    response = client.post("/v1/chat/completions", content=b"{oops", headers={"content-type": "application/json"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == 400


def test_mcp_initialize_and_tools_list() -> None:
    main.service_state.mcp_runtime = main.MCPRuntime(
        upstream_url="",
        auth_headers={},
        registry=MCPRegistry(
            tools=[MCPTool(name="search_docs", description="Search docs", inputSchema={"type": "object"})],
            resources=[MCPResource(uri="file:///playbook.md", name="playbook", mimeType="text/markdown")],
            prompts=[MCPPrompt(name="summary_prompt", description="Prompt", arguments=[])],
        ),
        scenarios=[],
    )
    client = TestClient(main.app)

    init = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    listed = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    resources = client.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}})
    prompts = client.post("/mcp", json={"jsonrpc": "2.0", "id": 4, "method": "prompts/list", "params": {}})

    assert init.status_code == 200
    assert init.json()["result"]["capabilities"]["tools"]["listChanged"] is False
    assert init.json()["result"]["capabilities"]["resources"]["listChanged"] is False
    assert init.json()["result"]["capabilities"]["prompts"]["listChanged"] is False
    assert listed.json()["result"]["tools"][0]["name"] == "search_docs"
    assert resources.json()["result"]["resources"][0]["uri"] == "file:///playbook.md"
    assert prompts.json()["result"]["prompts"][0]["name"] == "summary_prompt"


def test_mcp_malformed_request_returns_parse_error() -> None:
    main.service_state.mcp_runtime = main.MCPRuntime(
        upstream_url="",
        auth_headers={},
        registry=MCPRegistry(),
        scenarios=[],
    )
    client = TestClient(main.app)

    response = client.post("/mcp", content=b"{oops", headers={"content-type": "application/json"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32700


def test_mcp_schema_violation_scenario_corrupts_tool_result() -> None:
    main.service_state.mcp_runtime = main.MCPRuntime(
        upstream_url="",
        auth_headers={},
        registry=MCPRegistry(tools=[MCPTool(name="search_docs", description="Search docs", inputSchema={"type": "object"})]),
        scenarios=ScenarioFile.model_validate(
            {
                "scenarios": [
                    {
                        "name": "tool-bad-schema",
                        "summary": "Corrupt tool output",
                        "target": "mcp_tool",
                        "match": {"tool_name": "search_docs"},
                        "fault": {"kind": "schema_violation"},
                        "schedule": {"mode": "always"},
                    }
                ]
            }
        ).scenarios,
    )
    client = TestClient(main.app)

    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "search_docs", "arguments": {}}},
    )

    assert response.status_code == 200
    assert response.json()["result"]["content"] == "INVALID"


def test_mcp_resource_and_prompt_reads_work_in_mock_mode() -> None:
    main.service_state.mcp_runtime = main.MCPRuntime(
        upstream_url="",
        auth_headers={},
        registry=MCPRegistry(
            resources=[MCPResource(uri="file:///playbook.md", name="playbook", mimeType="text/markdown")],
            prompts=[MCPPrompt(name="summary_prompt", description="Prompt", arguments=[])],
        ),
        scenarios=[],
    )
    client = TestClient(main.app)

    resource = client.post("/mcp", json={"jsonrpc": "2.0", "id": 5, "method": "resources/read", "params": {"uri": "file:///playbook.md"}})
    prompt = client.post("/mcp", json={"jsonrpc": "2.0", "id": 6, "method": "prompts/get", "params": {"name": "summary_prompt", "arguments": {}}})

    assert resource.status_code == 200
    assert resource.json()["result"]["contents"][0]["uri"] == "file:///playbook.md"
    assert prompt.status_code == 200
    assert prompt.json()["result"]["messages"][0]["content"]["type"] == "text"


def test_mcp_scorecard_and_requests_are_tracked() -> None:
    main.service_state.mcp_runtime = main.MCPRuntime(
        upstream_url="",
        auth_headers={},
        registry=MCPRegistry(tools=[MCPTool(name="search_docs", description="Search docs", inputSchema={"type": "object"})]),
        scenarios=[],
    )
    client = TestClient(main.app)

    client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "search_docs", "arguments": {}}})

    scorecard = client.get("/_agentbreak/mcp-scorecard")
    requests = client.get("/_agentbreak/mcp-requests")

    assert scorecard.status_code == 200
    assert scorecard.json()["tool_calls"] == 1
    assert scorecard.json()["tool_call_counts"]["search_docs"] == 1
    assert scorecard.json()["method_counts"]["initialize"] == 1
    assert scorecard.json()["method_counts"]["tools/call"] == 1
    assert scorecard.json()["tool_successes_by_name"]["search_docs"] == 1
    assert requests.status_code == 200
    assert any(item["method"] == "tools/call" and item["tool_name"] == "search_docs" for item in requests.json()["recent_requests"])
    assert all("payload" not in item for item in requests.json()["recent_requests"])


def test_mcp_duplicate_fingerprint_ignores_request_id() -> None:
    main.service_state.mcp_runtime = main.MCPRuntime(
        upstream_url="",
        auth_headers={},
        registry=MCPRegistry(tools=[MCPTool(name="search_docs", description="Search docs", inputSchema={"type": "object"})]),
        scenarios=[],
    )
    client = TestClient(main.app)

    client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "search_docs", "arguments": {"q": "a"}}})
    client.post("/mcp", json={"jsonrpc": "2.0", "id": 99, "method": "tools/call", "params": {"name": "search_docs", "arguments": {"q": "a"}}})

    scorecard = client.get("/_agentbreak/mcp-scorecard")
    requests = client.get("/_agentbreak/mcp-requests")

    assert scorecard.json()["duplicate_requests"] == 1
    call_entries = [item for item in requests.json()["recent_requests"] if item["method"] == "tools/call"]
    assert len(call_entries) == 2
    assert call_entries[0]["fingerprint"] == call_entries[1]["fingerprint"]
    assert call_entries[1]["count"] == 2


def test_mcp_resource_fingerprint_ignores_request_id() -> None:
    main.service_state.mcp_runtime = main.MCPRuntime(
        upstream_url="",
        auth_headers={},
        registry=MCPRegistry(resources=[MCPResource(uri="file:///playbook.md", name="playbook", mimeType="text/markdown")]),
        scenarios=[],
    )
    client = TestClient(main.app)

    client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": "file:///playbook.md"}})
    client.post("/mcp", json={"jsonrpc": "2.0", "id": 99, "method": "resources/read", "params": {"uri": "file:///playbook.md"}})

    scorecard = client.get("/_agentbreak/mcp-scorecard")

    assert scorecard.json()["duplicate_requests"] == 1


def test_mcp_reinitializes_after_session_expiry(monkeypatch) -> None:
    DummyAsyncClient.responses = [
        DummyResponse(
            status_code=404,
            json_data={"jsonrpc": "2.0", "id": 1, "error": {"code": -32001, "message": "Session expired"}},
        ),
        DummyResponse(
            json_data={"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": mcp_discovery.MCP_PROTOCOL_VERSION}},
            headers={"content-type": "application/json", "mcp-session-id": "fresh-session"},
        ),
        DummyResponse(status_code=202, content=b""),
        DummyResponse(
            json_data={"jsonrpc": "2.0", "id": 1, "result": {"contents": [{"uri": "file:///playbook.md", "text": "fresh"}]}},
            headers={"content-type": "application/json", "mcp-session-id": "fresh-session"},
        ),
    ]
    monkeypatch.setattr(main.httpx, "AsyncClient", DummyAsyncClient)
    main.service_state.mcp_runtime = main.MCPRuntime(
        upstream_url="https://mcp.example.com",
        auth_headers={},
        registry=MCPRegistry(resources=[MCPResource(uri="file:///playbook.md", name="playbook", mimeType="text/markdown")]),
        scenarios=[],
    )
    main.service_state.mcp_runtime.session_id = "stale-session"
    client = TestClient(main.app)

    response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": "file:///playbook.md"}})

    assert response.status_code == 200
    assert response.json()["result"]["contents"][0]["text"] == "fresh"
    assert main.service_state.mcp_runtime.session_id == "fresh-session"


def test_mcp_tool_failures_are_tracked() -> None:
    main.service_state.mcp_runtime = main.MCPRuntime(
        upstream_url="",
        auth_headers={},
        registry=MCPRegistry(tools=[MCPTool(name="search_docs", description="Search docs", inputSchema={"type": "object"})]),
        scenarios=ScenarioFile.model_validate(
            {
                "scenarios": [
                    {
                        "name": "tool-timeout",
                        "summary": "Timeout one tool",
                        "target": "mcp_tool",
                        "match": {"tool_name": "search_docs"},
                        "fault": {"kind": "timeout", "min_ms": 1, "max_ms": 1},
                        "schedule": {"mode": "always"},
                    }
                ]
            }
        ).scenarios,
    )
    client = TestClient(main.app)

    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "search_docs", "arguments": {}}},
    )
    scorecard = client.get("/_agentbreak/mcp-scorecard")

    assert response.status_code == 504
    assert scorecard.json()["tool_failures_by_name"]["search_docs"] == 1


def test_discovery_client_inspects_mcp_tools(monkeypatch) -> None:
    DummyAsyncClient.responses = [
        DummyResponse(json_data={"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": mcp_discovery.MCP_PROTOCOL_VERSION}}),
        DummyResponse(status_code=202, content=b""),
        DummyResponse(
            json_data={
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "tools": [
                        {
                            "name": "calculator",
                            "description": "Math",
                            "inputSchema": {"type": "object"},
                        }
                    ]
                },
            }
        ),
        DummyResponse(status_code=404, content=b"{}"),
        DummyResponse(status_code=404, content=b"{}"),
    ]
    monkeypatch.setattr(mcp_discovery.httpx, "AsyncClient", DummyAsyncClient)

    registry = __import__("asyncio").run(
        mcp_discovery.inspect_mcp_server(
            main.ApplicationConfig.model_validate(
                {
                    "llm": {"enabled": False},
                    "mcp": {"enabled": True, "upstream_url": "https://mcp.example.com"},
                    "serve": {"host": "127.0.0.1", "port": 5000},
                }
            ).mcp
        )
    )

    assert len(registry.tools) == 1
    assert registry.tools[0].name == "calculator"
