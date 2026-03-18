"""Multi-service runner for AgentBreak."""

from __future__ import annotations

import asyncio
import signal
import sys
from typing import Any

import uvicorn

from agentbreak.config.models import AgentBreakConfig, ServiceType
from agentbreak.core.statistics import StatisticsTracker
from agentbreak.services.mcp import MCPService
from agentbreak.services.openai import OpenAIService


class MultiServiceRunner:
    """Run multiple AgentBreak services in one process."""

    def __init__(self, config: AgentBreakConfig) -> None:
        self.config = config
        self.stats = StatisticsTracker()
        self.services: dict[str, Any] = {}
        self.servers: list[uvicorn.Server] = []

    def create_service(self, service_config: Any) -> Any:
        """Factory to create appropriate service instance."""
        if service_config.type == ServiceType.OPENAI:
            return OpenAIService(service_config, self.stats)
        elif service_config.type == ServiceType.MCP:
            return MCPService(service_config, self.stats)
        else:
            raise ValueError(f"Unknown service type: {service_config.type}")

    async def start(self) -> None:
        """Start all configured services concurrently."""
        try:
            for service_config in self.config.services:
                service = self.create_service(service_config)
                service.setup_routes()
                self.services[service_config.name] = service

            tasks = []
            for service_config in self.config.services:
                service = self.services[service_config.name]
                uvi_config = uvicorn.Config(
                    service.get_app(),
                    host="0.0.0.0",
                    port=service_config.port,
                    log_level="warning",
                )
                server = uvicorn.Server(uvi_config)
                self.servers.append(server)
                tasks.append(server.serve())

            self._install_signal_handlers()

            try:
                await asyncio.gather(*tasks)
            except KeyboardInterrupt:
                await self.stop()
        except Exception:
            # Clean up any partially created services
            await self.stop()
            raise

    async def stop(self) -> None:
        """Stop all services and print scorecards."""
        for server in self.servers:
            server.should_exit = True

        # Cleanup all services, continuing even if some fail
        for service_name, service in self.services.items():
            if hasattr(service, "cleanup"):
                try:
                    await service.cleanup()
                except Exception as exc:
                    import logging
                    logging.warning("Failed to cleanup service %s: %s", service_name, exc)

        self._print_scorecards()

    def _print_scorecards(self) -> None:
        """Print scorecard for each service."""
        for service_config in self.config.services:
            data = self.stats.generate_scorecard(service_config.name)
            lines = [
                "",
                f"AgentBreak Scorecard: {service_config.name}",
                f"Requests Seen: {data['requests_seen']}",
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

    def _install_signal_handlers(self) -> None:
        """Install signal handlers for graceful shutdown."""

        def handle_signal(signum: int, _frame: Any) -> None:
            raise KeyboardInterrupt(f"received signal {signum}")

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)
