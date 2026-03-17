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


@dataclass
class MCPConfig:
    mode: str = "proxy"
    upstream_url: str = ""
    fail_rate: float = 0.1
    fault_codes: tuple[int, ...] = DEFAULT_FAULT_CODES
    latency_p: float = 0.0
    latency_min: float = 5.0
    latency_max: float = 15.0
    seed: int | None = None


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
    seen_fingerprints: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    recent_requests: list[dict[str, Any]] = field(default_factory=list)


mcp_config: MCPConfig | None = None
mcp_stats = MCPStats()
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


def filter_headers(headers: httpx.Headers) -> dict[str, str]:
    skip = {"host", "content-length"}
    return {key: value for key, value in headers.items() if key.lower() not in skip}


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
        f"Run Outcome: {data['run_outcome']}",
        f"Resilience Score: {data['resilience_score']}/100",
        "",
    ]
    print("\n".join(lines), file=sys.stderr)


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
        return JSONResponse(
            status_code=200,
            content=mcp_error_response(mcp_req.id, error),
        )

    await maybe_delay()

    if mcp_config.mode == "mock":
        mcp_stats.upstream_successes += 1
        return JSONResponse(
            status_code=200,
            content=MCPResponse(id=mcp_req.id, result={}).to_dict(),
        )

    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            response = await client.post(
                f"{mcp_config.upstream_url.rstrip('/')}/mcp",
                content=body,
                headers=filter_headers(request.headers),
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
    upstream_url: str = typer.Option("", help="Upstream MCP server base URL (proxy mode only)."),
    fail_rate: float = typer.Option(0.1, help="Probability of injecting a fault."),
    latency_p: float = typer.Option(0.0, help="Probability of injecting latency."),
    latency_min: float = typer.Option(5.0, help="Minimum injected latency in seconds."),
    latency_max: float = typer.Option(15.0, help="Maximum injected latency in seconds."),
    seed: int | None = typer.Option(None, help="Optional deterministic random seed."),
    port: int = typer.Option(PORT, help="Port to bind the MCP proxy on."),
) -> None:
    global mcp_config, mcp_stats

    if mode not in {"proxy", "mock"}:
        raise typer.BadParameter("mode must be 'proxy' or 'mock'")
    if mode == "proxy" and not upstream_url:
        raise typer.BadParameter("--upstream-url is required in proxy mode.")
    if latency_min < 0 or latency_max < 0:
        raise typer.BadParameter("Latency values must be >= 0.")
    if latency_min > latency_max:
        raise typer.BadParameter("--latency-min must be <= --latency-max.")

    mcp_config = MCPConfig(
        mode=mode,
        upstream_url=upstream_url,
        fail_rate=clamp_probability(fail_rate),
        latency_p=clamp_probability(latency_p),
        latency_min=latency_min,
        latency_max=latency_max,
        seed=seed,
    )
    mcp_stats = MCPStats()

    if mcp_config.seed is not None:
        random.seed(mcp_config.seed)

    install_signal_handlers()
    try:
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
    finally:
        print_scorecard()


if __name__ == "__main__":
    cli()
