"""Tests for Phase 4: Multi-service runner and API modules."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentbreak.api.health import setup_health_routes
from agentbreak.api.metrics import setup_metrics_routes
from agentbreak.config.models import (
    AgentBreakConfig,
    FaultConfig,
    LatencyConfig,
    MCPServiceConfig,
    OpenAIServiceConfig,
    ServiceType,
)
from agentbreak.core.statistics import StatisticsTracker
from agentbreak.runner import MultiServiceRunner
from agentbreak.services.mcp import MCPService
from agentbreak.services.openai import OpenAIService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_openai_config(**kwargs) -> OpenAIServiceConfig:
    defaults = dict(
        name="test-openai",
        port=5001,
        mode="mock",
        fault=FaultConfig(enabled=False),
        latency=LatencyConfig(enabled=False),
    )
    defaults.update(kwargs)
    return OpenAIServiceConfig(**defaults)


def _make_mcp_config(**kwargs) -> MCPServiceConfig:
    defaults = dict(
        name="test-mcp",
        port=5002,
        mode="mock",
        fault=FaultConfig(enabled=False),
        latency=LatencyConfig(enabled=False),
    )
    defaults.update(kwargs)
    return MCPServiceConfig(**defaults)


# ---------------------------------------------------------------------------
# api/health.py tests
# ---------------------------------------------------------------------------


def test_setup_health_routes_returns_ok():
    app = FastAPI()
    setup_health_routes(app, "my-service")
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["service"] == "my-service"


def test_setup_health_routes_service_name():
    app = FastAPI()
    setup_health_routes(app, "custom-name")
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.json()["service"] == "custom-name"


# ---------------------------------------------------------------------------
# api/metrics.py tests
# ---------------------------------------------------------------------------


def test_setup_metrics_routes_scorecard():
    app = FastAPI()
    stats = StatisticsTracker()
    setup_metrics_routes(app, "svc", stats)
    client = TestClient(app)
    resp = client.get("/_agentbreak/svc/scorecard")
    assert resp.status_code == 200
    data = resp.json()
    assert "requests_seen" in data
    assert "run_outcome" in data


def test_setup_metrics_routes_requests():
    app = FastAPI()
    stats = StatisticsTracker()
    setup_metrics_routes(app, "svc", stats)
    client = TestClient(app)
    resp = client.get("/_agentbreak/svc/requests")
    assert resp.status_code == 200
    assert "recent_requests" in resp.json()


async def test_metrics_routes_reflect_stats():
    app = FastAPI()
    stats = StatisticsTracker()
    setup_metrics_routes(app, "svc", stats)
    # Record some activity
    await stats.record_request("svc", b"req-1", "test")
    await stats.record_success("svc")
    client = TestClient(app)
    resp = client.get("/_agentbreak/svc/scorecard")
    assert resp.json()["requests_seen"] == 1
    assert resp.json()["upstream_successes"] == 1


# ---------------------------------------------------------------------------
# services/base.py uses api module
# ---------------------------------------------------------------------------


def test_base_service_common_routes_via_api_module():
    """Verify that setup_common_routes() uses api module functions."""
    stats = StatisticsTracker()
    cfg = _make_openai_config()
    service = OpenAIService(cfg, stats)
    service.setup_routes()  # calls setup_common_routes internally
    client = TestClient(service.get_app())

    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["service"] == "test-openai"

    resp = client.get("/_agentbreak/test-openai/scorecard")
    assert resp.status_code == 200

    resp = client.get("/_agentbreak/test-openai/requests")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# MultiServiceRunner tests
# ---------------------------------------------------------------------------


def test_runner_create_service_openai():
    cfg = AgentBreakConfig(services=[_make_openai_config()])
    runner = MultiServiceRunner(cfg)
    svc = runner.create_service(cfg.services[0])
    assert isinstance(svc, OpenAIService)


def test_runner_create_service_mcp():
    cfg = AgentBreakConfig(services=[_make_mcp_config()])
    runner = MultiServiceRunner(cfg)
    svc = runner.create_service(cfg.services[0])
    assert isinstance(svc, MCPService)


def test_runner_create_service_unknown_type():
    cfg = AgentBreakConfig(services=[_make_openai_config()])
    runner = MultiServiceRunner(cfg)
    bad_config = MagicMock()
    bad_config.type = "unknown"
    with pytest.raises(ValueError, match="Unknown service type"):
        runner.create_service(bad_config)


def test_runner_shared_stats():
    """All services share the same StatisticsTracker."""
    cfg = AgentBreakConfig(
        services=[
            _make_openai_config(name="openai", port=5001),
            _make_mcp_config(name="mcp", port=5002),
        ]
    )
    runner = MultiServiceRunner(cfg)
    svc_openai = runner.create_service(cfg.services[0])
    svc_mcp = runner.create_service(cfg.services[1])
    assert svc_openai.stats is runner.stats
    assert svc_mcp.stats is runner.stats


def test_runner_stop_calls_cleanup():
    """stop() calls cleanup() on services that have it."""
    cfg = AgentBreakConfig(services=[_make_mcp_config()])
    runner = MultiServiceRunner(cfg)
    svc = MCPService(cfg.services[0], runner.stats)
    svc.setup_routes()
    runner.services["test-mcp"] = svc

    mock_server = MagicMock()
    runner.servers.append(mock_server)

    async def _run():
        await runner.stop()

    asyncio.run(_run())
    assert mock_server.should_exit is True


def test_runner_print_scorecards(capsys):
    cfg = AgentBreakConfig(services=[_make_openai_config(name="my-svc", port=5001)])
    runner = MultiServiceRunner(cfg)

    runner._print_scorecards()

    captured = capsys.readouterr()
    assert "AgentBreak Scorecard: my-svc" in captured.err
    assert "Resilience Score:" in captured.err


def test_runner_creates_services_on_start():
    """start() populates runner.services for all configured services."""
    cfg = AgentBreakConfig(
        services=[
            _make_openai_config(name="openai", port=5001),
            _make_mcp_config(name="mcp", port=5002),
        ]
    )
    runner = MultiServiceRunner(cfg)

    # Patch uvicorn.Server so we don't actually listen
    mock_server = MagicMock()
    mock_server.serve = AsyncMock(side_effect=KeyboardInterrupt)

    with patch("agentbreak.runner.uvicorn.Server", return_value=mock_server):
        with patch("agentbreak.runner.uvicorn.Config"):
            try:
                asyncio.run(runner.start())
            except (KeyboardInterrupt, SystemExit):
                pass

    assert "openai" in runner.services
    assert "mcp" in runner.services
