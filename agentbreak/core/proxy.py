"""Abstract proxy interface for AgentBreak services."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request, Response

from agentbreak.config.models import FaultConfig, LatencyConfig, ServiceConfig
from agentbreak.core.fault_injection import FaultInjector, FaultResult
from agentbreak.core.latency import LatencyInjector
from agentbreak.core.statistics import StatisticsTracker


@dataclass
class ProxyContext:
    """Context passed through the proxy request pipeline."""

    request_id: str
    service_name: str
    raw_body: bytes
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, service_name: str, raw_body: bytes, **metadata: Any) -> "ProxyContext":
        return cls(
            request_id=str(uuid.uuid4()),
            service_name=service_name,
            raw_body=raw_body,
            metadata=dict(metadata),
        )


class BaseProxy(ABC):
    """Abstract base class for all proxy implementations."""

    def __init__(
        self,
        config: ServiceConfig,
        fault_config: FaultConfig,
        latency_config: LatencyConfig,
        stats: StatisticsTracker,
    ) -> None:
        self.config = config
        self.fault_injector = FaultInjector(fault_config)
        self.latency_injector = LatencyInjector(latency_config)
        self.stats = stats

    async def handle_request(self, request: Request) -> Response:
        """Main request handling pipeline."""
        body = await request.body()
        context = await self._create_context(request, body)

        await self.stats.record_request(
            context.service_name,
            context.raw_body,
            method=context.metadata.get("method", "unknown"),
        )

        # Phase 1: Fault injection check
        fault_result = await self.fault_injector.maybe_inject(context)
        if fault_result is not None:
            await self.stats.record_fault(context.service_name)
            return self._create_error_response(context, fault_result)

        # Phase 2: Latency injection
        injected_delay = await self.latency_injector.maybe_delay(context)
        if injected_delay is not None:
            await self.stats.record_latency(context.service_name)

        # Phase 3: Upstream/mock processing
        response = await self._process_request(request, context)

        # Phase 4: Response recording
        if self._is_success(response):
            await self.stats.record_success(context.service_name)
        else:
            await self.stats.record_failure(context.service_name)

        return response

    @abstractmethod
    async def _create_context(self, request: Request, body: bytes) -> ProxyContext:
        """Create a proxy context from the incoming request."""

    @abstractmethod
    async def _process_request(self, request: Request, context: ProxyContext) -> Response:
        """Process the request (forward to upstream or mock)."""

    @abstractmethod
    def _create_error_response(
        self, context: ProxyContext, fault: FaultResult
    ) -> Response:
        """Create an error response from fault data."""

    @abstractmethod
    def _is_success(self, response: Response) -> bool:
        """Determine if response indicates success."""
