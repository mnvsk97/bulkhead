"""Tests for AuthConfig validation/headers and RunHistory persistence."""

from __future__ import annotations

import base64
import os

import pytest

from agentbreak.config import ApplicationConfig, AuthConfig, MCPRegistry
from agentbreak.history import RunHistory
from agentbreak.scenarios import ScenarioFile


# ---------------------------------------------------------------------------
# AuthConfig validation
# ---------------------------------------------------------------------------


def test_bearer_requires_env_or_token():
    with pytest.raises(Exception):
        AuthConfig(type="bearer")


def test_bearer_with_token():
    a = AuthConfig(type="bearer", token="tok")
    assert a.headers() == {"authorization": "Bearer tok"}


def test_bearer_with_env(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "from-env")
    a = AuthConfig(type="bearer", env="MY_TOKEN")
    assert a.headers() == {"authorization": "Bearer from-env"}


def test_bearer_missing_env_returns_empty():
    a = AuthConfig(type="bearer", env="NONEXISTENT_VAR_XYZ")
    assert a.headers() == {}


def test_basic_requires_username_and_password_env():
    with pytest.raises(Exception):
        AuthConfig(type="basic", username="u")
    with pytest.raises(Exception):
        AuthConfig(type="basic", password_env="P")


def test_basic_headers(monkeypatch):
    monkeypatch.setenv("P", "secret")
    a = AuthConfig(type="basic", username="user", password_env="P")
    h = a.headers()
    assert h["authorization"].startswith("Basic ")
    decoded = base64.b64decode(h["authorization"].split(" ")[1]).decode()
    assert decoded == "user:secret"


def test_basic_missing_password_returns_empty():
    a = AuthConfig(type="basic", username="u", password_env="NONEXISTENT_XYZ")
    assert a.headers() == {}


def test_api_key_requires_header_name():
    with pytest.raises(Exception):
        AuthConfig(type="api_key", env="MY_KEY")


def test_api_key_requires_env_or_token():
    with pytest.raises(Exception):
        AuthConfig(type="api_key", header_name="x-api-key")


def test_api_key_with_token():
    a = AuthConfig(type="api_key", header_name="x-api-key", token="sk-ant-123")
    assert a.headers() == {"x-api-key": "sk-ant-123"}


def test_api_key_with_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    a = AuthConfig(type="api_key", header_name="x-api-key", env="ANTHROPIC_API_KEY")
    assert a.headers() == {"x-api-key": "sk-ant-env"}


def test_api_key_missing_env_returns_empty():
    a = AuthConfig(type="api_key", header_name="x-api-key", env="NONEXISTENT_VAR_XYZ")
    assert a.headers() == {}


def test_api_key_custom_header_name():
    a = AuthConfig(type="api_key", header_name="authorization", token="my-token")
    assert a.headers() == {"authorization": "my-token"}


def test_oauth2_requires_all_fields():
    with pytest.raises(Exception):
        AuthConfig(type="oauth2_client_credentials", token_url="http://x")
    with pytest.raises(Exception):
        AuthConfig(type="oauth2_client_credentials", token_url="http://x", client_id="id")


def test_oauth2_validates_ok():
    a = AuthConfig(
        type="oauth2_client_credentials",
        token_url="http://x/token",
        client_id="id",
        client_secret_env="S",
    )
    assert a.type == "oauth2_client_credentials"


def test_none_auth_returns_empty():
    a = AuthConfig()
    assert a.headers() == {}
    a2 = AuthConfig(type="none")
    assert a2.headers() == {}


# ---------------------------------------------------------------------------
# RunHistory
# ---------------------------------------------------------------------------


def test_save_and_get_run(tmp_path):
    h = RunHistory(db_path=str(tmp_path / "test.db"))
    run_id = h.save_run(
        llm_scorecard={"score": 80},
        mcp_scorecard=None,
        scenarios=[{"name": "test"}],
    )
    assert isinstance(run_id, int)

    run = h.get_run(run_id)
    assert run is not None
    assert run["llm_scorecard"]["score"] == 80
    assert run["mcp_scorecard"] is None
    assert run["scenarios"][0]["name"] == "test"


def test_get_runs_returns_recent_first(tmp_path):
    h = RunHistory(db_path=str(tmp_path / "test.db"))
    id1 = h.save_run(llm_scorecard={"run": 1}, mcp_scorecard=None)
    id2 = h.save_run(llm_scorecard={"run": 2}, mcp_scorecard=None)

    runs = h.get_runs(limit=10)
    assert len(runs) == 2
    assert runs[0]["id"] == id2  # most recent first
    assert runs[1]["id"] == id1


def test_get_runs_respects_limit(tmp_path):
    h = RunHistory(db_path=str(tmp_path / "test.db"))
    for i in range(5):
        h.save_run(llm_scorecard={"run": i}, mcp_scorecard=None)
    assert len(h.get_runs(limit=3)) == 3


def test_get_nonexistent_run_returns_none(tmp_path):
    h = RunHistory(db_path=str(tmp_path / "test.db"))
    assert h.get_run(999) is None


def test_history_creates_db_directory(tmp_path):
    path = tmp_path / "nested" / "dir" / "test.db"
    h = RunHistory(db_path=str(path))
    h.save_run(llm_scorecard=None, mcp_scorecard=None)
    assert path.exists()


# ---------------------------------------------------------------------------
# History API endpoints
# ---------------------------------------------------------------------------


def test_history_endpoint_when_disabled():
    from agentbreak import main

    main.service_state = main.ServiceState(
        application=ApplicationConfig.model_validate(
            {"llm": {"enabled": True, "mode": "mock"}, "mcp": {"enabled": False}}
        ),
        scenarios=ScenarioFile(),
        registry=MCPRegistry(),
        llm_runtime=None,
        mcp_runtime=None,
        history=None,
    )
    from fastapi.testclient import TestClient

    client = TestClient(main.app)
    resp = client.get("/_agentbreak/history")
    assert resp.status_code == 200
    assert resp.json()["runs"] == []


def test_history_endpoint_with_data(tmp_path):
    from agentbreak import main
    from fastapi.testclient import TestClient

    h = RunHistory(db_path=str(tmp_path / "test.db"))
    h.save_run(llm_scorecard={"score": 90}, mcp_scorecard=None)
    main.service_state = main.ServiceState(
        application=ApplicationConfig.model_validate(
            {"llm": {"enabled": True, "mode": "mock"}, "mcp": {"enabled": False}}
        ),
        scenarios=ScenarioFile(),
        registry=MCPRegistry(),
        llm_runtime=None,
        mcp_runtime=None,
        history=h,
    )
    client = TestClient(main.app)
    resp = client.get("/_agentbreak/history")
    assert len(resp.json()["runs"]) == 1

    run_id = resp.json()["runs"][0]["id"]
    detail = client.get(f"/_agentbreak/history/{run_id}")
    assert detail.status_code == 200
    assert detail.json()["llm_scorecard"]["score"] == 90


def test_history_detail_not_found(tmp_path):
    from agentbreak import main
    from fastapi.testclient import TestClient

    h = RunHistory(db_path=str(tmp_path / "test.db"))
    main.service_state = main.ServiceState(
        application=ApplicationConfig.model_validate(
            {"llm": {"enabled": True, "mode": "mock"}, "mcp": {"enabled": False}}
        ),
        scenarios=ScenarioFile(),
        registry=MCPRegistry(),
        llm_runtime=None,
        mcp_runtime=None,
        history=h,
    )
    client = TestClient(main.app)
    resp = client.get("/_agentbreak/history/999")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# ApplicationConfig validation
# ---------------------------------------------------------------------------


def test_proxy_mode_requires_upstream_url():
    with pytest.raises(Exception, match="upstream_url"):
        ApplicationConfig.model_validate(
            {"llm": {"enabled": True, "mode": "proxy"}, "mcp": {"enabled": False}}
        )


def test_mock_mode_no_upstream_required():
    cfg = ApplicationConfig.model_validate(
        {"llm": {"enabled": True, "mode": "mock"}, "mcp": {"enabled": False}}
    )
    assert cfg.llm.upstream_url == ""


def test_history_defaults_to_disabled():
    cfg = ApplicationConfig.model_validate(
        {"llm": {"enabled": False}, "mcp": {"enabled": False}}
    )
    assert cfg.history.enabled is False
    assert cfg.history.db_path == ".agentbreak/history.db"
