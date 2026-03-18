"""Abstract base class for AgentBreak service implementations."""

from __future__ import annotations

from abc import ABC, abstractmethod

from fastapi import FastAPI

from agentbreak.api import setup_health_routes, setup_metrics_routes
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
        setup_health_routes(self.app, self.config.name)
        setup_metrics_routes(self.app, self.config.name, self.stats)

    def get_app(self) -> FastAPI:
        """Return the FastAPI application."""
        return self.app

    async def close(self) -> None:
        """Close any resources held by the service."""
        if hasattr(self.proxy, 'close'):
            await self.proxy.close()
