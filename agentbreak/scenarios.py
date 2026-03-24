from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator


IMPLEMENTED_TARGETS = {"llm_chat", "mcp_tool"}
PRESET_SCENARIOS: dict[str, list[dict[str, Any]]] = {
    "brownout": [
        {
            "name": "brownout-latency",
            "summary": "Inject intermittent LLM latency",
            "target": "llm_chat",
            "match": {},
            "fault": {"kind": "latency", "min_ms": 5000, "max_ms": 15000},
            "schedule": {"mode": "random", "probability": 0.2},
        },
        {
            "name": "brownout-errors",
            "summary": "Inject intermittent LLM rate limits",
            "target": "llm_chat",
            "match": {},
            "fault": {"kind": "http_error", "status_code": 429},
            "schedule": {"mode": "random", "probability": 0.3},
        },
    ],
    "mcp-slow-tools": [
        {
            "name": "mcp-slow-tools",
            "summary": "Inject latency into MCP tool calls",
            "target": "mcp_tool",
            "match": {},
            "fault": {"kind": "latency", "min_ms": 5000, "max_ms": 15000},
            "schedule": {"mode": "random", "probability": 0.9},
        }
    ],
    "mcp-tool-failures": [
        {
            "name": "mcp-tool-failures",
            "summary": "Inject MCP transport failures",
            "target": "mcp_tool",
            "match": {},
            "fault": {"kind": "http_error", "status_code": 503},
            "schedule": {"mode": "random", "probability": 0.3},
        }
    ],
    "mcp-mixed-transient": [
        {
            "name": "mcp-mixed-transient-latency",
            "summary": "Inject intermittent MCP latency",
            "target": "mcp_tool",
            "match": {},
            "fault": {"kind": "latency", "min_ms": 5000, "max_ms": 15000},
            "schedule": {"mode": "random", "probability": 0.1},
        },
        {
            "name": "mcp-mixed-transient-errors",
            "summary": "Inject intermittent MCP transport failures",
            "target": "mcp_tool",
            "match": {},
            "fault": {"kind": "http_error", "status_code": 503},
            "schedule": {"mode": "random", "probability": 0.2},
        },
    ],
}

Target = Literal[
    "llm_chat",
    "mcp_tool",
    "queue",
    "state",
    "memory",
    "artifact_store",
    "approval",
    "browser_worker",
    "multi_agent",
    "telemetry",
]

FaultKind = Literal[
    "http_error",
    "latency",
    "timeout",
    "empty_response",
    "invalid_json",
    "schema_violation",
    "wrong_content",
    "large_response",
]

ScheduleMode = Literal["always", "random", "periodic"]


class MatchSpec(BaseModel):
    tool_name: str | None = None
    tool_name_pattern: str | None = None
    route: str | None = None
    method: str | None = None
    model: str | None = None

    def matches(self, request: dict[str, Any]) -> bool:
        if self.tool_name is not None and request.get("tool_name") != self.tool_name:
            return False
        if self.tool_name_pattern is not None and not fnmatch(request.get("tool_name", ""), self.tool_name_pattern):
            return False
        if self.route is not None and request.get("route") != self.route:
            return False
        if self.method is not None and request.get("method") != self.method:
            return False
        if self.model is not None and request.get("model") != self.model:
            return False
        return True


class FaultSpec(BaseModel):
    kind: FaultKind
    status_code: int | None = None
    min_ms: int | None = None
    max_ms: int | None = None
    size_bytes: int | None = None
    body: str | None = None

    @model_validator(mode="after")
    def validate_fault(self) -> "FaultSpec":
        if self.kind == "http_error" and self.status_code is None:
            raise ValueError("http_error faults require status_code")
        if self.kind in {"latency", "timeout"}:
            if self.min_ms is None or self.max_ms is None:
                raise ValueError(f"{self.kind} faults require min_ms and max_ms")
            if self.min_ms < 0 or self.max_ms < 0 or self.min_ms > self.max_ms:
                raise ValueError("fault min_ms/max_ms must be valid non-negative bounds")
        if self.kind == "large_response" and (self.size_bytes is None or self.size_bytes <= 0):
            raise ValueError("large_response faults require size_bytes > 0")
        return self


class ScheduleSpec(BaseModel):
    mode: ScheduleMode = "always"
    probability: float = 1.0
    every: int | None = None
    length: int | None = None

    @model_validator(mode="after")
    def validate_schedule(self) -> "ScheduleSpec":
        if self.mode == "random":
            if not 0.0 <= self.probability <= 1.0:
                raise ValueError("random schedules require probability between 0 and 1")
        if self.mode == "periodic":
            if self.every is None or self.every <= 0:
                raise ValueError("periodic schedules require every > 0")
            if self.length is None or self.length <= 0:
                raise ValueError("periodic schedules require length > 0")
            if self.length > self.every:
                raise ValueError("periodic schedules require length <= every")
        return self


class Scenario(BaseModel):
    name: str
    summary: str
    target: Target
    match: MatchSpec = Field(default_factory=MatchSpec)
    fault: FaultSpec
    schedule: ScheduleSpec = Field(default_factory=ScheduleSpec)
    tags: list[str] = Field(default_factory=list)


class ScenarioFile(BaseModel):
    version: int = 1
    scenarios: list[Scenario] = Field(default_factory=list)


def load_scenarios(path: str | None) -> ScenarioFile:
    candidate = Path(path) if path else Path("scenarios.yaml")
    if not candidate.exists():
        return ScenarioFile()
    with candidate.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if isinstance(data, dict):
        presets = data.pop("presets", [])
        preset = data.pop("preset", None)
        if preset is not None:
            presets = [preset, *presets]
        expanded: list[dict[str, Any]] = []
        for name in presets:
            if name not in PRESET_SCENARIOS:
                raise ValueError(f"Unknown scenario preset: {name}")
            expanded.extend(PRESET_SCENARIOS[name])
        if expanded:
            data["scenarios"] = [*expanded, *(data.get("scenarios") or [])]
    return ScenarioFile.model_validate(data)


def validate_supported_targets(scenarios: ScenarioFile) -> None:
    unsupported = sorted({scenario.target for scenario in scenarios.scenarios if scenario.target not in IMPLEMENTED_TARGETS})
    if unsupported:
        raise ValueError(
            "Recognized but unimplemented scenario targets: "
            + ", ".join(unsupported)
            + ". See docs/TODO_SCENARIOS.md."
        )
    invalid = sorted(
        scenario.name
        for scenario in scenarios.scenarios
        if scenario.target == "llm_chat" and scenario.fault.kind == "timeout"
    )
    if invalid:
        raise ValueError("llm_chat timeout faults are not implemented: " + ", ".join(invalid))
