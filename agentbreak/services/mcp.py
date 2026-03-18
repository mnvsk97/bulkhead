"""MCP service implementation for AgentBreak."""

from __future__ import annotations

import json
from typing import Any

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from agentbreak.config.models import MCPServiceConfig
from agentbreak.core.fault_injection import FaultResult
from agentbreak.core.proxy import BaseProxy, ProxyContext
from agentbreak.core.statistics import StatisticsTracker
from agentbreak.protocols.mcp import MCPError, MCPRequest, MCPResponse, PARSE_ERROR
from agentbreak.services.base import BaseService
from agentbreak.transports import create_transport

# Map HTTP-style error codes to MCP JSON-RPC error codes
_HTTP_TO_MCP_ERROR: dict[int, tuple[int, str]] = {
    400: (-32600, "Invalid request"),
    401: (-32603, "Authentication failed"),
    403: (-32603, "Permission denied"),
    404: (-32601, "Resource not found"),
    429: (-32000, "Rate limit exceeded"),
    500: (-32603, "Internal server error"),
    503: (-32603, "Service unavailable"),
}


class MCPProxy(BaseProxy):
    """Proxy implementation for MCP protocol."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._transport = None
        self._response_cache: dict[str, Any] = {}
        self._cacheable_methods = frozenset({"resources/list", "tools/list", "prompts/list"})

    async def _create_context(self, request: Request, body: bytes) -> ProxyContext:
        try:
            mcp_req = MCPRequest.from_json(body)
            method = mcp_req.method
            req_id = mcp_req.id
        except Exception:
            method = "unknown"
            req_id = None

        return ProxyContext.create(
            service_name=self.config.name,
            raw_body=body,
            method=method,
            mcp_request_id=req_id,
        )

    async def _process_request(self, request: Request, context: ProxyContext) -> Response:
        if self.config.mode == "mock":
            return await self._mock_mcp(context)
        return await self._proxy_mcp(context)

    def _create_error_response(self, context: ProxyContext, fault: FaultResult) -> Response:
        mcp_code, mcp_message = _HTTP_TO_MCP_ERROR.get(
            fault.error_code, (-32603, "Internal error")
        )
        req_id = context.metadata.get("mcp_request_id")

        return JSONResponse(
            status_code=200,
            content=MCPResponse(
                id=req_id,
                error=MCPError(code=mcp_code, message=mcp_message),
            ).to_dict(),
        )

    def _is_success(self, response: Response) -> bool:
        """Check if MCP response indicates success (no error field in JSON-RPC body)."""
        try:
            # FastAPI JSONResponse has .body attribute with JSON string content
            if isinstance(response, JSONResponse):
                data = json.loads(response.body)
            else:
                # Fallback: try to parse response content
                body = getattr(response, "body", None) or getattr(response, "content", None)
                if isinstance(body, bytes):
                    data = json.loads(body)
                elif isinstance(body, str):
                    data = json.loads(body)
                else:
                    return False
            return "error" not in data
        except Exception:
            return False

    async def _mock_mcp(self, context: ProxyContext) -> Response:
        try:
            mcp_req = MCPRequest.from_json(context.raw_body)
        except Exception:
            return JSONResponse(
                status_code=200,
                content=MCPResponse(
                    id=None,
                    error=MCPError(code=PARSE_ERROR, message="Parse error"),
                ).to_dict(),
            )

        result = self._generate_mock_result(mcp_req)
        return JSONResponse(
            status_code=200,
            content=MCPResponse(id=mcp_req.id, result=result).to_dict(),
        )

    def _generate_mock_result(self, request: MCPRequest) -> dict[str, Any]:
        if request.method == "initialize":
            return {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                "serverInfo": {"name": "agentbreak-mock", "version": "1.0.0"},
            }
        if request.method == "tools/list":
            return {"tools": self.config.mock_tools or []}
        if request.method == "tools/call":
            tool_name = (request.params or {}).get("name", "unknown")
            return {
                "content": [{"type": "text", "text": f"Mock result for tool: {tool_name}"}],
                "isError": False,
            }
        if request.method == "resources/list":
            return {"resources": self.config.mock_resources or []}
        if request.method == "resources/read":
            uri = (request.params or {}).get("uri", "")
            return {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": "text/plain",
                        "text": f"Mock content for resource: {uri}",
                    }
                ]
            }
        if request.method == "prompts/list":
            return {"prompts": self.config.mock_prompts or []}
        if request.method == "prompts/get":
            name = (request.params or {}).get("name", "unknown")
            return {
                "description": f"Mock prompt: {name}",
                "messages": [
                    {
                        "role": "user",
                        "content": {"type": "text", "text": f"Mock prompt for: {name}"},
                    }
                ],
            }
        return {}

    async def _proxy_mcp(self, context: ProxyContext) -> Response:
        if self._transport is None:
            self._transport = create_transport(
                self.config.upstream_transport,
                base_url=self.config.upstream_url,
                command=self.config.upstream_command,
                timeout=self.config.upstream_timeout,
            )
            await self._transport.start()

        mcp_req = MCPRequest.from_json(context.raw_body)
        result = await self._transport.send_request(mcp_req)
        return JSONResponse(status_code=200, content=result)

    async def cleanup(self) -> None:
        """Cleanup transport resources."""
        if self._transport is not None:
            await self._transport.stop()
            self._transport = None


class MCPService(BaseService):
    """MCP service implementation."""

    def _create_proxy(self) -> BaseProxy:
        return MCPProxy(
            config=self.config,
            fault_config=self.config.fault,
            latency_config=self.config.latency,
            stats=self.stats,
        )

    def setup_routes(self) -> None:
        @self.app.post("/mcp")
        async def mcp_endpoint(request: Request) -> Response:
            return await self.proxy.handle_request(request)

        self.setup_common_routes()

    async def cleanup(self) -> None:
        """Cleanup transport resources."""
        if isinstance(self.proxy, MCPProxy):
            await self.proxy.cleanup()
