from __future__ import annotations

import json

from fastapi.testclient import TestClient

from agentbreak import main
from agentbreak.scenarios import ScenarioFile
from tests.helpers import DummyAsyncClient, DummyResponse


OPENAI_STREAM_BODY = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}], "stream": True}
ANTHROPIC_STREAM_BODY = {"model": "claude-sonnet-4-20250514", "max_tokens": 1024, "messages": [{"role": "user", "content": "hi"}], "stream": True}


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


# ── OpenAI streaming mock ────────────────────────────────────────────


def test_openai_stream_mock_returns_sse():
    main.service_state.llm_runtime = _make_runtime()
    client = TestClient(main.app)
    r = client.post("/v1/chat/completions", json=OPENAI_STREAM_BODY)
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    text = r.text
    assert "data: " in text
    assert "[DONE]" in text
    # Parse the first data line
    for line in text.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            chunk = json.loads(line[6:])
            assert chunk["object"] == "chat.completion.chunk"
            assert chunk["model"] == "agentbreak-mock"
            assert chunk["choices"][0]["delta"]["content"] == "AgentBreak mock response."
            break


# ── Anthropic streaming mock ─────────────────────────────────────────


def test_anthropic_stream_mock_returns_sse():
    main.service_state.llm_runtime = _make_runtime()
    client = TestClient(main.app)
    r = client.post("/v1/messages", json=ANTHROPIC_STREAM_BODY)
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    text = r.text
    assert "event: message_start" in text
    assert "event: content_block_delta" in text
    assert "event: message_stop" in text
    # Parse the content delta
    for line in text.splitlines():
        if '"content_block_delta"' in line and line.startswith("data: "):
            delta = json.loads(line[6:])
            assert delta["delta"]["type"] == "text_delta"
            assert delta["delta"]["text"] == "AgentBreak mock response."
            break


# ── Pre-response faults still work with streaming ────────────────────


def test_stream_http_error_fault_returns_json_not_stream():
    """http_error faults fire before streaming starts, return normal JSON error."""
    main.service_state.llm_runtime = _make_runtime([
        _scenario("err-500", {"kind": "http_error", "status_code": 500}),
    ])
    client = TestClient(main.app)
    r = client.post("/v1/chat/completions", json=OPENAI_STREAM_BODY)
    assert r.status_code == 500
    assert r.json()["error"]["code"] == 500


def test_stream_http_error_anthropic_format():
    main.service_state.llm_runtime = _make_runtime([
        _scenario("err-429", {"kind": "http_error", "status_code": 429}),
    ])
    client = TestClient(main.app)
    r = client.post("/v1/messages", json=ANTHROPIC_STREAM_BODY)
    assert r.status_code == 429
    assert r.json()["type"] == "error"


def test_stream_latency_fault_still_returns_stream():
    main.service_state.llm_runtime = _make_runtime([
        _scenario("latency", {"kind": "latency", "min_ms": 1, "max_ms": 2}),
    ])
    client = TestClient(main.app)
    r = client.post("/v1/chat/completions", json=OPENAI_STREAM_BODY)
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    assert "[DONE]" in r.text


# ── Response mutation faults are skipped for streams ─────────────────


def test_stream_response_mutation_skipped():
    """Response mutations can't work on SSE streams, so mock stream is returned as-is."""
    main.service_state.llm_runtime = _make_runtime([
        _scenario("bad-json", {"kind": "invalid_json"}),
    ])
    client = TestClient(main.app)
    r = client.post("/v1/chat/completions", json=OPENAI_STREAM_BODY)
    assert r.status_code == 200
    # Should still be a valid SSE stream, not "{not valid"
    assert "text/event-stream" in r.headers["content-type"]
    assert "[DONE]" in r.text


# ── Proxy streaming ──────────────────────────────────────────────────


class DummyStreamResponse:
    """Simulates an httpx streaming response."""
    def __init__(self, status_code=200, chunks=None, headers=None):
        self.status_code = status_code
        self.headers = headers or {"content-type": "text/event-stream"}
        self._chunks = chunks or [b"data: {\"ok\":true}\n\n", b"data: [DONE]\n\n"]
        self._read_body = None

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    async def aread(self):
        self._read_body = b"".join(self._chunks)
        return self._read_body

    async def aclose(self):
        pass


class DummyStreamClient:
    """Simulates httpx.AsyncClient with stream support."""
    response: DummyStreamResponse = DummyStreamResponse()
    calls: list = []
    error: Exception | None = None

    def __init__(self, *a, **kw):
        pass

    def build_request(self, method, url, content=None, headers=None):
        DummyStreamClient.calls.append({"method": method, "url": url, "headers": headers or {}})
        return {"method": method, "url": url, "content": content, "headers": headers}

    async def send(self, req, stream=False):
        if DummyStreamClient.error:
            raise DummyStreamClient.error
        return DummyStreamClient.response

    async def aclose(self):
        pass


def _reset_stream_client():
    DummyStreamClient.calls = []
    DummyStreamClient.error = None
    DummyStreamClient.response = DummyStreamResponse()


def test_stream_proxy_forwards_upstream_sse(monkeypatch):
    _reset_stream_client()
    chunks = [b"data: {\"id\":\"1\"}\n\n", b"data: [DONE]\n\n"]
    DummyStreamClient.response = DummyStreamResponse(chunks=chunks)
    monkeypatch.setattr(main.httpx, "AsyncClient", DummyStreamClient)
    main.service_state.llm_runtime = _make_runtime(
        mode="proxy", upstream_url="https://api.openai.com",
    )
    client = TestClient(main.app)
    r = client.post("/v1/chat/completions", json=OPENAI_STREAM_BODY)
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    assert b"data: [DONE]" in r.content
    assert DummyStreamClient.calls[0]["url"] == "https://api.openai.com/v1/chat/completions"


def test_stream_proxy_anthropic_forwards_to_v1_messages(monkeypatch):
    _reset_stream_client()
    chunks = [b"event: message_start\ndata: {}\n\n", b"event: message_stop\ndata: {}\n\n"]
    DummyStreamClient.response = DummyStreamResponse(chunks=chunks)
    monkeypatch.setattr(main.httpx, "AsyncClient", DummyStreamClient)
    main.service_state.llm_runtime = _make_runtime(
        mode="proxy", upstream_url="https://api.anthropic.com",
    )
    client = TestClient(main.app)
    r = client.post("/v1/messages", json=ANTHROPIC_STREAM_BODY)
    assert r.status_code == 200
    assert DummyStreamClient.calls[0]["url"] == "https://api.anthropic.com/v1/messages"


def test_stream_proxy_upstream_error(monkeypatch):
    _reset_stream_client()
    DummyStreamClient.response = DummyStreamResponse(
        status_code=429,
        chunks=[b'{"error": "rate limited"}'],
        headers={"content-type": "application/json"},
    )
    monkeypatch.setattr(main.httpx, "AsyncClient", DummyStreamClient)
    main.service_state.llm_runtime = _make_runtime(
        mode="proxy", upstream_url="https://api.openai.com",
    )
    client = TestClient(main.app)
    r = client.post("/v1/chat/completions", json=OPENAI_STREAM_BODY)
    assert r.status_code == 429


def test_stream_proxy_upstream_unreachable(monkeypatch):
    import httpx
    _reset_stream_client()
    DummyStreamClient.error = httpx.ConnectError("refused")
    monkeypatch.setattr(main.httpx, "AsyncClient", DummyStreamClient)
    main.service_state.llm_runtime = _make_runtime(
        mode="proxy", upstream_url="http://bad-host:9999",
    )
    client = TestClient(main.app)
    r = client.post("/v1/chat/completions", json=OPENAI_STREAM_BODY)
    assert r.status_code == 502


# ── Non-streaming requests are unaffected ────────────────────────────


def test_non_stream_request_still_returns_json():
    """Requests without stream:true still get normal JSON responses."""
    main.service_state.llm_runtime = _make_runtime()
    client = TestClient(main.app)
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    r = client.post("/v1/chat/completions", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["object"] == "chat.completion"
    assert "text/event-stream" not in r.headers.get("content-type", "")


# ── Stats ────────────────────────────────────────────────────────────


def test_stream_mock_counted_in_scorecard():
    main.service_state.llm_runtime = _make_runtime()
    client = TestClient(main.app)
    client.post("/v1/chat/completions", json=OPENAI_STREAM_BODY)
    sc = client.get("/_agentbreak/scorecard").json()
    assert sc["requests_seen"] == 1
    assert sc["upstream_successes"] == 1
