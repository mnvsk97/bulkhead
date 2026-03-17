from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


JSONRPC_VERSION = "2.0"

# JSON-RPC 2.0 standard error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# MCP-specific error codes (-32000 to -32099 reserved for server errors)
MCP_TOOL_ERROR = -32000


@dataclass
class MCPError:
    code: int
    message: str
    data: Any = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            result["data"] = self.data
        return result

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> MCPError:
        return cls(
            code=int(raw["code"]),
            message=str(raw["message"]),
            data=raw.get("data"),
        )


@dataclass
class MCPRequest:
    method: str
    id: str | int | None = None
    params: dict[str, Any] | None = None
    jsonrpc: str = field(default=JSONRPC_VERSION)
    # Cached serialized form — avoids redundant JSON encode when forwarding to upstream.
    _json_bytes: bytes | None = field(default=None, repr=False, compare=False, init=False)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"jsonrpc": self.jsonrpc, "method": self.method}
        if self.id is not None:
            result["id"] = self.id
        if self.params is not None:
            result["params"] = self.params
        return result

    def to_json_bytes(self) -> bytes:
        """Return the JSON-encoded request bytes, using a cached copy when available."""
        if self._json_bytes is None:
            self._json_bytes = json.dumps(self.to_dict()).encode("utf-8")
        return self._json_bytes

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> MCPRequest:
        if raw.get("jsonrpc") != JSONRPC_VERSION:
            raise ValueError(f"Expected jsonrpc '2.0', got {raw.get('jsonrpc')!r}")
        if "method" not in raw:
            raise ValueError("Missing required field 'method'")
        params = raw.get("params")
        if params is not None and not isinstance(params, dict):
            raise ValueError("'params' must be an object (dict)")
        return cls(
            method=str(raw["method"]),
            id=raw.get("id"),
            params=params,
        )

    @classmethod
    def from_json(cls, data: str | bytes) -> MCPRequest:
        req = cls.from_dict(json.loads(data))
        if isinstance(data, bytes):
            req._json_bytes = data  # cache original bytes to skip re-serialization
        return req


@dataclass
class MCPResponse:
    id: str | int | None = None
    result: Any = None
    error: MCPError | None = None
    jsonrpc: str = field(default=JSONRPC_VERSION)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"jsonrpc": self.jsonrpc, "id": self.id}
        if self.error is not None:
            result["error"] = self.error.to_dict()
        else:
            result["result"] = self.result
        return result

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> MCPResponse:
        if raw.get("jsonrpc") != JSONRPC_VERSION:
            raise ValueError(f"Expected jsonrpc '2.0', got {raw.get('jsonrpc')!r}")
        error: MCPError | None = None
        if "error" in raw:
            error = MCPError.from_dict(raw["error"])
        return cls(
            id=raw.get("id"),
            result=raw.get("result"),
            error=error,
        )

    @classmethod
    def from_json(cls, data: str | bytes) -> MCPResponse:
        return cls.from_dict(json.loads(data))

    def is_error(self) -> bool:
        return self.error is not None


def fingerprint_mcp_request(request: MCPRequest) -> str:
    """
    Produce a stable SHA-256 fingerprint for an MCP request.

    For tools/call, the fingerprint is based on method + tool name + arguments,
    so identical tool invocations with different request IDs still collide.
    For all other methods, the fingerprint covers method + full params.
    """
    if request.method == "tools/call" and request.params:
        tool_name = request.params.get("name", "")
        arguments = request.params.get("arguments", {})
        key = json.dumps(
            {"method": request.method, "name": tool_name, "arguments": arguments},
            sort_keys=True,
        )
    else:
        key = json.dumps(
            {"method": request.method, "params": request.params},
            sort_keys=True,
        )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()
