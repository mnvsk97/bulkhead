"""Abstract base class for AgentBreak service implementations."""

from __future__ import annotations

from abc import ABC, abstractmethod

from fastapi import FastAPI

from agentbreak.config.models import ServiceConfig
from agentbreak.core.proxy import BaseProxy
from agentbreak.core.statistics import StatisticsTracker


class BaseService(ABC):
    """Abstract base for service implementations."""

    def __init__(self, config: ServiceConfig, stats: StatisticsTracker) -> None:
        self.config = config
        self.stats = stats
        self.app = FastAPI(title=f"agentbreak-{config.name}")
        self.proxy = self._create_proxy()

    @abstractmethod
    def _create_proxy(self) -> BaseProxy:
        """Create the proxy instance for this service."""

    @abstractmethod
    def setup_routes(self) -> None:
        """Setup service-specific routes."""

    def setup_common_routes(self) -> None:
        """Setup common routes (health check, scorecard, recent requests)."""
        service_name = self.config.name
        stats = self.stats

        @self.app.get("/healthz")
        async def health_check() -> dict:
            return {"status": "ok", "service": service_name}

        @self.app.get("/_agentbreak/scorecard")
        async def get_scorecard() -> dict:
            return stats.generate_scorecard(service_name)

        @self.app.get("/_agentbreak/requests")
        async def get_recent_requests() -> dict:
            service_stats = stats.get_service_stats(service_name)
            return {"recent_requests": service_stats.recent_requests}

    def get_app(self) -> FastAPI:
        """Return the FastAPI application."""
        return self.app
