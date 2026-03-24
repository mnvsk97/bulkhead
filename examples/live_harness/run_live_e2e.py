from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from langgraph_sdk import get_sync_client


REPO_ROOT = Path(__file__).resolve().parents[2]
_VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
PYTHON = str(_VENV_PYTHON) if _VENV_PYTHON.exists() else sys.executable
_VENV_LANGGRAPH = REPO_ROOT / ".venv" / "bin" / "langgraph"
LANGGRAPH = str(_VENV_LANGGRAPH) if _VENV_LANGGRAPH.exists() else shutil.which("langgraph") or "langgraph"
UV = shutil.which("uv")
PYTHON312 = shutil.which("python3.12") or "/opt/homebrew/bin/python3.12"
RUNTIME_ROOT = REPO_ROOT / ".tmp" / "agentbreak-live-runtime"
RUNTIME_PYTHON = RUNTIME_ROOT / "bin" / "python"
RUNTIME_LANGGRAPH = RUNTIME_ROOT / "bin" / "langgraph"


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def find_free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class RunLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, message: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
        print(line, flush=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


@dataclass
class ManagedProcess:
    name: str
    command: list[str]
    cwd: Path
    env: dict[str, str]
    log_path: Path
    process: subprocess.Popen[str] | None = None

    def start(self, logger: RunLogger) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = self.log_path.open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            self.command,
            cwd=self.cwd,
            env=self.env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        logger.log(f"started {self.name}: {' '.join(self.command)}")

    def stop(self, logger: RunLogger) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        logger.log(f"stopped {self.name} with exit={self.process.returncode}")

    def assert_running(self, logger: RunLogger) -> None:
        if self.process is not None and self.process.poll() is not None:
            tail = tail_file(self.log_path)
            raise RuntimeError(f"{self.name} exited early with code {self.process.returncode}\n{tail}")
        logger.log(f"{self.name} is still running")


def tail_file(path: Path, lines: int = 40) -> str:
    if not path.exists():
        return "(no log output)"
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(content[-lines:])


def run_command(command: list[str], cwd: Path, env: dict[str, str], logger: RunLogger) -> None:
    logger.log(f"running command: {' '.join(command)}")
    completed = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True)
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed: {' '.join(command)}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    if completed.stdout.strip():
        logger.log(completed.stdout.strip())


def ensure_example_dependencies(logger: RunLogger) -> None:
    logger.log("ensuring Python 3.12 LangGraph runtime is ready")
    if not UV:
        raise RuntimeError("uv is required for the live harness runtime bootstrap")
    if not Path(PYTHON312).exists():
        raise RuntimeError(f"python3.12 was not found: {PYTHON312}")
    if not RUNTIME_PYTHON.exists():
        run_command([UV, "venv", "--python", PYTHON312, str(RUNTIME_ROOT)], cwd=REPO_ROOT, env=os.environ.copy(), logger=logger)
    if not RUNTIME_LANGGRAPH.exists():
        run_command(
            [
                UV,
                "pip",
                "install",
                "--python",
                str(RUNTIME_PYTHON),
                "-r",
                str(REPO_ROOT / "examples" / "simple_react_agent" / "requirements.txt"),
            ],
            cwd=REPO_ROOT,
            env=os.environ.copy(),
            logger=logger,
        )
    has_langgraph_api = subprocess.run(
        [str(RUNTIME_PYTHON), "-c", "import langgraph_api"],
        cwd=REPO_ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    ).returncode == 0
    if not has_langgraph_api:
        run_command([str(RUNTIME_PYTHON), "-m", "ensurepip"], cwd=REPO_ROOT, env=os.environ.copy(), logger=logger)
        run_command([str(RUNTIME_PYTHON), "-m", "pip", "install", "langgraph-api"], cwd=REPO_ROOT, env=os.environ.copy(), logger=logger)


def langgraph_command() -> list[str]:
    if RUNTIME_LANGGRAPH.exists():
        return [str(RUNTIME_LANGGRAPH)]
    if Path(LANGGRAPH).exists():
        return [LANGGRAPH]
    if RUNTIME_PYTHON.exists():
        return [str(RUNTIME_PYTHON), "-m", "langgraph_cli"]
    return [str(PYTHON), "-m", "langgraph_cli"]


def wait_for_http(url: str, logger: RunLogger, timeout_s: float = 30.0) -> None:
    start = time.time()
    last_error = ""
    while time.time() - start < timeout_s:
        try:
            response = httpx.get(url, timeout=2.0)
            if response.status_code < 500:
                logger.log(f"healthy: {url} -> {response.status_code}")
                return
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
        time.sleep(0.25)
    raise RuntimeError(f"timed out waiting for {url}: {last_error}")


def wait_for_mcp_tools(url: str, logger: RunLogger, timeout_s: float = 30.0) -> None:
    start = time.time()
    last_error = ""
    while time.time() - start < timeout_s:
        try:
            init = httpx.post(
                url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "clientInfo": {"name": "agentbreak-live-harness", "version": "0.1.0"},
                    },
                },
                headers={
                    "content-type": "application/json",
                    "accept": "application/json, text/event-stream",
                    "mcp-protocol-version": "2024-11-05",
                },
                timeout=5.0,
            )
            init.raise_for_status()
            session_id = init.headers.get("mcp-session-id")
            headers = {
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
                "mcp-protocol-version": "2024-11-05",
            }
            if session_id:
                headers["mcp-session-id"] = session_id
            notify = {"jsonrpc": "2.0", "method": "notifications/initialized"}
            httpx.post(url, json=notify, headers=headers, timeout=5.0)
            tools = httpx.post(
                url,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                headers=headers,
                timeout=5.0,
            )
            tools.raise_for_status()
            payload = parse_mcp_http_response(tools)
            if payload.get("result", {}).get("tools"):
                logger.log(f"MCP tools ready at {url}")
                return
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
        time.sleep(0.5)
    raise RuntimeError(f"timed out waiting for MCP tools at {url}: {last_error}")


def wait_for_langgraph(base_url: str, logger: RunLogger, timeout_s: float = 45.0) -> None:
    start = time.time()
    last_error = ""
    while time.time() - start < timeout_s:
        try:
            response = httpx.get(f"{base_url}/docs", timeout=2.0)
            if response.status_code == 200:
                logger.log(f"LangGraph server ready at {base_url}")
                return
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
        time.sleep(0.5)
    raise RuntimeError(f"timed out waiting for LangGraph server at {base_url}: {last_error}")


def parse_mcp_http_response(response: httpx.Response) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        for line in response.text.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])
        raise ValueError("No JSON-RPC event found in SSE response")
    return response.json()


def write_env_file(path: Path, values: dict[str, str]) -> None:
    lines = [f"{key}={value}" for key, value in values.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_yaml_file(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def collect_http_json(url: str, path: Path) -> None:
    response = httpx.get(url, timeout=10.0)
    response.raise_for_status()
    path.write_text(json.dumps(response.json(), indent=2, sort_keys=True), encoding="utf-8")


def run_langgraph_request(base_url: str, prompt: str) -> dict[str, Any]:
    client = get_sync_client(url=base_url)
    result = client.runs.wait(
        None,
        "agent",
        input={"messages": [{"role": "human", "content": prompt}]},
    )
    return {"prompt": prompt, "result": result}


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def main() -> int:
    run_root = Path(tempfile.gettempdir()) / "agentbreak-live" / now_stamp()
    artifacts_dir = run_root / "artifacts"
    logs_dir = run_root
    logger = RunLogger(run_root / "run.log")
    logger.log(f"run directory: {run_root}")

    ensure_example_dependencies(logger)

    mock_openai_port = find_free_port()
    reporting_mcp_port = find_free_port()
    agentbreak_port = find_free_port()
    langgraph_port = 2024

    mock_openai_url = f"http://127.0.0.1:{mock_openai_port}"
    reporting_mcp_url = f"http://127.0.0.1:{reporting_mcp_port}/mcp"
    agentbreak_url = f"http://127.0.0.1:{agentbreak_port}"
    langgraph_url = f"http://127.0.0.1:{langgraph_port}"

    prompt = (
        "Generate a quarterly business review for acct-acme. "
        "Use the available tools, include the main sections, summarize KPI trends, "
        "mention notable account notes, and end with recommended actions."
    )

    common_env = os.environ.copy()
    common_env["PYTHONUNBUFFERED"] = "1"

    processes: list[ManagedProcess] = []
    try:
        mock_openai = ManagedProcess(
            name="mock-openai",
            command=[str(PYTHON), str(REPO_ROOT / "examples" / "live_harness" / "mock_openai_server.py")],
            cwd=REPO_ROOT,
            env={**common_env, "MOCK_OPENAI_PORT": str(mock_openai_port)},
            log_path=logs_dir / "mock-openai.log",
        )
        reporting_mcp = ManagedProcess(
            name="reporting-mcp",
            command=[str(PYTHON), str(REPO_ROOT / "examples" / "simple_mcp_server" / "main.py")],
            cwd=REPO_ROOT,
            env={**common_env, "REPORTING_MCP_PORT": str(reporting_mcp_port)},
            log_path=logs_dir / "reporting-mcp.log",
        )
        processes.extend([mock_openai, reporting_mcp])
        mock_openai.start(logger)
        reporting_mcp.start(logger)

        wait_for_http(f"{mock_openai_url}/healthz", logger)
        wait_for_mcp_tools(reporting_mcp_url, logger)

        direct_env_path = REPO_ROOT / "examples" / "simple_react_agent" / ".env"
        write_env_file(
            direct_env_path,
            {
                "OPENAI_API_KEY": "test-key",
                "OPENAI_BASE_URL": f"{mock_openai_url}/v1",
                "OPENAI_MODEL": "gpt-4o-mini",
                "REPORT_MCP_URL": reporting_mcp_url,
            },
        )
        logger.log("wrote direct LangGraph environment")

        langgraph_direct = ManagedProcess(
            name="langgraph-direct",
            command=[*langgraph_command(), "dev", "--port", str(langgraph_port)],
            cwd=REPO_ROOT / "examples" / "simple_react_agent",
            env=common_env,
            log_path=logs_dir / "langgraph.log",
        )
        processes.append(langgraph_direct)
        langgraph_direct.start(logger)
        wait_for_langgraph(langgraph_url, logger)
        direct_result = run_langgraph_request(langgraph_url, prompt)
        save_json(artifacts_dir / "direct-response.json", direct_result)
        logger.log("saved direct LangGraph response")

        langgraph_direct.stop(logger)

        application_payload = {
            "llm": {
                "enabled": True,
                "mode": "proxy",
                "upstream_url": mock_openai_url,
            },
            "mcp": {
                "enabled": True,
                "upstream_url": reporting_mcp_url,
            },
            "serve": {
                "host": "127.0.0.1",
                "port": agentbreak_port,
            },
        }
        scenarios_payload = {
            "version": 1,
            "scenarios": [
                {
                    "name": "llm-latency",
                    "summary": "Inject mild LLM latency",
                    "target": "llm_chat",
                    "match": {},
                    "fault": {"kind": "latency", "min_ms": 40, "max_ms": 120},
                    "schedule": {"mode": "always"},
                },
                {
                    "name": "mcp-latency",
                    "summary": "Inject mild tool latency",
                    "target": "mcp_tool",
                    "match": {"tool_name_pattern": "*"},
                    "fault": {"kind": "latency", "min_ms": 30, "max_ms": 80},
                    "schedule": {"mode": "always"},
                },
            ],
        }
        application_path = artifacts_dir / "effective-application.yaml"
        scenarios_path = artifacts_dir / "effective-scenarios.yaml"
        registry_path = artifacts_dir / "registry.json"
        write_yaml_file(application_path, application_payload)
        write_yaml_file(scenarios_path, scenarios_payload)

        run_command(
            [
                str(PYTHON),
                "-m",
                "agentbreak.main",
                "inspect",
                "--config",
                str(application_path),
                "--registry",
                str(registry_path),
            ],
            cwd=REPO_ROOT,
            env=common_env,
            logger=logger,
        )

        agentbreak = ManagedProcess(
            name="agentbreak",
            command=[
                str(PYTHON),
                "-m",
                "agentbreak.main",
                "serve",
                "--config",
                str(application_path),
                "--scenarios",
                str(scenarios_path),
                "--registry",
                str(registry_path),
            ],
            cwd=REPO_ROOT,
            env=common_env,
            log_path=logs_dir / "agentbreak.log",
        )
        processes.append(agentbreak)
        agentbreak.start(logger)
        wait_for_http(f"{agentbreak_url}/healthz", logger)
        wait_for_mcp_tools(f"{agentbreak_url}/mcp", logger)

        write_env_file(
            direct_env_path,
            {
                "OPENAI_API_KEY": "test-key",
                "OPENAI_BASE_URL": f"{agentbreak_url}/v1",
                "OPENAI_MODEL": "gpt-4o-mini",
                "REPORT_MCP_URL": f"{agentbreak_url}/mcp",
            },
        )
        logger.log("rewrote LangGraph environment for AgentBreak")

        langgraph_chaos = ManagedProcess(
            name="langgraph-chaos",
            command=[*langgraph_command(), "dev", "--port", str(langgraph_port)],
            cwd=REPO_ROOT / "examples" / "simple_react_agent",
            env=common_env,
            log_path=logs_dir / "langgraph.log",
        )
        processes.append(langgraph_chaos)
        langgraph_chaos.start(logger)
        wait_for_langgraph(langgraph_url, logger)
        chaos_result = run_langgraph_request(langgraph_url, prompt)
        save_json(artifacts_dir / "chaos-response.json", chaos_result)
        logger.log("saved AgentBreak-backed LangGraph response")

        collect_http_json(f"{agentbreak_url}/_agentbreak/llm-scorecard", artifacts_dir / "agentbreak-llm-scorecard.json")
        collect_http_json(f"{agentbreak_url}/_agentbreak/llm-requests", artifacts_dir / "agentbreak-llm-requests.json")
        collect_http_json(f"{agentbreak_url}/_agentbreak/mcp-scorecard", artifacts_dir / "agentbreak-mcp-scorecard.json")
        collect_http_json(f"{agentbreak_url}/_agentbreak/mcp-requests", artifacts_dir / "agentbreak-mcp-requests.json")

        logger.log("artifacts written:")
        for path in sorted(artifacts_dir.rglob("*")):
            if path.is_file():
                logger.log(f"  - {path}")

        logger.log("live E2E run completed successfully")
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.log(f"run failed: {exc}")
        for proc in processes:
            with suppress(Exception):
                proc.assert_running(logger)
        for proc in processes:
            logger.log(f"log tail for {proc.name}:\n{tail_file(proc.log_path)}")
        return 1
    finally:
        for proc in reversed(processes):
            with suppress(Exception):
                proc.stop(logger)


if __name__ == "__main__":
    raise SystemExit(main())
