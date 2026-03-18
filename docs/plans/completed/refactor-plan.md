# AgentBreak Refactor Plan - Code Quality & Multi-Service Proxy

## Executive Summary

This plan outlines a comprehensive refactoring of AgentBreak to improve code quality, add a unified proxy architecture for LLM and MCP services, and enable multi-service configuration via a single config file. The refactor will create a maintainable, extensible codebase while preserving all existing functionality.

## Current State Analysis

### Existing Structure
```
agentbreak/
├── __init__.py
├── main.py              (~500 lines) - LLM proxy, CLI entry point, config parsing
├── mcp_proxy.py         (~800 lines) - MCP proxy, CLI subcommands, transport management
├── mcp_protocol.py      (~150 lines) - MCP protocol dataclasses
└── mcp_transport.py     (~300 lines) - Transport abstraction (stdio, SSE, HTTP)
```

### Issues Identified
1. **Monolithic files**: `mcp_proxy.py` (800+ lines) mixes concerns: FastAPI app, fault injection, transport management, CLI commands, and statistics tracking
2. **Code duplication**: Similar fault injection logic exists in both `main.py` and `mcp_proxy.py`
3. **Tight coupling**: Configuration parsing is embedded in CLI handlers
4. **Global state**: Multiple global variables (`mcp_config`, `mcp_stats`, transports) make testing difficult
5. **No abstraction layer**: No common interface between LLM and MCP proxy implementations
6. **Missing utilities**: Common functions scattered across files (probability clamping, random injection, etc.)
7. **Single service limitation**: Can only run one proxy type at a time per process

### Strengths to Preserve
1. Well-defined protocol abstractions (`MCPRequest`, `MCPResponse`, `MCPTransport`)
2. Comprehensive statistics tracking and scorecard generation
3. Support for multiple transport types (stdio, SSE, HTTP)
4. Built-in fault scenarios system
5. YAML configuration support

---

## Refactor Goals

1. **Code Organization**: Split monolithic files into focused, single-responsibility modules
2. **Unified Architecture**: Create common proxy patterns for LLM and MCP services
3. **Multi-Service Support**: Enable a single instance to proxy multiple services simultaneously
4. **Extensibility**: Make it easy to add new service types (e.g., Anthropic, other APIs)
5. **Testability**: Reduce global state and improve dependency injection
6. **Maintainability**: Clear separation of concerns, type safety, documentation

---

## Proposed Directory Structure

```
agentbreak/
├── __init__.py
├── main.py                    # CLI entry point (simplified)
├── config/                    # Configuration management
│   ├── __init__.py
│   ├── models.py             # Pydantic models for config
│   ├── loader.py             # Config file parsing and validation
│   └── scenarios.py          # Built-in scenario definitions
├── core/                     # Core proxy functionality
│   ├── __init__.py
│   ├── proxy.py              # Abstract proxy interface
│   ├── fault_injection.py    # Fault injection logic
│   ├── latency.py             # Latency injection
│   └── statistics.py         # Stats tracking and scorecard
├── services/                 # Service-specific implementations
│   ├── __init__.py
│   ├── base.py               # Base service interface
│   ├── openai.py             # OpenAI/LLM service
│   └── mcp.py                # MCP service
├── transports/               # Transport layer (refactored)
│   ├── __init__.py
│   ├── base.py               # Abstract transport
│   ├── http.py               # HTTP transport
│   ├── stdio.py              # Stdio transport
│   └── sse.py                # SSE transport
├── protocols/                # Protocol definitions
│   ├── __init__.py
│   ├── mcp.py                # MCP protocol (refactored from mcp_protocol.py)
│   └── openai.py             # OpenAI protocol types
├── utils/                    # Utility functions
│   ├── __init__.py
│   ├── random.py             # Random utilities (clamp, should_inject)
│   ├── headers.py            # Header filtering
│   ├── hashing.py            # Fingerprinting
│   └── http.py               # HTTP client pooling
└── api/                      # FastAPI routes
    ├── __init__.py
    ├── health.py             # Health endpoints
    ├── metrics.py            # Metrics/scorecard endpoints
    └── routes.py             # Main proxy routes
```

---

## Detailed Design

### 1. Configuration Layer (`config/`)

#### `config/models.py` - Pydantic Configuration Models
```python
from pydantic import BaseModel, Field
from typing import Literal, tuple, Optional
from enum import Enum

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

class OpenAIServiceConfig(ServiceConfig):
    """OpenAI/LLM specific configuration."""
    type: Literal[ServiceType.OPENAI] = ServiceType.OPENAI
    upstream_url: str
    upstream_timeout: float = 120.0

class MCPServiceConfig(ServiceConfig):
    """MCP specific configuration."""
    type: Literal[ServiceType.MCP] = ServiceType.MCP
    upstream_url: str = ""
    upstream_transport: TransportType = TransportType.HTTP
    upstream_command: tuple[str, ...] = ()
    upstream_timeout: float = 30.0
    cache_ttl: float = 60.0
    # Mock data
    mock_tools: list[dict[str, Any]] = Field(default_factory=list)
    mock_resources: list[dict[str, Any]] = Field(default_factory=list)
    mock_prompts: list[dict[str, Any]] = Field(default_factory=list)

class AgentBreakConfig(BaseModel):
    """Top-level configuration for running multiple services."""
    version: str = "1.0"
    services: list[ServiceConfig] = Field(default_factory=list)

    @model_validator
    def validate_ports(cls, v):
        """Ensure no port conflicts."""
        ports = [s.port for s in v.services]
        if len(ports) != len(set(ports)):
            raise ValueError("Service ports must be unique")
        return v

    def get_service(self, name: str) -> ServiceConfig:
        """Get a service by name."""
        for service in self.services:
            if service.name == name:
                return service
        raise ValueError(f"Service '{name}' not found")
```

#### `config/loader.py` - Configuration Loading
```python
import yaml
from pathlib import Path
from typing import Optional

from .models import AgentBreakConfig, OpenAIServiceConfig, MCPServiceConfig

def load_config(path: Optional[Path] = None) -> AgentBreakConfig:
    """Load configuration from YAML file or defaults."""
    if path is None:
        path = Path("config.yaml")

    if not path.exists():
        # Return default single OpenAI service
        return AgentBreakConfig(
            services=[OpenAIServiceConfig(
                name="default",
                type=ServiceType.OPENAI,
                mode="mock",
                port=5000,
                upstream_url="https://api.openai.com"
            )]
        )

    with path.open("r") as f:
        data = yaml.safe_load(f)

    return AgentBreakConfig.model_validate(data)

def load_scenario(name: str) -> dict[str, Any]:
    """Load a built-in scenario configuration."""
    from .scenarios import SCENARIOS
    if name not in SCENARIOS:
        raise ValueError(f"Unknown scenario: {name}")
    return SCENARIOS[name].copy()
```

#### `config/scenarios.py` - Built-in Scenarios
```python
"""Built-in fault scenarios for LLM and MCP services."""

SCENARIOS: dict[str, dict[str, Any]] = {
    # LLM Scenarios
    "mixed-transient": {
        "fault": {
            "available_codes": [429, 500, 503],
            "per_error_rates": {429: 0.3, 500: 0.4, 503: 0.3}
        },
        "latency": {"probability": 0.0}
    },
    "rate-limited": {
        "fault": {
            "available_codes": [429],
            "overall_rate": 0.5
        }
    },
    "brownout": {
        "fault": {
            "available_codes": [429, 500, 503],
            "overall_rate": 0.3
        },
        "latency": {"probability": 0.2, "min_seconds": 5, "max_seconds": 15}
    },

    # MCP Scenarios
    "mcp-tool-failures": {
        "fault": {
            "available_codes": [429, 500, 503],
            "overall_rate": 0.3
        },
        "latency": {"probability": 0.0}
    },
    "mcp-resource-unavailable": {
        "fault": {
            "available_codes": [404, 503],
            "overall_rate": 0.5
        }
    },
    "mcp-slow-tools": {
        "fault": {"overall_rate": 0.0},
        "latency": {"probability": 0.9, "min_seconds": 5, "max_seconds": 15}
    },
    "mcp-initialization-failure": {
        "fault": {
            "available_codes": [500, 503],
            "overall_rate": 0.5
        }
    },
    "mcp-mixed-transient": {
        "fault": {
            "available_codes": [429, 500, 503],
            "overall_rate": 0.2
        },
        "latency": {"probability": 0.1, "min_seconds": 5, "max_seconds": 15}
    },
}

def apply_scenario(config: dict[str, Any], scenario_name: str) -> dict[str, Any]:
    """Apply a scenario to a configuration dict."""
    scenario = SCENARIOS.get(scenario_name, {})
    # Deep merge scenario into config
    result = config.copy()
    for key, value in scenario.items():
        if isinstance(value, dict) and key in result:
            result[key] = {**result[key], **value}
        else:
            result[key] = value
    return result
```

---

### 2. Core Proxy Layer (`core/`)

#### `core/proxy.py` - Abstract Proxy Interface
```python
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator
from dataclasses import dataclass

from fastapi import Request, Response

from core.fault_injection import FaultInjector
from core.latency import LatencyInjector
from core.statistics import StatisticsTracker
from config.models import ServiceConfig, FaultConfig, LatencyConfig

@dataclass
class ProxyContext:
    """Context passed through proxy request pipeline."""
    request_id: str
    service_name: str
    raw_body: bytes
    metadata: dict[str, Any]

class BaseProxy(ABC):
    """Abstract base class for all proxy implementations."""

    def __init__(
        self,
        config: ServiceConfig,
        fault_config: FaultConfig,
        latency_config: LatencyConfig,
        stats: StatisticsTracker
    ):
        self.config = config
        self.fault_injector = FaultInjector(fault_config)
        self.latency_injector = LatencyInjector(latency_config)
        self.stats = stats

    async def handle_request(self, request: Request) -> Response:
        """Main request handling pipeline."""
        context = await self._create_context(request)
        self.stats.record_request(context)

        # Phase 1: Fault injection check
        fault_result = await self.fault_injector.maybe_inject(context)
        if fault_result:
            self.stats.record_fault(context, fault_result)
            return self._create_error_response(context, fault_result)

        # Phase 2: Latency injection
        await self.latency_injector.maybe_delay(context)

        # Phase 3: Upstream/mock processing
        response = await self._process_request(request, context)

        # Phase 4: Response recording
        if self._is_success(response):
            self.stats.record_success(context)
        else:
            self.stats.record_failure(context)

        return response

    @abstractmethod
    async def _create_context(self, request: Request) -> ProxyContext:
        """Create a proxy context from the incoming request."""
        pass

    @abstractmethod
    async def _process_request(
        self,
        request: Request,
        context: ProxyContext
    ) -> Response:
        """Process the request (forward to upstream or mock)."""
        pass

    @abstractmethod
    def _create_error_response(
        self,
        context: ProxyContext,
        fault: dict[str, Any]
    ) -> Response:
        """Create an error response from fault data."""
        pass

    @abstractmethod
    def _is_success(self, response: Response) -> bool:
        """Determine if response indicates success."""
        pass
```

#### `core/fault_injection.py` - Fault Injection Logic
```python
import random
from typing import Optional
from dataclasses import dataclass

from core.proxy import ProxyContext
from config.models import FaultConfig

@dataclass
class FaultResult:
    """Result of fault injection check."""
    error_code: int
    error_type: str
    message: str
    metadata: dict[str, Any]

class FaultInjector:
    """Handles fault injection logic for proxy services."""

    def __init__(self, config: FaultConfig):
        self.config = config
        self._initialize_random_seed()

    def _initialize_random_seed(self) -> None:
        """Initialize random seed if specified."""
        seed = getattr(self.config, "seed", None)
        if seed is not None:
            random.seed(seed)

    async def maybe_inject(self, context: ProxyContext) -> Optional[FaultResult]:
        """Check if a fault should be injected and return fault details."""
        if not self.config.enabled:
            return None

        fault_code = self.config.get_fault_code()
        if fault_code is None:
            return None

        return FaultResult(
            error_code=fault_code,
            error_type=self._get_error_type(fault_code),
            message=self._get_error_message(fault_code),
            metadata={"injected_by": "agentbreak", "service": context.service_name}
        )

    def _get_error_type(self, code: int) -> str:
        """Map error code to error type string."""
        error_map = {
            400: "invalid_request_error",
            401: "authentication_error",
            403: "permission_error",
            404: "not_found_error",
            413: "invalid_request_error",
            429: "rate_limit_error",
            500: "server_error",
            503: "server_error",
        }
        return error_map.get(code, "unknown_error")

    def _get_error_message(self, code: int) -> str:
        """Get error message for given code."""
        base = "Error injected by AgentBreak fault injection."
        code_messages = {
            429: "Rate limit exceeded by AgentBreak fault injection.",
            500: "Upstream failure injected by AgentBreak.",
            503: "Service unavailable injected by AgentBreak.",
        }
        return code_messages.get(code, base)
```

#### `core/latency.py` - Latency Injection
```python
import asyncio
import random
from typing import Optional

from core.proxy import ProxyContext
from config.models import LatencyConfig

class LatencyInjector:
    """Handles latency injection for proxy services."""

    def __init__(self, config: LatencyConfig):
        self.config = config

    async def maybe_delay(self, context: ProxyContext) -> None:
        """Inject delay if configured probability triggers."""
        if not self.config.enabled or self.config.probability == 0:
            return

        if random.random() < self.config.probability:
            delay = random.uniform(
                self.config.min_seconds,
                self.config.max_seconds
            )
            await asyncio.sleep(delay)
            # Record latency injection in context
            context.metadata["latency_injected"] = delay
```

#### `core/statistics.py` - Statistics Tracking
```python
import time
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from core.proxy import ProxyContext

@dataclass
class ServiceStatistics:
    """Statistics for a single service."""
    total_requests: int = 0
    injected_faults: int = 0
    latency_injections: int = 0
    upstream_successes: int = 0
    upstream_failures: int = 0
    duplicate_requests: int = 0
    suspected_loops: int = 0
    method_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    seen_fingerprints: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    recent_requests: list[dict[str, Any]] = field(default_factory=list)
    total_processing_time_ms: float = 0.0
    session_start_time: float = field(default_factory=time.monotonic)

class StatisticsTracker:
    """Track statistics across all services."""

    def __init__(self):
        self.services: dict[str, ServiceStatistics] = {}

    def get_service_stats(self, service_name: str) -> ServiceStatistics:
        """Get or create statistics for a service."""
        if service_name not in self.services:
            self.services[service_name] = ServiceStatistics()
        return self.services[service_name]

    def record_request(self, context: ProxyContext) -> None:
        """Record an incoming request."""
        stats = self.get_service_stats(context.service_name)
        stats.total_requests += 1

        # Fingerprint tracking
        fingerprint = hashlib.sha256(context.raw_body).hexdigest()
        stats.seen_fingerprints[fingerprint] += 1
        seen = stats.seen_fingerprints[fingerprint]

        if seen > 1:
            stats.duplicate_requests += 1
        if seen > 2:
            stats.suspected_loops += 1

        # Recent requests tracking
        stats.recent_requests.append({
            "fingerprint": fingerprint,
            "count": seen,
            "method": context.metadata.get("method", "unknown"),
        })
        if len(stats.recent_requests) > 20:
            stats.recent_requests.pop(0)

    def record_fault(self, context: ProxyContext, fault: Any) -> None:
        """Record an injected fault."""
        stats = self.get_service_stats(context.service_name)
        stats.injected_faults += 1
        stats.upstream_failures += 1

    def record_success(self, context: ProxyContext) -> None:
        """Record a successful upstream call."""
        stats = self.get_service_stats(context.service_name)
        stats.upstream_successes += 1

    def record_failure(self, context: ProxyContext) -> None:
        """Record an upstream failure (non-injected)."""
        stats = self.get_service_stats(context.service_name)
        stats.upstream_failures += 1

    def generate_scorecard(self, service_name: str) -> dict[str, Any]:
        """Generate a resilience scorecard for a service."""
        stats = self.get_service_stats(service_name)

        score = 100
        score -= stats.injected_faults * 3
        score -= stats.upstream_failures * 12
        score -= stats.duplicate_requests * 2
        score -= stats.suspected_loops * 10
        score = max(0, min(100, score))

        # Determine outcome
        if stats.upstream_failures == 0 and stats.suspected_loops == 0:
            outcome = "PASS"
        elif stats.upstream_successes > 0:
            outcome = "DEGRADED"
        else:
            outcome = "FAIL"

        # Calculate throughput
        elapsed = time.monotonic() - stats.session_start_time
        rps = round(stats.total_requests / elapsed, 2) if elapsed > 0 else 0.0

        avg_processing_ms = (
            round(stats.total_processing_time_ms / stats.total_requests, 2)
            if stats.total_requests > 0 else 0.0
        )

        return {
            "service_name": service_name,
            "requests_seen": stats.total_requests,
            "injected_faults": stats.injected_faults,
            "latency_injections": stats.latency_injections,
            "upstream_successes": stats.upstream_successes,
            "upstream_failures": stats.upstream_failures,
            "duplicate_requests": stats.duplicate_requests,
            "suspected_loops": stats.suspected_loops,
            "method_counts": dict(stats.method_counts),
            "run_outcome": outcome,
            "resilience_score": score,
            "avg_processing_ms": avg_processing_ms,
            "requests_per_second": rps,
        }
```

---

### 3. Service Implementations (`services/`)

#### `services/base.py` - Base Service
```python
from abc import ABC, abstractmethod
from typing import Any
from fastapi import FastAPI

from config.models import ServiceConfig
from core.proxy import BaseProxy
from core.statistics import StatisticsTracker

class BaseService(ABC):
    """Abstract base for service implementations."""

    def __init__(self, config: ServiceConfig, stats: StatisticsTracker):
        self.config = config
        self.stats = stats
        self.app = FastAPI(title=f"agentbreak-{config.name}")
        self.proxy = self._create_proxy()

    @abstractmethod
    def _create_proxy(self) -> BaseProxy:
        """Create the proxy instance for this service."""
        pass

    @abstractmethod
    def setup_routes(self) -> None:
        """Setup service-specific routes."""
        pass

    def setup_common_routes(self) -> None:
        """Setup common routes (health, metrics)."""
        from api.health import setup_health_routes
        from api.metrics import setup_metrics_routes

        setup_health_routes(self.app, self.config.name)
        setup_metrics_routes(self.app, self.config.name, self.stats)

    def get_app(self) -> FastAPI:
        """Return the FastAPI application."""
        return self.app
```

#### `services/openai.py` - OpenAI/LLM Service
```python
from typing import Any
from fastapi import Request, Response, JSONResponse
import httpx

from services.base import BaseService
from config.models import OpenAIServiceConfig
from core.proxy import BaseProxy, ProxyContext
from core.statistics import StatisticsTracker
from utils.headers import filter_headers

class OpenAIProxy(BaseProxy):
    """Proxy implementation for OpenAI-compatible APIs."""

    async def _create_context(self, request: Request) -> ProxyContext:
        """Create context from incoming OpenAI request."""
        body = await request.body()
        return ProxyContext(
            request_id=request.headers.get("x-request-id", str(id(request))),
            service_name=self.config.name,
            raw_body=body,
            metadata={"method": "chat/completions"}
        )

    async def _process_request(
        self,
        request: Request,
        context: ProxyContext
    ) -> Response:
        """Process OpenAI chat completion request."""
        if self.config.mode == "mock":
            return self._mock_response(context)

        # Proxy to upstream
        return await self._proxy_upstream(request)

    def _create_error_response(
        self,
        context: ProxyContext,
        fault: dict[str, Any]
    ) -> Response:
        """Create OpenAI-style error response."""
        return JSONResponse(
            status_code=fault["error_code"],
            content={
                "error": {
                    "message": fault["message"],
                    "type": fault["error_type"],
                    "code": fault["error_code"],
                }
            }
        )

    def _is_success(self, response: Response) -> bool:
        """Check if response is successful."""
        return response.status_code < 400

    def _mock_response(self, context: ProxyContext) -> Response:
        """Generate mock OpenAI response."""
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
            }
        )

    async def _proxy_upstream(self, request: Request) -> Response:
        """Proxy request to upstream OpenAI API."""
        body = await request.body()

        async with httpx.AsyncClient(
            timeout=self.config.upstream_timeout
        ) as client:
            try:
                response = await client.post(
                    f"{self.config.upstream_url}/v1/chat/completions",
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
                    }
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
        """Create OpenAI proxy instance."""
        return OpenAIProxy(
            config=self.config,
            fault_config=self.config.fault,
            latency_config=self.config.latency,
            stats=self.stats
        )

    def setup_routes(self) -> None:
        """Setup OpenAI-specific routes."""
        @self.app.post("/v1/chat/completions")
        async def chat_completions(request: Request):
            return await self.proxy.handle_request(request)

        self.setup_common_routes()
```

#### `services/mcp.py` - MCP Service
```python
from typing import Any
from fastapi import Request, Response, JSONResponse
import json
import asyncio

from services.base import BaseService
from config.models import MCPServiceConfig
from core.proxy import BaseProxy, ProxyContext
from core.statistics import StatisticsTracker
from transports import create_transport
from protocols.mcp import MCPRequest, MCPResponse

class MCPProxy(BaseProxy):
    """Proxy implementation for MCP protocol."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._transport = None
        self._response_cache = {}
        self._cacheable_methods = frozenset({"resources/list", "tools/list", "prompts/list"})

    async def _create_context(self, request: Request) -> ProxyContext:
        """Create context from incoming MCP request."""
        body = await request.body()
        try:
            mcp_req = MCPRequest.from_json(body)
            method = mcp_req.method
        except Exception:
            method = "unknown"

        return ProxyContext(
            request_id=str(id(request)),
            service_name=self.config.name,
            raw_body=body,
            metadata={"method": method}
        )

    async def _process_request(
        self,
        request: Request,
        context: ProxyContext
    ) -> Response:
        """Process MCP request."""
        if self.config.mode == "mock":
            return await self._mock_mcp(request, context)
        return await self._proxy_mcp(request)

    def _create_error_response(
        self,
        context: ProxyContext,
        fault: dict[str, Any]
    ) -> Response:
        """Create MCP error response."""
        # Map HTTP error codes to MCP error codes
        error_map = {
            400: (-32600, "Invalid request"),
            401: (-32603, "Authentication failed"),
            403: (-32603, "Permission denied"),
            404: (-32601, "Resource not found"),
            429: (-32000, "Rate limit exceeded"),
            500: (-32603, "Internal server error"),
            503: (-32603, "Service unavailable"),
        }
        code, message = error_map.get(fault["error_code"], (-32603, "Internal error"))

        return JSONResponse(
            status_code=200,
            content=MCPResponse(
                id=None,
                error={"code": code, "message": message}
            ).to_dict()
        )

    def _is_success(self, response: Response) -> bool:
        """Check if MCP response is successful."""
        try:
            data = json.loads(response.body)
            return "error" not in data
        except Exception:
            return False

    async def _mock_mcp(self, request: Request, context: ProxyContext) -> Response:
        """Generate mock MCP response."""
        try:
            body = await request.body()
            mcp_req = MCPRequest.from_json(body)
        except Exception:
            return JSONResponse(
                status_code=200,
                content=MCPResponse(
                    id=None,
                    error={"code": -32700, "message": "Parse error"}
                ).to_dict()
            )

        result = self._generate_mock_result(mcp_req)
        return JSONResponse(
            status_code=200,
            content=MCPResponse(id=mcp_req.id, result=result).to_dict()
        )

    def _generate_mock_result(self, request: MCPRequest) -> dict[str, Any]:
        """Generate mock MCP result."""
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
                "contents": [{
                    "uri": uri,
                    "mimeType": "text/plain",
                    "text": f"Mock content for resource: {uri}",
                }]
            }
        if request.method == "prompts/list":
            return {"prompts": self.config.mock_prompts or []}
        if request.method == "prompts/get":
            name = (request.params or {}).get("name", "unknown")
            return {
                "description": f"Mock prompt: {name}",
                "messages": [{
                    "role": "user",
                    "content": {"type": "text", "text": f"Mock prompt for: {name}"},
                }],
            }
        return {}

    async def _proxy_mcp(self, request: Request) -> Response:
        """Proxy MCP request to upstream."""
        if self._transport is None:
            self._transport = create_transport(
                self.config.upstream_transport,
                base_url=self.config.upstream_url,
                command=self.config.upstream_command,
                timeout=self.config.upstream_timeout,
            )
            await self._transport.start()

        body = await request.body()
        mcp_req = MCPRequest.from_json(body)
        result = await self._transport.send_request(mcp_req)
        return JSONResponse(status_code=200, content=result)

class MCPService(BaseService):
    """MCP service implementation."""

    def _create_proxy(self) -> BaseProxy:
        """Create MCP proxy instance."""
        return MCPProxy(
            config=self.config,
            fault_config=self.config.fault,
            latency_config=self.config.latency,
            stats=self.stats
        )

    def setup_routes(self) -> None:
        """Setup MCP-specific routes."""
        @self.app.post("/mcp")
        async def mcp_endpoint(request: Request):
            return await self.proxy.handle_request(request)

        self.setup_common_routes()

    async def cleanup(self) -> None:
        """Cleanup transport resources."""
        if hasattr(self.proxy, "_transport") and self.proxy._transport:
            await self.proxy._transport.stop()
```

---

### 4. API Routes (`api/`)

#### `api/health.py` - Health Endpoints
```python
from fastapi import FastAPI

def setup_health_routes(app: FastAPI, service_name: str) -> None:
    """Setup health check endpoints."""

    @app.get("/healthz")
    async def health_check():
        return {"status": "ok", "service": service_name}
```

#### `api/metrics.py` - Metrics/Scorecard Endpoints
```python
from fastapi import FastAPI

from core.statistics import StatisticsTracker

def setup_metrics_routes(
    app: FastAPI,
    service_name: str,
    stats: StatisticsTracker
) -> None:
    """Setup metrics and scorecard endpoints."""

    @app.get("/_agentbreak/scorecard")
    async def get_scorecard():
        return stats.generate_scorecard(service_name)

    @app.get("/_agentbreak/requests")
    async def get_recent_requests():
        service_stats = stats.get_service_stats(service_name)
        return {"recent_requests": service_stats.recent_requests}
```

---

### 5. Multi-Service Runner

Create a new module to run multiple services simultaneously:

```python
# agentbreak/runner.py
import asyncio
import signal
from typing import Any

import uvicorn
from config import load_config
from config.models import AgentBreakConfig, ServiceType
from core.statistics import StatisticsTracker
from services.openai import OpenAIService
from services.mcp import MCPService

class MultiServiceRunner:
    """Run multiple AgentBreak services in one process."""

    def __init__(self, config: AgentBreakConfig):
        self.config = config
        self.stats = StatisticsTracker()
        self.services: dict[str, Any] = {}
        self.servers: list[Any] = []

    def create_service(self, service_config: dict[str, Any]):
        """Factory to create appropriate service instance."""
        service_type = service_config["type"]

        if service_type == ServiceType.OPENAI:
            return OpenAIService(service_config, self.stats)
        elif service_type == ServiceType.MCP:
            return MCPService(service_config, self.stats)
        else:
            raise ValueError(f"Unknown service type: {service_type}")

    async def start(self) -> None:
        """Start all configured services."""
        for service_config in self.config.services:
            service = self.create_service(service_config)
            service.setup_routes()
            self.services[service_config.name] = service

        # Start all services concurrently
        tasks = []
        for service_config in self.config.services:
            service = self.services[service_config.name]
            config = uvicorn.Config(
                service.get_app(),
                host="0.0.0.0",
                port=service_config.port,
                log_level="warning"
            )
            server = uvicorn.Server(config)
            self.servers.append(server)
            tasks.append(server.serve())

        # Install signal handlers
        self._install_signal_handlers()

        # Run all servers
        await asyncio.gather(*tasks)

    async def stop(self) -> None:
        """Stop all services and print scorecards."""
        # Stop servers
        for server in self.servers:
            server.should_exit = True

        # Cleanup MCP transports
        for service in self.services.values():
            if hasattr(service, "cleanup"):
                await service.cleanup()

        # Print scorecards
        self._print_scorecards()

    def _print_scorecards(self) -> None:
        """Print scorecard for each service."""
        import sys
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
        import sys
        def handle_signal(signum: int, _frame: Any) -> None:
            raise KeyboardInterrupt(f"received signal {signum}")

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)
```

---

## Configuration File Specification

### Example Multi-Service Config (`config.yaml`)

```yaml
version: "1.0"

services:
  # OpenAI/LLM Service
  - name: openai-proxy
    type: openai
    mode: proxy  # or "mock"
    port: 5000
    upstream_url: https://api.openai.com
    upstream_timeout: 120.0

    # Fault Injection Configuration
    fault:
      enabled: true
      # Overall fault rate (fallback when per_error_rates not set)
      overall_rate: 0.1
      # Per-error-type rates (these take precedence over overall_rate)
      # Values represent probability (0.0 to 1.0) of that specific error
      per_error_rates:
        429: 0.4  # 40% chance of rate limit error
        500: 0.3  # 30% chance of server error
        503: 0.2  # 20% chance of service unavailable
      # Available error codes to randomly select from (when using overall_rate)
      available_codes: [429, 500, 503]

    # Latency Configuration
    latency:
      enabled: true
      probability: 0.0  # 0% chance of latency injection
      min_seconds: 5.0
      max_seconds: 15.0

    # Optional: Seed for reproducible fault injection
    seed: 12345

  # MCP Service (Mock Mode)
  - name: mcp-mock
    type: mcp
    mode: mock
    port: 5001

    fault:
      enabled: true
      overall_rate: 0.2
      available_codes: [429, 500, 503]

    latency:
      enabled: true
      probability: 0.1
      min_seconds: 5.0
      max_seconds: 15.0

    # Mock data for MCP service
    mock_tools:
      - name: echo
        description: Echo back the input text.
        inputSchema:
          type: object
          properties:
            text:
              type: string
              description: Text to echo
          required: [text]
      - name: get_time
        description: Return the current UTC time.
        inputSchema:
          type: object
          properties: {}

    mock_resources:
      - uri: file:///example/readme.txt
        name: README
        description: Example README file.
        mimeType: text/plain

    mock_prompts:
      - name: summarize
        description: Summarize a piece of text.
        arguments:
          - name: text
            description: Text to summarize.
            required: true

  # MCP Service (Proxy Mode - HTTP)
  - name: mcp-proxy-http
    type: mcp
    mode: proxy
    port: 5002
    upstream_transport: http
    upstream_url: http://localhost:8080
    upstream_timeout: 30.0

    fault:
      enabled: true
      per_error_rates:
        429: 0.3
        500: 0.4
        503: 0.3

    cache_ttl: 60.0

  # MCP Service (Proxy Mode - Stdio)
  - name: mcp-proxy-stdio
    type: mcp
    mode: proxy
    port: 5003
    upstream_transport: stdio
    upstream_command: [python, my_mcp_server.py]
    upstream_timeout: 30.0

    fault:
      enabled: true
      overall_rate: 0.15
      available_codes: [429, 500, 503]
```

### Example Config with Scenario Override

```yaml
version: "1.0"

services:
  - name: openai-brownout
    type: openai
    mode: proxy
    port: 5000
    upstream_url: https://api.openai.com
    scenario: brownout  # Applies built-in scenario settings

  - name: mcp-mixed
    type: mcp
    mode: proxy
    port: 5001
    upstream_transport: http
    upstream_url: http://localhost:8080
    scenario: mcp-mixed-transient
```

### Config Schema Reference

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `version` | string | Yes | - | Config format version |
| `services` | array | Yes | [] | List of service configurations |
| `services[].name` | string | Yes | - | Unique service identifier |
| `services[].type` | enum | Yes | - | Service type: `openai` or `mcp` |
| `services[].mode` | enum | Yes | - | Operation mode: `proxy` or `mock` |
| `services[].port` | integer | Yes | - | Port to bind the service on |
| `services[].upstream_url` | string | Conditional | - | Upstream URL (required for proxy mode) |
| `services[].upstream_transport` | enum | No | `http` | Transport type: `http`, `stdio`, `sse` |
| `services[].upstream_command` | array | Conditional | - | Command for stdio transport |
| `services[].upstream_timeout` | float | No | 30.0 | Upstream request timeout (seconds) |
| `services[].fault.enabled` | boolean | No | `true` | Enable fault injection |
| `services[].fault.overall_rate` | float | No | 0.1 | Overall fault probability |
| `services[].fault.per_error_rates` | object | No | {} | Per-error-type probabilities |
| `services[].fault.available_codes` | array | No | [429,500,503] | Error codes to select from |
| `services[].latency.enabled` | boolean | No | `true` | Enable latency injection |
| `services[].latency.probability` | float | No | 0.0 | Latency injection probability |
| `services[].latency.min_seconds` | float | No | 5.0 | Minimum latency |
| `services[].latency.max_seconds` | float | No | 15.0 | Maximum latency |
| `services[].seed` | integer | No | `null` | Random seed for reproducibility |
| `services[].scenario` | string | No | - | Built-in scenario name |
| `services[].cache_ttl` | float | No | 60.0 | Cache TTL for MCP (seconds) |
| `services[].mock_tools` | array | No | [] | Mock tools for MCP |
| `services[].mock_resources` | array | No | [] | Mock resources for MCP |
| `services[].mock_prompts` | array | No | [] | Mock prompts for MCP |

---

## Error Type Configuration System

### Design Philosophy

The error injection system is designed to be **practical** and **predictable**:

1. **Per-error-type rates** take precedence over `overall_rate`
2. **Cumulative probability** - multiple per-error rates can be set, each is checked independently
3. **Available codes** - pool of codes to choose from when `overall_rate` triggers
4. **Reproducibility** - optional seed for deterministic testing

### Practical Error Patterns

#### Pattern 1: Rate Limiting Focus
```yaml
fault:
  per_error_rates:
    429: 0.5  # 50% of all requests hit rate limits
```

#### Pattern 2: Server Unreliability
```yaml
fault:
  per_error_rates:
    500: 0.3  # 30% chance of 500 error
    503: 0.2  # 20% chance of 503 error
```

#### Pattern 3: Mixed Transient Errors
```yaml
fault:
  overall_rate: 0.2  # 20% chance of any error
  available_codes: [429, 500, 503]  # Randomly choose one
```

#### Pattern 4: Weighted Random Selection
```yaml
fault:
  per_error_rates:
    429: 0.4  # 40% of all requests
    500: 0.15 # 15% of all requests
    503: 0.1  # 10% of all requests
  # Total: ~65% of requests will fail
```

#### Pattern 5: Rare Critical Failures
```yaml
fault:
  overall_rate: 0.05  # 5% of all requests fail
  available_codes: [500, 503]  # Only server errors
```

### Fault Injection Algorithm

```python
def get_fault_code(self) -> Optional[int]:
    """
    Determine if a fault should be injected.

    Priority:
    1. Check per_error_rates (each checked independently)
    2. Fall back to overall_rate with random code selection
    """
    if not self.enabled:
        return None

    # Check per-error rates first (each checked independently)
    for code, rate in self.per_error_rates.items():
        if random.random() < rate:
            return code

    # Fall back to overall rate
    if random.random() < self.overall_rate:
        return random.choice(self.available_codes)

    return None
```

### Example Scenarios with Configurable Errors

#### Scenario: Production-like Brownout
```yaml
# 20% of requests fail, mostly rate limits
fault:
  overall_rate: 0.15
  available_codes: [429, 500, 503]
  per_error_rates:
    429: 0.1  # Additional 10% for rate limits

latency:
  probability: 0.15  # 15% get latency
  min_seconds: 2.0
  max_seconds: 10.0
```

#### Scenario: Catastrophic Failure
```yaml
# 80% of requests fail, mostly 500/503
fault:
  per_error_rates:
    500: 0.5
    503: 0.3

latency:
  probability: 0.3  # High latency too
  min_seconds: 10.0
  max_seconds: 30.0
```

#### Scenario: Gradual Degradation
```yaml
# Use seed for reproducible gradual degradation
seed: 42

fault:
  # This pattern needs time-based degradation
  # Could be extended with time_window configuration
  overall_rate: 0.05  # Starts at 5%
  available_codes: [429, 500, 503]
```

---

## Refactor Implementation Phases

### Phase 1: Foundation (Week 1)
- [x] Create new directory structure
- [x] Implement `config/` module with Pydantic models
- [x] Implement `core/statistics.py` (refactor from existing)
- [x] Write unit tests for config models
- [x] Update `pyproject.toml` with new dependencies (pydantic)

### Phase 2: Core Proxy Logic (Week 2)
- [x] Implement `core/proxy.py` (BaseProxy)
- [x] Implement `core/fault_injection.py`
- [x] Implement `core/latency.py`
- [x] Write unit tests for core components
- [x] Refactor `utils/` module
- [x] Move `mcp_transport.py` to `transports/` directory

### Phase 3: Service Implementations (Week 2-3)
- [x] Implement `services/base.py`
- [x] Implement `services/openai.py` (refactor from main.py)
- [x] Implement `services/mcp.py` (refactor from mcp_proxy.py)
- [x] Write integration tests for services
- [x] Move protocol definitions to `protocols/`

### Phase 4: Multi-Service Support (Week 3)
- [x] Implement `agentbreak/runner.py`
- [x] Implement `api/` module for common routes
- [x] Update CLI to use MultiServiceRunner
- [x] Write tests for multi-service scenarios
- [x] Update documentation

### Phase 5: Configuration & Scenarios (Week 4)
- [x] Implement scenario loading system
- [x] Create example config files
- [x] Update CLI `--config-file` flag
- [x] Migrate existing config.example.yaml
- [x] Update README with multi-service examples

### Phase 6: Testing & Cleanup (Week 4-5)
- [x] Write comprehensive test suite
- [x] Update all existing tests to work with new structure
- [x] Performance testing
- [x] Update examples to use new config format
- [x] Documentation updates

### Phase 7: Release Preparation (Week 5)
- [x] Update version to 0.3.0
- [x] Update RELEASE_NOTES.md with breaking changes
- [x] Create migration guide
- [x] Update CI/CD pipelines
- [x] Release candidate testing

---

## Migration Path

### For Existing Users

**Old Config (0.2.0):**
```yaml
mode: proxy
upstream_url: https://api.openai.com
scenario: mixed-transient
fail_rate: 0.2
```

**New Config (0.3.0):**
```yaml
version: "1.0"
services:
  - name: default
    type: openai
    mode: proxy
    port: 5000
    upstream_url: https://api.openai.com
    fault:
      overall_rate: 0.2
      available_codes: [429, 500, 503]
```

**Migration Strategy:**
1. Provide backward compatibility layer in CLI
2. Auto-migrate old config format to new format
3. Deprecation warnings for old CLI flags
4. Support both formats for 2 major versions

---

## Breaking Changes

1. **Config File Format**: Old single-service format deprecated (auto-migration provided)
2. **CLI Flags**: Some flags replaced by `--config-file` (backward compatible)
3. **Module Structure**: Internal module structure changes (affects direct imports only)
4. **API Endpoints**: Metrics endpoints now service-prefixed (e.g., `/_agentbreak/{service_name}/scorecard`)

---

## Testing Strategy

### Unit Tests
- Config model validation
- Fault injection logic
- Latency injection
- Statistics tracking
- Protocol parsing

### Integration Tests
- Service startup/shutdown
- Request routing
- Multi-service coordination
- Config file loading

### End-to-End Tests
- OpenAI proxy with real upstream
- MCP proxy with mock and real servers
- Multi-service scenarios
- Scenario application

### Performance Tests
- Concurrent request handling
- Memory usage
- Connection pooling
- Cache effectiveness

---

## Success Criteria

1. **Code Quality**
   - All modules have clear single responsibilities
   - Code duplication < 5%
   - Test coverage > 80%
   - No global state in production code paths

2. **Functionality**
   - All existing features preserved
   - Multi-service support working
   - Config file loading working
   - Error injection system flexible and predictable

3. **Usability**
   - Clear migration path for existing users
   - Comprehensive documentation
   - Example configs for common scenarios
   - Helpful error messages

4. **Performance**
   - No performance regression vs 0.2.0
   - Multi-service overhead < 5%
   - Memory usage scales linearly with services

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Breaking existing user workflows | High | Provide auto-migration and deprecation warnings |
| Performance degradation in multi-service mode | Medium | Benchmark and optimize connection pooling |
| Complexity increases maintenance burden | Medium | Clear documentation, comprehensive tests |
| Delay in timeline | Medium | Prioritize core features, defer edge cases |
| Test gaps leading to regressions | High | Require passing all tests before merging |

---

## Next Steps

1. Review and approve this plan
2. Set up development branch for refactor
3. Begin Phase 1 implementation
4. Weekly check-ins to track progress
5. Adjust timeline based on discoveries

---

## Appendix: File Comparison

### Lines of Code (Estimated)

| Module | Current | Refactored | Change |
|--------|---------|------------|--------|
| main.py | 500 | 100 (CLI only) | -400 |
| mcp_proxy.py | 800 | 0 (split) | -800 |
| mcp_protocol.py | 150 | 100 | -50 |
| mcp_transport.py | 300 | 400 (refactored) | +100 |
| **New modules** | 0 | ~1500 | +1500 |
| **Total** | 1750 | 2100 | +350 |

**Note**: Increase in total LOC is due to better separation of concerns, type safety, and comprehensive error handling. Per-file complexity is significantly reduced.

### Module Complexity (Cyclomatic Complexity)

| Module | Current | Target |
|--------|---------|--------|
| main.py | 25 | 5 |
| mcp_proxy.py | 35 | 8 |
| core/proxy.py | N/A | 10 |
| services/openai.py | N/A | 12 |
| services/mcp.py | N/A | 15 |

---

*End of Refactor Plan*
