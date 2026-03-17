from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentbreak import main


def reset_state() -> None:
    main.config = main.Config(upstream_url="https://upstream.example", fail_rate=0.0, latency_p=0.0)
    main.stats = main.Stats()


class DummyResponse:
    def __init__(self, status_code: int = 200, content: bytes = b'{"ok":true}', headers: dict[str, str] | None = None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {"content-type": "application/json"}


class DummyAsyncClient:
    response = DummyResponse()
    error: Exception | None = None
    calls: list[dict] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self) -> "DummyAsyncClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def post(self, url: str, content: bytes, headers: dict[str, str]):
        DummyAsyncClient.calls.append({"url": url, "content": content, "headers": headers})
        if DummyAsyncClient.error is not None:
            raise DummyAsyncClient.error
        return DummyAsyncClient.response


class FakeUpstreamHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length)
        FakeUpstreamHandler.requests.append(
            {
                "path": self.path,
                "headers": {key.lower(): value for key, value in self.headers.items()},
                "body": json.loads(body.decode("utf-8")),
            }
        )
        payload = {
            "id": "chatcmpl-fake-upstream",
            "object": "chat.completion",
            "created": 0,
            "model": "fake-upstream",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "fake upstream response"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return None


def start_fake_upstream() -> tuple[ThreadingHTTPServer, str]:
    FakeUpstreamHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstreamHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}"


def test_clamp_probability() -> None:
    assert main.clamp_probability(-1) == 0.0
    assert main.clamp_probability(2) == 1.0
    assert main.clamp_probability(0.25) == 0.25


def test_openai_error_shape() -> None:
    payload = main.openai_error(429)
    assert payload["error"]["type"] == "rate_limit_error"
    assert payload["error"]["code"] == 429


@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [
        (400, "invalid_request_error"),
        (401, "authentication_error"),
        (403, "permission_error"),
        (404, "not_found_error"),
        (413, "invalid_request_error"),
        (429, "rate_limit_error"),
        (500, "server_error"),
        (503, "server_error"),
    ],
)
def test_injected_failure_updates_scorecard(status_code: int, error_type: str) -> None:
    reset_state()
    main.config.fail_rate = 1.0
    main.config.error_codes = (status_code,)
    client = TestClient(main.app)

    response = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hello"}]},
    )

    assert response.status_code == status_code
    assert response.json()["error"]["type"] == error_type
    assert response.json()["error"]["code"] == status_code
    assert main.stats.injected_faults == 1
    assert main.stats.upstream_failures == 1
    assert client.get("/_agentbreak/scorecard").json()["requests_seen"] == 1


def test_parse_error_codes() -> None:
    assert main.parse_error_codes("400, 429,500") == (400, 429, 500)


def test_parse_error_codes_rejects_unsupported() -> None:
    with pytest.raises(Exception):
        main.parse_error_codes("418")


def test_parse_fault_weights() -> None:
    assert main.parse_fault_weights("500=0.3,429=0.2") == ((500, 0.3), (429, 0.2))


def test_parse_fault_weights_rejects_total_over_one() -> None:
    with pytest.raises(Exception):
        main.parse_fault_weights("500=0.8,429=0.5")


def test_pick_error_code_uses_fault_weights() -> None:
    reset_state()
    main.config.fault_weights = ((401, 1.0),)
    assert main.pick_error_code() == 401


def test_resolve_scenario() -> None:
    scenario = main.resolve_scenario("brownout")
    assert scenario["error_codes"] == (429, 500, 503)
    assert scenario["latency_p"] == 0.2


def test_resolve_scenario_rejects_unknown() -> None:
    with pytest.raises(Exception):
        main.resolve_scenario("nope")


def test_maybe_load_config_missing_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert main.maybe_load_config(None) == {}


def test_maybe_load_config_default_config_yaml(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("mode: mock\nfail_rate: 0.4\n", encoding="utf-8")
    assert main.maybe_load_config(None)["mode"] == "mock"


def test_maybe_load_config_explicit_path(tmp_path: Path) -> None:
    config_path = tmp_path / "agentbreak.yaml"
    config_path.write_text("mode: proxy\nscenario: brownout\n", encoding="utf-8")
    assert main.maybe_load_config(str(config_path))["scenario"] == "brownout"


def test_choose_prefers_cli_value() -> None:
    assert main.choose("cli", "file") == "cli"
    assert main.choose(None, "file") == "file"
    assert main.choose("", "file") == "file"


def test_has_cli_overrides() -> None:
    assert main.has_cli_overrides(mode="mock") is True
    assert main.has_cli_overrides(mode=None, upstream_url="") is False


def test_validate_latency_range() -> None:
    assert main.validate_latency_range(1.0, 2.0) == (1.0, 2.0)


def test_validate_latency_range_rejects_invalid_values() -> None:
    with pytest.raises(Exception):
        main.validate_latency_range(2.0, 1.0)
    with pytest.raises(Exception):
        main.validate_latency_range(-1.0, 1.0)


def test_parse_fault_weights_mapping() -> None:
    assert main.parse_fault_weights_mapping({"500": 0.3, 429: 0.2}) == ((500, 0.3), (429, 0.2))


def test_successful_forwarding(monkeypatch) -> None:
    reset_state()
    DummyAsyncClient.calls = []
    DummyAsyncClient.error = None
    DummyAsyncClient.response = DummyResponse(
        status_code=200,
        content=b'{"id":"cmpl_123","object":"chat.completion"}',
    )
    monkeypatch.setattr(main.httpx, "AsyncClient", DummyAsyncClient)
    client = TestClient(main.app)

    response = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hello"}]},
        headers={"authorization": "Bearer test"},
    )

    assert response.status_code == 200
    assert response.json()["id"] == "cmpl_123"
    assert DummyAsyncClient.calls[0]["url"] == "https://upstream.example/v1/chat/completions"
    assert DummyAsyncClient.calls[0]["headers"]["authorization"] == "Bearer test"
    assert main.stats.upstream_successes == 1


def test_upstream_connection_error_returns_502(monkeypatch) -> None:
    reset_state()
    DummyAsyncClient.calls = []
    DummyAsyncClient.error = main.httpx.ConnectError("boom")
    monkeypatch.setattr(main.httpx, "AsyncClient", DummyAsyncClient)
    client = TestClient(main.app)

    response = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hello"}]},
    )

    assert response.status_code == 502
    assert response.json()["error"]["type"] == "upstream_connection_error"
    assert main.stats.upstream_failures == 1


def test_duplicate_requests_are_counted(monkeypatch) -> None:
    reset_state()
    DummyAsyncClient.calls = []
    DummyAsyncClient.error = None
    DummyAsyncClient.response = DummyResponse()
    monkeypatch.setattr(main.httpx, "AsyncClient", DummyAsyncClient)
    client = TestClient(main.app)
    payload = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hello"}]}

    client.post("/v1/chat/completions", json=payload)
    client.post("/v1/chat/completions", json=payload)
    client.post("/v1/chat/completions", json=payload)

    scorecard = client.get("/_agentbreak/scorecard").json()
    requests = client.get("/_agentbreak/requests").json()

    assert scorecard["duplicate_requests"] == 2
    assert scorecard["suspected_loops"] == 1
    assert len(requests["recent_requests"]) == 3
    assert requests["recent_requests"][-1]["count"] == 3


def test_mock_mode_returns_fake_success() -> None:
    reset_state()
    main.config.mode = "mock"
    main.config.fail_rate = 0.0
    client = TestClient(main.app)

    response = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hello"}]},
    )

    assert response.status_code == 200
    assert response.json()["model"] == "agentbreak-mock"
    assert main.stats.upstream_successes == 1


# ---------------------------------------------------------------------------
# MCP configuration tests
# ---------------------------------------------------------------------------

def test_config_mcp_defaults() -> None:
    cfg = main.Config(upstream_url="https://api.openai.com")
    assert cfg.mcp_mode == "disabled"
    assert cfg.mcp_upstream_transport == "http"
    assert cfg.mcp_upstream_command == ()
    assert cfg.mcp_upstream_url == ""
    assert cfg.mcp_fail_rate == 0.1
    assert cfg.mcp_error_codes == main.DEFAULT_ERROR_CODES


def test_config_mcp_fields() -> None:
    cfg = main.Config(
        upstream_url="",
        mcp_mode="mock",
        mcp_upstream_transport="sse",
        mcp_upstream_url="http://localhost:8080",
        mcp_fail_rate=0.3,
        mcp_error_codes=(429, 500),
    )
    assert cfg.mcp_mode == "mock"
    assert cfg.mcp_upstream_transport == "sse"
    assert cfg.mcp_upstream_url == "http://localhost:8080"
    assert cfg.mcp_fail_rate == 0.3
    assert cfg.mcp_error_codes == (429, 500)


def test_start_mcp_mode_disabled_by_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("mode: mock\n", encoding="utf-8")
    from typer.testing import CliRunner
    runner = CliRunner()
    # Patch uvicorn.run to avoid actually starting a server
    monkeypatch.setattr(main.uvicorn, "run", lambda *a, **kw: None)
    result = runner.invoke(main.cli, ["start", "--config", str(tmp_path / "config.yaml")])
    assert result.exit_code == 0
    assert main.config is not None
    assert main.config.mcp_mode == "disabled"


def test_start_mcp_mode_mock_from_cli(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    from typer.testing import CliRunner
    runner = CliRunner()
    monkeypatch.setattr(main.uvicorn, "run", lambda *a, **kw: None)
    result = runner.invoke(main.cli, ["start", "--mode", "mock", "--mcp-mode", "mock"])
    assert result.exit_code == 0
    assert main.config is not None
    assert main.config.mcp_mode == "mock"


def test_start_mcp_proxy_mode_requires_upstream_url(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    from typer.testing import CliRunner
    runner = CliRunner()
    monkeypatch.setattr(main.uvicorn, "run", lambda *a, **kw: None)
    result = runner.invoke(main.cli, ["start", "--mode", "mock", "--mcp-mode", "proxy", "--mcp-upstream-transport", "http"])
    assert result.exit_code != 0 or "mcp-upstream-url" in (result.output + str(result.exception or ""))


def test_start_mcp_proxy_mode_requires_command_for_stdio(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    from typer.testing import CliRunner
    runner = CliRunner()
    monkeypatch.setattr(main.uvicorn, "run", lambda *a, **kw: None)
    result = runner.invoke(main.cli, ["start", "--mode", "mock", "--mcp-mode", "proxy", "--mcp-upstream-transport", "stdio"])
    assert result.exit_code != 0 or "mcp-upstream-command" in (result.output + str(result.exception or ""))


def test_start_mcp_config_from_yaml(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    config_content = (
        "mode: mock\n"
        "mcp_mode: mock\n"
        "mcp_upstream_transport: sse\n"
        "mcp_upstream_url: http://mcp.example.com\n"
        "mcp_fail_rate: 0.25\n"
        "mcp_error_codes:\n"
        "  - 429\n"
        "  - 503\n"
    )
    (tmp_path / "config.yaml").write_text(config_content, encoding="utf-8")
    from typer.testing import CliRunner
    runner = CliRunner()
    monkeypatch.setattr(main.uvicorn, "run", lambda *a, **kw: None)
    result = runner.invoke(main.cli, ["start", "--config", str(tmp_path / "config.yaml")])
    assert result.exit_code == 0
    assert main.config is not None
    assert main.config.mcp_mode == "mock"
    assert main.config.mcp_upstream_transport == "sse"
    assert main.config.mcp_upstream_url == "http://mcp.example.com"
    assert main.config.mcp_fail_rate == 0.25
    assert main.config.mcp_error_codes == (429, 503)


def test_start_mcp_upstream_command_as_string(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    config_content = (
        "mode: mock\n"
        "mcp_mode: proxy\n"
        "mcp_upstream_transport: stdio\n"
        "mcp_upstream_command: python my_server.py\n"
    )
    (tmp_path / "config.yaml").write_text(config_content, encoding="utf-8")
    from typer.testing import CliRunner
    runner = CliRunner()
    monkeypatch.setattr(main.uvicorn, "run", lambda *a, **kw: None)
    result = runner.invoke(main.cli, ["start", "--config", str(tmp_path / "config.yaml")])
    assert result.exit_code == 0
    assert main.config is not None
    assert main.config.mcp_upstream_command == ("python", "my_server.py")


def test_start_mcp_upstream_command_as_list(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    config_content = (
        "mode: mock\n"
        "mcp_mode: proxy\n"
        "mcp_upstream_transport: stdio\n"
        "mcp_upstream_command:\n"
        "  - python\n"
        "  - my_server.py\n"
    )
    (tmp_path / "config.yaml").write_text(config_content, encoding="utf-8")
    from typer.testing import CliRunner
    runner = CliRunner()
    monkeypatch.setattr(main.uvicorn, "run", lambda *a, **kw: None)
    result = runner.invoke(main.cli, ["start", "--config", str(tmp_path / "config.yaml")])
    assert result.exit_code == 0
    assert main.config is not None
    assert main.config.mcp_upstream_command == ("python", "my_server.py")


def test_start_mcp_mode_invalid_value(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    from typer.testing import CliRunner
    runner = CliRunner()
    monkeypatch.setattr(main.uvicorn, "run", lambda *a, **kw: None)
    result = runner.invoke(main.cli, ["start", "--mode", "mock", "--mcp-mode", "bogus"])
    assert result.exit_code != 0


def test_start_mcp_transport_invalid_value(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    from typer.testing import CliRunner
    runner = CliRunner()
    monkeypatch.setattr(main.uvicorn, "run", lambda *a, **kw: None)
    result = runner.invoke(main.cli, ["start", "--mode", "mock", "--mcp-mode", "mock", "--mcp-upstream-transport", "grpc"])
    assert result.exit_code != 0


def test_proxy_mode_forwards_to_fake_upstream() -> None:
    reset_state()
    server, upstream_url = start_fake_upstream()
    try:
        main.config.mode = "proxy"
        main.config.upstream_url = upstream_url
        main.config.fail_rate = 0.0
        client = TestClient(main.app)

        response = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hello"}]},
            headers={"authorization": "Bearer test"},
        )

        scorecard = client.get("/_agentbreak/scorecard").json()

        assert response.status_code == 200
        assert response.json()["model"] == "fake-upstream"
        assert len(FakeUpstreamHandler.requests) == 1
        assert FakeUpstreamHandler.requests[0]["path"] == "/v1/chat/completions"
        assert FakeUpstreamHandler.requests[0]["body"]["model"] == "gpt-4o-mini"
        assert FakeUpstreamHandler.requests[0]["headers"]["authorization"] == "Bearer test"
        assert scorecard["requests_seen"] == 1
        assert scorecard["upstream_successes"] == 1
        assert scorecard["upstream_failures"] == 0
        assert scorecard["injected_faults"] == 0
    finally:
        server.shutdown()
        server.server_close()
