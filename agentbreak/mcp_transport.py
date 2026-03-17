"""MCP transport layer abstraction.

Provides a common interface for communicating with upstream MCP servers over
different transport mechanisms: stdio subprocess, SSE (Server-Sent Events), and
plain HTTP.
"""
from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from typing import Any

import httpx

from agentbreak.mcp_protocol import MCPRequest

# Default timeout for upstream requests (seconds).
DEFAULT_TRANSPORT_TIMEOUT = 30.0


class MCPTransport(ABC):
    """Abstract base class for MCP transport implementations.

    Subclasses must implement `send_request`, `start`, and `stop`.
    The lifecycle is:
        1. Optionally call `start()` to initialise resources.
        2. Call `send_request()` one or more times.
        3. Call `stop()` to release resources.

    `send_request` may call `start()` lazily if the transport has not yet been
    started, so explicit `start()` calls are optional in simple scenarios.
    """

    @abstractmethod
    async def send_request(self, request: MCPRequest) -> dict[str, Any]:
        """Send an MCP request and return the raw JSON-RPC response dict.

        Raises:
            TimeoutError: If the upstream does not respond in time.
            RuntimeError: If the connection is broken or unusable.
            OSError: If a low-level I/O error occurs.
        """

    @abstractmethod
    async def start(self) -> None:
        """Initialise any long-lived resources (connections, subprocesses, etc.)."""

    @abstractmethod
    async def stop(self) -> None:
        """Release all resources held by this transport."""


class StdioTransport(MCPTransport):
    """Transport that communicates with an MCP server via a stdio subprocess.

    The subprocess is started on first use and restarted automatically if it
    terminates unexpectedly (one restart attempt per request).  All requests are
    serialised through an asyncio.Lock because stdio is a single-channel pipe.
    """

    def __init__(
        self,
        command: tuple[str, ...],
        timeout: float = DEFAULT_TRANSPORT_TIMEOUT,
    ) -> None:
        if not command:
            raise ValueError("upstream_command must not be empty for stdio transport")
        self.command = command
        self.timeout = timeout
        self._process: asyncio.subprocess.Process | None = None
        self._lock: asyncio.Lock | None = None
        self._started = False

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def _ensure_process(self) -> asyncio.subprocess.Process:
        if self._process is None or self._process.returncode is not None:
            self._process = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        return self._process

    async def start(self) -> None:
        """Pre-start the subprocess (optional; happens lazily on first request)."""
        if not self._started:
            self._started = True
            await self._ensure_process()

    async def send_request(self, request: MCPRequest) -> dict[str, Any]:
        async with self._get_lock():
            for attempt in range(2):
                process = await self._ensure_process()
                assert process.stdin is not None and process.stdout is not None
                line = request.to_json_bytes().decode("utf-8") + "\n"
                try:
                    process.stdin.write(line.encode())
                    await process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    self._process = None
                    if attempt == 0:
                        continue
                    raise RuntimeError(
                        "Stdio upstream closed the connection unexpectedly"
                    )
                try:
                    response_line = await asyncio.wait_for(
                        process.stdout.readline(),
                        timeout=self.timeout,
                    )
                except asyncio.TimeoutError as exc:
                    raise TimeoutError(
                        f"Stdio upstream timed out after {self.timeout}s"
                    ) from exc
                if not response_line:
                    # Process exited; reset and retry once with a fresh process.
                    self._process = None
                    if attempt == 0:
                        continue
                    raise RuntimeError(
                        "Stdio upstream closed the connection unexpectedly"
                    )
                return json.loads(response_line.decode().strip())
            raise RuntimeError("Stdio upstream failed after restart attempt")

    async def stop(self) -> None:
        """Terminate the subprocess and clean up."""
        if self._process is not None:
            try:
                if self._process.stdin:
                    self._process.stdin.close()
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except Exception:
                self._process.kill()
            self._process = None
        self._started = False


class SSETransport(MCPTransport):
    """Transport that communicates with an MCP server via Server-Sent Events.

    MCP SSE servers expose two endpoints:
    - GET /sse  — long-lived stream where the server pushes events.
      The first event is ``event: endpoint`` containing the POST URL.
    - POST <endpoint_url>  — where the client sends JSON-RPC requests.
      Responses arrive as ``event: message`` SSE events on the /sse stream.
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = DEFAULT_TRANSPORT_TIMEOUT,
        max_connections: int = 10,
        max_keepalive_connections: int = 5,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_connections = max_connections
        self.max_keepalive_connections = max_keepalive_connections
        self._endpoint_url: str | None = None
        self._pending: dict[str | int | None, asyncio.Future[dict[str, Any]]] = {}
        self._client: httpx.AsyncClient | None = None
        self._sse_task: asyncio.Task[None] | None = None
        self._started = False

    async def _listen_sse(self) -> None:
        """Background task: reads SSE events and resolves pending futures."""
        assert self._client is not None
        event_type = ""
        try:
            async with self._client.stream("GET", f"{self.base_url}/sse") as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("event:"):
                        event_type = line[len("event:"):].strip()
                    elif line.startswith("data:"):
                        data = line[len("data:"):].strip()
                        if event_type == "endpoint":
                            if data.startswith("http"):
                                self._endpoint_url = data
                            else:
                                self._endpoint_url = f"{self.base_url}{data}"
                        elif event_type == "message":
                            try:
                                msg = json.loads(data)
                                req_id = msg.get("id")
                                future = self._pending.pop(req_id, None)
                                if future is not None and not future.done():
                                    future.set_result(msg)
                            except json.JSONDecodeError:
                                pass
                        event_type = ""
        except Exception as exc:
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(exc)
            self._pending.clear()

    async def start(self) -> None:
        """Connect to the SSE stream and wait for the endpoint URL."""
        if self._started:
            return
        self._started = True
        limits = httpx.Limits(
            max_connections=self.max_connections,
            max_keepalive_connections=self.max_keepalive_connections,
        )
        self._client = httpx.AsyncClient(timeout=self.timeout, limits=limits)
        loop = asyncio.get_event_loop()
        self._sse_task = loop.create_task(self._listen_sse())
        # Wait up to 5 seconds for the server to send the endpoint URL.
        for _ in range(50):
            if self._endpoint_url is not None:
                return
            await asyncio.sleep(0.1)
        raise RuntimeError(
            "SSE upstream did not send an endpoint URL within 5 seconds"
        )

    async def send_request(self, request: MCPRequest) -> dict[str, Any]:
        if not self._started:
            await self.start()
        if self._endpoint_url is None:
            raise RuntimeError("SSE endpoint URL is not available")
        assert self._client is not None
        loop = asyncio.get_event_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request.id] = future
        try:
            await self._client.post(
                self._endpoint_url,
                content=request.to_json_bytes(),
                headers={"Content-Type": "application/json"},
            )
            return await asyncio.wait_for(future, timeout=self.timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(request.id, None)
            raise TimeoutError(
                f"SSE upstream timed out after {self.timeout}s"
            ) from exc

    async def stop(self) -> None:
        """Cancel the SSE listener and close the HTTP client."""
        if self._sse_task is not None:
            self._sse_task.cancel()
            self._sse_task = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._endpoint_url = None
        self._started = False


class HTTPTransport(MCPTransport):
    """Transport that forwards MCP requests to an upstream HTTP server.

    Maintains a persistent httpx.AsyncClient for connection reuse with a
    configurable connection pool.  The client is created on `start()` (or
    lazily on first `send_request()`) and closed on `stop()`.
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = DEFAULT_TRANSPORT_TIMEOUT,
        extra_headers: dict[str, str] | None = None,
        max_connections: int = 10,
        max_keepalive_connections: int = 5,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.extra_headers = extra_headers or {}
        self.max_connections = max_connections
        self.max_keepalive_connections = max_keepalive_connections
        self._client: httpx.AsyncClient | None = None
        self._started = False

    async def start(self) -> None:
        """Create the HTTP client (optional; happens lazily on first request)."""
        if not self._started:
            self._started = True
            limits = httpx.Limits(
                max_connections=self.max_connections,
                max_keepalive_connections=self.max_keepalive_connections,
            )
            self._client = httpx.AsyncClient(timeout=self.timeout, limits=limits)

    async def send_request(self, request: MCPRequest) -> dict[str, Any]:
        if not self._started:
            await self.start()
        assert self._client is not None
        headers = {"Content-Type": "application/json", **self.extra_headers}
        body = request.to_json_bytes()
        try:
            response = await self._client.post(
                f"{self.base_url}/mcp",
                content=body,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"HTTP upstream error: {exc}"
            ) from exc
        try:
            return response.json()
        except Exception as exc:
            raise RuntimeError(
                "HTTP upstream returned non-JSON response"
            ) from exc

    async def stop(self) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._started = False


def create_transport(
    transport_type: str,
    *,
    base_url: str = "",
    command: tuple[str, ...] = (),
    timeout: float = DEFAULT_TRANSPORT_TIMEOUT,
    extra_headers: dict[str, str] | None = None,
    max_connections: int = 10,
    max_keepalive_connections: int = 5,
) -> MCPTransport:
    """Factory function to create a transport by type name.

    Args:
        transport_type: One of "stdio", "sse", "http".
        base_url: Required for "sse" and "http" transports.
        command: Required for "stdio" transport.
        timeout: Request/connection timeout in seconds.
        extra_headers: Optional extra headers for HTTP/SSE transports.
        max_connections: Maximum number of connections in the pool (http/sse).
        max_keepalive_connections: Maximum number of idle keep-alive connections (http/sse).

    Returns:
        An MCPTransport instance ready for use.

    Raises:
        ValueError: If transport_type is unknown or required args are missing.
    """
    if transport_type == "stdio":
        if not command:
            raise ValueError("command is required for stdio transport")
        return StdioTransport(command=command, timeout=timeout)
    if transport_type == "sse":
        if not base_url:
            raise ValueError("base_url is required for sse transport")
        return SSETransport(
            base_url=base_url,
            timeout=timeout,
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
        )
    if transport_type == "http":
        if not base_url:
            raise ValueError("base_url is required for http transport")
        return HTTPTransport(
            base_url=base_url,
            timeout=timeout,
            extra_headers=extra_headers,
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
        )
    raise ValueError(
        f"Unknown transport type '{transport_type}'. Must be one of: stdio, sse, http"
    )
