"""SSE (Server-Sent Events) transport for MCP."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any

import httpx

from agentbreak.protocols.mcp import MCPRequest
from agentbreak.transports.base import DEFAULT_TRANSPORT_TIMEOUT, MCPTransport

logger = logging.getLogger(__name__)


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
        if self._client is None:
            raise RuntimeError("SSE transport not started")
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
                            except json.JSONDecodeError as exc:
                                print(
                                    f"AgentBreak SSE: malformed JSON from upstream, ignoring message: {exc}",
                                    file=sys.stderr,
                                )
                    elif line == "":
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
        limits = httpx.Limits(
            max_connections=self.max_connections,
            max_keepalive_connections=self.max_keepalive_connections,
        )
        self._client = httpx.AsyncClient(timeout=self.timeout, limits=limits)
        loop = asyncio.get_running_loop()
        self._sse_task = loop.create_task(self._listen_sse())
        for _ in range(50):
            if self._endpoint_url is not None:
                self._started = True
                return
            if self._sse_task is not None and self._sse_task.done() and not self._sse_task.cancelled():
                task_exc = self._sse_task.exception()
                await self._client.aclose()
                self._client = None
                if task_exc is not None:
                    raise RuntimeError(f"SSE upstream connection failed: {task_exc}") from task_exc
                raise RuntimeError("SSE listener task terminated unexpectedly")
            await asyncio.sleep(0.1)
        self._sse_task.cancel()
        try:
            await self._sse_task
        except (asyncio.CancelledError, Exception):
            pass
        self._sse_task = None
        await self._client.aclose()
        self._client = None
        raise RuntimeError(
            "SSE upstream did not send an endpoint URL within 5 seconds"
        )

    async def send_request(self, request: MCPRequest) -> dict[str, Any]:
        if not self._started:
            await self.start()
        if self._endpoint_url is None:
            raise RuntimeError("SSE endpoint URL is not available")
        if self._sse_task is not None and self._sse_task.done():
            raise RuntimeError("SSE listener task has terminated; cannot send requests")
        assert self._client is not None
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request.id] = future
        try:
            await self._client.post(
                self._endpoint_url,
                content=request.to_json_bytes(),
                headers={"Content-Type": "application/json"},
            )
            return await asyncio.wait_for(future, timeout=self.timeout)
        except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
            self._pending.pop(request.id, None)
            raise TimeoutError(
                f"SSE upstream timed out after {self.timeout}s"
            ) from exc
        except httpx.HTTPError as exc:
            self._pending.pop(request.id, None)
            raise RuntimeError(f"SSE upstream HTTP error: {exc}") from exc
        except Exception:
            self._pending.pop(request.id, None)
            raise

    async def stop(self) -> None:
        """Cancel the SSE listener and close the HTTP client."""
        if self._sse_task is not None:
            self._sse_task.cancel()
            result = await asyncio.gather(self._sse_task, return_exceptions=True)
            if isinstance(result[0], Exception):
                logger.warning("Exception during SSE task cancellation: %s", result[0])
            self._sse_task = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._endpoint_url = None
        self._started = False
