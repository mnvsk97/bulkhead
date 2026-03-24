from __future__ import annotations

from fastapi.testclient import TestClient

from agentbreak import main
from agentbreak.scenarios import ScenarioFile


MESSAGES_BODY = {"model": "claude-sonnet-4-20250514", "max_tokens": 1024, "messages": [{"role": "user", "content": "hi"}]}


def _make_runtime(scenarios_raw=None, mode="mock", upstream_url="", auth_headers=None):
    scenarios = []
    if scenarios_raw is not None:
        scenarios = ScenarioFile.model_validate({"scenarios": scenarios_raw}).scenarios
    return main.LLMRuntime(
        mode=mode,
        upstream_url=upstream_url,
        auth_headers=auth_headers or {},
        scenarios=scenarios,
    )


def _scenario(name, fault, schedule=None):
    return {
        "name": name,
        "summary": name,
        "target": "llm_chat",
        "fault": fault,
        "schedule": schedule or {"mode": "always"},
    }


# ── Mock mode ────────────────────────────────────────────────────────


def test_anthropic_mock_returns_message_format():
    main.service_state.llm_runtime = _make_runtime()
    client = TestClient(main.app)
    r = client.post("/v1/messages", json=MESSAGES_BODY)
    assert r.status_code == 200
    data = r.json()
    assert data["type"] == "message"
    assert data["role"] == "assistant"
    assert data["content"][0]["type"] == "text"
    assert data["content"][0]["text"] == "AgentBreak mock response."
    assert data["model"] == "agentbreak-mock"
    assert data["stop_reason"] == "end_turn"


# ── Error format ─────────────────────────────────────────────────────


def test_anthropic_http_error_uses_anthropic_format():
    main.service_state.llm_runtime = _make_runtime([
        _scenario("err-500", {"kind": "http_error", "status_code": 500}),
    ])
    client = TestClient(main.app)
    r = client.post("/v1/messages", json=MESSAGES_BODY)
    assert r.status_code == 500
    data = r.json()
    assert data["type"] == "error"
    assert data["error"]["type"] == "api_error"


def test_anthropic_429_error_format():
    main.service_state.llm_runtime = _make_runtime([
        _scenario("err-429", {"kind": "http_error", "status_code": 429}),
    ])
    client = TestClient(main.app)
    r = client.post("/v1/messages", json=MESSAGES_BODY)
    assert r.status_code == 429
    data = r.json()
    assert data["type"] == "error"
    assert data["error"]["type"] == "rate_limit_error"


def test_anthropic_malformed_json_returns_400():
    main.service_state.llm_runtime = _make_runtime()
    client = TestClient(main.app)
    r = client.post("/v1/messages", content=b"{oops", headers={"content-type": "application/json"})
    assert r.status_code == 400
    data = r.json()
    assert data["type"] == "error"
    assert data["error"]["type"] == "invalid_request_error"


# ── Mutation faults ──────────────────────────────────────────────────


def test_anthropic_empty_response_scenario():
    main.service_state.llm_runtime = _make_runtime([
        _scenario("empty", {"kind": "empty_response"}),
    ])
    client = TestClient(main.app)
    r = client.post("/v1/messages", json=MESSAGES_BODY)
    assert r.status_code == 200
    assert r.content == b""


def test_anthropic_invalid_json_scenario():
    main.service_state.llm_runtime = _make_runtime([
        _scenario("bad-json", {"kind": "invalid_json"}),
    ])
    client = TestClient(main.app)
    r = client.post("/v1/messages", json=MESSAGES_BODY)
    assert r.status_code == 200
    assert r.content == b"{not valid"


def test_anthropic_wrong_content_scenario():
    main.service_state.llm_runtime = _make_runtime([
        _scenario("wrong", {"kind": "wrong_content", "body": "REPLACED"}),
    ])
    client = TestClient(main.app)
    r = client.post("/v1/messages", json=MESSAGES_BODY)
    assert r.status_code == 200
    data = r.json()
    assert data["content"][0]["text"] == "REPLACED"


def test_anthropic_large_response_scenario():
    main.service_state.llm_runtime = _make_runtime([
        _scenario("large", {"kind": "large_response", "size_bytes": 10000}),
    ])
    client = TestClient(main.app)
    r = client.post("/v1/messages", json=MESSAGES_BODY)
    assert r.status_code == 200
    assert len(r.content) >= 10000


def test_anthropic_schema_violation_scenario():
    main.service_state.llm_runtime = _make_runtime([
        _scenario("schema", {"kind": "schema_violation"}),
    ])
    client = TestClient(main.app)
    r = client.post("/v1/messages", json=MESSAGES_BODY)
    assert r.status_code == 200
    data = r.json()
    assert data["content"][0]["type"] == "tool_use"
    assert data["content"][0]["input"] == "INVALID"


# ── Proxy mode ───────────────────────────────────────────────────────


def test_anthropic_proxy_forwards_to_v1_messages(monkeypatch):
    from tests.helpers import DummyAsyncClient, DummyResponse

    DummyAsyncClient.response = DummyResponse(
        status_code=200,
        json_data={"id": "msg_123", "type": "message", "role": "assistant", "content": [{"type": "text", "text": "hello"}]},
        headers={"content-type": "application/json", "request-id": "req_abc"},
    )
    monkeypatch.setattr(main.httpx, "AsyncClient", DummyAsyncClient)
    main.service_state.llm_runtime = _make_runtime(
        mode="proxy", upstream_url="https://api.anthropic.com",
    )
    client = TestClient(main.app)
    r = client.post("/v1/messages", json=MESSAGES_BODY, headers={"x-api-key": "sk-test"})
    assert r.status_code == 200
    assert r.json()["id"] == "msg_123"
    assert DummyAsyncClient.calls[0]["url"] == "https://api.anthropic.com/v1/messages"


def test_anthropic_proxy_upstream_unreachable(monkeypatch):
    import httpx
    from tests.helpers import DummyAsyncClient

    DummyAsyncClient.error = httpx.ConnectError("refused")
    monkeypatch.setattr(main.httpx, "AsyncClient", DummyAsyncClient)
    main.service_state.llm_runtime = _make_runtime(
        mode="proxy", upstream_url="http://bad-host:9999",
    )
    client = TestClient(main.app)
    r = client.post("/v1/messages", json=MESSAGES_BODY)
    assert r.status_code == 502
    data = r.json()
    assert data["type"] == "error"
    assert "upstream" in data["error"]["message"].lower()


# ── Disabled ─────────────────────────────────────────────────────────


def test_anthropic_disabled_returns_404():
    main.service_state.llm_runtime = None
    client = TestClient(main.app)
    r = client.post("/v1/messages", json=MESSAGES_BODY)
    assert r.status_code == 404
    data = r.json()
    assert data["type"] == "error"


# ── Stats shared with OpenAI endpoint ────────────────────────────────


def test_anthropic_requests_counted_in_llm_scorecard():
    main.service_state.llm_runtime = _make_runtime()
    client = TestClient(main.app)
    client.post("/v1/messages", json=MESSAGES_BODY)
    sc = client.get("/_agentbreak/scorecard").json()
    assert sc["requests_seen"] == 1
    assert sc["upstream_successes"] == 1
