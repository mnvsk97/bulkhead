"""Fault injection logic for AgentBreak proxy services."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

from agentbreak.config.models import FaultConfig

if TYPE_CHECKING:
    from agentbreak.core.proxy import ProxyContext

_ERROR_TYPE_MAP: dict[int, str] = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "invalid_request_error",
    429: "rate_limit_error",
    500: "server_error",
    503: "server_error",
}

_ERROR_MESSAGE_MAP: dict[int, str] = {
    400: "Invalid request injected by AgentBreak.",
    401: "Authentication failure injected by AgentBreak.",
    403: "Permission failure injected by AgentBreak.",
    404: "Resource not found injected by AgentBreak.",
    413: "Request too large injected by AgentBreak.",
    429: "Rate limit exceeded by AgentBreak fault injection.",
    500: "Upstream failure injected by AgentBreak.",
    503: "Service unavailable injected by AgentBreak.",
}


@dataclass
class FaultResult:
    """Result of a fault injection decision."""

    error_code: int
    error_type: str
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def for_code(cls, code: int, service_name: str = "") -> "FaultResult":
        return cls(
            error_code=code,
            error_type=_ERROR_TYPE_MAP.get(code, "unknown_error"),
            message=_ERROR_MESSAGE_MAP.get(code, "Error injected by AgentBreak fault injection."),
            metadata={"injected_by": "agentbreak", "service": service_name},
        )


class FaultInjector:
    """Handles fault injection logic for proxy services."""

    def __init__(self, config: FaultConfig) -> None:
        self.config = config

    async def maybe_inject(self, context: "ProxyContext") -> Optional[FaultResult]:
        """Check if a fault should be injected and return fault details.

        Returns a FaultResult if a fault should be injected, otherwise None.
        """
        if not self.config.enabled:
            return None

        fault_code = self.config.get_fault_code()
        if fault_code is None:
            return None

        return FaultResult.for_code(fault_code, service_name=context.service_name)
