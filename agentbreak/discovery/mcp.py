from __future__ import annotations

from typing import Any

import httpx

from agentbreak import __version__
from agentbreak.config import MCPConfig, MCPPrompt, MCPRegistry, MCPResource, MCPTool

MCP_PROTOCOL_VERSION = "2024-11-05"


def _rpc_envelope(method: str, params: dict[str, Any] | None, request_id: int) -> dict[str, Any]:
    envelope: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        envelope["params"] = params
    return envelope


def parse_mcp_response(response: httpx.Response) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        for line in response.text.splitlines():
            if line.startswith("data: "):
                return httpx.Response(200, content=line[6:].encode("utf-8")).json()
        raise ValueError("No MCP JSON-RPC event found in SSE response")
    return response.json()


async def inspect_mcp_server(config: MCPConfig) -> MCPRegistry:
    headers = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
        "mcp-protocol-version": MCP_PROTOCOL_VERSION,
        **config.auth.headers(),
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        init_response = await client.post(
            config.upstream_url,
            json=_rpc_envelope(
                "initialize",
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "clientInfo": {"name": "agentbreak", "version": __version__},
                },
                1,
            ),
            headers=headers,
        )
        init_response.raise_for_status()
        parse_mcp_response(init_response)
        session_id = init_response.headers.get("mcp-session-id")
        if session_id:
            headers["mcp-session-id"] = session_id

        await client.post(
            config.upstream_url,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=headers,
        )

        tools = await _collect_paginated(client, config.upstream_url, headers, "tools/list", "tools", 2)
        resources = await _collect_paginated(client, config.upstream_url, headers, "resources/list", "resources", 100)
        prompts = await _collect_paginated(client, config.upstream_url, headers, "prompts/list", "prompts", 200)
    return MCPRegistry(
        tools=[
            MCPTool(
                name=tool["name"],
                description=tool.get("description", ""),
                inputSchema=tool.get("inputSchema", {}),
            )
            for tool in tools
        ],
        resources=[
            MCPResource(
                uri=resource["uri"],
                name=resource.get("name", ""),
                description=resource.get("description", ""),
                mimeType=resource.get("mimeType", ""),
            )
            for resource in resources
        ],
        prompts=[
            MCPPrompt(
                name=prompt["name"],
                description=prompt.get("description", ""),
                arguments=prompt.get("arguments", []),
            )
            for prompt in prompts
        ],
    )


async def _collect_paginated(
    client: httpx.AsyncClient,
    upstream_url: str,
    headers: dict[str, str],
    method: str,
    result_key: str,
    request_id: int,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        params = {"cursor": cursor} if cursor else {}
        response = await client.post(
            upstream_url,
            json=_rpc_envelope(method, params, request_id),
            headers=headers,
        )
        if response.status_code == 404:
            return items
        response.raise_for_status()
        payload = parse_mcp_response(response).get("result", {})
        items.extend(payload.get(result_key, []))
        cursor = payload.get("nextCursor")
        if not cursor:
            return items
