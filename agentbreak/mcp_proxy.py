from __future__ import annotations

import asyncio
import json
import random
import signal
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import httpx
import typer
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agentbreak.mcp_protocol import (
    INTERNAL_ERROR,
    INVALID_REQUEST,
    MCP_TOOL_ERROR,
    METHOD_NOT_FOUND,
    MCPError,
    MCPRequest,
    MCPResponse,
    fingerprint_mcp_request,
)

PORT = 5001
cli = typer.Typer(add_completion=False, help="MCP JSON-RPC 2.0 proxy with fault injection.")

# Default timeout for upstream requests (seconds).
DEFAULT_UPSTREAM_TIMEOUT = 30.0

# Supported "HTTP-style" fault codes that map to MCP error codes.
# These mirror the OpenAI proxy codes so MCP scenarios can reuse the same config.
SUPPORTED_FAULT_CODES = (400, 401, 403, 404, 413, 429, 500, 503)
DEFAULT_FAULT_CODES = (429, 500, 503)

# Map HTTP-style codes -> (mcp_error_code, message)
_HTTP_TO_MCP: dict[int, tuple[int, str]] = {
    400: (INVALID_REQUEST, "Invalid request injected by AgentBreak."),
    401: (INTERNAL_ERROR, "Authentication failure injected by AgentBreak."),
    403: (INTERNAL_ERROR, "Permission failure injected by AgentBreak."),
    404: (METHOD_NOT_FOUND, "Resource not found injected by AgentBreak."),
    413: (INVALID_REQUEST, "Request too large injected by AgentBreak."),
    429: (MCP_TOOL_ERROR, "Rate limit exceeded by AgentBreak fault injection."),
    500: (INTERNAL_ERROR, "Upstream failure injected by AgentBreak."),
    503: (INTERNAL_ERROR, "Service unavailable injected by AgentBreak."),
}


_DEFAULT_MOCK_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "echo",
        "description": "Echo back the input text.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Text to echo"}},
            "required": ["text"],
        },
    },
    {
        "name": "get_time",
        "description": "Return the current UTC time.",
        "inputSchema": {"type": "object", "properties": {}},
    },
)

_DEFAULT_MOCK_RESOURCES: tuple[dict[str, Any], ...] = (
    {
        "uri": "file:///example/readme.txt",
        "name": "README",
        "description": "Example README file.",
        "mimeType": "text/plain",
    },
    {
        "uri": "file:///example/data.json",
        "name": "Data",
        "description": "Example data file.",
        "mimeType": "application/json",
    },
)

_DEFAULT_MOCK_PROMPTS: tuple[dict[str, Any], ...] = (
    {
        "name": "summarize",
        "description": "Summarize a piece of text.",
        "arguments": [
            {"name": "text", "description": "Text to summarize.", "required": True}
        ],
    },
)


@dataclass
class MCPConfig:
    mode: str = "proxy"
    upstream_url: str = ""
    upstream_transport: str = "http"  # http, stdio, sse
    upstream_command: tuple[str, ...] = ()  # for stdio transport
    upstream_timeout: float = DEFAULT_UPSTREAM_TIMEOUT
    fail_rate: float = 0.1
    fault_codes: tuple[int, ...] = DEFAULT_FAULT_CODES
    latency_p: float = 0.0
    latency_min: float = 5.0
    latency_max: float = 15.0
    seed: int | None = None
    mock_tools: tuple[dict[str, Any], ...] = _DEFAULT_MOCK_TOOLS
    mock_resources: tuple[dict[str, Any], ...] = _DEFAULT_MOCK_RESOURCES
    mock_prompts: tuple[dict[str, Any], ...] = _DEFAULT_MOCK_PROMPTS


@dataclass
class MCPStats:
    total_requests: int = 0
    injected_faults: int = 0
    latency_injections: int = 0
    upstream_successes: int = 0
    upstream_failures: int = 0
    duplicate_requests: int = 0
    suspected_loops: int = 0
    tool_calls: int = 0
    resource_reads: int = 0
    init_requests: int = 0
    # Per-method call counts (e.g. {"tools/call": 5, "initialize": 1})
    method_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    # Per-tool-name success and failure counts
    tool_successes_by_name: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    tool_failures_by_name: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    # Per-URI resource read success and failure counts
    resource_reads_by_uri: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    resource_failures_by_uri: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    seen_fingerprints: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    recent_requests: list[dict[str, Any]] = field(default_factory=list)


class StdioTransportManager:
    """Manages a persistent subprocess for stdio-based MCP communication.

    The subprocess is started on first use and restarted if it terminates
    unexpectedly.  All requests are serialised through an asyncio.Lock so
    that only one in-flight JSON-RPC exchange occurs at a time (stdio is
    inherently single-channel).
    """

    def __init__(self, command: tuple[str, ...], timeout: float = DEFAULT_UPSTREAM_TIMEOUT) -> None:
        if not command:
            raise ValueError("upstream_command must not be empty for stdio transport")
        self.command = command
        self.timeout = timeout
        self._process: asyncio.subprocess.Process | None = None
        self._lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def _ensure_started(self) -> asyncio.subprocess.Process:
        if self._process is None or self._process.returncode is not None:
            self._process = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        return self._process

    async def send_request(self, request: MCPRequest) -> dict[str, Any]:
        async with self._get_lock():
            for attempt in range(2):
                process = await self._ensure_started()
                assert process.stdin is not None and process.stdout is not None
                line = json.dumps(request.to_dict()) + "\n"
                try:
                    process.stdin.write(line.encode())
                    await process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    self._process = None
                    if attempt == 0:
                        continue
                    raise RuntimeError("Stdio upstream closed the connection unexpectedly")
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
                    raise RuntimeError("Stdio upstream closed the connection unexpectedly")
                return json.loads(response_line.decode().strip())
            raise RuntimeError("Stdio upstream failed after restart attempt")

    async def stop(self) -> None:
        if self._process is not None:
            try:
                if self._process.stdin:
                    self._process.stdin.close()
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except Exception:
                self._process.kill()
            self._process = None


class SSETransportManager:
    """Manages an SSE (Server-Sent Events) connection to an MCP server.

    MCP SSE servers expose two endpoints:
    - GET /sse  — long-lived stream where the server pushes events.
      The first event is ``event: endpoint`` with the URL to POST messages to.
    - POST <endpoint_url>  — where the client sends JSON-RPC requests.
      Responses arrive as ``event: message`` SSE events on the /sse stream.
    """

    def __init__(self, base_url: str, timeout: float = DEFAULT_UPSTREAM_TIMEOUT) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._endpoint_url: str | None = None
        self._pending: dict[str | int | None, asyncio.Future[dict[str, Any]]] = {}
        self._client: httpx.AsyncClient | None = None
        self._sse_task: asyncio.Task[None] | None = None
        self._started = False
        self._lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def _listen_sse(self) -> None:
        """Background task: reads SSE events and resolves pending request futures."""
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
        self._client = httpx.AsyncClient(timeout=self.timeout)
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
            self._started = True
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
                content=json.dumps(request.to_dict()).encode(),
                headers={"Content-Type": "application/json"},
            )
            return await asyncio.wait_for(future, timeout=self.timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(request.id, None)
            raise TimeoutError(
                f"SSE upstream timed out after {self.timeout}s"
            ) from exc

    async def stop(self) -> None:
        if self._sse_task is not None:
            self._sse_task.cancel()
            self._sse_task = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._endpoint_url = None
        self._started = False


mcp_config: MCPConfig | None = None
mcp_stats = MCPStats()
# Transport managers are created per run and cleaned up on shutdown.
_stdio_transport: StdioTransportManager | None = None
_sse_transport: SSETransportManager | None = None
app = FastAPI(title="agentbreak-mcp")


def clamp_probability(value: float) -> float:
    return max(0.0, min(1.0, value))


def should_inject(probability: float) -> bool:
    return random.random() < clamp_probability(probability)


def pick_mcp_error() -> MCPError:
    assert mcp_config is not None
    http_code = random.choice(mcp_config.fault_codes)
    mcp_code, message = _HTTP_TO_MCP[http_code]
    return MCPError(code=mcp_code, message=message)


def mcp_error_response(request_id: str | int | None, error: MCPError) -> dict[str, Any]:
    return MCPResponse(id=request_id, error=error).to_dict()


def record_mcp_request(mcp_req: MCPRequest, raw_body: bytes) -> None:
    mcp_stats.total_requests += 1
    mcp_stats.method_counts[mcp_req.method] += 1

    fp = fingerprint_mcp_request(mcp_req)
    mcp_stats.seen_fingerprints[fp] += 1
    seen = mcp_stats.seen_fingerprints[fp]
    if seen > 1:
        mcp_stats.duplicate_requests += 1
    if seen > 2:
        mcp_stats.suspected_loops += 1

    if mcp_req.method == "tools/call":
        mcp_stats.tool_calls += 1
    elif mcp_req.method in ("resources/read", "resources/list"):
        mcp_stats.resource_reads += 1
    elif mcp_req.method == "initialize":
        mcp_stats.init_requests += 1

    try:
        payload: Any = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {"raw": raw_body.decode("utf-8", errors="replace")}

    mcp_stats.recent_requests.append({
        "fingerprint": fp,
        "count": seen,
        "method": mcp_req.method,
        "body": payload,
    })
    if len(mcp_stats.recent_requests) > 20:
        mcp_stats.recent_requests.pop(0)


async def maybe_delay() -> None:
    assert mcp_config is not None
    if not should_inject(mcp_config.latency_p):
        return
    mcp_stats.latency_injections += 1
    delay = random.uniform(mcp_config.latency_min, mcp_config.latency_max)
    await asyncio.sleep(delay)


def generate_mock_result(method: str, params: dict[str, Any] | None, config: MCPConfig) -> dict[str, Any]:
    """Return a realistic stub result for the given MCP method."""
    if method == "initialize":
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
            "serverInfo": {"name": "agentbreak-mock", "version": "1.0.0"},
        }
    if method == "tools/list":
        return {"tools": list(config.mock_tools)}
    if method == "tools/call":
        tool_name = (params or {}).get("name", "unknown")
        return {
            "content": [{"type": "text", "text": f"Mock result for tool: {tool_name}"}],
            "isError": False,
        }
    if method == "resources/list":
        return {"resources": list(config.mock_resources)}
    if method == "resources/read":
        uri = (params or {}).get("uri", "")
        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": "text/plain",
                    "text": f"Mock content for resource: {uri}",
                }
            ]
        }
    if method == "prompts/list":
        return {"prompts": list(config.mock_prompts)}
    if method == "prompts/get":
        name = (params or {}).get("name", "unknown")
        return {
            "description": f"Mock prompt: {name}",
            "messages": [
                {
                    "role": "user",
                    "content": {"type": "text", "text": f"Mock prompt template for: {name}"},
                }
            ],
        }
    # Catch-all for unknown methods
    return {}


def filter_headers(headers: httpx.Headers) -> dict[str, str]:
    skip = {"host", "content-length"}
    return {key: value for key, value in headers.items() if key.lower() not in skip}


def _is_success_response(response: JSONResponse) -> bool:
    """Return True if the JSON-RPC response body does not contain an 'error' field."""
    try:
        data = json.loads(response.body)
        return "error" not in data
    except Exception:
        return False


def _record_method_outcome(mcp_req: MCPRequest, is_success: bool) -> None:
    """Update per-tool and per-URI success/failure counters."""
    if mcp_req.method == "tools/call":
        tool_name = (mcp_req.params or {}).get("name", "unknown")
        if is_success:
            mcp_stats.tool_successes_by_name[tool_name] += 1
        else:
            mcp_stats.tool_failures_by_name[tool_name] += 1
    elif mcp_req.method == "resources/read":
        uri = (mcp_req.params or {}).get("uri", "unknown")
        if is_success:
            mcp_stats.resource_reads_by_uri[uri] += 1
        else:
            mcp_stats.resource_failures_by_uri[uri] += 1


def scorecard_data() -> dict[str, Any]:
    score = 100
    score -= mcp_stats.injected_faults * 3
    score -= mcp_stats.upstream_failures * 12
    score -= mcp_stats.duplicate_requests * 2
    score -= mcp_stats.suspected_loops * 10
    score = max(0, min(100, score))
    if mcp_stats.upstream_failures == 0 and mcp_stats.suspected_loops == 0:
        outcome = "PASS"
    elif mcp_stats.upstream_successes > 0:
        outcome = "DEGRADED"
    else:
        outcome = "FAIL"
    return {
        "requests_seen": mcp_stats.total_requests,
        "injected_faults": mcp_stats.injected_faults,
        "latency_injections": mcp_stats.latency_injections,
        "upstream_successes": mcp_stats.upstream_successes,
        "upstream_failures": mcp_stats.upstream_failures,
        "duplicate_requests": mcp_stats.duplicate_requests,
        "suspected_loops": mcp_stats.suspected_loops,
        "tool_calls": mcp_stats.tool_calls,
        "resource_reads": mcp_stats.resource_reads,
        "init_requests": mcp_stats.init_requests,
        "method_counts": dict(mcp_stats.method_counts),
        "tool_successes_by_name": dict(mcp_stats.tool_successes_by_name),
        "tool_failures_by_name": dict(mcp_stats.tool_failures_by_name),
        "resource_reads_by_uri": dict(mcp_stats.resource_reads_by_uri),
        "resource_failures_by_uri": dict(mcp_stats.resource_failures_by_uri),
        "run_outcome": outcome,
        "resilience_score": score,
    }


def print_scorecard() -> None:
    data = scorecard_data()
    lines = [
        "",
        "AgentBreak MCP Resilience Scorecard",
        f"Requests Seen: {data['requests_seen']}",
        f"Tool Calls: {data['tool_calls']}",
        f"Resource Reads: {data['resource_reads']}",
        f"Init Requests: {data['init_requests']}",
        f"Injected Faults: {data['injected_faults']}",
        f"Latency Injections: {data['latency_injections']}",
        f"Upstream Successes: {data['upstream_successes']}",
        f"Upstream Failures: {data['upstream_failures']}",
        f"Duplicate Requests: {data['duplicate_requests']}",
        f"Suspected Loops: {data['suspected_loops']}",
    ]
    if data["method_counts"]:
        lines.append("Method Counts:")
        for method, count in sorted(data["method_counts"].items()):
            lines.append(f"  {method}: {count}")
    if data["tool_successes_by_name"] or data["tool_failures_by_name"]:
        all_tools = set(data["tool_successes_by_name"]) | set(data["tool_failures_by_name"])
        lines.append("Tool Call Results:")
        for tool in sorted(all_tools):
            ok = data["tool_successes_by_name"].get(tool, 0)
            fail = data["tool_failures_by_name"].get(tool, 0)
            lines.append(f"  {tool}: {ok} ok, {fail} fail")
    if data["resource_reads_by_uri"] or data["resource_failures_by_uri"]:
        all_uris = set(data["resource_reads_by_uri"]) | set(data["resource_failures_by_uri"])
        lines.append("Resource Read Results:")
        for uri in sorted(all_uris):
            ok = data["resource_reads_by_uri"].get(uri, 0)
            fail = data["resource_failures_by_uri"].get(uri, 0)
            lines.append(f"  {uri}: {ok} ok, {fail} fail")
    lines += [
        f"Run Outcome: {data['run_outcome']}",
        f"Resilience Score: {data['resilience_score']}/100",
        "",
    ]
    print("\n".join(lines), file=sys.stderr)


async def _forward_http(
    mcp_req: MCPRequest, body: bytes, http_request: Request
) -> JSONResponse:
    """Forward an MCP request to an HTTP upstream server."""
    assert mcp_config is not None
    timeout = mcp_config.upstream_timeout
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.post(
                f"{mcp_config.upstream_url.rstrip('/')}/mcp",
                content=body,
                headers=filter_headers(http_request.headers),
            )
        except httpx.HTTPError as exc:
            mcp_stats.upstream_failures += 1
            return JSONResponse(
                status_code=200,
                content=mcp_error_response(
                    mcp_req.id,
                    MCPError(
                        code=INTERNAL_ERROR,
                        message=f"AgentBreak could not reach upstream: {exc}",
                    ),
                ),
            )

    if response.status_code < 400:
        mcp_stats.upstream_successes += 1
    else:
        mcp_stats.upstream_failures += 1

    try:
        return JSONResponse(status_code=200, content=response.json())
    except Exception:
        return JSONResponse(
            status_code=200,
            content=mcp_error_response(
                mcp_req.id,
                MCPError(code=INTERNAL_ERROR, message="Upstream returned non-JSON response."),
            ),
        )


async def _forward_stdio(mcp_req: MCPRequest) -> JSONResponse:
    """Forward an MCP request to a stdio subprocess."""
    global _stdio_transport
    assert mcp_config is not None
    if _stdio_transport is None:
        _stdio_transport = StdioTransportManager(
            command=mcp_config.upstream_command,
            timeout=mcp_config.upstream_timeout,
        )
    try:
        result = await _stdio_transport.send_request(mcp_req)
        mcp_stats.upstream_successes += 1
        return JSONResponse(status_code=200, content=result)
    except (TimeoutError, RuntimeError, OSError) as exc:
        mcp_stats.upstream_failures += 1
        return JSONResponse(
            status_code=200,
            content=mcp_error_response(
                mcp_req.id,
                MCPError(
                    code=INTERNAL_ERROR,
                    message=f"Stdio upstream error: {exc}",
                ),
            ),
        )


async def _forward_sse(mcp_req: MCPRequest) -> JSONResponse:
    """Forward an MCP request to an SSE upstream server."""
    global _sse_transport
    assert mcp_config is not None
    if _sse_transport is None:
        _sse_transport = SSETransportManager(
            base_url=mcp_config.upstream_url,
            timeout=mcp_config.upstream_timeout,
        )
    try:
        result = await _sse_transport.send_request(mcp_req)
        mcp_stats.upstream_successes += 1
        return JSONResponse(status_code=200, content=result)
    except (TimeoutError, RuntimeError, OSError) as exc:
        mcp_stats.upstream_failures += 1
        return JSONResponse(
            status_code=200,
            content=mcp_error_response(
                mcp_req.id,
                MCPError(
                    code=INTERNAL_ERROR,
                    message=f"SSE upstream error: {exc}",
                ),
            ),
        )


@app.post("/mcp")
async def proxy_mcp(request: Request) -> JSONResponse:
    assert mcp_config is not None
    body = await request.body()

    try:
        mcp_req = MCPRequest.from_json(body)
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        return JSONResponse(
            status_code=200,
            content=mcp_error_response(
                None,
                MCPError(code=-32700, message=f"Parse error: {exc}"),
            ),
        )

    record_mcp_request(mcp_req, body)

    if should_inject(mcp_config.fail_rate):
        error = pick_mcp_error()
        mcp_stats.injected_faults += 1
        mcp_stats.upstream_failures += 1
        _record_method_outcome(mcp_req, False)
        return JSONResponse(
            status_code=200,
            content=mcp_error_response(mcp_req.id, error),
        )

    await maybe_delay()

    if mcp_config.mode == "mock":
        mcp_stats.upstream_successes += 1
        result = generate_mock_result(mcp_req.method, mcp_req.params, mcp_config)
        _record_method_outcome(mcp_req, True)
        return JSONResponse(
            status_code=200,
            content=MCPResponse(id=mcp_req.id, result=result).to_dict(),
        )

    transport = mcp_config.upstream_transport
    if transport == "stdio":
        response = await _forward_stdio(mcp_req)
    elif transport == "sse":
        response = await _forward_sse(mcp_req)
    else:
        response = await _forward_http(mcp_req, body, request)
    _record_method_outcome(mcp_req, _is_success_response(response))
    return response


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/_agentbreak/mcp/scorecard")
async def get_mcp_scorecard() -> dict[str, Any]:
    return scorecard_data()


@app.get("/_agentbreak/mcp/tool-calls")
async def get_mcp_tool_calls() -> dict[str, Any]:
    tool_requests = [r for r in mcp_stats.recent_requests if r.get("method") == "tools/call"]
    return {"recent_tool_calls": tool_requests}


def install_signal_handlers() -> None:
    def handle_signal(signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt(f"received signal {signum}")

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)


@cli.command(
    help=(
        "Start AgentBreak MCP proxy.\n\n"
        "Examples:\n"
        "  python -m agentbreak.mcp_proxy --mode mock --fail-rate 0.2\n"
        "  python -m agentbreak.mcp_proxy --mode proxy --upstream-url http://localhost:8080"
    )
)
def start(
    mode: str = typer.Option("mock", help="proxy forwards to a real upstream, mock returns stub responses."),
    upstream_url: str = typer.Option("", help="Upstream MCP server base URL (http/sse proxy mode)."),
    upstream_transport: str = typer.Option("http", help="Transport to upstream: http, stdio, or sse."),
    upstream_command: list[str] = typer.Option([], help="Command (and args) for stdio transport, e.g. 'python server.py'."),
    upstream_timeout: float = typer.Option(DEFAULT_UPSTREAM_TIMEOUT, help="Timeout in seconds for upstream requests."),
    fail_rate: float = typer.Option(0.1, help="Probability of injecting a fault."),
    latency_p: float = typer.Option(0.0, help="Probability of injecting latency."),
    latency_min: float = typer.Option(5.0, help="Minimum injected latency in seconds."),
    latency_max: float = typer.Option(15.0, help="Maximum injected latency in seconds."),
    seed: int | None = typer.Option(None, help="Optional deterministic random seed."),
    port: int = typer.Option(PORT, help="Port to bind the MCP proxy on."),
) -> None:
    global mcp_config, mcp_stats, _stdio_transport, _sse_transport

    if mode not in {"proxy", "mock"}:
        raise typer.BadParameter("mode must be 'proxy' or 'mock'")
    if upstream_transport not in {"http", "stdio", "sse"}:
        raise typer.BadParameter("upstream-transport must be 'http', 'stdio', or 'sse'.")
    if mode == "proxy" and upstream_transport in {"http", "sse"} and not upstream_url:
        raise typer.BadParameter("--upstream-url is required for http and sse transports.")
    if mode == "proxy" and upstream_transport == "stdio" and not upstream_command:
        raise typer.BadParameter("--upstream-command is required for stdio transport.")
    if latency_min < 0 or latency_max < 0:
        raise typer.BadParameter("Latency values must be >= 0.")
    if latency_min > latency_max:
        raise typer.BadParameter("--latency-min must be <= --latency-max.")
    if upstream_timeout <= 0:
        raise typer.BadParameter("--upstream-timeout must be > 0.")

    mcp_config = MCPConfig(
        mode=mode,
        upstream_url=upstream_url,
        upstream_transport=upstream_transport,
        upstream_command=tuple(upstream_command),
        upstream_timeout=upstream_timeout,
        fail_rate=clamp_probability(fail_rate),
        latency_p=clamp_probability(latency_p),
        latency_min=latency_min,
        latency_max=latency_max,
        seed=seed,
    )
    mcp_stats = MCPStats()
    _stdio_transport = None
    _sse_transport = None

    if mcp_config.seed is not None:
        random.seed(mcp_config.seed)

    install_signal_handlers()
    try:
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
    finally:
        print_scorecard()


if __name__ == "__main__":
    cli()
