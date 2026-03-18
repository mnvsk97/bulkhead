from __future__ import annotations

import asyncio
import json
import random
import signal
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import httpx
import typer
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from agentbreak.protocols.mcp import (
    INTERNAL_ERROR,
    INVALID_REQUEST,
    MCP_TOOL_ERROR,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    MCPError,
    MCPRequest,
    MCPResponse,
    fingerprint_mcp_request,
)
from agentbreak.transports import (
    DEFAULT_TRANSPORT_TIMEOUT,
    SSETransport,
    StdioTransport,
    create_transport,
)
# Import SCENARIOS lazily to avoid circular import issues at module level.
# main.py does not import mcp_proxy, so this is safe at call time.
def _get_scenarios() -> dict[str, dict[str, Any]]:
    from agentbreak.main import SCENARIOS  # noqa: PLC0415
    return SCENARIOS

PORT = 5001
cli = typer.Typer(add_completion=False, help="MCP JSON-RPC 2.0 proxy with fault injection.")

# Default timeout for upstream requests (seconds) — re-exported from mcp_transport.
DEFAULT_UPSTREAM_TIMEOUT = DEFAULT_TRANSPORT_TIMEOUT

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
    # TTL (seconds) for caching list-style responses (resources/list, tools/list, etc.)
    cache_ttl: float = 60.0
    # Optional method filters: if set, fault/latency injection only applies to listed methods.
    fault_methods: tuple[str, ...] | None = None
    latency_methods: tuple[str, ...] | None = None


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
    # Cache stats
    cache_hits: int = 0
    cache_misses: int = 0
    # Proxy overhead metrics (milliseconds)
    total_processing_time_ms: float = 0.0
    # Detailed overhead breakdown (milliseconds)
    parse_time_ms: float = 0.0
    fault_check_time_ms: float = 0.0
    cache_lookup_time_ms: float = 0.0
    upstream_time_ms: float = 0.0
    serialization_time_ms: float = 0.0
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
    # Connection pool stats
    http_pool_size: int = 0
    # Throughput metrics
    requests_per_second: float = 0.0
    session_start_time: float = field(default_factory=time.monotonic)
    # Lock for thread-safe updates to shared state
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)




mcp_config: MCPConfig | None = None
mcp_stats = MCPStats()
# Transport managers are created per run and cleaned up on shutdown.
_stdio_transport: StdioTransport | None = None
_sse_transport: SSETransport | None = None
# Shared HTTP client with connection pooling (replaces per-request client creation).
_upstream_http_client: httpx.AsyncClient | None = None
# Response cache: maps cache key -> (result_dict, expiry_timestamp).
_response_cache: dict[str, tuple[dict[str, Any], float]] = {}
# Locks for thread-safe access
_cache_lock: asyncio.Lock = asyncio.Lock()
_client_lock: asyncio.Lock = asyncio.Lock()

# Methods whose responses can be safely cached (read-only, rarely change).
_CACHEABLE_METHODS = frozenset({"resources/list", "tools/list", "prompts/list"})

app = FastAPI(title="agentbreak-mcp")


def clamp_probability(value: float) -> float:
    return max(0.0, min(1.0, value))


def should_inject(probability: float) -> bool:
    return random.random() < clamp_probability(probability)


def _get_config() -> MCPConfig:
    """Get the MCP config, raising an error if not configured."""
    if mcp_config is None:
        raise RuntimeError("MCP proxy is not configured. Ensure the MCP mode is enabled and configuration is complete.")
    return mcp_config


def pick_mcp_error() -> MCPError:
    config = _get_config()
    http_code = random.choice(config.fault_codes)
    entry = _HTTP_TO_MCP.get(http_code, (INTERNAL_ERROR, f"Fault injected by AgentBreak (code {http_code})."))
    mcp_code, message = entry
    return MCPError(code=mcp_code, message=message)


def mcp_error_response(request_id: str | int | None, error: MCPError) -> dict[str, Any]:
    return MCPResponse(id=request_id, error=error).to_dict()


async def record_mcp_request(mcp_req: MCPRequest, raw_body: bytes) -> None:
    fp = fingerprint_mcp_request(mcp_req)
    try:
        payload: Any = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {"raw": raw_body.decode("utf-8", errors="replace")}

    async with mcp_stats._lock:
        mcp_stats.total_requests += 1
        mcp_stats.method_counts[mcp_req.method] += 1

        mcp_stats.seen_fingerprints[fp] += 1
        seen = mcp_stats.seen_fingerprints[fp]
        if seen > 1:
            mcp_stats.duplicate_requests += 1
        if seen > 2:
            mcp_stats.suspected_loops += 1

        if mcp_req.method == "tools/call":
            mcp_stats.tool_calls += 1
        elif mcp_req.method == "resources/read":
            mcp_stats.resource_reads += 1
        elif mcp_req.method == "initialize":
            mcp_stats.init_requests += 1

        mcp_stats.recent_requests.append({
            "fingerprint": fp,
            "count": seen,
            "method": mcp_req.method,
            "body": payload,
        })
        if len(mcp_stats.recent_requests) > 20:
            mcp_stats.recent_requests.pop(0)


async def maybe_delay() -> None:
    config = _get_config()
    if not should_inject(config.latency_p):
        return
    async with mcp_stats._lock:
        mcp_stats.latency_injections += 1
    delay = random.uniform(config.latency_min, config.latency_max)
    await asyncio.sleep(delay)


def _cache_key(method: str, params: dict[str, Any] | None) -> str:
    params_str = json.dumps(params, sort_keys=True) if params else ""
    return f"{method}:{params_str}"


_MAX_CACHE_SIZE = 1000


async def _get_from_cache(method: str, params: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return cached result if present and not expired, else None."""
    key = _cache_key(method, params)
    async with _cache_lock:
        entry = _response_cache.get(key)
        if entry is None:
            return None
        result, expiry = entry
        if time.monotonic() > expiry:
            del _response_cache[key]
            return None
        return result


async def _put_in_cache(method: str, params: dict[str, Any] | None, result: dict[str, Any]) -> None:
    """Store a result in the response cache with the configured TTL."""
    config = _get_config()
    key = _cache_key(method, params)
    async with _cache_lock:
        _response_cache[key] = (result, time.monotonic() + config.cache_ttl)
        # Evict oldest entry if cache is too large
        if len(_response_cache) > _MAX_CACHE_SIZE:
            oldest_key = min(
                _response_cache.keys(),
                key=lambda k: _response_cache[k][1]
            )
            del _response_cache[oldest_key]


async def _get_upstream_http_client() -> httpx.AsyncClient:
    """Return the shared upstream HTTP client, creating it on first call."""
    global _upstream_http_client
    config = _get_config()
    async with _client_lock:
        if _upstream_http_client is None:
            _upstream_http_client = httpx.AsyncClient(
                timeout=config.upstream_timeout,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
            mcp_stats.http_pool_size = 20
    return _upstream_http_client


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



async def _record_method_outcome(mcp_req: MCPRequest, is_success: bool) -> None:
    """Update per-tool and per-URI success/failure counters with lock protection."""
    async with mcp_stats._lock:
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
    # PASS requires actual upstream successes, not merely an absence of failures.
    # An all-fault run (injected_faults > 0, upstream_successes == 0) should not PASS.
    no_failures = mcp_stats.upstream_failures == 0 and mcp_stats.suspected_loops == 0
    if no_failures and (mcp_stats.upstream_successes > 0 or mcp_stats.total_requests == 0):
        outcome = "PASS"
    elif mcp_stats.upstream_successes > 0:
        outcome = "DEGRADED"
    else:
        outcome = "FAIL"
    avg_processing_ms = (
        round(mcp_stats.total_processing_time_ms / mcp_stats.total_requests, 2)
        if mcp_stats.total_requests > 0
        else 0.0
    )
    # Calculate requests per second
    elapsed = time.monotonic() - mcp_stats.session_start_time
    rps = round(mcp_stats.total_requests / elapsed, 2) if elapsed > 0 else 0.0
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
        "cache_hits": mcp_stats.cache_hits,
        "cache_misses": mcp_stats.cache_misses,
        "avg_processing_ms": avg_processing_ms,
        # Detailed overhead breakdown
        "parse_time_ms": round(mcp_stats.parse_time_ms, 2),
        "fault_check_time_ms": round(mcp_stats.fault_check_time_ms, 2),
        "cache_lookup_time_ms": round(mcp_stats.cache_lookup_time_ms, 2),
        "upstream_time_ms": round(mcp_stats.upstream_time_ms, 2),
        "serialization_time_ms": round(mcp_stats.serialization_time_ms, 2),
        # Throughput metrics
        "requests_per_second": rps,
        # Connection pool stats
        "http_pool_size": mcp_stats.http_pool_size,
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
        f"Cache Hits: {data['cache_hits']}",
        f"Cache Misses: {data['cache_misses']}",
        f"Avg Processing Time: {data['avg_processing_ms']}ms",
        f"Throughput: {data['requests_per_second']} req/s",
    ]
    # Detailed overhead breakdown
    if data["requests_seen"] > 0:
        lines.append("Proxy Overhead Breakdown:")
        lines.append(f"  Parse Time: {data['parse_time_ms']}ms")
        lines.append(f"  Fault Check Time: {data['fault_check_time_ms']}ms")
        lines.append(f"  Cache Lookup Time: {data['cache_lookup_time_ms']}ms")
        lines.append(f"  Upstream Time: {data['upstream_time_ms']}ms")
        lines.append(f"  Serialization Time: {data['serialization_time_ms']}ms")
    # Connection pool stats
    if data["http_pool_size"] > 0:
        lines.append("HTTP Connection Pool:")
        lines.append(f"  Pool Size: {data['http_pool_size']}")
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
) -> tuple[JSONResponse, bool]:
    """Forward an MCP request to an HTTP upstream server using the shared connection pool.

    Returns (response, transport_success) where transport_success is False only when
    we could not reach the upstream at all (network error or non-2xx HTTP status).
    """
    config = _get_config()
    client = await _get_upstream_http_client()
    try:
        response = await client.post(
            f"{config.upstream_url.rstrip('/')}/mcp",
            content=body,
            headers=filter_headers(http_request.headers),
        )
    except httpx.HTTPError as exc:
        return (
            JSONResponse(
                status_code=200,
                content=mcp_error_response(
                    mcp_req.id,
                    MCPError(
                        code=INTERNAL_ERROR,
                        message=f"AgentBreak could not reach upstream: {exc}",
                    ),
                ),
            ),
            False,
        )

    transport_success = response.status_code < 300
    try:
        return JSONResponse(status_code=200, content=response.json()), transport_success
    except Exception:
        return (
            JSONResponse(
                status_code=200,
                content=mcp_error_response(
                    mcp_req.id,
                    MCPError(code=INTERNAL_ERROR, message="Upstream returned non-JSON response."),
                ),
            ),
            False,
        )


async def _forward_stdio(mcp_req: MCPRequest) -> tuple[JSONResponse, bool]:
    """Forward an MCP request to a stdio subprocess.

    Returns (response, transport_success) where transport_success is False only when
    the subprocess raised a transport-level exception.
    """
    global _stdio_transport
    config = _get_config()
    if _stdio_transport is None:
        _stdio_transport = StdioTransport(
            command=config.upstream_command,
            timeout=config.upstream_timeout,
        )
    try:
        result = await _stdio_transport.send_request(mcp_req)
        return JSONResponse(status_code=200, content=result), True
    except (TimeoutError, RuntimeError, OSError) as exc:
        return (
            JSONResponse(
                status_code=200,
                content=mcp_error_response(
                    mcp_req.id,
                    MCPError(
                        code=INTERNAL_ERROR,
                        message=f"Stdio upstream error: {exc}",
                    ),
                ),
            ),
            False,
        )


async def _forward_sse(mcp_req: MCPRequest) -> tuple[JSONResponse, bool]:
    """Forward an MCP request to an SSE upstream server.

    Returns (response, transport_success) where transport_success is False only when
    the SSE transport raised a transport-level exception.
    """
    global _sse_transport
    config = _get_config()
    if _sse_transport is None:
        _sse_transport = SSETransport(
            base_url=config.upstream_url,
            timeout=config.upstream_timeout,
        )
    try:
        result = await _sse_transport.send_request(mcp_req)
        return JSONResponse(status_code=200, content=result), True
    except (TimeoutError, RuntimeError, OSError) as exc:
        # If the SSE listener task has died, reset the singleton so the next
        # request creates a fresh transport and reconnects.
        if _sse_transport is not None and (
            _sse_transport._sse_task is None or _sse_transport._sse_task.done()
        ):
            try:
                await _sse_transport.stop()
            except Exception:
                pass
            _sse_transport = None
        return (
            JSONResponse(
                status_code=200,
                content=mcp_error_response(
                    mcp_req.id,
                    MCPError(
                        code=INTERNAL_ERROR,
                        message=f"SSE upstream error: {exc}",
                    ),
                ),
            ),
            False,
        )


async def _process_single_mcp_request(
    raw: Any, body: bytes, http_request: Request
) -> dict[str, Any]:
    """Process one parsed JSON-RPC request dict and return a response dict."""
    if not isinstance(raw, dict):
        return mcp_error_response(None, MCPError(code=INVALID_REQUEST, message="Invalid Request: batch items must be JSON objects"))
    _get_config()  # Validate config is set
    start_time = time.monotonic()

    # Phase 1: Parse request
    parse_start = time.monotonic()
    try:
        mcp_req = MCPRequest.from_dict(raw)
        mcp_req._json_bytes = body  # cache original bytes to skip re-serialization
    except (ValueError, KeyError) as exc:
        parse_elapsed = (time.monotonic() - parse_start) * 1000
        elapsed = (time.monotonic() - start_time) * 1000
        async with mcp_stats._lock:
            mcp_stats.parse_time_ms += parse_elapsed
            mcp_stats.total_processing_time_ms += elapsed
        return mcp_error_response(raw.get("id"), MCPError(code=INVALID_REQUEST, message=f"Invalid Request: {exc}"))
    parse_elapsed = (time.monotonic() - parse_start) * 1000
    async with mcp_stats._lock:
        mcp_stats.parse_time_ms += parse_elapsed

    await record_mcp_request(mcp_req, body)

    # Phase 2: Fault injection check
    fault_check_start = time.monotonic()
    method_faultable = mcp_config.fault_methods is None or mcp_req.method in mcp_config.fault_methods
    should_fault = method_faultable and should_inject(mcp_config.fail_rate)
    fault_check_elapsed = (time.monotonic() - fault_check_start) * 1000
    async with mcp_stats._lock:
        mcp_stats.fault_check_time_ms += fault_check_elapsed

    if should_fault:
        error = pick_mcp_error()
        elapsed = (time.monotonic() - start_time) * 1000
        async with mcp_stats._lock:
            mcp_stats.injected_faults += 1
            mcp_stats.total_processing_time_ms += elapsed
        await _record_method_outcome(mcp_req, False)
        return mcp_error_response(mcp_req.id, error)

    method_delayable = mcp_config.latency_methods is None or mcp_req.method in mcp_config.latency_methods
    if method_delayable:
        await maybe_delay()

    # Phase 3: Cache lookup (for cacheable methods)
    cache_start = time.monotonic()
    if mcp_req.method in _CACHEABLE_METHODS:
        cached = await _get_from_cache(mcp_req.method, mcp_req.params)
        cache_lookup_elapsed = (time.monotonic() - cache_start) * 1000
        async with mcp_stats._lock:
            mcp_stats.cache_lookup_time_ms += cache_lookup_elapsed
        if cached is not None:
            async with mcp_stats._lock:
                mcp_stats.cache_hits += 1
            await _record_method_outcome(mcp_req, True)
            # Phase 5: Serialize response (for cache hits)
            serialize_start = time.monotonic()
            response = MCPResponse(id=mcp_req.id, result=cached).to_dict()
            serialization_elapsed = (time.monotonic() - serialize_start) * 1000
            elapsed = (time.monotonic() - start_time) * 1000
            async with mcp_stats._lock:
                mcp_stats.serialization_time_ms += serialization_elapsed
                mcp_stats.total_processing_time_ms += elapsed
            return response
        async with mcp_stats._lock:
            mcp_stats.cache_misses += 1
    else:
        cache_lookup_elapsed = (time.monotonic() - cache_start) * 1000
        async with mcp_stats._lock:
            mcp_stats.cache_lookup_time_ms += cache_lookup_elapsed

    # Phase 4: Upstream request / mock response
    upstream_start = time.monotonic()
    if mcp_config.mode == "mock":
        async with mcp_stats._lock:
            mcp_stats.upstream_successes += 1
        result = generate_mock_result(mcp_req.method, mcp_req.params, mcp_config)
        await _record_method_outcome(mcp_req, True)
        if mcp_req.method in _CACHEABLE_METHODS:
            await _put_in_cache(mcp_req.method, mcp_req.params, result)
    else:
        transport = mcp_config.upstream_transport
        if transport == "stdio":
            jresp, transport_success = await _forward_stdio(mcp_req)
        elif transport == "sse":
            jresp, transport_success = await _forward_sse(mcp_req)
        else:
            jresp, transport_success = await _forward_http(mcp_req, body, http_request)
        resp_dict: dict[str, Any] = json.loads(jresp.body)
        # Count transport failures immediately; for transport-level successes, also
        # check whether the upstream returned a JSON-RPC error body so that
        # run_outcome reflects actual tool-level outcomes, not just connectivity.
        if not transport_success:
            async with mcp_stats._lock:
                mcp_stats.upstream_failures += 1
            is_success = False
        else:
            is_success = "error" not in resp_dict
            async with mcp_stats._lock:
                if is_success:
                    mcp_stats.upstream_successes += 1
                else:
                    mcp_stats.upstream_failures += 1
        # Populate cache for successful list responses.
        if is_success and mcp_req.method in _CACHEABLE_METHODS:
            result_payload = resp_dict.get("result") if resp_dict.get("result") is not None else {}
            await _put_in_cache(mcp_req.method, mcp_req.params, result_payload)
        await _record_method_outcome(mcp_req, is_success)
        upstream_elapsed = (time.monotonic() - upstream_start) * 1000
        elapsed = (time.monotonic() - start_time) * 1000
        async with mcp_stats._lock:
            mcp_stats.upstream_time_ms += upstream_elapsed
            mcp_stats.total_processing_time_ms += elapsed
        return resp_dict

    upstream_elapsed = (time.monotonic() - upstream_start) * 1000
    async with mcp_stats._lock:
        mcp_stats.upstream_time_ms += upstream_elapsed

    # Phase 5: Serialize response
    serialize_start = time.monotonic()
    response = MCPResponse(id=mcp_req.id, result=result).to_dict()
    serialization_elapsed = (time.monotonic() - serialize_start) * 1000

    elapsed = (time.monotonic() - start_time) * 1000
    async with mcp_stats._lock:
        mcp_stats.serialization_time_ms += serialization_elapsed
        mcp_stats.total_processing_time_ms += elapsed
    return response


@app.post("/mcp")
async def proxy_mcp(request: Request) -> JSONResponse:
    _get_config()  # Validate config is set
    body = await request.body()

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        return JSONResponse(
            status_code=200,
            content=mcp_error_response(
                None,
                MCPError(code=PARSE_ERROR, message=f"Parse error: {exc}"),
            ),
        )

    # JSON-RPC 2.0 batch request: process all items concurrently.
    if isinstance(parsed, list):
        # Empty batch is invalid per JSON-RPC 2.0 spec.
        if not parsed:
            return JSONResponse(
                status_code=200,
                content=mcp_error_response(
                    None, MCPError(code=INVALID_REQUEST, message="Invalid Request: batch must not be empty")
                ),
            )
        # For stdio/SSE proxy mode, forwarding notifications would await a response
        # that the upstream will never send. Skip those items entirely.
        skip_stdio_sse_notifications = (
            mcp_config.mode == "proxy" and mcp_config.upstream_transport in {"stdio", "sse"}
        )
        items_to_process = [
            (item, json.dumps(item).encode())
            for item in parsed
            if not (skip_stdio_sse_notifications and isinstance(item, dict) and "id" not in item)
        ]
        tasks = [
            _process_single_mcp_request(item, item_bytes, request)
            for item, item_bytes in items_to_process
        ]
        responses = await asyncio.gather(*tasks)
        # Per JSON-RPC 2.0, notifications (items with no "id" field) must not
        # receive a response. Filter them out of the batch response array.
        non_notification_responses = [
            resp
            for (item, _), resp in zip(items_to_process, responses)
            if isinstance(item, dict) and "id" in item
        ]
        if not non_notification_responses:
            # Per JSON-RPC 2.0: if batch contained only notifications, return no response.
            return Response(status_code=200)
        return JSONResponse(status_code=200, content=non_notification_responses)

    # Per JSON-RPC 2.0, notifications (requests with no "id") must not receive a response.
    # Skip processing entirely — forwarding notifications in proxy mode would await a
    # response the upstream will never send, and processing them in mock mode inflates stats.
    is_notification = isinstance(parsed, dict) and "id" not in parsed
    if is_notification:
        return Response(status_code=200)
    result = await _process_single_mcp_request(parsed, body, request)
    return JSONResponse(status_code=200, content=result)


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
        "  python -m agentbreak.mcp_proxy --mode mock --scenario mcp-tool-failures\n"
        "  python -m agentbreak.mcp_proxy --mode proxy --upstream-url http://localhost:8080"
    )
)
def start(
    mode: str = typer.Option("mock", help="proxy forwards to a real upstream, mock returns stub responses."),
    upstream_url: str = typer.Option("", help="Upstream MCP server base URL (http/sse proxy mode)."),
    upstream_transport: str = typer.Option("http", help="Transport to upstream: http, stdio, or sse."),
    upstream_command: list[str] = typer.Option([], help="Command (and args) for stdio transport, e.g. 'python server.py'."),
    upstream_timeout: float = typer.Option(DEFAULT_UPSTREAM_TIMEOUT, help="Timeout in seconds for upstream requests."),
    scenario: str | None = typer.Option(None, help="Built-in MCP fault scenario name (e.g. mcp-tool-failures)."),
    fail_rate: float | None = typer.Option(None, help="Probability of injecting a fault."),
    latency_p: float | None = typer.Option(None, help="Probability of injecting latency."),
    latency_min: float = typer.Option(5.0, help="Minimum injected latency in seconds."),
    latency_max: float = typer.Option(15.0, help="Maximum injected latency in seconds."),
    fault_codes: str | None = typer.Option(None, help="Comma-separated HTTP-style codes for fault injection. Supported: 400,401,403,404,413,429,500,503."),
    seed: int | None = typer.Option(None, help="Optional deterministic random seed."),
    port: int = typer.Option(PORT, help="Port to bind the MCP proxy on."),
) -> None:
    global mcp_config, mcp_stats, _stdio_transport, _sse_transport, _upstream_http_client, _response_cache

    if mode not in {"proxy", "mock"}:
        raise typer.BadParameter("mode must be 'proxy' or 'mock'. 'mock' returns fake responses without a real server. 'proxy' forwards to a real MCP server.")
    if upstream_transport not in {"http", "stdio", "sse"}:
        raise typer.BadParameter("upstream-transport must be 'http', 'stdio', or 'sse'. Choose the transport type that matches your MCP server: 'http' for HTTP/SSE servers, 'stdio' for subprocess-based servers.")
    if mode == "proxy" and upstream_transport in {"http", "sse"} and not upstream_url:
        raise typer.BadParameter(f"--upstream-url is required for {upstream_transport} transport in proxy mode. Provide the base URL of your MCP server (e.g., http://localhost:8080).")
    if mode == "proxy" and upstream_transport == "stdio" and not upstream_command:
        raise typer.BadParameter("--upstream-command is required for stdio transport in proxy mode. Provide the command to start your MCP server (e.g., 'python server.py').")
    if latency_min < 0 or latency_max < 0:
        raise typer.BadParameter("Latency values must be >= 0. Latency is the delay (in seconds) to add before forwarding requests.")
    if latency_min > latency_max:
        raise typer.BadParameter("--latency-min must be <= --latency-max. The minimum delay cannot be greater than the maximum.")
    if upstream_timeout <= 0:
        raise typer.BadParameter(f"--upstream-timeout must be > 0. Current value: {upstream_timeout}. Timeout is the maximum time (in seconds) to wait for MCP server responses.")

    scenario_config: dict[str, Any] = {}
    if scenario is not None:
        scenarios = _get_scenarios()
        if scenario not in scenarios:
            raise typer.BadParameter(
                f"Unknown scenario '{scenario}'. Available: {', '.join(scenarios)}"
            )
        scenario_config = scenarios[scenario]

    resolved_fail_rate = fail_rate if fail_rate is not None else float(scenario_config.get("mcp_fail_rate", 0.1))
    resolved_latency_p = latency_p if latency_p is not None else float(scenario_config.get("mcp_latency_p", 0.0))
    if fault_codes is not None:
        parsed: list[int] = []
        for item in fault_codes.split(","):
            value = item.strip()
            if not value:
                continue
            try:
                code = int(value)
            except ValueError:
                raise typer.BadParameter(f"Invalid fault code {value!r}: must be an integer.")
            if code not in SUPPORTED_FAULT_CODES:
                raise typer.BadParameter(
                    f"Unsupported fault code {code}. Supported: {', '.join(str(c) for c in SUPPORTED_FAULT_CODES)}"
                )
            parsed.append(code)
        if not parsed:
            raise typer.BadParameter("At least one fault code is required.")
        resolved_fault_codes: tuple[int, ...] = tuple(parsed)
    else:
        resolved_fault_codes = scenario_config.get("mcp_error_codes", DEFAULT_FAULT_CODES)

    mcp_config = MCPConfig(
        mode=mode,
        upstream_url=upstream_url,
        upstream_transport=upstream_transport,
        upstream_command=tuple(upstream_command),
        upstream_timeout=upstream_timeout,
        fail_rate=clamp_probability(resolved_fail_rate),
        fault_codes=resolved_fault_codes,
        latency_p=clamp_probability(resolved_latency_p),
        latency_min=latency_min,
        latency_max=latency_max,
        seed=seed,
    )
    mcp_stats = MCPStats()
    _stdio_transport = None
    _sse_transport = None
    _upstream_http_client = None
    _response_cache = {}

    if mcp_config.seed is not None:
        random.seed(mcp_config.seed)

    install_signal_handlers()
    try:
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
    finally:
        # Cleanup resources
        # Use the running event loop instead of asyncio.run() to avoid:
        # RuntimeError: asyncio.run() cannot be called from a running event loop
        loop = asyncio.get_running_loop()
        cleanup_tasks = []
        if _upstream_http_client is not None:
            cleanup_tasks.append(_upstream_http_client.aclose())
        if _stdio_transport is not None:
            cleanup_tasks.append(_stdio_transport.stop())
        if _sse_transport is not None:
            cleanup_tasks.append(_sse_transport.stop())
        if cleanup_tasks:
            loop.run_until_complete(asyncio.gather(*cleanup_tasks, return_exceptions=True))
        _upstream_http_client = None
        _stdio_transport = None
        _sse_transport = None
        _response_cache.clear()
        print_scorecard()


async def _send_one_request(
    method: str,
    params: dict[str, Any] | None,
    transport_type: str,
    url: str,
    command: tuple[str, ...],
    timeout: float,
) -> dict[str, Any]:
    """Send a single MCP request using the given transport and return the raw response dict."""
    t = create_transport(
        transport_type,
        base_url=url,
        command=command,
        timeout=timeout,
    )
    req = MCPRequest(method=method, id=1, params=params)
    try:
        await t.start()
        result = await t.send_request(req)
    finally:
        await t.stop()
    return result


@cli.command(
    "test",
    help=(
        "Test connectivity to an MCP server.\n\n"
        "Sends an 'initialize' request and reports success or failure.\n\n"
        "Examples:\n"
        "  agentbreak mcp test\n"
        "  agentbreak mcp test --url http://localhost:8080\n"
        "  agentbreak mcp test --transport stdio --command 'python server.py'"
    ),
)
def test_connectivity(
    url: str = typer.Option("http://localhost:5001", help="MCP server URL (http/sse transports)."),
    transport: str = typer.Option("http", help="Transport: http, stdio, or sse."),
    command: list[str] = typer.Option([], help="Command for stdio transport."),
    timeout: float = typer.Option(DEFAULT_UPSTREAM_TIMEOUT, help="Request timeout in seconds."),
) -> None:
    if transport not in {"http", "stdio", "sse"}:
        raise typer.BadParameter("transport must be 'http', 'stdio', or 'sse'.")
    if transport == "stdio" and not command:
        raise typer.BadParameter("--command is required for stdio transport.")
    if transport in {"http", "sse"} and not url:
        raise typer.BadParameter("--url is required for http and sse transports.")
    params: dict[str, Any] = {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "agentbreak", "version": "1.0"},
    }
    try:
        result = asyncio.run(
            _send_one_request("initialize", params, transport, url, tuple(command), timeout)
        )
    except (TimeoutError, RuntimeError, OSError) as exc:
        typer.echo(f"Connection failed: {exc}", err=True)
        raise typer.Exit(1)
    if "error" in result:
        err = result["error"]
        typer.echo(f"Server returned error: {err.get('message', err)}", err=True)
        raise typer.Exit(1)
    server_info = (result.get("result") or {}).get("serverInfo", {})
    name = server_info.get("name", "unknown")
    version = server_info.get("version", "unknown")
    typer.echo(f"OK  server={name}  version={version}")


@cli.command(
    "list-tools",
    help=(
        "List available tools from an MCP server.\n\n"
        "Examples:\n"
        "  agentbreak mcp list-tools\n"
        "  agentbreak mcp list-tools --url http://localhost:8080\n"
        "  agentbreak mcp list-tools --transport stdio --command 'python server.py'"
    ),
)
def list_tools(
    url: str = typer.Option("http://localhost:5001", help="MCP server URL (http/sse transports)."),
    transport: str = typer.Option("http", help="Transport: http, stdio, or sse."),
    command: list[str] = typer.Option([], help="Command for stdio transport."),
    timeout: float = typer.Option(DEFAULT_UPSTREAM_TIMEOUT, help="Request timeout in seconds."),
) -> None:
    if transport not in {"http", "stdio", "sse"}:
        raise typer.BadParameter("transport must be 'http', 'stdio', or 'sse'.")
    if transport == "stdio" and not command:
        raise typer.BadParameter("--command is required for stdio transport.")
    if transport in {"http", "sse"} and not url:
        raise typer.BadParameter("--url is required for http and sse transports.")
    try:
        result = asyncio.run(
            _send_one_request("tools/list", None, transport, url, tuple(command), timeout)
        )
    except (TimeoutError, RuntimeError, OSError) as exc:
        typer.echo(f"Request failed: {exc}", err=True)
        raise typer.Exit(1)
    if "error" in result:
        err = result["error"]
        typer.echo(f"Server returned error: {err.get('message', err)}", err=True)
        raise typer.Exit(1)
    tools = (result.get("result") or {}).get("tools", [])
    if not tools:
        typer.echo("No tools available.")
        return
    for tool in tools:
        name = tool.get("name", "?")
        desc = tool.get("description", "")
        typer.echo(f"  {name}  {desc}")


@cli.command(
    "call-tool",
    help=(
        "Call a tool through an MCP server.\n\n"
        "Examples:\n"
        '  agentbreak mcp call-tool echo --args \'{"text": "hello"}\'\n'
        "  agentbreak mcp call-tool get_time --url http://localhost:8080"
    ),
)
def call_tool(
    tool_name: str = typer.Argument(help="Name of the tool to call."),
    args: str = typer.Option("{}", help="Tool arguments as a JSON object string."),
    url: str = typer.Option("http://localhost:5001", help="MCP server URL (http/sse transports)."),
    transport: str = typer.Option("http", help="Transport: http, stdio, or sse."),
    command: list[str] = typer.Option([], help="Command for stdio transport."),
    timeout: float = typer.Option(DEFAULT_UPSTREAM_TIMEOUT, help="Request timeout in seconds."),
) -> None:
    if transport not in {"http", "stdio", "sse"}:
        raise typer.BadParameter("transport must be 'http', 'stdio', or 'sse'.")
    if transport == "stdio" and not command:
        raise typer.BadParameter("--command is required for stdio transport.")
    if transport in {"http", "sse"} and not url:
        raise typer.BadParameter("--url is required for http and sse transports.")
    try:
        tool_args = json.loads(args)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"--args must be valid JSON: {exc}")
    if not isinstance(tool_args, dict):
        raise typer.BadParameter("--args must be a JSON object.")
    params: dict[str, Any] = {"name": tool_name, "arguments": tool_args}
    try:
        result = asyncio.run(
            _send_one_request("tools/call", params, transport, url, tuple(command), timeout)
        )
    except (TimeoutError, RuntimeError, OSError) as exc:
        typer.echo(f"Request failed: {exc}", err=True)
        raise typer.Exit(1)
    if "error" in result:
        err = result["error"]
        typer.echo(f"Server returned error: {err.get('message', err)}", err=True)
        raise typer.Exit(1)
    content = (result.get("result") or {}).get("content", [])
    if not content:
        typer.echo(json.dumps(result.get("result") or result, indent=2))
        return
    for item in content:
        if item.get("type") == "text":
            typer.echo(item.get("text", ""))
        else:
            typer.echo(json.dumps(item, indent=2))


if __name__ == "__main__":
    cli()
