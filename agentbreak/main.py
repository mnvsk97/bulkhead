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
from pathlib import Path
from typing import Any

logger = logging.getLogger("agentbreak")

import httpx
import typer
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from agentbreak import __version__
from agentbreak.behaviors import apply_response_behavior
from agentbreak.config import ApplicationConfig, MCPRegistry, load_application_config, load_registry, save_registry
from agentbreak.discovery.mcp import MCP_PROTOCOL_VERSION, inspect_mcp_server, parse_mcp_response
from agentbreak.scenarios import Scenario, ScenarioFile, load_scenarios, validate_supported_targets


cli = typer.Typer(add_completion=False, help="Chaos testing for OpenAI-compatible LLM and MCP tool runtimes.")
app = FastAPI(title="agentbreak")


@dataclass
class ServiceState:
    application: ApplicationConfig
    scenarios: ScenarioFile
    registry: MCPRegistry
    llm_runtime: LLMRuntime | None
    mcp_runtime: MCPRuntime | None


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

    async def handle_chat(self, request: Request) -> Response:
        body = await request.body()
        self._record_request(body)
        payload, has_parse_error = parse_json_body(body)
        if has_parse_error:
            return JSONResponse(status_code=400, content=openai_error(400, message_override="Malformed JSON request body."))
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
                logger.info("injecting http_error %d via %s", scenario.fault.status_code or 500, scenario.name)
                return JSONResponse(status_code=scenario.fault.status_code or 500, content=openai_error(scenario.fault.status_code or 500))

        if self.mode == "mock":
            response_body = json.dumps(mock_completion()).encode("utf-8")
        else:
            async with httpx.AsyncClient(timeout=120.0) as client:
                try:
                    upstream = await client.post(
                        f"{self.upstream_url.rstrip('/')}/v1/chat/completions",
                        content=body,
                        headers=filter_request_headers(request.headers, self.auth_headers),
                    )
                except httpx.HTTPError as exc:
                    self.stats.upstream_failures += 1
                    logger.warning("upstream unreachable: %s", exc)
                    return JSONResponse(
                        status_code=502,
                        content={"error": {"message": f"AgentBreak could not reach upstream: {exc}", "type": "upstream_connection_error", "code": 502}},
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
            response_body = mutate_llm_body(response_body, scenario)
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

    def scorecard_data(self) -> dict[str, Any]:
        score = 100
        score -= self.stats.injected_faults * 3
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
            "injected_faults": self.stats.injected_faults,
            "latency_injections": self.stats.latency_injections,
            "upstream_successes": self.stats.upstream_successes,
            "upstream_failures": self.stats.upstream_failures,
            "duplicate_requests": self.stats.duplicate_requests,
            "suspected_loops": self.stats.suspected_loops,
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
        if not self.upstream_url or self.session_id:
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
        if self.upstream_url:
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
) -> ServiceState:
    application = load_application_config(config_path)
    scenarios = load_scenarios(scenarios_path)
    validate_supported_targets(scenarios)
    registry = MCPRegistry()
    if application.mcp.enabled:
        registry = load_registry(registry_path)

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
        )

    return ServiceState(
        application=application,
        scenarios=scenarios,
        registry=registry,
        llm_runtime=llm_runtime,
        mcp_runtime=mcp_runtime,
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


def mock_completion() -> dict[str, Any]:
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


@app.post("/v1/chat/completions")
async def proxy_chat_completions(request: Request):
    state = require_service_state()
    if state.llm_runtime is None:
        return JSONResponse(status_code=404, content={"error": "LLM runtime is disabled"})
    return await state.llm_runtime.handle_chat(request)


@app.post("/mcp")
async def handle_mcp(request: Request):
    state = require_service_state()
    if state.mcp_runtime is None:
        return JSONResponse(status_code=404, content={"error": "MCP runtime is disabled"})
    return await state.mcp_runtime.handle_rpc(request)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


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
    state = require_service_state()
    if state.llm_runtime is None:
        return {"requests_seen": 0}
    return state.llm_runtime.scorecard_data()


@app.get("/_agentbreak/llm-requests")
async def get_agentbreak_llm_requests() -> dict[str, Any]:
    state = require_service_state()
    if state.llm_runtime is None:
        return {"recent_requests": []}
    return state.llm_runtime.current_requests()


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


@cli.command(help="Inspect an upstream MCP server, validate auth, and write a tool registry artifact.")
def inspect(
    config_path: str | None = typer.Option(None, "--config", help="Application config path. Defaults to ./application.yaml."),
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
    config_path: str | None = typer.Option(None, "--config", help="Application config path. Defaults to ./application.yaml."),
    scenarios_path: str | None = typer.Option(None, "--scenarios", help="Scenario config path. Defaults to ./scenarios.yaml."),
    registry_path: str | None = typer.Option(None, "--registry", help="Registry path. Defaults to ./.agentbreak/registry.json."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose logging."),
) -> None:
    global service_state

    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    service_state = load_service_state(config_path, scenarios_path, registry_path)
    host = service_state.application.serve.host
    port = service_state.application.serve.port

    logger.info("starting on %s:%d", host, port)
    logger.info(
        "llm=%s mcp=%s scenarios=%d",
        "proxy" if service_state.application.llm.enabled else "off",
        "on" if service_state.application.mcp.enabled else "off",
        len(service_state.scenarios.scenarios),
    )

    install_signal_handlers()
    uv_level = "debug" if verbose else "warning"
    try:
        uvicorn.run(app, host=host, port=port, log_level=uv_level)
    finally:
        print_scorecard()


@cli.command(help="Validate application, scenario, and registry files without starting the server.")
def validate(
    config_path: str | None = typer.Option(None, "--config", help="Application config path."),
    scenarios_path: str | None = typer.Option(None, "--scenarios", help="Scenario config path."),
    registry_path: str | None = typer.Option(None, "--registry", help="Registry path."),
) -> None:
    state = load_service_state(config_path, scenarios_path, registry_path)
    typer.echo(
        "Config valid: "
        f"llm_enabled={state.application.llm.enabled} "
        f"mcp_enabled={state.application.mcp.enabled} "
        f"scenarios={len(state.scenarios.scenarios)} "
        f"tools={len(state.registry.tools)}"
    )


@cli.command(help="Run the local verification suite. Use --live to include the full LangGraph chaos harness.")
def verify(
    live: bool = typer.Option(False, "--live", help="Also run the live LangGraph + MCP + AgentBreak harness."),
) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    commands = [
        [sys.executable, "-m", "pytest", "-q"],
    ]
    if live:
        commands.append([sys.executable, str(repo_root / "examples/live_harness/run_live_e2e.py")])
    for command in commands:
        typer.echo("$ " + " ".join(command))
        subprocess.run(command, cwd=repo_root, check=True)


if __name__ == "__main__":
    cli()
