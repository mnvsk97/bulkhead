"""Unit tests for core proxy components (Phase 2)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentbreak.config.models import FaultConfig, LatencyConfig, ServiceConfig, ServiceType
from agentbreak.core.fault_injection import FaultInjector, FaultResult
from agentbreak.core.latency import LatencyInjector
from agentbreak.core.proxy import BaseProxy, ProxyContext
from agentbreak.core.statistics import ServiceStatistics, StatisticsTracker
from agentbreak.utils.hashing import fingerprint_bytes
from agentbreak.utils.headers import filter_headers
from agentbreak.utils.random import clamp_probability, should_inject


# ---------------------------------------------------------------------------
# ProxyContext
# ---------------------------------------------------------------------------


def test_proxy_context_create() -> None:
    ctx = ProxyContext.create("test-svc", b"hello", method="POST")
    assert ctx.service_name == "test-svc"
    assert ctx.raw_body == b"hello"
    assert ctx.metadata["method"] == "POST"
    assert ctx.request_id  # non-empty string


# ---------------------------------------------------------------------------
# FaultResult
# ---------------------------------------------------------------------------


def test_fault_result_for_code_429() -> None:
    result = FaultResult.for_code(429, service_name="svc")
    assert result.error_code == 429
    assert result.error_type == "rate_limit_error"
    assert "AgentBreak" in result.message
    assert result.metadata["service"] == "svc"


def test_fault_result_for_unknown_code() -> None:
    result = FaultResult.for_code(418)
    assert result.error_type == "unknown_error"


# ---------------------------------------------------------------------------
# FaultInjector
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fault_injector_disabled() -> None:
    config = FaultConfig(enabled=False)
    injector = FaultInjector(config)
    ctx = ProxyContext.create("svc", b"body")
    result = await injector.maybe_inject(ctx)
    assert result is None


@pytest.mark.asyncio
async def test_fault_injector_always_injects() -> None:
    config = FaultConfig(
        enabled=True,
        overall_rate=1.0,
        per_error_rates={},
        available_codes=(500,),
    )
    injector = FaultInjector(config)
    ctx = ProxyContext.create("svc", b"body")
    result = await injector.maybe_inject(ctx)
    assert result is not None
    assert result.error_code == 500


@pytest.mark.asyncio
async def test_fault_injector_never_injects() -> None:
    config = FaultConfig(
        enabled=True,
        overall_rate=0.0,
        per_error_rates={},
        available_codes=(500,),
    )
    injector = FaultInjector(config)
    ctx = ProxyContext.create("svc", b"body")
    # Run many times to be statistically certain
    for _ in range(20):
        result = await injector.maybe_inject(ctx)
        assert result is None


# ---------------------------------------------------------------------------
# LatencyInjector
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_latency_injector_disabled() -> None:
    config = LatencyConfig(enabled=False, probability=1.0, min_seconds=0.0, max_seconds=0.1)
    injector = LatencyInjector(config)
    ctx = ProxyContext.create("svc", b"body")
    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await injector.maybe_delay(ctx)
    assert result is None
    mock_sleep.assert_not_called()


@pytest.mark.asyncio
async def test_latency_injector_zero_probability() -> None:
    config = LatencyConfig(enabled=True, probability=0.0, min_seconds=1.0, max_seconds=2.0)
    injector = LatencyInjector(config)
    ctx = ProxyContext.create("svc", b"body")
    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await injector.maybe_delay(ctx)
    assert result is None
    mock_sleep.assert_not_called()


@pytest.mark.asyncio
async def test_latency_injector_always_injects() -> None:
    config = LatencyConfig(enabled=True, probability=1.0, min_seconds=0.01, max_seconds=0.02)
    injector = LatencyInjector(config)
    ctx = ProxyContext.create("svc", b"body")
    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await injector.maybe_delay(ctx)
    assert result is not None
    assert 0.01 <= result <= 0.02
    assert ctx.metadata.get("latency_injected") == result
    mock_sleep.assert_called_once_with(result)


# ---------------------------------------------------------------------------
# StatisticsTracker
# ---------------------------------------------------------------------------


async def test_statistics_tracker_record_request() -> None:
    tracker = StatisticsTracker()
    await tracker.record_request("svc", b"body", method="POST")
    stats = tracker.get_service_stats("svc")
    assert stats.total_requests == 1
    assert stats.duplicate_requests == 0


async def test_statistics_tracker_detects_duplicates() -> None:
    tracker = StatisticsTracker()
    body = b"same-body"
    await tracker.record_request("svc", body)
    await tracker.record_request("svc", body)
    stats = tracker.get_service_stats("svc")
    assert stats.total_requests == 2
    assert stats.duplicate_requests == 1
    assert stats.suspected_loops == 0


async def test_statistics_tracker_detects_loops() -> None:
    tracker = StatisticsTracker()
    body = b"loop-body"
    for _ in range(3):
        await tracker.record_request("svc", body)
    stats = tracker.get_service_stats("svc")
    assert stats.suspected_loops == 1


async def test_statistics_tracker_record_fault() -> None:
    tracker = StatisticsTracker()
    await tracker.record_fault("svc")
    stats = tracker.get_service_stats("svc")
    assert stats.injected_faults == 1
    assert stats.upstream_failures == 1


async def test_statistics_tracker_record_latency() -> None:
    tracker = StatisticsTracker()
    await tracker.record_latency("svc")
    stats = tracker.get_service_stats("svc")
    assert stats.latency_injections == 1


async def test_statistics_tracker_scorecard_pass() -> None:
    tracker = StatisticsTracker()
    await tracker.record_request("svc", b"body")
    await tracker.record_success("svc")
    card = tracker.generate_scorecard("svc")
    assert card["run_outcome"] == "PASS"
    assert card["resilience_score"] == 100


async def test_statistics_tracker_scorecard_fail() -> None:
    tracker = StatisticsTracker()
    await tracker.record_request("svc", b"body")
    await tracker.record_fault("svc")
    card = tracker.generate_scorecard("svc")
    assert card["run_outcome"] == "FAIL"
    assert card["resilience_score"] < 100


async def test_statistics_tracker_scorecard_degraded() -> None:
    tracker = StatisticsTracker()
    await tracker.record_request("svc", b"a")
    await tracker.record_success("svc")
    await tracker.record_request("svc", b"b")
    await tracker.record_fault("svc")
    card = tracker.generate_scorecard("svc")
    assert card["run_outcome"] == "DEGRADED"


async def test_statistics_tracker_separate_services() -> None:
    tracker = StatisticsTracker()
    await tracker.record_request("svc1", b"body1")
    await tracker.record_request("svc2", b"body2")
    assert tracker.get_service_stats("svc1").total_requests == 1
    assert tracker.get_service_stats("svc2").total_requests == 1


# ---------------------------------------------------------------------------
# Utils
# ---------------------------------------------------------------------------


def test_clamp_probability() -> None:
    assert clamp_probability(-1.0) == 0.0
    assert clamp_probability(2.0) == 1.0
    assert clamp_probability(0.5) == 0.5


def test_should_inject_always() -> None:
    assert should_inject(1.0) is True


def test_should_inject_never() -> None:
    assert should_inject(0.0) is False


def test_fingerprint_bytes_consistent() -> None:
    data = b"hello world"
    assert fingerprint_bytes(data) == fingerprint_bytes(data)
    assert len(fingerprint_bytes(data)) == 64  # SHA-256 hex


def test_fingerprint_bytes_different() -> None:
    assert fingerprint_bytes(b"a") != fingerprint_bytes(b"b")


def test_filter_headers_removes_skip_headers() -> None:
    import httpx

    raw_headers = [
        ("host", "example.com"),
        ("content-length", "42"),
        ("content-type", "application/json"),
        ("authorization", "Bearer token"),
    ]
    headers = httpx.Headers(raw_headers)
    filtered = filter_headers(headers)
    assert "host" not in filtered
    assert "content-length" not in filtered
    assert filtered["content-type"] == "application/json"
    assert filtered["authorization"] == "Bearer token"


# ---------------------------------------------------------------------------
# Transports - basic instantiation and factory
# ---------------------------------------------------------------------------


def test_create_transport_stdio() -> None:
    from agentbreak.transports import create_transport

    t = create_transport("stdio", command=("echo", "hello"))
    from agentbreak.transports.stdio import StdioTransport

    assert isinstance(t, StdioTransport)


def test_create_transport_http() -> None:
    from agentbreak.transports import create_transport

    t = create_transport("http", base_url="http://localhost:8080")
    from agentbreak.transports.http import HTTPTransport

    assert isinstance(t, HTTPTransport)


def test_create_transport_sse() -> None:
    from agentbreak.transports import create_transport

    t = create_transport("sse", base_url="http://localhost:8080")
    from agentbreak.transports.sse import SSETransport

    assert isinstance(t, SSETransport)


def test_create_transport_unknown_raises() -> None:
    from agentbreak.transports import create_transport

    with pytest.raises(ValueError, match="Unknown transport type"):
        create_transport("unknown")


def test_create_transport_stdio_no_command_raises() -> None:
    from agentbreak.transports import create_transport

    with pytest.raises(ValueError, match="command is required"):
        create_transport("stdio")


def test_create_transport_http_no_url_raises() -> None:
    from agentbreak.transports import create_transport

    with pytest.raises(ValueError, match="base_url is required"):
        create_transport("http")


# ---------------------------------------------------------------------------
# mcp_transport shim backward compatibility
# ---------------------------------------------------------------------------


def test_mcp_transport_shim_imports() -> None:
    from agentbreak.mcp_transport import (
        DEFAULT_TRANSPORT_TIMEOUT,
        HTTPTransport,
        MCPTransport,
        SSETransport,
        StdioTransport,
        create_transport,
    )

    assert DEFAULT_TRANSPORT_TIMEOUT == 30.0
    assert issubclass(StdioTransport, MCPTransport)
    assert issubclass(HTTPTransport, MCPTransport)
    assert issubclass(SSETransport, MCPTransport)
    assert callable(create_transport)
