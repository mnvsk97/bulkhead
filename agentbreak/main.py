from __future__ import annotations

import asyncio
import hashlib
import json
import random
import signal
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import typer
import uvicorn
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

PORT = 5000
cli = typer.Typer(add_completion=False, help="Minimal chaos proxy for OpenAI-compatible LLM apps.")
SUPPORTED_ERROR_CODES = (400, 401, 403, 404, 413, 429, 500, 503)
DEFAULT_ERROR_CODES = (429, 500, 503)
SCENARIOS: dict[str, dict[str, Any]] = {
    "mixed-transient": {"error_codes": (429, 500, 503), "latency_p": 0.0},
    "rate-limited": {"error_codes": (429,), "latency_p": 0.0},
    "provider-flaky": {"error_codes": (500, 503), "latency_p": 0.0},
    "non-retryable": {"error_codes": (400, 401, 403, 404, 413), "latency_p": 0.0},
    "brownout": {"error_codes": (429, 500, 503), "latency_p": 0.2},
    # MCP-specific scenarios
    "mcp-tool-failures": {
        "error_codes": DEFAULT_ERROR_CODES,
        "latency_p": 0.0,
        "mcp_fail_rate": 0.3,
        "mcp_error_codes": (429, 500, 503),
        "mcp_latency_p": 0.0,
    },
    "mcp-resource-unavailable": {
        "error_codes": DEFAULT_ERROR_CODES,
        "latency_p": 0.0,
        "mcp_fail_rate": 0.5,
        "mcp_error_codes": (404, 503),
        "mcp_latency_p": 0.0,
    },
    "mcp-slow-tools": {
        "error_codes": DEFAULT_ERROR_CODES,
        "latency_p": 0.0,
        "mcp_fail_rate": 0.0,
        "mcp_error_codes": DEFAULT_ERROR_CODES,
        "mcp_latency_p": 0.9,
    },
    "mcp-initialization-failure": {
        "error_codes": DEFAULT_ERROR_CODES,
        "latency_p": 0.0,
        "mcp_fail_rate": 0.5,
        "mcp_error_codes": (500, 503),
        "mcp_latency_p": 0.0,
    },
    "mcp-mixed-transient": {
        "error_codes": DEFAULT_ERROR_CODES,
        "latency_p": 0.0,
        "mcp_fail_rate": 0.2,
        "mcp_error_codes": (429, 500, 503),
        "mcp_latency_p": 0.1,
    },
}


@dataclass
class Config:
    mode: str = "proxy"
    upstream_url: str = ""
    fail_rate: float = 0.1
    error_codes: tuple[int, ...] = DEFAULT_ERROR_CODES
    fault_weights: tuple[tuple[int, float], ...] = ()
    latency_p: float = 0.0
    latency_min: float = 5.0
    latency_max: float = 15.0
    seed: int | None = None
    # MCP-specific configuration
    mcp_mode: str = "disabled"  # "disabled", "mock", "proxy"
    mcp_upstream_transport: str = "http"  # "http", "stdio", "sse"
    mcp_upstream_command: tuple[str, ...] = ()
    mcp_upstream_url: str = ""
    mcp_fail_rate: float = 0.1
    mcp_error_codes: tuple[int, ...] = DEFAULT_ERROR_CODES
    mcp_latency_p: float = 0.0


@dataclass
class Stats:
    total_requests: int = 0
    injected_faults: int = 0
    latency_injections: int = 0
    upstream_successes: int = 0
    upstream_failures: int = 0
    duplicate_requests: int = 0
    suspected_loops: int = 0
    seen_fingerprints: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    recent_requests: list[dict[str, Any]] = field(default_factory=list)


config: Config | None = None
stats = Stats()
app = FastAPI(title="agentbreak")


@cli.callback()
def cli_root() -> None:
    pass


def clamp_probability(value: float) -> float:
    return max(0.0, min(1.0, value))


def should_inject(probability: float) -> bool:
    return random.random() < clamp_probability(probability)


def pick_error_code() -> int:
    assert config is not None
    if config.fault_weights:
        codes = [code for code, _ in config.fault_weights]
        weights = [weight for _, weight in config.fault_weights]
        return random.choices(codes, weights=weights, k=1)[0]
    return random.choice(config.error_codes)


def openai_error(status_code: int) -> dict[str, Any]:
    error_map = {
        400: ("Invalid request injected by AgentBreak.", "invalid_request_error"),
        401: ("Authentication failure injected by AgentBreak.", "authentication_error"),
        403: ("Permission failure injected by AgentBreak.", "permission_error"),
        404: ("Resource not found injected by AgentBreak.", "not_found_error"),
        413: ("Request too large injected by AgentBreak.", "invalid_request_error"),
        429: ("Rate limit exceeded by AgentBreak fault injection.", "rate_limit_error"),
        500: ("Upstream failure injected by AgentBreak.", "server_error"),
        503: ("Service unavailable injected by AgentBreak.", "server_error"),
    }
    message, error_type = error_map[status_code]
    return {
        "error": {
            "message": message,
            "type": error_type,
            "code": status_code,
        }
    }


def parse_error_codes(raw: str) -> tuple[int, ...]:
    codes = []
    for item in raw.split(","):
        value = item.strip()
        if not value:
            continue
        try:
            code = int(value)
        except ValueError:
            raise typer.BadParameter(f"Invalid error code {value!r}: must be an integer.")
        if code not in SUPPORTED_ERROR_CODES:
            raise typer.BadParameter(
                f"Unsupported error code {code}. Supported: {', '.join(str(c) for c in SUPPORTED_ERROR_CODES)}"
            )
        codes.append(code)
    if not codes:
        raise typer.BadParameter("At least one error code is required.")
    return tuple(codes)


def parse_fault_weights(raw: str) -> tuple[tuple[int, float], ...]:
    weights = []
    total = 0.0
    for item in raw.split(","):
        value = item.strip()
        if not value:
            continue
        if "=" not in value:
            raise typer.BadParameter("Faults must look like 500=0.3,429=0.2")
        code_raw, weight_raw = value.split("=", 1)
        code = int(code_raw.strip())
        if code not in SUPPORTED_ERROR_CODES:
            raise typer.BadParameter(
                f"Unsupported error code {code}. Supported: {', '.join(str(c) for c in SUPPORTED_ERROR_CODES)}"
            )
        weight = float(weight_raw.strip())
        if weight < 0 or weight > 1:
            raise typer.BadParameter("Fault weights must be between 0.0 and 1.0")
        weights.append((code, weight))
        total += weight
    if not weights:
        raise typer.BadParameter("At least one fault weight is required.")
    if total > 1.0:
        raise typer.BadParameter("Total fault weight must be <= 1.0")
    return tuple(weights)


def parse_fault_weights_mapping(raw: dict[Any, Any]) -> tuple[tuple[int, float], ...]:
    parts = []
    for key, value in raw.items():
        parts.append(f"{int(key)}={float(value)}")
    return parse_fault_weights(",".join(parts))


def maybe_load_config(path: str | None) -> dict[str, Any]:
    candidate = Path(path) if path else Path("config.yaml")
    if not candidate.exists():
        return {}
    with candidate.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise typer.BadParameter("Config file must contain a top-level mapping.")
    return data


def resolve_scenario(name: str) -> dict[str, Any]:
    if name not in SCENARIOS:
        raise typer.BadParameter(f"Unknown scenario '{name}'. Available: {', '.join(SCENARIOS)}")
    return SCENARIOS[name]


def choose(value: Any, fallback: Any) -> Any:
    if value == "":
        return fallback
    return fallback if value is None else value


def has_cli_overrides(**values: Any) -> bool:
    return any(value not in (None, "") for value in values.values())


def validate_latency_range(latency_min: float, latency_max: float) -> tuple[float, float]:
    if latency_min < 0 or latency_max < 0:
        raise typer.BadParameter("Latency values must be >= 0.")
    if latency_min > latency_max:
        raise typer.BadParameter("--latency-min must be <= --latency-max.")
    return latency_min, latency_max


def mock_completion() -> dict[str, Any]:
    return {
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


def fingerprint_request(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def record_request(body: bytes) -> None:
    stats.total_requests += 1
    fingerprint = fingerprint_request(body)
    stats.seen_fingerprints[fingerprint] += 1
    seen = stats.seen_fingerprints[fingerprint]
    if seen > 1:
        stats.duplicate_requests += 1
    if seen > 2:
        stats.suspected_loops += 1
    payload: Any
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {"raw": body.decode("utf-8", errors="replace")}
    stats.recent_requests.append({"fingerprint": fingerprint, "count": seen, "body": payload})
    if len(stats.recent_requests) > 20:
        stats.recent_requests.pop(0)


def scorecard_data() -> dict[str, Any]:
    score = 100
    score -= stats.injected_faults * 3
    score -= stats.upstream_failures * 12
    score -= stats.duplicate_requests * 2
    score -= stats.suspected_loops * 10
    score = max(0, min(100, score))
    if stats.upstream_failures == 0 and stats.suspected_loops == 0:
        outcome = "PASS"
    elif stats.upstream_successes > 0:
        outcome = "DEGRADED"
    else:
        outcome = "FAIL"
    return {
        "requests_seen": stats.total_requests,
        "injected_faults": stats.injected_faults,
        "latency_injections": stats.latency_injections,
        "upstream_successes": stats.upstream_successes,
        "upstream_failures": stats.upstream_failures,
        "duplicate_requests": stats.duplicate_requests,
        "suspected_loops": stats.suspected_loops,
        "run_outcome": outcome,
        "resilience_score": score,
    }


def print_scorecard() -> None:
    data = scorecard_data()
    lines = [
        "",
        "AgentBreak Resilience Scorecard",
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


def filter_headers(headers: httpx.Headers) -> dict[str, str]:
    skip = {"host", "content-length"}
    return {key: value for key, value in headers.items() if key.lower() not in skip}


async def maybe_delay() -> None:
    assert config is not None
    if not should_inject(config.latency_p):
        return
    stats.latency_injections += 1
    delay = random.uniform(config.latency_min, config.latency_max)
    await asyncio.sleep(delay)


@app.post("/v1/chat/completions")
async def proxy_chat_completions(request: Request) -> Response:
    assert config is not None
    body = await request.body()
    record_request(body)

    if should_inject(config.fail_rate):
        status_code = pick_error_code()
        stats.injected_faults += 1
        stats.upstream_failures += 1
        return JSONResponse(status_code=status_code, content=openai_error(status_code))

    await maybe_delay()

    if config.mode == "mock":
        stats.upstream_successes += 1
        return JSONResponse(status_code=200, content=mock_completion())

    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            response = await client.post(
                f"{config.upstream_url.rstrip('/')}/v1/chat/completions",
                content=body,
                headers=filter_headers(request.headers),
            )
        except httpx.HTTPError as exc:
            stats.upstream_failures += 1
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

    if response.status_code < 400:
        stats.upstream_successes += 1
    else:
        stats.upstream_failures += 1

    return Response(
        content=response.content,
        status_code=response.status_code,
        headers=filter_headers(response.headers),
        media_type=response.headers.get("content-type"),
    )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


def current_scorecard() -> dict[str, Any]:
    return scorecard_data()


def current_requests() -> dict[str, Any]:
    return {"recent_requests": stats.recent_requests}


@app.get("/_agentbreak/scorecard")
async def get_agentbreak_scorecard() -> dict[str, Any]:
    return current_scorecard()


@app.get("/_agentbreak/requests")
async def get_agentbreak_requests() -> dict[str, Any]:
    return current_requests()


def install_signal_handlers() -> None:
    def handle_signal(signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt(f"received signal {signum}")

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)


@cli.command(
    help=(
        "Start AgentBreak.\n\n"
        "If --config is omitted, AgentBreak looks for ./config.yaml.\n"
        "Examples:\n"
        "  agentbreak start --mode mock --scenario mixed-transient\n"
        "  agentbreak start --mode proxy --upstream-url https://api.openai.com --scenario mixed-transient\n"
        "  agentbreak start --config agentbreak.yaml\n"
        "  agentbreak start --mode mock --mcp-mode mock\n"
        "  agentbreak start --mode mock --mcp-mode proxy --mcp-upstream-url http://localhost:8080"
    )
)
def start(
    config_path: str | None = typer.Option(None, "--config", help="Optional YAML config path. Defaults to ./config.yaml if present."),
    mode: str | None = typer.Option(None, help="proxy forwards to a real upstream, mock returns fake successes."),
    upstream_url: str | None = typer.Option(None, help="OpenAI-compatible upstream base URL, without /v1."),
    scenario: str | None = typer.Option(None, help="Built-in fault scenario."),
    fail_rate: float | None = typer.Option(None, help="Probability of injecting a fault before success/forwarding."),
    faults: str | None = typer.Option(None, help="Absolute per-code rates, e.g. 500=0.3,429=0.2."),
    error_codes: str | None = typer.Option(
        None,
        help="Comma-separated injected status codes. Supported: 400,401,403,404,413,429,500,503.",
    ),
    latency_p: float | None = typer.Option(None, help="Probability of injecting latency before forwarding."),
    latency_min: float | None = typer.Option(None, help="Minimum injected latency in seconds."),
    latency_max: float | None = typer.Option(None, help="Maximum injected latency in seconds."),
    seed: int | None = typer.Option(None, help="Optional deterministic random seed."),
    port: int = typer.Option(PORT, help="Port to bind AgentBreak on."),
    mcp_mode: str | None = typer.Option(None, "--mcp-mode", help="MCP proxy mode: disabled, mock, or proxy."),
    mcp_upstream_url: str | None = typer.Option(None, "--mcp-upstream-url", help="Upstream MCP server base URL (http/sse transports)."),
    mcp_upstream_transport: str | None = typer.Option(None, "--mcp-upstream-transport", help="Transport to upstream MCP server: http, stdio, or sse."),
    mcp_upstream_command: str | None = typer.Option(None, "--mcp-upstream-command", help="Command for stdio MCP transport, e.g. 'python server.py'."),
    mcp_fail_rate: float | None = typer.Option(None, "--mcp-fail-rate", help="Probability of injecting a fault into MCP requests."),
    mcp_error_codes: str | None = typer.Option(
        None,
        "--mcp-error-codes",
        help="Comma-separated HTTP-style codes for MCP fault injection. Supported: 400,401,403,404,413,429,500,503.",
    ),
    mcp_latency_p: float | None = typer.Option(None, "--mcp-latency-p", help="Probability of injecting latency into MCP requests."),
) -> None:
    global config
    file_config = maybe_load_config(config_path)
    if not file_config and not has_cli_overrides(
        mode=mode,
        upstream_url=upstream_url,
        scenario=scenario,
        fail_rate=fail_rate,
        faults=faults,
        error_codes=error_codes,
        latency_p=latency_p,
        latency_min=latency_min,
        latency_max=latency_max,
        seed=seed,
        mcp_mode=mcp_mode,
        mcp_upstream_url=mcp_upstream_url,
        mcp_upstream_transport=mcp_upstream_transport,
        mcp_upstream_command=mcp_upstream_command,
        mcp_fail_rate=mcp_fail_rate,
        mcp_error_codes=mcp_error_codes,
        mcp_latency_p=mcp_latency_p,
    ):
        raise typer.BadParameter(
            "No config.yaml found and no CLI settings were provided. "
            "Create config.yaml, pass --config, or run with explicit flags such as "
            "--mode mock --scenario mixed-transient."
        )

    resolved_mode = choose(mode, file_config.get("mode", "proxy"))
    resolved_upstream_url = choose(upstream_url, file_config.get("upstream_url", ""))
    resolved_scenario_name = choose(scenario, file_config.get("scenario", "mixed-transient"))
    resolved_latency_min = choose(latency_min, file_config.get("latency_min", 5.0))
    resolved_latency_max = choose(latency_max, file_config.get("latency_max", 15.0))
    resolved_seed = choose(seed, file_config.get("seed"))

    if resolved_mode not in {"proxy", "mock"}:
        raise typer.BadParameter("mode must be 'proxy' or 'mock'")
    if resolved_mode == "proxy" and not resolved_upstream_url:
        raise typer.BadParameter("--upstream-url is required in proxy mode.")

    scenario_config = resolve_scenario(resolved_scenario_name)

    raw_faults = choose(faults, file_config.get("faults"))
    if isinstance(raw_faults, dict):
        fault_weights = parse_fault_weights_mapping(raw_faults)
    elif raw_faults:
        fault_weights = parse_fault_weights(str(raw_faults))
    else:
        fault_weights = ()

    raw_error_codes = choose(error_codes, file_config.get("error_codes"))
    if isinstance(raw_error_codes, list):
        resolved_error_codes = parse_error_codes(",".join(str(code) for code in raw_error_codes))
    elif raw_error_codes:
        resolved_error_codes = parse_error_codes(str(raw_error_codes))
    else:
        resolved_error_codes = scenario_config["error_codes"]

    resolved_latency_p = choose(latency_p, file_config.get("latency_p", scenario_config["latency_p"]))
    resolved_latency_min, resolved_latency_max = validate_latency_range(resolved_latency_min, resolved_latency_max)
    resolved_fail_rate = sum(weight for _, weight in fault_weights) if fault_weights else choose(
        fail_rate, file_config.get("fail_rate", 0.1)
    )

    # Resolve MCP-specific configuration
    resolved_mcp_mode = choose(mcp_mode, file_config.get("mcp_mode", "disabled"))
    if resolved_mcp_mode not in {"disabled", "mock", "proxy"}:
        raise typer.BadParameter("mcp-mode must be 'disabled', 'mock', or 'proxy'")

    resolved_mcp_upstream_transport = choose(
        mcp_upstream_transport, file_config.get("mcp_upstream_transport", "http")
    )
    if resolved_mcp_upstream_transport not in {"http", "stdio", "sse"}:
        raise typer.BadParameter("mcp-upstream-transport must be 'http', 'stdio', or 'sse'")

    resolved_mcp_upstream_url = choose(mcp_upstream_url, file_config.get("mcp_upstream_url", ""))
    resolved_mcp_upstream_command_raw = choose(
        mcp_upstream_command, file_config.get("mcp_upstream_command", "")
    )
    if isinstance(resolved_mcp_upstream_command_raw, list):
        resolved_mcp_upstream_command: tuple[str, ...] = tuple(str(x) for x in resolved_mcp_upstream_command_raw)
    elif resolved_mcp_upstream_command_raw:
        resolved_mcp_upstream_command = tuple(str(resolved_mcp_upstream_command_raw).split())
    else:
        resolved_mcp_upstream_command = ()

    if resolved_mcp_mode == "proxy" and resolved_mcp_upstream_transport in {"http", "sse"} and not resolved_mcp_upstream_url:
        raise typer.BadParameter("--mcp-upstream-url is required for http and sse MCP transports.")
    if resolved_mcp_mode == "proxy" and resolved_mcp_upstream_transport == "stdio" and not resolved_mcp_upstream_command:
        raise typer.BadParameter("--mcp-upstream-command is required for stdio MCP transport.")

    resolved_mcp_fail_rate = choose(
        mcp_fail_rate, file_config.get("mcp_fail_rate", scenario_config.get("mcp_fail_rate", 0.1))
    )

    raw_mcp_error_codes = choose(mcp_error_codes, file_config.get("mcp_error_codes"))
    if isinstance(raw_mcp_error_codes, list):
        resolved_mcp_error_codes = parse_error_codes(",".join(str(c) for c in raw_mcp_error_codes))
    elif raw_mcp_error_codes:
        resolved_mcp_error_codes = parse_error_codes(str(raw_mcp_error_codes))
    else:
        resolved_mcp_error_codes = scenario_config.get("mcp_error_codes", DEFAULT_ERROR_CODES)

    resolved_mcp_latency_p = choose(
        mcp_latency_p, file_config.get("mcp_latency_p", scenario_config.get("mcp_latency_p", 0.0))
    )

    config = Config(
        mode=resolved_mode,
        upstream_url=resolved_upstream_url,
        fail_rate=clamp_probability(resolved_fail_rate),
        error_codes=resolved_error_codes,
        fault_weights=fault_weights,
        latency_p=clamp_probability(resolved_latency_p),
        latency_min=resolved_latency_min,
        latency_max=resolved_latency_max,
        seed=resolved_seed,
        mcp_mode=resolved_mcp_mode,
        mcp_upstream_transport=resolved_mcp_upstream_transport,
        mcp_upstream_command=resolved_mcp_upstream_command,
        mcp_upstream_url=resolved_mcp_upstream_url,
        mcp_fail_rate=clamp_probability(resolved_mcp_fail_rate),
        mcp_error_codes=resolved_mcp_error_codes,
        mcp_latency_p=clamp_probability(resolved_mcp_latency_p),
    )
    if config.seed is not None:
        random.seed(config.seed)
    if resolved_mcp_mode != "disabled":
        from agentbreak import mcp_proxy as _mcp_proxy  # noqa: PLC0415
        _mcp_proxy.mcp_config = _mcp_proxy.MCPConfig(
            mode=resolved_mcp_mode,
            upstream_url=resolved_mcp_upstream_url,
            upstream_transport=resolved_mcp_upstream_transport,
            upstream_command=resolved_mcp_upstream_command,
            fail_rate=clamp_probability(resolved_mcp_fail_rate),
            fault_codes=tuple(resolved_mcp_error_codes),
            latency_p=clamp_probability(resolved_mcp_latency_p),
            latency_min=resolved_latency_min,
            latency_max=resolved_latency_max,
            seed=resolved_seed,
        )
        _mcp_proxy.mcp_stats = _mcp_proxy.MCPStats()
        _mcp_proxy._stdio_transport = None
        _mcp_proxy._sse_transport = None
        _mcp_proxy._upstream_http_client = None
        _mcp_proxy._response_cache = {}
        app.mount("/", _mcp_proxy.app)
    install_signal_handlers()
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        print_scorecard()
        if resolved_mcp_mode != "disabled":
            _mcp_proxy.print_scorecard()


def _register_mcp_subcommands() -> None:
    from agentbreak import mcp_proxy as _mcp_proxy_module  # noqa: PLC0415
    cli.add_typer(_mcp_proxy_module.cli, name="mcp")


_register_mcp_subcommands()


if __name__ == "__main__":
    cli()
