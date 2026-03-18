"""Configuration loading and validation for AgentBreak."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml

from .models import (
    AgentBreakConfig,
    MCPServiceConfig,
    OpenAIServiceConfig,
    ServiceType,
)


def load_config(path: Optional[Path] = None) -> AgentBreakConfig:
    """Load configuration from YAML file or return defaults."""
    if path is None:
        path = Path("config.yaml")

    if not path.exists():
        return AgentBreakConfig(
            services=[
                OpenAIServiceConfig(
                    name="default",
                    type=ServiceType.OPENAI,
                    mode="mock",
                    port=5000,
                    upstream_url="https://api.openai.com",
                )
            ]
        )

    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    return _parse_config(data)


def _parse_config(data: dict[str, Any]) -> AgentBreakConfig:
    """Parse a config dict into an AgentBreakConfig, resolving service types."""
    from .scenarios import apply_scenario

    services_data = data.get("services", [])
    services = []
    for svc in services_data:
        scenario_name = svc.get("scenario")
        if scenario_name:
            svc = apply_scenario(svc, scenario_name)
        svc_type = svc.get("type", "openai")
        if svc_type == ServiceType.MCP or svc_type == "mcp":
            services.append(MCPServiceConfig.model_validate(svc))
        else:
            services.append(OpenAIServiceConfig.model_validate(svc))
    return AgentBreakConfig(version=data.get("version", "1.0"), services=services)


def load_scenario(name: str) -> dict[str, Any]:
    """Load a built-in scenario configuration dict."""
    from .scenarios import SCENARIOS

    if name not in SCENARIOS:
        raise ValueError(f"Unknown scenario: {name}. Available: {list(SCENARIOS.keys())}")
    return SCENARIOS[name].copy()
