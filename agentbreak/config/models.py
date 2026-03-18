"""Pydantic configuration models for AgentBreak."""

from __future__ import annotations

import random
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator


class ServiceType(str, Enum):
    OPENAI = "openai"
    MCP = "mcp"


class TransportType(str, Enum):
    HTTP = "http"
    STDIO = "stdio"
    SSE = "sse"


class FaultConfig(BaseModel):
    """Configurable fault injection with per-error-type percentages."""

    enabled: bool = True
    overall_rate: float = Field(ge=0.0, le=1.0, default=0.1)
    # Per-error-type rates override overall_rate if specified
    per_error_rates: dict[int, float] = Field(
        default_factory=lambda: {429: 0.4, 500: 0.4, 503: 0.2}
    )
    # Error codes to randomly select from when using overall_rate
    available_codes: tuple[int, ...] = (429, 500, 503)

    model_config = {"arbitrary_types_allowed": True}

    def get_fault_code(self) -> Optional[int]:
        """Determine if a fault should be injected and which code."""
        if not self.enabled:
            return None

        # Check per-error rates first
        for code, rate in self.per_error_rates.items():
            if random.random() < rate:
                return code

        # Fall back to overall rate
        if random.random() < self.overall_rate:
            if self.available_codes:
                return random.choice(self.available_codes)

        return None


class LatencyConfig(BaseModel):
    """Latency injection configuration."""

    enabled: bool = True
    probability: float = Field(ge=0.0, le=1.0, default=0.0)
    min_seconds: float = Field(ge=0.0, default=5.0)
    max_seconds: float = Field(ge=0.0, default=15.0)


class ServiceConfig(BaseModel):
    """Base configuration for any service."""

    name: str
    type: ServiceType
    mode: Literal["proxy", "mock"] = "proxy"
    port: int
    fault: FaultConfig = Field(default_factory=FaultConfig)
    latency: LatencyConfig = Field(default_factory=LatencyConfig)
    seed: Optional[int] = None
    scenario: Optional[str] = None


class OpenAIServiceConfig(ServiceConfig):
    """OpenAI/LLM specific configuration."""

    type: Literal[ServiceType.OPENAI] = ServiceType.OPENAI  # type: ignore[assignment]
    upstream_url: str = "https://api.openai.com"
    upstream_timeout: float = 120.0


class MCPServiceConfig(ServiceConfig):
    """MCP specific configuration."""

    type: Literal[ServiceType.MCP] = ServiceType.MCP  # type: ignore[assignment]
    upstream_url: str = ""
    upstream_transport: TransportType = TransportType.HTTP
    upstream_command: tuple[str, ...] = ()
    upstream_timeout: float = 30.0
    cache_ttl: float = 60.0
    # Mock data
    mock_tools: list[dict[str, Any]] = Field(default_factory=list)
    mock_resources: list[dict[str, Any]] = Field(default_factory=list)
    mock_prompts: list[dict[str, Any]] = Field(default_factory=list)

    model_config = {"arbitrary_types_allowed": True}


class AgentBreakConfig(BaseModel):
    """Top-level configuration for running multiple services."""

    version: str = "1.0"
    services: list[ServiceConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_ports(self) -> "AgentBreakConfig":
        """Ensure no port conflicts."""
        ports = [s.port for s in self.services]
        if len(ports) != len(set(ports)):
            raise ValueError("Service ports must be unique")
        return self

    def get_service(self, name: str) -> ServiceConfig:
        """Get a service by name."""
        for service in self.services:
            if service.name == name:
                return service
        raise ValueError(f"Service '{name}' not found")
