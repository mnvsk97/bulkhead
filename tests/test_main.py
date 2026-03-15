from __future__ import annotations

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
