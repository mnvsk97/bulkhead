from __future__ import annotations

import json
import os
from typing import Any

import httpx
from langchain_core.tools import tool


def _mcp_url() -> str:
    return os.environ["REPORT_MCP_URL"]


def _parse_mcp_response(response: httpx.Response) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        for line in response.text.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])
        raise ValueError("No JSON-RPC payload found in SSE response")
    return response.json()


async def _call_mcp_tool(name: str, arguments: dict[str, Any]) -> str:
    headers = {}
    token = os.getenv("REPORT_MCP_BEARER_TOKEN")
    if token:
        headers["authorization"] = f"Bearer {token}"

    async with httpx.AsyncClient(
        headers=headers,
        timeout=httpx.Timeout(30.0, read=120.0),
    ) as client:
        init = await client.post(
            _mcp_url(),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "clientInfo": {"name": "langgraph-report-agent", "version": "0.1.0"},
                },
            },
            headers={
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
                "mcp-protocol-version": "2024-11-05",
            },
        )
        init.raise_for_status()
        session_id = init.headers.get("mcp-session-id")
        rpc_headers = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            "mcp-protocol-version": "2024-11-05",
        }
        if session_id:
            rpc_headers["mcp-session-id"] = session_id
        await client.post(_mcp_url(), json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=rpc_headers)
        result = await client.post(
            _mcp_url(),
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
            headers=rpc_headers,
        )
        result.raise_for_status()
        payload = _parse_mcp_response(result)["result"]["content"]
    return json.dumps(payload, indent=2, sort_keys=True)


@tool
async def list_report_sections(report_type: str) -> str:
    """List the canonical sections for a given report type."""
    return await _call_mcp_tool("list_report_sections", {"report_type": report_type})


@tool
async def fetch_kpi_snapshot(metric_names: list[str], as_of: str) -> str:
    """Fetch deterministic KPI data for the requested metrics and date."""
    return await _call_mcp_tool("fetch_kpi_snapshot", {"metric_names": metric_names, "as_of": as_of})


@tool
async def lookup_account_notes(account_id: str) -> str:
    """Look up recent qualitative notes for an account."""
    return await _call_mcp_tool("lookup_account_notes", {"account_id": account_id})


@tool
async def render_report_brief(account_id: str, report_type: str) -> str:
    """Generate a compact report brief for an account and report type."""
    return await _call_mcp_tool("render_report_brief", {"account_id": account_id, "report_type": report_type})


TOOLS = [
    list_report_sections,
    fetch_kpi_snapshot,
    lookup_account_notes,
    render_report_brief,
]
