"""AgentBreak MCP server — exposes chaos testing tools for Claude Code."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("agentbreak")


# ---------------------------------------------------------------------------
# State — tracks proxy process, env backup, wiring status
# ---------------------------------------------------------------------------

@dataclass
class _State:
    proxy_process: subprocess.Popen | None = None
    proxy_port: int | None = None
    env_file_path: str | None = None
    env_backup_path: str | None = None
    is_wired: bool = False
    project_path: str = "."


_state = _State()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EXCLUDE_DIRS = {
    ".venv", "venv", "node_modules", "__pycache__", ".git",
    ".agentbreak", "dist", "build", ".tox", ".mypy_cache", ".pytest_cache",
}

_PROVIDER_PATTERNS: dict[str, list[str]] = {
    "openai": [
        r"from\s+openai\b", r"import\s+openai\b", r"ChatOpenAI",
        r"OpenAI\(", r"langchain_openai", r"OPENAI_API_KEY",
        r'model\s*=\s*["\']gpt-',
    ],
    "anthropic": [
        r"from\s+anthropic\b", r"import\s+anthropic\b", r"ChatAnthropic",
        r"Anthropic\(", r"langchain_anthropic", r"ANTHROPIC_API_KEY",
        r'model\s*=\s*["\']claude-',
    ],
}

_FRAMEWORK_PATTERNS: dict[str, list[str]] = {
    "langgraph": [r"from\s+langgraph\b", r"import\s+langgraph\b", r"StateGraph"],
    "langchain": [r"from\s+langchain\b", r"import\s+langchain\b"],
    "crewai": [r"from\s+crewai\b", r"import\s+crewai\b"],
    "autogen": [r"from\s+autogen\b", r"import\s+autogen\b"],
    "llama_index": [r"from\s+llama_index\b", r"import\s+llama_index\b"],
    "smolagents": [r"from\s+smolagents\b", r"import\s+smolagents\b"],
}

_MCP_PATTERNS = [
    r"MCPClient", r"tools/call", r"@mcp\.tool", r"mcp_server",
    r"StreamableHTTPTransport",
]

_ERROR_HANDLING_PATTERNS = {
    "retry_logic": [r"max_retries", r"from\s+tenacity\b", r"from\s+backoff\b", r"@retry"],
    "timeout": [r"timeout\s*=", r"request_timeout"],
    "try_except": [r"except\s+.*(?:Error|Exception)", r"except\s+\("],
}


def _scan_files(root: str, patterns: list[str], extensions: tuple[str, ...] = (".py",)) -> list[str]:
    """Return list of pattern descriptions that matched any file."""
    matches: list[str] = []
    root_path = Path(root)
    for ext in extensions:
        for fpath in root_path.rglob(f"*{ext}"):
            if _EXCLUDE_DIRS & set(fpath.parts):
                continue
            try:
                text = fpath.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for pat in patterns:
                if pat not in matches and re.search(pat, text):
                    matches.append(pat)
    return matches


def _detect_category(root: str, category_patterns: dict[str, list[str]]) -> list[str]:
    """Return category names where at least one pattern matched."""
    found: list[str] = []
    for name, pats in category_patterns.items():
        if _scan_files(root, pats):
            found.append(name)
    return found


def _find_env_file(root: str) -> str | None:
    """Find .env file in project root."""
    for name in (".env", ".env.local"):
        path = Path(root) / name
        if path.exists():
            return str(path)
    return None


def _parse_env_key(line: str) -> str | None:
    """Extract the env var key from a .env line, handling export prefix and quotes."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    # Handle export prefix
    if stripped.startswith("export "):
        stripped = stripped[7:].strip()
    if "=" not in stripped:
        return None
    key = stripped.split("=", 1)[0].strip()
    # Strip quotes around key (rare but possible)
    if len(key) >= 2 and key[0] in ("'", '"') and key[-1] == key[0]:
        key = key[1:-1]
    return key


def _wait_for_health(port: int, process: subprocess.Popen, timeout: float = 10.0) -> bool:
    """Poll /healthz until it responds, process dies, or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            return False
        try:
            resp = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=2.0)
            if resp.status_code == 200:
                return True
        except (httpx.ConnectError, httpx.ReadError):
            pass
        time.sleep(0.3)
    return False


def _read_port() -> int:
    """Read serve port from application.yaml, defaulting to 5005."""
    try:
        from agentbreak.config import load_application_config
        app_yaml = str(Path(_state.project_path) / ".agentbreak" / "application.yaml")
        cfg = load_application_config(app_yaml)
        return cfg.serve.port
    except Exception:
        return 5005


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def agentbreak_init(project_path: str = ".") -> str:
    """Initialize .agentbreak/ directory with default config files (application.yaml and scenarios.yaml).
    Call this first before any other agentbreak tools."""
    _state.project_path = project_path
    agentbreak_dir = Path(project_path) / ".agentbreak"
    agentbreak_dir.mkdir(exist_ok=True)

    results = []
    app_path = agentbreak_dir / "application.yaml"
    if app_path.exists():
        results.append(f"Already exists: {app_path}")
    else:
        from agentbreak.main import DEFAULT_APPLICATION_YAML
        app_path.write_text(DEFAULT_APPLICATION_YAML, encoding="utf-8")
        results.append(f"Created {app_path}")

    scenarios_path = agentbreak_dir / "scenarios.yaml"
    if scenarios_path.exists():
        results.append(f"Already exists: {scenarios_path}")
    else:
        from agentbreak.main import SCENARIOS_YAML_LLM_ONLY
        scenarios_path.write_text(SCENARIOS_YAML_LLM_ONLY, encoding="utf-8")
        results.append(f"Created {scenarios_path}")

    return "\n".join(results)


@mcp.tool()
def agentbreak_analyze(project_path: str = ".") -> str:
    """Scan the codebase to detect LLM provider, agent framework, MCP tool usage, and error handling patterns.
    Returns a JSON report of findings to inform scenario generation."""
    _state.project_path = project_path

    providers = _detect_category(project_path, _PROVIDER_PATTERNS)
    frameworks = _detect_category(project_path, _FRAMEWORK_PATTERNS)
    mcp_usage = bool(_scan_files(project_path, _MCP_PATTERNS))

    error_handling: dict[str, bool] = {}
    for category, pats in _ERROR_HANDLING_PATTERNS.items():
        error_handling[category] = bool(_scan_files(project_path, pats))

    # Detect env var names (not values)
    env_file = _find_env_file(project_path)
    env_vars: list[str] = []
    if env_file:
        try:
            text = Path(env_file).read_text(encoding="utf-8", errors="ignore")
            for line in text.splitlines():
                key = _parse_env_key(line)
                if key and any(kw in key.upper() for kw in ("API_KEY", "BASE_URL", "TOKEN", "SECRET")):
                    env_vars.append(key)
        except OSError:
            pass

    return json.dumps({
        "providers": providers if providers else ["unknown"],
        "frameworks": frameworks if frameworks else ["raw_sdk"],
        "mcp_tools_detected": mcp_usage,
        "error_handling": error_handling,
        "env_file": env_file,
        "relevant_env_vars": env_vars,
    }, indent=2)


@mcp.tool()
def agentbreak_generate_config(
    provider: str = "openai",
    mcp_enabled: bool = False,
    mcp_upstream_url: str = "",
    has_retry_logic: bool = False,
    has_timeout_handling: bool = False,
    model_name: str = "",
    tool_names: list[str] | None = None,
    mode: str = "mock",
) -> str:
    """Generate tailored application.yaml and scenarios.yaml based on analysis findings.

    Args:
        provider: LLM provider - 'openai' or 'anthropic'
        mcp_enabled: Whether MCP tools were detected
        mcp_upstream_url: MCP server URL if mcp_enabled
        has_retry_logic: Whether the agent has retry logic
        has_timeout_handling: Whether the agent handles timeouts
        model_name: Specific model name to target (e.g. 'gpt-4o')
        tool_names: Specific MCP tool names to target
        mode: 'mock' (no API key needed) or 'proxy' (forwards to real API)
    """
    project_path = _state.project_path
    agentbreak_dir = Path(project_path) / ".agentbreak"
    agentbreak_dir.mkdir(exist_ok=True)

    # Build application.yaml
    app_config: dict[str, Any] = {
        "llm": {"enabled": True, "mode": mode},
        "mcp": {"enabled": mcp_enabled},
        "serve": {"port": 5005},
    }
    if mode == "proxy":
        if provider == "openai":
            app_config["llm"]["upstream_url"] = "https://api.openai.com"
            app_config["llm"]["auth"] = {"type": "bearer", "env": "OPENAI_API_KEY"}
        else:
            app_config["llm"]["upstream_url"] = "https://api.anthropic.com"
            app_config["llm"]["auth"] = {"type": "bearer", "env": "ANTHROPIC_API_KEY"}
    if mcp_enabled and mcp_upstream_url:
        app_config["mcp"]["upstream_url"] = mcp_upstream_url

    # Build scenarios
    scenarios: list[dict[str, Any]] = []
    match_field: dict[str, str] = {}
    if model_name:
        match_field = {"model": model_name}

    # Error scenarios — prioritize if no retry logic
    error_prob = 0.4 if not has_retry_logic else 0.2
    scenarios.append({
        "name": "llm-http-error",
        "summary": f"HTTP 500 errors on LLM calls (agent {'lacks' if not has_retry_logic else 'has'} retry logic)",
        "target": "llm_chat",
        **({"match": match_field} if match_field else {}),
        "fault": {"kind": "http_error", "status_code": 500},
        "schedule": {"mode": "random", "probability": error_prob},
    })

    scenarios.append({
        "name": "llm-rate-limit",
        "summary": "Rate limit errors (429)",
        "target": "llm_chat",
        **({"match": match_field} if match_field else {}),
        "fault": {"kind": "http_error", "status_code": 429},
        "schedule": {"mode": "random", "probability": 0.3},
    })

    # Latency — prioritize if no timeout handling
    latency_ms = (5000, 15000) if not has_timeout_handling else (2000, 5000)
    scenarios.append({
        "name": "llm-latency",
        "summary": f"Latency spikes on LLM calls (agent {'lacks' if not has_timeout_handling else 'has'} timeout handling)",
        "target": "llm_chat",
        **({"match": match_field} if match_field else {}),
        "fault": {"kind": "latency", "min_ms": latency_ms[0], "max_ms": latency_ms[1]},
        "schedule": {"mode": "random", "probability": 0.3},
    })

    # Response mutation
    scenarios.append({
        "name": "llm-bad-json",
        "summary": "Malformed JSON responses",
        "target": "llm_chat",
        **({"match": match_field} if match_field else {}),
        "fault": {"kind": "invalid_json"},
        "schedule": {"mode": "random", "probability": 0.15},
    })

    # MCP scenarios
    if mcp_enabled:
        if tool_names:
            for tool_name in tool_names:
                scenarios.append({
                    "name": f"mcp-{tool_name}-error",
                    "summary": f"Errors on {tool_name}",
                    "target": "mcp_tool",
                    "match": {"tool_name": tool_name},
                    "fault": {"kind": "http_error", "status_code": 503},
                    "schedule": {"mode": "random", "probability": 0.3},
                })
                scenarios.append({
                    "name": f"mcp-{tool_name}-timeout",
                    "summary": f"Timeouts on {tool_name}",
                    "target": "mcp_tool",
                    "match": {"tool_name": tool_name},
                    "fault": {"kind": "timeout", "min_ms": 5000, "max_ms": 10000},
                    "schedule": {"mode": "random", "probability": 0.2},
                })
        else:
            scenarios.append({
                "name": "mcp-tool-errors",
                "summary": "Random MCP tool failures",
                "target": "mcp_tool",
                "fault": {"kind": "http_error", "status_code": 503},
                "schedule": {"mode": "random", "probability": 0.3},
            })
            scenarios.append({
                "name": "mcp-tool-latency",
                "summary": "MCP tool latency",
                "target": "mcp_tool",
                "fault": {"kind": "latency", "min_ms": 3000, "max_ms": 8000},
                "schedule": {"mode": "random", "probability": 0.4},
            })

    scenario_file = {"version": 1, "scenarios": scenarios}

    # Write files
    app_path = agentbreak_dir / "application.yaml"
    app_path.write_text(yaml.safe_dump(app_config, default_flow_style=False, sort_keys=False), encoding="utf-8")

    scenarios_path = agentbreak_dir / "scenarios.yaml"
    scenarios_path.write_text(yaml.safe_dump(scenario_file, default_flow_style=False, sort_keys=False), encoding="utf-8")

    return json.dumps({
        "application_yaml": str(app_path),
        "scenarios_yaml": str(scenarios_path),
        "scenarios_count": len(scenarios),
        "scenarios": [{"name": s["name"], "summary": s["summary"], "target": s["target"]} for s in scenarios],
    }, indent=2)


@mcp.tool()
def agentbreak_inspect() -> str:
    """Discover tools from the upstream MCP server. Requires mcp.enabled=true and mcp.upstream_url set in application.yaml.
    Writes .agentbreak/registry.json."""
    result = subprocess.run(
        [sys.executable, "-m", "agentbreak.main", "inspect"],
        capture_output=True, text=True, cwd=_state.project_path,
        timeout=60,
    )
    if result.returncode != 0:
        return json.dumps({"status": "error", "message": result.stderr.strip()})
    return json.dumps({"status": "ok", "output": result.stdout.strip()})


@mcp.tool()
def agentbreak_validate() -> str:
    """Validate the .agentbreak/ config files. Returns validation results."""
    result = subprocess.run(
        [sys.executable, "-m", "agentbreak.main", "validate"],
        capture_output=True, text=True, cwd=_state.project_path,
        timeout=30,
    )
    if result.returncode != 0:
        return json.dumps({"status": "error", "message": result.stderr.strip()})
    return json.dumps({"status": "ok", "output": result.stdout.strip()})


@mcp.tool()
def agentbreak_start(label: str = "", verbose: bool = True) -> str:
    """Start the AgentBreak chaos proxy in the background.
    Returns the port number and health status.

    Args:
        label: Optional label for this run (used in history)
        verbose: Enable verbose logging (default true)
    """
    if _state.proxy_process is not None:
        if _state.proxy_process.poll() is None:
            return json.dumps({
                "status": "already_running",
                "port": _state.proxy_port,
                "message": f"Proxy is already running on port {_state.proxy_port}",
            })
        _state.proxy_process = None
        _state.proxy_port = None

    port = _read_port()

    cmd = [sys.executable, "-m", "agentbreak.main", "serve"]
    if verbose:
        cmd.append("-v")
    if label:
        cmd.extend(["--label", label])

    _state.proxy_process = subprocess.Popen(
        cmd,
        cwd=_state.project_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    _state.proxy_port = port

    healthy = _wait_for_health(port, _state.proxy_process)

    # Check if process died during startup
    if not healthy and _state.proxy_process.poll() is not None:
        exit_code = _state.proxy_process.returncode
        _state.proxy_process = None
        _state.proxy_port = None
        return json.dumps({
            "status": "failed_to_start",
            "message": f"Proxy exited during startup with code {exit_code}",
        })

    return json.dumps({
        "status": "running" if healthy else "started_but_unhealthy",
        "port": port,
        "pid": _state.proxy_process.pid,
        "healthy": healthy,
    })


@mcp.tool()
def agentbreak_wire(
    env_file: str = "",
    provider: str = "openai",
    port: int = 0,
) -> str:
    """Rewire the agent's .env file to point at the AgentBreak proxy.
    Backs up the original .env first. Call agentbreak_revert to restore.

    Args:
        env_file: Path to .env file. Auto-detected if empty.
        provider: 'openai' or 'anthropic' — determines which env vars to set.
        port: Proxy port. Uses the running proxy port if 0.
    """
    if port == 0:
        port = _state.proxy_port or 5005

    # Find env file
    if not env_file:
        found = _find_env_file(_state.project_path)
        if not found:
            return json.dumps({
                "status": "error",
                "message": "No .env file found. Create one or pass env_file path.",
            })
        env_file = found

    env_path = Path(env_file)
    if not env_path.exists():
        return json.dumps({
            "status": "error",
            "message": f"File not found: {env_file}",
        })

    # Backup — detect orphaned backups from previous crashed sessions
    backup_path = str(env_path) + ".agentbreak-backup"
    if not Path(backup_path).exists():
        shutil.copy2(str(env_path), backup_path)
    _state.env_file_path = str(env_path)
    _state.env_backup_path = backup_path

    # Read and modify
    lines = env_path.read_text(encoding="utf-8").splitlines()
    new_lines: list[str] = []
    modified_keys: list[str] = []

    base_url_key = "OPENAI_BASE_URL" if provider == "openai" else "ANTHROPIC_BASE_URL"
    base_url_value = f"http://127.0.0.1:{port}/v1" if provider == "openai" else f"http://127.0.0.1:{port}"

    found_base_url = False
    for line in lines:
        key = _parse_env_key(line)
        if key == base_url_key:
            new_lines.append(f"{base_url_key}={base_url_value}")
            modified_keys.append(key)
            found_base_url = True
            continue
        new_lines.append(line)

    if not found_base_url:
        new_lines.append(f"{base_url_key}={base_url_value}")
        modified_keys.append(base_url_key)

    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    _state.is_wired = True

    return json.dumps({
        "status": "wired",
        "env_file": str(env_path),
        "backup": backup_path,
        "modified_keys": modified_keys,
        "base_url": base_url_value,
        "message": f"Agent wired to AgentBreak on port {port}. Call agentbreak_revert to restore.",
    })


@mcp.tool()
def agentbreak_revert() -> str:
    """Restore the original .env file from backup. Safe to call multiple times."""
    if not _state.env_backup_path or not _state.env_file_path:
        return json.dumps({
            "status": "nothing_to_revert",
            "message": "No backup found. Either wire was never called, or already reverted.",
        })

    backup = Path(_state.env_backup_path)
    target = Path(_state.env_file_path)

    if not backup.exists():
        _state.is_wired = False
        _state.env_backup_path = None
        return json.dumps({
            "status": "error",
            "message": f"Backup file missing: {backup}",
        })

    shutil.copy2(str(backup), str(target))
    backup.unlink()
    _state.is_wired = False
    _state.env_backup_path = None

    return json.dumps({
        "status": "reverted",
        "env_file": str(target),
        "message": "Original .env restored.",
    })


@mcp.tool()
def agentbreak_stop() -> str:
    """Stop the AgentBreak proxy. Automatically reverts .env if still wired."""
    results: list[str] = []

    # Auto-revert env if still wired
    if _state.is_wired:
        revert_result = json.loads(agentbreak_revert())
        results.append(f"env: {revert_result['status']}")

    # Stop proxy
    if _state.proxy_process is not None:
        if _state.proxy_process.poll() is None:
            _state.proxy_process.terminate()
            try:
                _state.proxy_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _state.proxy_process.kill()
                _state.proxy_process.wait(timeout=2)
            results.append(f"proxy: stopped (pid {_state.proxy_process.pid})")
        else:
            results.append("proxy: already exited")
        _state.proxy_process = None
        _state.proxy_port = None
    else:
        results.append("proxy: not running")

    return json.dumps({
        "status": "stopped",
        "details": results,
    })


@mcp.tool()
def agentbreak_scorecard() -> str:
    """Fetch the current scorecard from the running proxy. Returns LLM and MCP scorecards."""
    port = _state.proxy_port or 5005

    results: dict[str, Any] = {}
    for key, endpoint in [("llm_scorecard", "scorecard"), ("mcp_scorecard", "mcp-scorecard")]:
        try:
            resp = httpx.get(f"http://127.0.0.1:{port}/_agentbreak/{endpoint}", timeout=5.0)
            if resp.status_code == 200:
                results[key] = resp.json()
            else:
                results[key] = {"error": f"HTTP {resp.status_code}"}
        except httpx.ConnectError:
            results[key] = {"error": "Proxy not reachable — is it running?"}
        except Exception as e:
            results[key] = {"error": str(e)}

    return json.dumps(results, indent=2)


@mcp.tool()
def agentbreak_status() -> str:
    """Check current AgentBreak state: is proxy running, is .env wired, etc."""
    proxy_running = (
        _state.proxy_process is not None
        and _state.proxy_process.poll() is None
    )

    return json.dumps({
        "proxy_running": proxy_running,
        "proxy_port": _state.proxy_port if proxy_running else None,
        "proxy_pid": _state.proxy_process.pid if proxy_running else None,
        "env_wired": _state.is_wired,
        "env_file": _state.env_file_path if _state.is_wired else None,
        "env_backup": _state.env_backup_path if _state.is_wired else None,
        "project_path": _state.project_path,
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_server() -> None:
    """Start the MCP server on stdio."""
    mcp.run()
