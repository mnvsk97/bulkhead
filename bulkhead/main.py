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
app = FastAPI(title="bulkhead")


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
        400: ("Invalid request injected by Bulkhead.", "invalid_request_error"),
        401: ("Authentication failure injected by Bulkhead.", "authentication_error"),
        403: ("Permission failure injected by Bulkhead.", "permission_error"),
        404: ("Resource not found injected by Bulkhead.", "not_found_error"),
        413: ("Request too large injected by Bulkhead.", "invalid_request_error"),
        429: ("Rate limit exceeded by Bulkhead fault injection.", "rate_limit_error"),
        500: ("Upstream failure injected by Bulkhead.", "server_error"),
        503: ("Service unavailable injected by Bulkhead.", "server_error"),
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
        code = int(value)
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
        "id": "chatcmpl-bulkhead-mock",
        "object": "chat.completion",
        "created": 0,
        "model": "bulkhead-mock",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Bulkhead mock response."},
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
        "Bulkhead Resilience Scorecard",
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
                        "message": f"Bulkhead could not reach upstream: {exc}",
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


@app.get("/_bulkhead/scorecard")
async def get_scorecard() -> dict[str, Any]:
    return scorecard_data()


@app.get("/_bulkhead/requests")
async def get_requests() -> dict[str, Any]:
    return {"recent_requests": stats.recent_requests}


def install_signal_handlers() -> None:
    def handle_signal(signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt(f"received signal {signum}")

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)


@cli.command(
    help=(
        "Start Bulkhead.\n\n"
        "If --config is omitted, Bulkhead looks for ./config.yaml.\n"
        "Examples:\n"
        "  bulkhead start --mode mock --scenario mixed-transient\n"
        "  bulkhead start --mode proxy --upstream-url https://api.openai.com --scenario mixed-transient\n"
        "  bulkhead start --config bulkhead.yaml"
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
    port: int = typer.Option(PORT, help="Port to bind Bulkhead on."),
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
    )
    if config.seed is not None:
        random.seed(config.seed)
    install_signal_handlers()
    try:
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
    finally:
        print_scorecard()


if __name__ == "__main__":
    cli()
