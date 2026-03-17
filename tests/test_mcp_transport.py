"""Integration tests for agentbreak/mcp_transport.py.

These tests exercise StdioTransport, SSETransport, HTTPTransport, and the
create_transport factory.  Actual network/subprocess calls are mocked so the
tests run without external dependencies.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from agentbreak.mcp_protocol import MCPRequest
from agentbreak.mcp_transport import (
    DEFAULT_TRANSPORT_TIMEOUT,
    HTTPTransport,
    MCPTransport,
    SSETransport,
    StdioTransport,
    create_transport,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_request(method: str = "tools/list", req_id: int = 1) -> MCPRequest:
    return MCPRequest(id=req_id, method=method, params=None)


# ---------------------------------------------------------------------------
# MCPTransport ABC
# ---------------------------------------------------------------------------

def test_mcp_transport_is_abstract() -> None:
    with pytest.raises(TypeError):
        MCPTransport()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# create_transport factory
# ---------------------------------------------------------------------------

def test_create_transport_stdio() -> None:
    t = create_transport("stdio", command=("python", "server.py"), timeout=10.0)
    assert isinstance(t, StdioTransport)
    assert t.command == ("python", "server.py")
    assert t.timeout == 10.0


def test_create_transport_sse() -> None:
    t = create_transport("sse", base_url="http://localhost:8080", timeout=5.0)
    assert isinstance(t, SSETransport)
    assert t.base_url == "http://localhost:8080"


def test_create_transport_http() -> None:
    t = create_transport("http", base_url="http://localhost:8080")
    assert isinstance(t, HTTPTransport)
    assert t.base_url == "http://localhost:8080"


def test_create_transport_unknown_raises() -> None:
    with pytest.raises(ValueError, match="Unknown transport type"):
        create_transport("grpc")


def test_create_transport_stdio_missing_command_raises() -> None:
    with pytest.raises(ValueError, match="command is required"):
        create_transport("stdio")


def test_create_transport_sse_missing_url_raises() -> None:
    with pytest.raises(ValueError, match="base_url is required"):
        create_transport("sse")


def test_create_transport_http_missing_url_raises() -> None:
    with pytest.raises(ValueError, match="base_url is required"):
        create_transport("http")


# ---------------------------------------------------------------------------
# Default timeout constant
# ---------------------------------------------------------------------------

def test_default_timeout_value() -> None:
    assert DEFAULT_TRANSPORT_TIMEOUT == 30.0


# ---------------------------------------------------------------------------
# StdioTransport
# ---------------------------------------------------------------------------

def test_stdio_transport_empty_command_raises() -> None:
    with pytest.raises(ValueError, match="upstream_command"):
        StdioTransport(command=())


def test_stdio_transport_send_request_success() -> None:
    response = {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}
    response_line = (json.dumps(response) + "\n").encode()

    mock_process = MagicMock()
    mock_process.returncode = None
    mock_process.stdin = AsyncMock()
    mock_process.stdin.write = MagicMock()
    mock_process.stdin.drain = AsyncMock()

    async def fake_readline() -> bytes:
        return response_line

    mock_process.stdout = MagicMock()
    mock_process.stdout.readline = fake_readline

    async def _run() -> None:
        with patch(
            "agentbreak.mcp_transport.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=mock_process),
        ):
            transport = StdioTransport(command=("fake-server",), timeout=5.0)
            req = make_request("tools/list", req_id=1)
            result = await transport.send_request(req)
            assert result["result"] == {"tools": []}

    asyncio.run(_run())


def test_stdio_transport_timeout_raises() -> None:
    mock_process = MagicMock()
    mock_process.returncode = None
    mock_process.stdin = AsyncMock()
    mock_process.stdin.write = MagicMock()
    mock_process.stdin.drain = AsyncMock()

    async def slow_readline() -> bytes:
        await asyncio.sleep(100)
        return b""

    mock_process.stdout = MagicMock()
    mock_process.stdout.readline = slow_readline

    async def _run() -> None:
        with patch(
            "agentbreak.mcp_transport.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=mock_process),
        ):
            transport = StdioTransport(command=("fake-server",), timeout=0.01)
            req = make_request("tools/list", req_id=2)
            with pytest.raises(TimeoutError, match="timed out"):
                await transport.send_request(req)

    asyncio.run(_run())


def test_stdio_transport_reconnects_on_empty_response() -> None:
    """Transport should restart the process when stdout returns empty bytes."""
    response = {"jsonrpc": "2.0", "id": 1, "result": {}}
    response_line = (json.dumps(response) + "\n").encode()
    call_count = 0

    async def readline_with_restart() -> bytes:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return b""  # first process exits immediately
        return response_line

    mock_process = MagicMock()
    mock_process.returncode = None
    mock_process.stdin = AsyncMock()
    mock_process.stdin.write = MagicMock()
    mock_process.stdin.drain = AsyncMock()
    mock_process.stdout = MagicMock()
    mock_process.stdout.readline = readline_with_restart

    async def _run() -> None:
        with patch(
            "agentbreak.mcp_transport.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=mock_process),
        ):
            transport = StdioTransport(command=("fake-server",), timeout=5.0)
            req = make_request("initialize", req_id=1)
            result = await transport.send_request(req)
            assert "result" in result

    asyncio.run(_run())


def test_stdio_transport_stop() -> None:
    mock_process = MagicMock()
    mock_process.stdin = MagicMock()
    mock_process.stdin.close = MagicMock()
    mock_process.wait = AsyncMock(return_value=0)

    async def _run() -> None:
        transport = StdioTransport(command=("fake-server",), timeout=5.0)
        transport._process = mock_process
        transport._started = True
        await transport.stop()
        assert transport._process is None
        assert not transport._started

    asyncio.run(_run())


def test_stdio_transport_start_creates_process() -> None:
    mock_process = MagicMock()
    mock_process.returncode = None

    async def _run() -> None:
        with patch(
            "agentbreak.mcp_transport.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=mock_process),
        ) as mock_exec:
            transport = StdioTransport(command=("my-server", "--flag"), timeout=5.0)
            await transport.start()
            mock_exec.assert_called_once_with(
                "my-server", "--flag",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            assert transport._started

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# SSETransport
# ---------------------------------------------------------------------------

def test_sse_transport_strips_trailing_slash() -> None:
    t = SSETransport(base_url="http://localhost:9000/", timeout=5.0)
    assert t.base_url == "http://localhost:9000"


def test_sse_transport_start_resolves_endpoint() -> None:
    endpoint_data = ["event: endpoint", "data: /messages", ""]

    class FakeStream:
        async def __aenter__(self) -> "FakeStream":
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def aiter_lines(self) -> Any:
            for line in endpoint_data:
                yield line

    mock_client = MagicMock()
    mock_client.stream.return_value = FakeStream()
    mock_client.aclose = AsyncMock()

    async def _run() -> None:
        with patch("agentbreak.mcp_transport.httpx.AsyncClient", return_value=mock_client):
            transport = SSETransport(base_url="http://localhost:9000", timeout=5.0)
            await transport.start()
            assert transport._endpoint_url == "http://localhost:9000/messages"
            await transport.stop()

    asyncio.run(_run())


def test_sse_transport_stop_cleans_up() -> None:
    async def _run() -> None:
        transport = SSETransport(base_url="http://localhost:9000", timeout=5.0)
        transport._started = True
        transport._endpoint_url = "http://localhost:9000/messages"
        mock_client = MagicMock()
        mock_client.aclose = AsyncMock()
        transport._client = mock_client
        mock_task = MagicMock()
        mock_task.cancel = MagicMock()
        transport._sse_task = mock_task

        await transport.stop()

        mock_task.cancel.assert_called_once()
        mock_client.aclose.assert_called_once()
        assert transport._client is None
        assert transport._endpoint_url is None
        assert not transport._started

    asyncio.run(_run())


def test_sse_transport_send_request_without_start_raises() -> None:
    """send_request raises if endpoint URL is still None after start."""
    async def _run() -> None:
        transport = SSETransport(base_url="http://localhost:9000", timeout=5.0)
        # Manually mark as started but leave endpoint_url as None.
        transport._started = True
        transport._endpoint_url = None
        req = make_request("tools/list", req_id=1)
        with pytest.raises(RuntimeError, match="endpoint URL is not available"):
            await transport.send_request(req)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# HTTPTransport
# ---------------------------------------------------------------------------

def test_http_transport_strips_trailing_slash() -> None:
    t = HTTPTransport(base_url="http://upstream:8080/", timeout=5.0)
    assert t.base_url == "http://upstream:8080"


def test_http_transport_send_request_success() -> None:
    response_data = {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}
    mock_response = MagicMock()
    mock_response.json.return_value = response_data

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.aclose = AsyncMock()

    async def _run() -> None:
        with patch("agentbreak.mcp_transport.httpx.AsyncClient", return_value=mock_client):
            transport = HTTPTransport(base_url="http://upstream:8080", timeout=5.0)
            req = make_request("tools/list", req_id=1)
            result = await transport.send_request(req)
            assert result["result"] == {"tools": []}
            call_args = mock_client.post.call_args
            assert call_args[0][0] == "http://upstream:8080/mcp"

    asyncio.run(_run())


def test_http_transport_connection_error_raises_runtime_error() -> None:
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(
        side_effect=httpx.ConnectError("connection refused")
    )
    mock_client.aclose = AsyncMock()

    async def _run() -> None:
        with patch("agentbreak.mcp_transport.httpx.AsyncClient", return_value=mock_client):
            transport = HTTPTransport(base_url="http://upstream:8080", timeout=5.0)
            req = make_request("tools/list", req_id=1)
            with pytest.raises(RuntimeError, match="HTTP upstream error"):
                await transport.send_request(req)

    asyncio.run(_run())


def test_http_transport_non_json_response_raises() -> None:
    mock_response = MagicMock()
    mock_response.json.side_effect = Exception("not json")

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.aclose = AsyncMock()

    async def _run() -> None:
        with patch("agentbreak.mcp_transport.httpx.AsyncClient", return_value=mock_client):
            transport = HTTPTransport(base_url="http://upstream:8080", timeout=5.0)
            req = make_request("tools/list", req_id=1)
            with pytest.raises(RuntimeError, match="non-JSON"):
                await transport.send_request(req)

    asyncio.run(_run())


def test_http_transport_extra_headers_sent() -> None:
    response_data = {"jsonrpc": "2.0", "id": 1, "result": {}}
    mock_response = MagicMock()
    mock_response.json.return_value = response_data

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.aclose = AsyncMock()

    async def _run() -> None:
        with patch("agentbreak.mcp_transport.httpx.AsyncClient", return_value=mock_client):
            transport = HTTPTransport(
                base_url="http://upstream:8080",
                timeout=5.0,
                extra_headers={"Authorization": "Bearer token123"},
            )
            req = make_request("initialize", req_id=1)
            await transport.send_request(req)
            _, kwargs = mock_client.post.call_args
            assert kwargs["headers"]["Authorization"] == "Bearer token123"
            assert kwargs["headers"]["Content-Type"] == "application/json"

    asyncio.run(_run())


def test_http_transport_stop() -> None:
    mock_client = AsyncMock()
    mock_client.aclose = AsyncMock()

    async def _run() -> None:
        with patch("agentbreak.mcp_transport.httpx.AsyncClient", return_value=mock_client):
            transport = HTTPTransport(base_url="http://upstream:8080", timeout=5.0)
            await transport.start()
            assert transport._started
            await transport.stop()
            mock_client.aclose.assert_called_once()
            assert transport._client is None
            assert not transport._started

    asyncio.run(_run())


def test_http_transport_lazy_start() -> None:
    """send_request should start the transport if not yet started."""
    response_data = {"jsonrpc": "2.0", "id": 1, "result": {}}
    mock_response = MagicMock()
    mock_response.json.return_value = response_data

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.aclose = AsyncMock()

    async def _run() -> None:
        with patch("agentbreak.mcp_transport.httpx.AsyncClient", return_value=mock_client):
            transport = HTTPTransport(base_url="http://upstream:8080", timeout=5.0)
            assert not transport._started
            req = make_request("tools/list", req_id=1)
            await transport.send_request(req)
            assert transport._started

    asyncio.run(_run())
