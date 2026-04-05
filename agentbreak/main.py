from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import signal
import subprocess
import sys
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("agentbreak")

import httpx
import typer
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from agentbreak import __version__
from agentbreak.behaviors import apply_response_behavior
from agentbreak.config import ApplicationConfig, MCPConfig, MCPRegistry, load_application_config, load_registry, save_registry
from agentbreak.discovery.mcp import MCP_PROTOCOL_VERSION, inspect_mcp_server, parse_mcp_response
from agentbreak.history import RunHistory
from agentbreak.scenarios import Scenario, ScenarioFile, load_scenarios, validate_scenarios


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"agentbreak {__version__}")
        raise typer.Exit()


cli = typer.Typer(
    add_completion=False,
    help=(
        "AgentBreak — chaos proxy for LLM and MCP agent testing.\n\n"
        "Quick start:\n\n"
        "  agentbreak init       Create .agentbreak/ config\n"
        "  agentbreak serve      Start the chaos proxy\n"
        "  agentbreak history    View past run results\n\n"
        "Point your agent at http://localhost:5005 and check results:\n\n"
        "  curl localhost:5005/_agentbreak/scorecard"
    ),
)


@cli.callback(invoke_without_command=True)
def main(
    version: bool = typer.Option(False, "--version", "-V", help="Show version and exit.", callback=_version_callback, is_eager=True),
) -> None:
    pass
app = FastAPI(title="agentbreak")

DEFAULT_APPLICATION_YAML = """\
llm:
  enabled: true
  mode: mock
  # Proxy mode — uncomment for your provider:
  # upstream_url: https://api.openai.com
  # upstream_url: https://api.anthropic.com
  # auth:
  #   type: bearer
  #   env: OPENAI_API_KEY

mcp:
  enabled: false
  # upstream_url: http://127.0.0.1:8001/mcp
  # auth:
  #   type: bearer
  #   env: MCP_TOKEN

serve:
  port: 5005
"""

SCENARIOS_YAML_LLM_ONLY = """\
version: 1
preset: standard
# Standard preset includes: rate limit (429), server error (500),
# latency (3-8s), invalid JSON, empty response, schema violation.
#
# Add project-specific scenarios below:
# scenarios:
#   - name: my-scenario
#     summary: Description
#     target: llm_chat
#     fault:
#       kind: http_error
#       status_code: 429
#     schedule:
#       mode: random
#       probability: 0.2
#
# ─── Available presets ───────────────────────────────────────
# standard          — Baseline LLM faults (6 scenarios)
# standard-mcp      — Baseline MCP faults (7 scenarios)
# standard-all      — Both LLM + MCP baselines (13 scenarios)
# brownout          — LLM latency + rate limits
# mcp-slow-tools    — MCP latency
# mcp-tool-failures — MCP 503 errors
# mcp-mixed-transient — MCP latency + errors
"""

SCENARIOS_YAML_MCP_ONLY = """\
version: 1
preset: standard-mcp
# Standard MCP preset includes: 503 unavailable, timeout (5-15s),
# latency (3-8s), empty response, invalid JSON, schema violation, wrong content.
#
# Add project-specific scenarios below:
# scenarios:
#   - name: my-tool-scenario
#     summary: Description
#     target: mcp_tool
#     match:
#       tool_name: my_tool
#     fault:
#       kind: timeout
#       min_ms: 5000
#       max_ms: 10000
#     schedule:
#       mode: random
#       probability: 0.2
"""

SCENARIOS_YAML_ALL = """\
version: 1
preset: standard-all
# Standard preset includes 13 baseline scenarios for both LLM and MCP.
#
# Add project-specific scenarios below:
# scenarios:
#   - name: my-scenario
#     summary: Description
#     target: llm_chat
#     fault:
#       kind: http_error
#       status_code: 429
#     schedule:
#       mode: random
#       probability: 0.2
"""


@dataclass
class ServiceState:
    application: ApplicationConfig
    scenarios: ScenarioFile
    registry: MCPRegistry
    llm_runtime: LLMRuntime | None
    mcp_runtime: MCPRuntime | None
    history: RunHistory | None = None
    run_label: str | None = None


service_state: ServiceState | None = None


@dataclass
class LLMStats:
    total_requests: int = 0
    injected_faults: int = 0
    latency_injections: int = 0
    upstream_successes: int = 0
    upstream_failures: int = 0
    response_mutations: int = 0
    duplicate_requests: int = 0
    suspected_loops: int = 0
    fault_recoveries: int = 0
    unrecovered_faults: int = 0
    _pending_fault: bool = field(default=False, repr=False)
    seen_fingerprints: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    recent_requests: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=20))


@dataclass
class MCPStats:
    total_requests: int = 0
    tool_calls: int = 0
    injected_faults: int = 0
    latency_injections: int = 0
    upstream_successes: int = 0
    upstream_failures: int = 0
    response_mutations: int = 0
    duplicate_requests: int = 0
    suspected_loops: int = 0
    method_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    tool_call_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    tool_successes_by_name: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    tool_failures_by_name: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    seen_fingerprints: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    recent_requests: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=40))


@dataclass
class LLMRuntime:
    mode: str
    upstream_url: str
    auth_headers: dict[str, str]
    scenarios: list[Scenario]
    stats: LLMStats = field(default_factory=LLMStats)
    scenario_counters: dict[str, int] = field(default_factory=dict)

    async def handle_chat(self, request: Request, *, api_format: str = "openai") -> Response:
        body = await request.body()
        self._record_request(body)
        payload, has_parse_error = parse_json_body(body)
        error_fn = anthropic_error if api_format == "anthropic" else openai_error
        if has_parse_error:
            return JSONResponse(status_code=400, content=error_fn(400, message_override="Malformed JSON request body."))

        is_streaming = payload.get("stream") is True

        scenario = choose_matching_scenario(
            self.scenarios,
            "llm_chat",
            {"route": str(request.url.path), "method": request.method, "model": payload.get("model")},
            self.scenario_counters,
        )

        if scenario is not None:
            logger.debug("matched scenario %s for llm_chat", scenario.name)
            if scenario.fault.kind == "latency":
                self.stats.latency_injections += 1
                await apply_latency_fault(scenario)
            elif scenario.fault.kind == "http_error":
                self.stats.injected_faults += 1
                self.stats.upstream_failures += 1
                self.stats._pending_fault = True
                logger.info("injecting http_error %d via %s", scenario.fault.status_code or 500, scenario.name)
                return JSONResponse(status_code=scenario.fault.status_code or 500, content=error_fn(scenario.fault.status_code or 500))
        else:
            if self.stats._pending_fault:
                self.stats.fault_recoveries += 1
                self.stats._pending_fault = False

        upstream_path = "/v1/messages" if api_format == "anthropic" else "/v1/chat/completions"

        if is_streaming:
            return await self._handle_streaming(body, request, api_format, upstream_path, scenario)

        if self.mode == "mock":
            mock_fn = mock_anthropic_completion if api_format == "anthropic" else mock_completion
            response_body = json.dumps(mock_fn(payload)).encode("utf-8")
        else:
            async with httpx.AsyncClient(timeout=120.0) as client:
                try:
                    upstream = await client.post(
                        f"{self.upstream_url.rstrip('/')}{upstream_path}",
                        content=body,
                        headers=filter_request_headers(request.headers, self.auth_headers),
                    )
                except httpx.HTTPError as exc:
                    self.stats.upstream_failures += 1
                    logger.warning("upstream unreachable: %s", exc)
                    return JSONResponse(
                        status_code=502,
                        content=error_fn(502, message_override=f"AgentBreak could not reach upstream: {exc}"),
                    )
            if upstream.status_code >= 400:
                self.stats.upstream_failures += 1
                return Response(
                    content=upstream.content,
                    status_code=upstream.status_code,
                    headers=filter_response_headers(upstream.headers),
                    media_type=upstream.headers.get("content-type"),
                )
            response_body = upstream.content

        if scenario is not None and scenario.fault.kind in {"empty_response", "invalid_json", "large_response", "wrong_content", "schema_violation"}:
            mutate_fn = mutate_anthropic_body if api_format == "anthropic" else mutate_llm_body
            response_body = mutate_fn(response_body, scenario)
            self.stats.response_mutations += 1
            self.stats.upstream_successes += 1
            return Response(content=response_body, status_code=200, media_type="application/json")

        self.stats.upstream_successes += 1
        if self.mode == "proxy":
            return Response(
                content=response_body,
                status_code=200,
                headers=filter_response_headers(upstream.headers),
                media_type=upstream.headers.get("content-type"),
            )
        return Response(content=response_body, status_code=200, media_type="application/json")

    async def _handle_streaming(
        self,
        body: bytes,
        request: Request,
        api_format: str,
        upstream_path: str,
        scenario: Scenario | None,
    ) -> Response:
        error_fn = anthropic_error if api_format == "anthropic" else openai_error

        if scenario is not None and scenario.fault.kind in {"empty_response", "invalid_json", "large_response", "wrong_content", "schema_violation"}:
            logger.debug("skipping response mutation %s for streaming request", scenario.fault.kind)

        if self.mode == "mock":
            mock_fn = mock_openai_stream if api_format == "openai" else mock_anthropic_stream
            self.stats.upstream_successes += 1
            return StreamingResponse(mock_fn(), status_code=200, media_type="text/event-stream")

        url = f"{self.upstream_url.rstrip('/')}{upstream_path}"
        req_headers = filter_request_headers(request.headers, self.auth_headers)
        client = httpx.AsyncClient(timeout=120.0)
        try:
            req = client.build_request("POST", url, content=body, headers=req_headers)
            upstream = await client.send(req, stream=True)
        except httpx.HTTPError as exc:
            await client.aclose()
            self.stats.upstream_failures += 1
            logger.warning("upstream unreachable: %s", exc)
            return JSONResponse(
                status_code=502,
                content=error_fn(502, message_override=f"AgentBreak could not reach upstream: {exc}"),
            )

        if upstream.status_code >= 400:
            error_body = await upstream.aread()
            await upstream.aclose()
            await client.aclose()
            self.stats.upstream_failures += 1
            return Response(
                content=error_body,
                status_code=upstream.status_code,
                headers=filter_response_headers(upstream.headers),
                media_type=upstream.headers.get("content-type"),
            )

        self.stats.upstream_successes += 1

        async def generate():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(
            generate(),
            status_code=200,
            headers=filter_response_headers(upstream.headers),
            media_type=upstream.headers.get("content-type", "text/event-stream"),
        )

    def scorecard_data(self) -> dict[str, Any]:
        unrecovered = self.stats.injected_faults - self.stats.fault_recoveries
        if unrecovered < 0:
            unrecovered = 0
        self.stats.unrecovered_faults = unrecovered
        score = 100
        score -= self.stats.injected_faults * 3
        score -= self.stats.upstream_failures * 12
        score -= self.stats.duplicate_requests * 2
        score -= self.stats.suspected_loops * 10
        score += self.stats.fault_recoveries * 5
        score = max(0, min(100, score))
        if self.stats.upstream_failures == 0 and self.stats.suspected_loops == 0:
            outcome = "PASS"
        elif self.stats.upstream_successes > 0:
            outcome = "DEGRADED"
        else:
            outcome = "FAIL"
        return {
            "requests_seen": self.stats.total_requests,
            "injected_faults": self.stats.injected_faults,
            "latency_injections": self.stats.latency_injections,
            "upstream_successes": self.stats.upstream_successes,
            "upstream_failures": self.stats.upstream_failures,
            "response_mutations": self.stats.response_mutations,
            "duplicate_requests": self.stats.duplicate_requests,
            "suspected_loops": self.stats.suspected_loops,
            "fault_recoveries": self.stats.fault_recoveries,
            "unrecovered_faults": unrecovered,
            "run_outcome": outcome,
            "resilience_score": score,
        }

    def current_requests(self) -> dict[str, Any]:
        return {"recent_requests": list(self.stats.recent_requests)}

    def _record_request(self, body: bytes) -> None:
        self.stats.total_requests += 1
        fingerprint = hashlib.sha256(body).hexdigest()
        self.stats.seen_fingerprints[fingerprint] += 1
        if len(self.stats.seen_fingerprints) > 10000:
            self.stats.seen_fingerprints.clear()
            self.stats.seen_fingerprints[fingerprint] = 1
        seen = self.stats.seen_fingerprints[fingerprint]
        if seen > 1:
            self.stats.duplicate_requests += 1
        if seen > 2:
            self.stats.suspected_loops += 1
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {"raw": body.decode("utf-8", errors="replace")}
        self.stats.recent_requests.append({"fingerprint": fingerprint, "count": seen, "body": payload})


@dataclass
class MCPRuntime:
    upstream_url: str
    auth_headers: dict[str, str]
    registry: MCPRegistry
    scenarios: list[Scenario]
    config: MCPConfig = field(default_factory=MCPConfig)
    scenario_counters: dict[str, int] = field(default_factory=dict)
    session_id: str | None = None
    upstream_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    stats: MCPStats = field(default_factory=MCPStats)

    async def handle_rpc(self, request: Request) -> Response:
        body = await request.body()
        if not body:
            return Response(content=b"", media_type="application/json")
        payload, has_parse_error = parse_json_body(body)
        if has_parse_error:
            self._record_request(None, {"method": "parse_error", "path": str(request.url.path)})
            return JSONResponse(
                status_code=400,
                content={"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            )
        method = payload.get("method")
        request_id = payload.get("id")
        entry: dict[str, Any] = {"method": method, "path": str(request.url.path)}
        if method == "tools/call":
            params = payload.get("params", {})
            entry["tool_name"] = params.get("name")
            entry["has_arguments"] = bool(params.get("arguments"))
        self._record_request(payload, entry)

        if method == "initialize":
            await self._initialize_upstream()
            return JSONResponse(
                content={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": MCP_PROTOCOL_VERSION,
                        "capabilities": {
                            "tools": {"listChanged": False},
                            "resources": {"listChanged": False} if self.registry.resources else {},
                            "prompts": {"listChanged": False} if self.registry.prompts else {},
                        },
                        "serverInfo": {"name": "agentbreak-mcp", "version": __version__},
                    },
                }
            )
        if method == "notifications/initialized":
            await self._notify_upstream_initialized()
            return Response(status_code=202)
        if method == "tools/list":
            return JSONResponse(content={"jsonrpc": "2.0", "id": request_id, "result": {"tools": [tool.model_dump(by_alias=True) for tool in self.registry.tools]}})
        if method == "resources/list":
            return JSONResponse(content={"jsonrpc": "2.0", "id": request_id, "result": {"resources": [resource.model_dump(by_alias=True) for resource in self.registry.resources]}})
        if method == "prompts/list":
            return JSONResponse(content={"jsonrpc": "2.0", "id": request_id, "result": {"prompts": [prompt.model_dump(by_alias=True) for prompt in self.registry.prompts]}})
        if method == "tools/call":
            return await self._handle_action(payload, request_id, "tools/call")
        if method == "resources/read":
            return await self._handle_action(payload, request_id, "resources/read")
        if method == "prompts/get":
            return await self._handle_action(payload, request_id, "prompts/get")
        return JSONResponse(status_code=404, content={"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Unknown method {method}"}})

    async def _initialize_upstream(self) -> None:
        if not self.upstream_url or self.session_id or self.config.mode == "mock":
            return
        async with self.upstream_lock:
            if self.session_id:
                return
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    self.upstream_url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": MCP_PROTOCOL_VERSION,
                            "capabilities": {"tools": {}},
                            "clientInfo": {"name": "agentbreak", "version": __version__},
                        },
                    },
                    headers={
                        "content-type": "application/json",
                        "accept": "application/json, text/event-stream",
                        "mcp-protocol-version": MCP_PROTOCOL_VERSION,
                        **self.auth_headers,
                    },
                )
                response.raise_for_status()
                parse_mcp_response(response)
                self.session_id = response.headers.get("mcp-session-id")

    async def _notify_upstream_initialized(self) -> None:
        if not self.upstream_url or not self.session_id:
            return
        async with self.upstream_lock:
            async with httpx.AsyncClient(timeout=30.0) as client:
                await client.post(
                    self.upstream_url,
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                    headers={
                        "content-type": "application/json",
                        "accept": "application/json, text/event-stream",
                        "mcp-protocol-version": MCP_PROTOCOL_VERSION,
                        "mcp-session-id": self.session_id,
                        **self.auth_headers,
                    },
                )

    async def _handle_action(self, payload: dict[str, Any], request_id: Any, method: str) -> Response:
        params = payload.get("params", {})
        action_name = params.get("name") or params.get("uri") or ""
        if method == "tools/call":
            self.stats.tool_calls += 1
            self.stats.tool_call_counts[action_name] += 1
        scenario = choose_matching_scenario(
            self.scenarios,
            "mcp_tool",
            {"tool_name": action_name, "method": method, "route": "/mcp"},
            self.scenario_counters,
        )
        if scenario is not None and scenario.fault.kind in {"latency", "timeout"}:
            self.stats.latency_injections += 1
            await apply_latency_fault(scenario)
            if scenario.fault.kind == "timeout":
                self.stats.injected_faults += 1
                self.stats.upstream_failures += 1
                if method == "tools/call":
                    self.stats.tool_failures_by_name[action_name] += 1
                return JSONResponse(status_code=504, content={"jsonrpc": "2.0", "id": request_id, "error": {"code": -32001, "message": "MCP action timed out"}})
        if scenario is not None and scenario.fault.kind == "http_error":
            self.stats.injected_faults += 1
            self.stats.upstream_failures += 1
            if method == "tools/call":
                self.stats.tool_failures_by_name[action_name] += 1
            return JSONResponse(status_code=scenario.fault.status_code or 500, content={"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": "AgentBreak injected MCP transport error"}})

        result = await self._call_upstream_or_mock(method, params, request_id)
        if isinstance(result, Response):
            if method == "tools/call":
                self.stats.tool_failures_by_name[action_name] += 1
            return result
        if scenario is not None and scenario.fault.kind in {"empty_response", "invalid_json", "schema_violation", "wrong_content", "large_response"}:
            mutated = mutate_mcp_result(result, scenario)
            self.stats.response_mutations += 1
            if isinstance(mutated, bytes):
                self.stats.injected_faults += 1
                self.stats.upstream_successes += 1
                if method == "tools/call":
                    self.stats.tool_successes_by_name[action_name] += 1
                return Response(content=mutated, status_code=200, media_type="application/json")
            result = mutated
        self.stats.upstream_successes += 1
        if method == "tools/call":
            self.stats.tool_successes_by_name[action_name] += 1
        return JSONResponse(content={"jsonrpc": "2.0", "id": request_id, "result": result})

    async def _call_upstream_or_mock(self, method: str, params: dict[str, Any], request_id: Any) -> dict[str, Any] | Response:
        if self.upstream_url and self.config.mode != "mock":
            async with self.upstream_lock:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    try:
                        response = await self._post_upstream_rpc(client, method, params, request_id)
                        if isinstance(response, Response):
                            return response
                        return response.get("result", mock_mcp_result(method, params, self.registry))
                    except httpx.HTTPError as exc:
                        self.stats.upstream_failures += 1
                        return JSONResponse(status_code=502, content={"jsonrpc": "2.0", "id": request_id, "error": {"code": -32002, "message": f"Upstream MCP error: {exc}"}})
        return mock_mcp_result(method, params, self.registry)

    async def _post_upstream_rpc(
        self,
        client: httpx.AsyncClient,
        method: str,
        params: dict[str, Any],
        request_id: Any,
    ) -> dict[str, Any] | Response:
        await self._ensure_upstream_session(client)
        response = await client.post(
            self.upstream_url,
            json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
            headers=self._upstream_headers(),
        )
        if self._is_invalid_session_response(response):
            self.session_id = None
            await self._ensure_upstream_session(client)
            response = await client.post(
                self.upstream_url,
                json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
                headers=self._upstream_headers(),
            )
        if response.status_code >= 400:
            self.stats.upstream_failures += 1
            return JSONResponse(
                status_code=response.status_code,
                content=parse_mcp_response(response),
            )
        if response.headers.get("mcp-session-id"):
            self.session_id = response.headers["mcp-session-id"]
        return parse_mcp_response(response)

    async def _ensure_upstream_session(self, client: httpx.AsyncClient) -> None:
        if self.session_id:
            return
        response = await client.post(
            self.upstream_url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                    "clientInfo": {"name": "agentbreak", "version": __version__},
                },
            },
            headers=self._upstream_headers(include_session=False),
        )
        response.raise_for_status()
        parse_mcp_response(response)
        self.session_id = response.headers.get("mcp-session-id")
        await client.post(
            self.upstream_url,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=self._upstream_headers(),
        )

    def _upstream_headers(self, *, include_session: bool = True) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            "mcp-protocol-version": MCP_PROTOCOL_VERSION,
            **self.auth_headers,
        }
        if include_session and self.session_id:
            headers["mcp-session-id"] = self.session_id
        return headers

    def _is_invalid_session_response(self, response: httpx.Response) -> bool:
        if response.status_code not in {400, 404}:
            return False
        try:
            payload = parse_mcp_response(response)
        except Exception:
            return False
        error = payload.get("error", {})
        message = str(error.get("message", "")).lower()
        return "session" in message

    def scorecard_data(self) -> dict[str, Any]:
        score = 100
        score -= self.stats.injected_faults * 5
        score -= self.stats.upstream_failures * 12
        score -= self.stats.duplicate_requests * 2
        score -= self.stats.suspected_loops * 10
        score = max(0, min(100, score))
        if self.stats.upstream_failures == 0 and self.stats.suspected_loops == 0:
            outcome = "PASS"
        elif self.stats.upstream_successes > 0:
            outcome = "DEGRADED"
        else:
            outcome = "FAIL"
        return {
            "requests_seen": self.stats.total_requests,
            "tool_calls": self.stats.tool_calls,
            "method_counts": dict(self.stats.method_counts),
            "tool_call_counts": dict(self.stats.tool_call_counts),
            "tool_successes_by_name": dict(self.stats.tool_successes_by_name),
            "tool_failures_by_name": dict(self.stats.tool_failures_by_name),
            "injected_faults": self.stats.injected_faults,
            "latency_injections": self.stats.latency_injections,
            "response_mutations": self.stats.response_mutations,
            "upstream_successes": self.stats.upstream_successes,
            "upstream_failures": self.stats.upstream_failures,
            "duplicate_requests": self.stats.duplicate_requests,
            "suspected_loops": self.stats.suspected_loops,
            "run_outcome": outcome,
            "resilience_score": score,
        }

    def current_requests(self) -> dict[str, Any]:
        return {"recent_requests": list(self.stats.recent_requests)}

    def _record_request(self, payload: dict[str, Any] | None, entry: dict[str, Any]) -> None:
        self.stats.total_requests += 1
        method = str(entry.get("method") or "unknown")
        self.stats.method_counts[method] += 1
        if payload is not None:
            fingerprint = fingerprint_mcp_request(payload)
            self.stats.seen_fingerprints[fingerprint] += 1
            if len(self.stats.seen_fingerprints) > 10000:
                self.stats.seen_fingerprints.clear()
                self.stats.seen_fingerprints[fingerprint] = 1
            seen = self.stats.seen_fingerprints[fingerprint]
            entry["fingerprint"] = fingerprint
            entry["count"] = seen
            if seen > 1:
                self.stats.duplicate_requests += 1
            if seen > 2:
                self.stats.suspected_loops += 1
        self.stats.recent_requests.append(entry)


def load_service_state(
    config_path: str | None,
    scenarios_path: str | None,
    registry_path: str | None,
    *,
    require_registry: bool = True,
) -> ServiceState:
    application = load_application_config(config_path)
    scenarios = load_scenarios(scenarios_path)
    validate_scenarios(scenarios)
    registry = MCPRegistry()
    if application.mcp.enabled:
        try:
            registry = load_registry(registry_path)
        except ValueError:
            if require_registry:
                raise
            logger.warning("MCP enabled but registry not found. Run `agentbreak inspect` to create it.")

    llm_runtime = None
    if application.llm.enabled:
        llm_runtime = LLMRuntime(
            mode=application.llm.mode,
            upstream_url=application.llm.upstream_url,
            auth_headers=application.llm.auth.headers(),
            scenarios=scenarios.scenarios,
        )

    mcp_runtime = None
    if application.mcp.enabled:
        mcp_runtime = MCPRuntime(
            upstream_url=application.mcp.upstream_url,
            auth_headers=application.mcp.auth.headers(),
            registry=registry,
            scenarios=scenarios.scenarios,
            config=application.mcp,
        )

    history = RunHistory(db_path=application.history.db_path) if application.history.enabled else None

    return ServiceState(
        application=application,
        scenarios=scenarios,
        registry=registry,
        llm_runtime=llm_runtime,
        mcp_runtime=mcp_runtime,
        history=history,
    )


def choose_matching_scenario(
    scenarios: list[Scenario],
    target: str,
    request: dict[str, Any],
    counters: dict[str, int],
) -> Scenario | None:
    for scenario in scenarios:
        if scenario.target != target:
            continue
        if not scenario.match.matches(request):
            continue
        count = counters.get(scenario.name, 0) + 1
        counters[scenario.name] = count
        if should_apply_scenario(scenario, count):
            return scenario
    return None


def should_apply_scenario(scenario: Scenario, count: int) -> bool:
    if scenario.schedule.mode == "always":
        return True
    if scenario.schedule.mode == "random":
        return random.random() < max(0.0, min(1.0, scenario.schedule.probability))
    assert scenario.schedule.every is not None and scenario.schedule.length is not None
    return (count - 1) % scenario.schedule.every < scenario.schedule.length


async def apply_latency_fault(scenario: Scenario) -> None:
    assert scenario.fault.min_ms is not None and scenario.fault.max_ms is not None
    await asyncio.sleep(random.randint(scenario.fault.min_ms, scenario.fault.max_ms) / 1000)


def _should_mock_tool_call(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Check if the request has tools and the last message isn't a tool result — if so, return a mock tool call."""
    tools = payload.get("tools")
    if not tools:
        return None
    messages = payload.get("messages") or []
    if messages and messages[-1].get("role") == "tool":
        return None
    tool = tools[0]
    fn = tool.get("function", tool)
    return {"name": fn.get("name", "mock_tool"), "arguments": "{}"}


def mock_anthropic_completion(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    tool_call = None
    tools = payload.get("tools")
    if tools:
        messages = payload.get("messages") or []
        last_role = messages[-1].get("role") if messages else None
        if last_role != "tool":
            tool = tools[0]
            tool_call = {"type": "tool_use", "id": "toolu_agentbreak_mock", "name": tool.get("name", "mock_tool"), "input": {}}
    if tool_call:
        return {
            "id": "msg-agentbreak-mock",
            "type": "message",
            "role": "assistant",
            "content": [tool_call],
            "model": "agentbreak-mock",
            "stop_reason": "tool_use",
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
    return {
        "id": "msg-agentbreak-mock",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "AgentBreak mock response."}],
        "model": "agentbreak-mock",
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


def mock_completion(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    tool_call = _should_mock_tool_call(payload)
    if tool_call:
        return {
            "id": "chatcmpl-agentbreak-mock",
            "object": "chat.completion",
            "created": 0,
            "model": "agentbreak-mock",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{"id": "call_agentbreak_mock", "type": "function", "function": tool_call}],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
    return {
        "id": "chatcmpl-agentbreak-mock",
        "object": "chat.completion",
        "created": 0,
        "model": "agentbreak-mock",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "AgentBreak mock response."},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


async def mock_openai_stream():
    chunk = {
        "id": "chatcmpl-agentbreak-mock",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "agentbreak-mock",
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": "AgentBreak mock response."}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(chunk)}\n\n"
    yield "data: [DONE]\n\n"


async def mock_anthropic_stream():
    yield f"event: message_start\ndata: {json.dumps({'type': 'message_start', 'message': {'id': 'msg-agentbreak-mock', 'type': 'message', 'role': 'assistant', 'content': [], 'model': 'agentbreak-mock', 'stop_reason': None, 'usage': {'input_tokens': 0, 'output_tokens': 0}}})}\n\n"
    yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})}\n\n"
    yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'AgentBreak mock response.'}})}\n\n"
    yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"
    yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 0}})}\n\n"
    yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"


def mutate_llm_body(body: bytes, scenario: Scenario) -> bytes:
    kind = scenario.fault.kind
    if kind == "empty_response":
        return b""
    if kind == "invalid_json":
        return b"{not valid"
    payload = json.loads(body.decode("utf-8"))
    if kind == "large_response":
        payload["choices"][0]["message"]["content"] = large_text(scenario.fault.size_bytes or 0)
        return json.dumps(payload).encode("utf-8")
    if kind == "wrong_content":
        payload["choices"][0]["message"]["content"] = scenario.fault.body or "AgentBreak injected wrong content."
        return json.dumps(payload).encode("utf-8")
    if kind == "schema_violation":
        return apply_response_behavior(body, "malformed_tool_calls")
    return body


def mutate_anthropic_body(body: bytes, scenario: Scenario) -> bytes:
    kind = scenario.fault.kind
    if kind == "empty_response":
        return b""
    if kind == "invalid_json":
        return b"{not valid"
    payload = json.loads(body.decode("utf-8"))
    if kind == "large_response":
        payload["content"] = [{"type": "text", "text": large_text(scenario.fault.size_bytes or 0)}]
        return json.dumps(payload).encode("utf-8")
    if kind == "wrong_content":
        payload["content"] = [{"type": "text", "text": scenario.fault.body or "AgentBreak injected wrong content."}]
        return json.dumps(payload).encode("utf-8")
    if kind == "schema_violation":
        return apply_response_behavior(body, "malformed_tool_use")
    return body


def mutate_mcp_result(result: dict[str, Any], scenario: Scenario) -> bytes | dict[str, Any]:
    kind = scenario.fault.kind
    if kind == "empty_response":
        return b""
    if kind == "invalid_json":
        return b"{not valid"
    result_kind = result.get("_meta", {}).get("kind", "tool")
    identifier = result.get("_meta", {}).get("identifier", "tool")
    if kind == "schema_violation":
        if result_kind == "resource":
            return {"contents": "INVALID"}
        if result_kind == "prompt":
            return {"messages": "INVALID"}
        return {"content": "INVALID"}
    if kind == "wrong_content":
        return mock_mcp_payload(result_kind, identifier, scenario.fault.body or "wrong content")
    if kind == "large_response":
        return mock_mcp_payload(result_kind, identifier, large_text(scenario.fault.size_bytes or 0))
    return result


def large_text(size_bytes: int) -> str:
    chunk = "AgentBreak large response. "
    repeats = max(1, (size_bytes // len(chunk)) + 1)
    return (chunk * repeats)[:size_bytes]


def mcp_success_result(tool_name: str, payload: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": payload}],
        "isError": False,
        "_meta": {"kind": "tool", "identifier": tool_name, "tool_name": tool_name},
    }


def mcp_resource_result(uri: str, payload: str, mime_type: str = "text/plain") -> dict[str, Any]:
    return {
        "contents": [{"uri": uri, "mimeType": mime_type, "text": payload}],
        "_meta": {"kind": "resource", "identifier": uri},
    }


def mcp_prompt_result(name: str, payload: str) -> dict[str, Any]:
    return {
        "messages": [{"role": "user", "content": {"type": "text", "text": payload}}],
        "_meta": {"kind": "prompt", "identifier": name},
    }


def mock_mcp_payload(kind: str, identifier: str, payload: str) -> dict[str, Any]:
    if kind == "resource":
        return mcp_resource_result(identifier, payload)
    if kind == "prompt":
        return mcp_prompt_result(identifier, payload)
    return mcp_success_result(identifier, payload)


def mock_mcp_result(method: str, params: dict[str, Any], registry: MCPRegistry) -> dict[str, Any]:
    if method == "tools/call":
        tool_name = str(params.get("name", "tool"))
        return mcp_success_result(tool_name, f"mock result for {tool_name}")
    if method == "resources/read":
        uri = str(params.get("uri", "resource://mock"))
        resource = next((item for item in registry.resources if item.uri == uri), None)
        return mcp_resource_result(uri, f"mock resource for {uri}", resource.mime_type if resource else "text/plain")
    if method == "prompts/get":
        name = str(params.get("name", "prompt"))
        return mcp_prompt_result(name, f"mock prompt for {name}")
    return {}


def filter_request_headers(headers: httpx.Headers, extra_headers: dict[str, str]) -> dict[str, str]:
    skip = {"host", "content-length"}
    filtered = {key: value for key, value in headers.items() if key.lower() not in skip}
    filtered.update(extra_headers)
    return filtered


def filter_response_headers(headers: httpx.Headers) -> dict[str, str]:
    skip = {"content-length", "transfer-encoding", "connection"}
    return {key: value for key, value in headers.items() if key.lower() not in skip}


def parse_json_body(body: bytes) -> tuple[dict[str, Any], bool]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}, True
    if not isinstance(payload, dict):
        return {}, True
    return payload, False


def fingerprint_mcp_request(payload: dict[str, Any]) -> str:
    method = payload.get("method")
    params = payload.get("params")
    if method == "tools/call" and isinstance(params, dict):
        material = {
            "method": method,
            "name": params.get("name"),
            "arguments": params.get("arguments", {}),
        }
    else:
        material = {
            "method": method,
            "params": params if isinstance(params, dict) else None,
        }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()


def openai_error(status_code: int, message_override: str | None = None) -> dict[str, Any]:
    error_map = {
        400: ("Invalid request injected by AgentBreak.", "invalid_request_error"),
        401: ("Authentication failure injected by AgentBreak.", "authentication_error"),
        403: ("Permission failure injected by AgentBreak.", "permission_error"),
        404: ("Resource not found injected by AgentBreak.", "not_found_error"),
        413: ("Request too large injected by AgentBreak.", "invalid_request_error"),
        429: ("Rate limit exceeded by AgentBreak fault injection.", "rate_limit_error"),
        500: ("Upstream failure injected by AgentBreak.", "server_error"),
        503: ("Service unavailable injected by AgentBreak.", "server_error"),
    }
    message, error_type = error_map.get(status_code, error_map[500])
    if message_override is not None:
        message = message_override
    return {"error": {"message": message, "type": error_type, "code": status_code}}


def anthropic_error(status_code: int, message_override: str | None = None) -> dict[str, Any]:
    error_map = {
        400: ("Invalid request injected by AgentBreak.", "invalid_request_error"),
        401: ("Authentication failure injected by AgentBreak.", "authentication_error"),
        403: ("Permission failure injected by AgentBreak.", "permission_error"),
        404: ("Resource not found injected by AgentBreak.", "not_found_error"),
        429: ("Rate limit exceeded by AgentBreak fault injection.", "rate_limit_error"),
        500: ("Upstream failure injected by AgentBreak.", "api_error"),
        502: ("AgentBreak could not reach upstream.", "api_error"),
        503: ("Service unavailable injected by AgentBreak.", "overloaded_error"),
    }
    message, error_type = error_map.get(status_code, ("Upstream failure injected by AgentBreak.", "api_error"))
    if message_override is not None:
        message = message_override
    return {"type": "error", "error": {"type": error_type, "message": message}}


def require_service_state() -> ServiceState:
    if service_state is None:
        raise RuntimeError("AgentBreak is not configured. Run `agentbreak serve` or set main.service_state in tests.")
    return service_state


def install_signal_handlers() -> None:
    def handle_signal(signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt(f"received signal {signum}")

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)


def print_scorecard() -> None:
    state = service_state
    if state is None:
        return
    lines = [
        "",
        "AgentBreak Resilience Scorecard",
        "",
    ]
    if state.llm_runtime is not None:
        llm = state.llm_runtime.scorecard_data()
        lines.extend(
            [
                "LLM Runtime",
                f"Requests Seen: {llm['requests_seen']}",
                f"Injected Faults: {llm['injected_faults']}",
                f"Latency Injections: {llm['latency_injections']}",
                f"Upstream Successes: {llm['upstream_successes']}",
                f"Upstream Failures: {llm['upstream_failures']}",
                f"Duplicate Requests: {llm['duplicate_requests']}",
                f"Suspected Loops: {llm['suspected_loops']}",
                f"Run Outcome: {llm['run_outcome']}",
                f"Resilience Score: {llm['resilience_score']}/100",
                "",
            ]
        )
    if state.mcp_runtime is not None:
        mcp = state.mcp_runtime.scorecard_data()
        lines.extend(
            [
                "MCP Runtime",
                f"Requests Seen: {mcp['requests_seen']}",
                f"Tool Calls: {mcp['tool_calls']}",
                f"Injected Faults: {mcp['injected_faults']}",
                f"Latency Injections: {mcp['latency_injections']}",
                f"Response Mutations: {mcp['response_mutations']}",
                f"Upstream Successes: {mcp['upstream_successes']}",
                f"Upstream Failures: {mcp['upstream_failures']}",
                f"Duplicate Requests: {mcp['duplicate_requests']}",
                f"Suspected Loops: {mcp['suspected_loops']}",
                f"Run Outcome: {mcp['run_outcome']}",
                f"Resilience Score: {mcp['resilience_score']}/100",
                "",
            ]
        )
    print("\n".join(lines), file=sys.stderr)


def _save_run_to_history() -> None:
    state = service_state
    if state is None or state.history is None:
        return
    try:
        llm_scorecard = state.llm_runtime.scorecard_data() if state.llm_runtime else None
        mcp_scorecard = state.mcp_runtime.scorecard_data() if state.mcp_runtime else None
        scenarios = [s.model_dump() for s in state.scenarios.scenarios] if state.scenarios.scenarios else None
        state.history.save_run(llm_scorecard=llm_scorecard, mcp_scorecard=mcp_scorecard, scenarios=scenarios, label=state.run_label)
        logger.info("saved run to history")
    except Exception:
        logger.debug("failed to save run to history", exc_info=True)


@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def proxy_chat_completions(request: Request):
    state = require_service_state()
    if state.llm_runtime is None:
        return JSONResponse(status_code=404, content={"error": "LLM runtime is disabled"})
    return await state.llm_runtime.handle_chat(request)


@app.post("/v1/messages")
@app.post("/messages")
async def proxy_anthropic_messages(request: Request):
    state = require_service_state()
    if state.llm_runtime is None:
        return JSONResponse(status_code=404, content={"type": "error", "error": {"type": "not_found_error", "message": "LLM runtime is disabled"}})
    return await state.llm_runtime.handle_chat(request, api_format="anthropic")


@app.post("/mcp")
async def handle_mcp(request: Request):
    state = require_service_state()
    if state.mcp_runtime is None:
        return JSONResponse(status_code=404, content={"error": "MCP runtime is disabled"})
    return await state.mcp_runtime.handle_rpc(request)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/_agentbreak/reset")
async def reset_agentbreak_stats() -> dict[str, str]:
    state = require_service_state()
    if state.llm_runtime is not None:
        state.llm_runtime.stats = LLMStats()
        state.llm_runtime.scenario_counters.clear()
    if state.mcp_runtime is not None:
        state.mcp_runtime.stats = MCPStats()
        state.mcp_runtime.scenario_counters.clear()
    return {"status": "reset"}


@app.get("/_agentbreak/scorecard")
async def get_agentbreak_scorecard() -> dict[str, Any]:
    state = require_service_state()
    if state.llm_runtime is None:
        return {"requests_seen": 0}
    return state.llm_runtime.scorecard_data()


@app.get("/_agentbreak/requests")
async def get_agentbreak_requests() -> dict[str, Any]:
    state = require_service_state()
    if state.llm_runtime is None:
        return {"recent_requests": []}
    return state.llm_runtime.current_requests()


@app.get("/_agentbreak/llm-scorecard")
async def get_agentbreak_llm_scorecard() -> dict[str, Any]:
    return await get_agentbreak_scorecard()


@app.get("/_agentbreak/llm-requests")
async def get_agentbreak_llm_requests() -> dict[str, Any]:
    return await get_agentbreak_requests()


@app.get("/_agentbreak/mcp-scorecard")
async def get_agentbreak_mcp_scorecard() -> dict[str, Any]:
    state = require_service_state()
    if state.mcp_runtime is None:
        return {"requests_seen": 0, "tool_calls": 0}
    return state.mcp_runtime.scorecard_data()


@app.get("/_agentbreak/mcp-requests")
async def get_agentbreak_mcp_requests() -> dict[str, Any]:
    state = require_service_state()
    if state.mcp_runtime is None:
        return {"recent_requests": []}
    return state.mcp_runtime.current_requests()


@app.get("/_agentbreak/history")
async def get_agentbreak_history(limit: int = 20) -> dict[str, Any]:
    state = require_service_state()
    if state.history is None:
        return {"runs": []}
    return {"runs": state.history.get_runs(limit=limit)}


@app.get("/_agentbreak/history/{run_id}")
async def get_agentbreak_history_run(run_id: int) -> JSONResponse:
    state = require_service_state()
    if state.history is None:
        return JSONResponse(status_code=404, content={"error": "History is disabled."})
    result = state.history.get_run(run_id)
    if result is None:
        return JSONResponse(status_code=404, content={"error": f"Run {run_id} not found."})
    return JSONResponse(content=result)


def _detect_framework() -> dict[str, str]:
    """Scan project files to detect LLM framework, MCP usage, and suggest config."""
    detection: dict[str, str] = {}
    # Check pyproject.toml
    pyproject = Path("pyproject.toml")
    requirements = Path("requirements.txt")
    content = ""
    if pyproject.exists():
        content += pyproject.read_text(encoding="utf-8")
    if requirements.exists():
        content += requirements.read_text(encoding="utf-8")
    # Also check package.json for JS projects
    package_json = Path("package.json")
    if package_json.exists():
        content += package_json.read_text(encoding="utf-8")

    content_lower = content.lower()

    if "langchain-openai" in content_lower or "openai" in content_lower:
        detection["provider"] = "openai"
        detection["upstream_url"] = "https://api.openai.com"
        detection["env"] = "OPENAI_API_KEY"
    if "langchain-anthropic" in content_lower or "anthropic" in content_lower:
        detection["provider"] = "anthropic"
        detection["upstream_url"] = "https://api.anthropic.com"
        detection["env"] = "ANTHROPIC_API_KEY"

    # --- MCP detection ---
    # Check dependency files for MCP packages
    mcp_dep_markers = [
        "langchain-mcp-adapters",
        "langchain-mcp",
        "@modelcontextprotocol/sdk",
        "mcp-client",
        "fastmcp",
    ]
    for marker in mcp_dep_markers:
        if marker in content_lower:
            detection["mcp"] = "true"
            break

    # Scan Python source files for MCP imports/usage
    if "mcp" not in detection:
        mcp_source_markers = [
            "MultiServerMCPClient",
            "MCPClient",
            "from mcp",
            "import mcp",
            "mcp_tool",
            "langchain_mcp",
        ]
        for py_file in Path(".").glob("*.py"):
            try:
                src = py_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for marker in mcp_source_markers:
                if marker in src:
                    detection["mcp"] = "true"
                    break
            if "mcp" in detection:
                break

    # Check for custom gateways (TFY, LiteLLM, etc.)
    env_file = Path(".env")
    if env_file.exists():
        env_content = env_file.read_text(encoding="utf-8")
        for line in env_content.splitlines():
            if "GATEWAY_URL" in line and "=" in line:
                _, _, val = line.partition("=")
                val = val.strip().strip("\"'")
                if val:
                    detection["upstream_url"] = val
                    detection["provider"] = "gateway"
            if "TFY_API_KEY" in line:
                detection["env"] = "TFY_API_KEY"
            # Detect MCP upstream URL from env
            if "mcp" not in detection and "MCP" in line and "URL" in line and "=" in line:
                detection["mcp"] = "true"
            if "MCP" in line and "URL" in line and "=" in line:
                _, _, val = line.partition("=")
                val = val.strip().strip("\"'")
                if val:
                    detection["mcp_upstream_url"] = val
            if "MCP" in line and "API_KEY" in line and "=" in line:
                key_name = line.partition("=")[0].strip()
                detection["mcp_auth_env"] = key_name

    return detection


def _generate_application_yaml(detection: dict[str, str]) -> str:
    """Generate application.yaml content, customized if framework detected."""
    if not detection:
        return DEFAULT_APPLICATION_YAML
    upstream = detection.get("upstream_url", "")
    env_key = detection.get("env", "")
    provider = detection.get("provider", "")
    mcp_detected = detection.get("mcp") == "true"
    mcp_upstream = detection.get("mcp_upstream_url", "")
    mcp_auth_env = detection.get("mcp_auth_env", "")

    lines: list[str] = []

    # LLM section
    lines.append("llm:")
    lines.append("  enabled: true")
    lines.append("  mode: mock")
    if provider and upstream:
        lines.append(f"  # Detected: {provider}")
        lines.append("  # Switch to proxy mode to test against real upstream:")
        lines.append("  # mode: proxy")
        lines.append(f"  # upstream_url: {upstream}")
        lines.append("  # auth:")
        lines.append("  #   type: bearer")
        lines.append(f"  #   env: {env_key}")
    lines.append("")

    # MCP section — enabled when detected
    lines.append("mcp:")
    if mcp_detected:
        lines.append("  enabled: true")
        if mcp_upstream:
            lines.append(f"  upstream_url: {mcp_upstream}")
            lines.append("  # mode: mock  # Switch to mock mode to test without upstream")
        else:
            lines.append("  mode: mock")
            lines.append("  # upstream_url: http://127.0.0.1:8001/mcp")
            lines.append("  # mode: proxy  # Switch to proxy mode to test against real MCP server")
        if mcp_auth_env:
            lines.append("  auth:")
            lines.append("    type: bearer")
            lines.append(f"    env: {mcp_auth_env}")
        else:
            lines.append("  # auth:")
            lines.append("  #   type: bearer")
            lines.append("  #   env: MCP_TOKEN")
    else:
        lines.append("  enabled: false")
        lines.append("  # upstream_url: http://127.0.0.1:8001/mcp")
        lines.append("  # auth:")
        lines.append("  #   type: bearer")
        lines.append("  #   env: MCP_TOKEN")
    lines.append("")

    lines.append("serve:")
    lines.append("  port: 5005")
    lines.append("")

    return "\n".join(lines)


@cli.command(help="Initialize .agentbreak/ with default configuration files.")
def init() -> None:
    agentbreak_dir = Path(".agentbreak")
    agentbreak_dir.mkdir(exist_ok=True)

    detection = _detect_framework()
    if detection:
        typer.echo(f"Detected: {detection.get('provider', 'unknown')} (upstream: {detection.get('upstream_url', 'N/A')})")
        if detection.get("mcp") == "true":
            mcp_url = detection.get("mcp_upstream_url", "auto")
            typer.echo(f"Detected: MCP usage (upstream: {mcp_url})")

    app_path = agentbreak_dir / "application.yaml"
    if app_path.exists():
        typer.echo(f"Already exists: {app_path}")
    else:
        app_path.write_text(_generate_application_yaml(detection), encoding="utf-8")
        typer.echo(f"Created {app_path}")

    scenarios_path = agentbreak_dir / "scenarios.yaml"
    if scenarios_path.exists():
        typer.echo(f"Already exists: {scenarios_path}")
    else:
        has_mcp = detection.get("mcp") == "true"
        has_llm = detection.get("provider") is not None
        if has_llm and has_mcp:
            scenarios_yaml = SCENARIOS_YAML_ALL
        elif has_mcp:
            scenarios_yaml = SCENARIOS_YAML_MCP_ONLY
        else:
            scenarios_yaml = SCENARIOS_YAML_LLM_ONLY
        scenarios_path.write_text(scenarios_yaml, encoding="utf-8")
        typer.echo(f"Created {scenarios_path}")


@cli.command(help="Inspect an upstream MCP server, validate auth, and write a tool registry artifact.")
def inspect(
    config_path: str | None = typer.Option(None, "--config", help="Config path. Defaults to .agentbreak/application.yaml."),
    registry_path: str | None = typer.Option(None, "--registry", help="Registry output path. Defaults to ./.agentbreak/registry.json."),
) -> None:
    application = load_application_config(config_path)
    if not application.mcp.enabled:
        raise typer.BadParameter("mcp.enabled must be true to run inspect")

    registry = asyncio.run(inspect_mcp_server(application.mcp))
    output_path = save_registry(registry, registry_path)
    typer.echo(f"Discovered {len(registry.tools)} MCP tools")
    typer.echo(f"Wrote registry: {output_path}")


@cli.command(help="Serve AgentBreak using application.yaml, scenarios.yaml, and an MCP registry artifact.")
def serve(
    config_path: str | None = typer.Option(None, "--config", help="Config path. Defaults to .agentbreak/application.yaml."),
    scenarios_path: str | None = typer.Option(None, "--scenarios", help="Scenarios path. Defaults to .agentbreak/scenarios.yaml."),
    registry_path: str | None = typer.Option(None, "--registry", help="Registry path. Defaults to ./.agentbreak/registry.json."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose logging."),
    label: str | None = typer.Option(None, "--label", "-l", help="Label for this run in history."),
) -> None:
    global service_state

    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    service_state = load_service_state(config_path, scenarios_path, registry_path)
    service_state.run_label = label
    host = service_state.application.serve.host
    port = service_state.application.serve.port

    logger.info("starting on %s:%d", host, port)
    logger.info(
        "llm=%s mcp=%s scenarios=%d",
        service_state.application.llm.mode if service_state.application.llm.enabled else "off",
        "on" if service_state.application.mcp.enabled else "off",
        len(service_state.scenarios.scenarios),
    )

    install_signal_handlers()
    uv_level = "debug" if verbose else "warning"
    try:
        uvicorn.run(app, host=host, port=port, log_level=uv_level)
    finally:
        print_scorecard()
        _save_run_to_history()


def _check_upstream_auth(application: ApplicationConfig) -> list[str]:
    """Check connectivity and auth for proxy-mode upstreams. Returns list of status messages."""
    results: list[str] = []
    if application.llm.enabled and application.llm.mode == "proxy" and application.llm.upstream_url:
        url = application.llm.upstream_url.rstrip("/")
        headers = application.llm.auth.headers()
        # Detect provider from URL to pick the right health check
        is_anthropic = "anthropic" in url
        try:
            if is_anthropic:
                resp = httpx.post(
                    f"{url}/v1/messages",
                    headers={**headers, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                    json={"model": "claude-haiku-4-5-20251001", "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]},
                    timeout=10.0,
                )
            else:
                resp = httpx.get(f"{url}/v1/models", headers=headers, timeout=10.0)
            if resp.status_code in (200, 201):
                results.append(f"LLM upstream ({url}): OK")
            elif resp.status_code == 401:
                results.append(f"LLM upstream ({url}): AUTH FAILED (401)")
            elif resp.status_code == 403:
                results.append(f"LLM upstream ({url}): FORBIDDEN (403)")
            else:
                results.append(f"LLM upstream ({url}): HTTP {resp.status_code}")
        except httpx.ConnectError:
            results.append(f"LLM upstream ({url}): CONNECTION FAILED")
        except httpx.TimeoutException:
            results.append(f"LLM upstream ({url}): TIMEOUT")
        except Exception as exc:
            results.append(f"LLM upstream ({url}): ERROR ({exc})")

    if application.mcp.enabled and application.mcp.mode == "proxy" and application.mcp.upstream_url:
        url = application.mcp.upstream_url
        headers = application.mcp.auth.headers()
        try:
            resp = httpx.post(
                url,
                headers={**headers, "content-type": "application/json"},
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "agentbreak-check", "version": "1.0"}}},
                timeout=10.0,
            )
            if resp.status_code in (200, 201):
                results.append(f"MCP upstream ({url}): OK")
            elif resp.status_code == 401:
                results.append(f"MCP upstream ({url}): AUTH FAILED (401)")
            elif resp.status_code == 403:
                results.append(f"MCP upstream ({url}): FORBIDDEN (403)")
            else:
                results.append(f"MCP upstream ({url}): HTTP {resp.status_code}")
        except httpx.ConnectError:
            results.append(f"MCP upstream ({url}): CONNECTION FAILED")
        except httpx.TimeoutException:
            results.append(f"MCP upstream ({url}): TIMEOUT")
        except Exception as exc:
            results.append(f"MCP upstream ({url}): ERROR ({exc})")

    return results


@cli.command(help="Validate application, scenario, and registry files without starting the server.")
def validate(
    config_path: str | None = typer.Option(None, "--config", help="Config path. Defaults to .agentbreak/application.yaml."),
    scenarios_path: str | None = typer.Option(None, "--scenarios", help="Scenarios path. Defaults to .agentbreak/scenarios.yaml."),
    registry_path: str | None = typer.Option(None, "--registry", help="Registry path."),
    test_connection: bool = typer.Option(False, "--test-connection", help="Test upstream connectivity and auth for proxy-mode endpoints."),
) -> None:
    state = load_service_state(config_path, scenarios_path, registry_path, require_registry=False)
    typer.echo(
        "Config valid: "
        f"llm_enabled={state.application.llm.enabled} "
        f"mcp_enabled={state.application.mcp.enabled} "
        f"scenarios={len(state.scenarios.scenarios)} "
        f"tools={len(state.registry.tools)}"
    )
    if test_connection:
        results = _check_upstream_auth(state.application)
        if results:
            for r in results:
                typer.echo(r)
        else:
            typer.echo("No proxy-mode upstreams to check.")


@cli.command(help="Run the local verification suite.")
def verify() -> None:
    try:
        import pytest as _pytest  # noqa: F401
    except ImportError:
        typer.echo(
            "pytest is not installed. Install dev dependencies first:\n\n"
            "  pip install -e '.[dev]'\n",
            err=True,
        )
        raise typer.Exit(code=1)
    repo_root = Path(__file__).resolve().parent.parent
    command = [sys.executable, "-m", "pytest", "-q"]
    typer.echo("$ " + " ".join(command))
    subprocess.run(command, cwd=repo_root, check=True)


@cli.command("mcp-server", help="Start AgentBreak as an MCP server for Claude Code.")
def mcp_server_command() -> None:
    try:
        from agentbreak.mcp_server import run_server
    except ImportError:
        typer.echo(
            "MCP server requires the mcp package. Install it with:\n\n"
            "  pip install mcp\n",
            err=True,
        )
        raise typer.Exit(code=1)
    run_server()


history_cli = typer.Typer(help="View past run history.")
cli.add_typer(history_cli, name="history")


def _history_db_path() -> str:
    """Resolve history DB path from application.yaml, falling back to default."""
    try:
        cfg = load_application_config(None)
        return cfg.history.db_path
    except FileNotFoundError:
        return ".agentbreak/history.db"


@history_cli.callback(invoke_without_command=True)
def history_list(ctx: typer.Context, limit: int = typer.Option(10, "--limit", "-n", help="Number of runs to show.")):
    """List recent runs."""
    if ctx.invoked_subcommand is not None:
        return
    db_path = _history_db_path()
    if not Path(db_path).exists():
        typer.echo("No history found. Run `agentbreak serve` with `history.enabled: true` first.")
        raise typer.Exit(1)
    h = RunHistory(db_path=db_path)
    runs = h.get_runs(limit=limit)
    if not runs:
        typer.echo("No runs recorded yet.")
        return
    # Print table header
    typer.echo(f"{'ID':>4}  {'Timestamp':<20}  {'Label':<20}  {'LLM':>5}  {'MCP':>5}  {'Outcome':<10}")
    typer.echo("-" * 75)
    for run in runs:
        ts = datetime.fromtimestamp(run["timestamp"]).strftime("%Y-%m-%d %H:%M:%S")
        run_label = run.get("label") or run.get("run_label") or "-"
        llm = run.get("llm_scorecard") or {}
        mcp = run.get("mcp_scorecard") or {}
        llm_score = str(llm.get("resilience_score", "-")) if llm else "-"
        mcp_score = str(mcp.get("resilience_score", "-")) if mcp else "-"
        outcome = llm.get("run_outcome") or mcp.get("run_outcome") or "-"
        typer.echo(f"{run['id']:>4}  {ts:<20}  {run_label:<20}  {llm_score:>5}  {mcp_score:>5}  {outcome:<10}")


@history_cli.command()
def show(run_id: int = typer.Argument(..., help="Run ID to show.")):
    """Show details of a specific run."""
    db_path = _history_db_path()
    if not Path(db_path).exists():
        typer.echo("No history found.")
        raise typer.Exit(1)
    h = RunHistory(db_path=db_path)
    run = h.get_run(run_id)
    if run is None:
        typer.echo(f"Run {run_id} not found.")
        raise typer.Exit(1)
    typer.echo(json.dumps(run, indent=2, default=str))


@history_cli.command()
def compare(
    run_a: int = typer.Argument(..., help="First run ID."),
    run_b: int = typer.Argument(..., help="Second run ID."),
):
    """Compare two runs side-by-side."""
    db_path = _history_db_path()
    if not Path(db_path).exists():
        typer.echo("No history found.")
        raise typer.Exit(1)
    h = RunHistory(db_path=db_path)
    a, b = h.get_run(run_a), h.get_run(run_b)
    if a is None or b is None:
        typer.echo(f"Run {run_a if a is None else run_b} not found.")
        raise typer.Exit(1)

    typer.echo(f"Comparing run {run_a} vs {run_b}\n")

    for section, key in [("LLM", "llm_scorecard"), ("MCP", "mcp_scorecard")]:
        sa, sb = a.get(key) or {}, b.get(key) or {}
        if not sa and not sb:
            continue
        typer.echo(f"{section} Scorecard:")
        metrics = ["resilience_score", "run_outcome", "requests_seen", "injected_faults",
                   "upstream_successes", "upstream_failures", "duplicate_requests", "suspected_loops"]
        typer.echo(f"  {'Metric':<25} {'Run ' + str(run_a):>12} {'Run ' + str(run_b):>12} {'Delta':>10}")
        typer.echo(f"  {'-'*60}")
        for m in metrics:
            va, vb = sa.get(m, "-"), sb.get(m, "-")
            if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
                delta = vb - va
                sign = "+" if delta > 0 else ""
                typer.echo(f"  {m:<25} {str(va):>12} {str(vb):>12} {sign + str(delta):>10}")
            else:
                typer.echo(f"  {m:<25} {str(va):>12} {str(vb):>12}")
        typer.echo()


if __name__ == "__main__":
    cli()
