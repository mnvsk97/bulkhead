"""OpenAI-compatible service implementation for AgentBreak."""

from __future__ import annotations

import httpx
from fastapi import Request, Response
from fastapi.responses import JSONResponse

from agentbreak.config.models import OpenAIServiceConfig
from agentbreak.core.fault_injection import FaultResult
from agentbreak.core.proxy import BaseProxy, ProxyContext
from agentbreak.core.statistics import StatisticsTracker
from agentbreak.services.base import BaseService
from agentbreak.utils.headers import filter_headers


class OpenAIProxy(BaseProxy):
    """Proxy implementation for OpenAI-compatible APIs."""

    async def _create_context(self, request: Request, body: bytes) -> ProxyContext:
        return ProxyContext.create(
            service_name=self.config.name,
            raw_body=body,
            method="chat/completions",
        )

    async def _process_request(self, request: Request, context: ProxyContext) -> Response:
        if self.config.mode == "mock":
            return self._mock_response()
        return await self._proxy_upstream(context.raw_body, request)

    def _create_error_response(self, context: ProxyContext, fault: FaultResult) -> Response:
        return JSONResponse(
            status_code=fault.error_code,
            content={
                "error": {
                    "message": fault.message,
                    "type": fault.error_type,
                    "code": fault.error_code,
                }
            },
        )

    def _is_success(self, response: Response) -> bool:
        return response.status_code < 400

    def _mock_response(self) -> Response:
        return JSONResponse(
            status_code=200,
            content={
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
            },
        )

    async def _proxy_upstream(self, body: bytes, request: Request) -> Response:
        async with httpx.AsyncClient(timeout=self.config.upstream_timeout) as client:
            try:
                response = await client.post(
                    f"{self.config.upstream_url.rstrip('/')}/v1/chat/completions",
                    content=body,
                    headers=filter_headers(request.headers),
                )
            except httpx.HTTPError as exc:
                return JSONResponse(
                    status_code=502,
                    content={
                        "error": {
                            "message": f"AgentBreak could not reach upstream: {exc}",
                            "type": "upstream_connection_error",
                            "code": 502,
                        }
                    },
                )

        return Response(
            content=response.content,
            status_code=response.status_code,
            headers=filter_headers(response.headers),
            media_type=response.headers.get("content-type"),
        )


class OpenAIService(BaseService):
    """OpenAI-compatible service implementation."""

    def _create_proxy(self) -> BaseProxy:
        return OpenAIProxy(
            config=self.config,
            fault_config=self.config.fault,
            latency_config=self.config.latency,
            stats=self.stats,
        )

    def setup_routes(self) -> None:
        @self.app.post("/v1/chat/completions")
        async def chat_completions(request: Request) -> Response:
            return await self.proxy.handle_request(request)

        self.setup_common_routes()
