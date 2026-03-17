from __future__ import annotations

import json

import pytest

from agentbreak.mcp_protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    MCP_TOOL_ERROR,
    PARSE_ERROR,
    MCPError,
    MCPRequest,
    MCPResponse,
    fingerprint_mcp_request,
)


# ---------------------------------------------------------------------------
# MCPError
# ---------------------------------------------------------------------------

def test_mcp_error_to_dict_without_data() -> None:
    err = MCPError(code=-32600, message="Invalid Request")
    d = err.to_dict()
    assert d == {"code": -32600, "message": "Invalid Request"}
    assert "data" not in d


def test_mcp_error_to_dict_with_data() -> None:
    err = MCPError(code=-32000, message="Tool error", data={"detail": "boom"})
    d = err.to_dict()
    assert d["data"] == {"detail": "boom"}


def test_mcp_error_from_dict() -> None:
    err = MCPError.from_dict({"code": -32700, "message": "Parse error"})
    assert err.code == PARSE_ERROR
    assert err.message == "Parse error"
    assert err.data is None


def test_mcp_error_from_dict_with_data() -> None:
    err = MCPError.from_dict({"code": -32000, "message": "err", "data": [1, 2]})
    assert err.data == [1, 2]


def test_mcp_error_constants() -> None:
    assert PARSE_ERROR == -32700
    assert INVALID_REQUEST == -32600
    assert METHOD_NOT_FOUND == -32601
    assert INVALID_PARAMS == -32602
    assert INTERNAL_ERROR == -32603
    assert MCP_TOOL_ERROR == -32000


# ---------------------------------------------------------------------------
# MCPRequest
# ---------------------------------------------------------------------------

def test_mcp_request_from_dict_minimal() -> None:
    req = MCPRequest.from_dict({"jsonrpc": "2.0", "method": "tools/list"})
    assert req.method == "tools/list"
    assert req.id is None
    assert req.params is None
    assert req.jsonrpc == "2.0"


def test_mcp_request_from_dict_full() -> None:
    raw = {
        "jsonrpc": "2.0",
        "id": 42,
        "method": "tools/call",
        "params": {"name": "my_tool", "arguments": {"x": 1}},
    }
    req = MCPRequest.from_dict(raw)
    assert req.id == 42
    assert req.params == {"name": "my_tool", "arguments": {"x": 1}}


def test_mcp_request_from_dict_wrong_version() -> None:
    with pytest.raises(ValueError, match="jsonrpc"):
        MCPRequest.from_dict({"jsonrpc": "1.0", "method": "initialize"})


def test_mcp_request_from_dict_missing_method() -> None:
    with pytest.raises(ValueError, match="method"):
        MCPRequest.from_dict({"jsonrpc": "2.0"})


def test_mcp_request_from_dict_params_not_dict() -> None:
    with pytest.raises(ValueError, match="params"):
        MCPRequest.from_dict({"jsonrpc": "2.0", "method": "tools/list", "params": [1, 2]})


def test_mcp_request_from_json() -> None:
    data = json.dumps({"jsonrpc": "2.0", "id": "abc", "method": "resources/list"})
    req = MCPRequest.from_json(data)
    assert req.id == "abc"
    assert req.method == "resources/list"


def test_mcp_request_to_dict_omits_none_id() -> None:
    req = MCPRequest(method="initialize")
    d = req.to_dict()
    assert "id" not in d
    assert d["method"] == "initialize"
    assert d["jsonrpc"] == "2.0"


def test_mcp_request_to_dict_omits_none_params() -> None:
    req = MCPRequest(method="tools/list", id=1)
    d = req.to_dict()
    assert "params" not in d


def test_mcp_request_to_dict_includes_params_when_present() -> None:
    req = MCPRequest(method="tools/call", id=1, params={"name": "t", "arguments": {}})
    d = req.to_dict()
    assert d["params"] == {"name": "t", "arguments": {}}


# ---------------------------------------------------------------------------
# MCPResponse
# ---------------------------------------------------------------------------

def test_mcp_response_success_to_dict() -> None:
    resp = MCPResponse(id=1, result={"tools": []})
    d = resp.to_dict()
    assert d["jsonrpc"] == "2.0"
    assert d["id"] == 1
    assert d["result"] == {"tools": []}
    assert "error" not in d


def test_mcp_response_error_to_dict() -> None:
    resp = MCPResponse(id=2, error=MCPError(code=-32601, message="not found"))
    d = resp.to_dict()
    assert "result" not in d
    assert d["error"]["code"] == -32601


def test_mcp_response_from_dict_success() -> None:
    raw = {"jsonrpc": "2.0", "id": 5, "result": {"content": "hi"}}
    resp = MCPResponse.from_dict(raw)
    assert resp.id == 5
    assert resp.result == {"content": "hi"}
    assert resp.error is None
    assert not resp.is_error()


def test_mcp_response_from_dict_error() -> None:
    raw = {
        "jsonrpc": "2.0",
        "id": 3,
        "error": {"code": -32000, "message": "tool failed"},
    }
    resp = MCPResponse.from_dict(raw)
    assert resp.is_error()
    assert resp.error is not None
    assert resp.error.code == -32000


def test_mcp_response_from_dict_wrong_version() -> None:
    with pytest.raises(ValueError, match="jsonrpc"):
        MCPResponse.from_dict({"jsonrpc": "1.0", "id": 1, "result": {}})


def test_mcp_response_from_json() -> None:
    data = json.dumps({"jsonrpc": "2.0", "id": "x", "result": None})
    resp = MCPResponse.from_json(data)
    assert resp.id == "x"
    assert not resp.is_error()


# ---------------------------------------------------------------------------
# fingerprint_mcp_request
# ---------------------------------------------------------------------------

def test_fingerprint_tools_call_is_stable() -> None:
    req = MCPRequest(method="tools/call", id=1, params={"name": "my_tool", "arguments": {"a": 1}})
    fp1 = fingerprint_mcp_request(req)
    fp2 = fingerprint_mcp_request(req)
    assert fp1 == fp2
    assert len(fp1) == 64  # SHA-256 hex digest


def test_fingerprint_tools_call_ignores_request_id() -> None:
    req1 = MCPRequest(method="tools/call", id=1, params={"name": "t", "arguments": {"x": 1}})
    req2 = MCPRequest(method="tools/call", id=99, params={"name": "t", "arguments": {"x": 1}})
    assert fingerprint_mcp_request(req1) == fingerprint_mcp_request(req2)


def test_fingerprint_tools_call_differs_by_tool_name() -> None:
    req1 = MCPRequest(method="tools/call", id=1, params={"name": "tool_a", "arguments": {}})
    req2 = MCPRequest(method="tools/call", id=1, params={"name": "tool_b", "arguments": {}})
    assert fingerprint_mcp_request(req1) != fingerprint_mcp_request(req2)


def test_fingerprint_tools_call_differs_by_arguments() -> None:
    req1 = MCPRequest(method="tools/call", id=1, params={"name": "t", "arguments": {"x": 1}})
    req2 = MCPRequest(method="tools/call", id=1, params={"name": "t", "arguments": {"x": 2}})
    assert fingerprint_mcp_request(req1) != fingerprint_mcp_request(req2)


def test_fingerprint_tools_call_argument_order_invariant() -> None:
    req1 = MCPRequest(method="tools/call", id=1, params={"name": "t", "arguments": {"a": 1, "b": 2}})
    req2 = MCPRequest(method="tools/call", id=1, params={"name": "t", "arguments": {"b": 2, "a": 1}})
    assert fingerprint_mcp_request(req1) == fingerprint_mcp_request(req2)


def test_fingerprint_non_tool_call_uses_full_params() -> None:
    req1 = MCPRequest(method="resources/read", id=1, params={"uri": "file:///foo"})
    req2 = MCPRequest(method="resources/read", id=2, params={"uri": "file:///foo"})
    # id is ignored (not included in fingerprint)
    assert fingerprint_mcp_request(req1) == fingerprint_mcp_request(req2)


def test_fingerprint_differs_by_method() -> None:
    req1 = MCPRequest(method="tools/list", id=1)
    req2 = MCPRequest(method="resources/list", id=1)
    assert fingerprint_mcp_request(req1) != fingerprint_mcp_request(req2)


def test_fingerprint_no_params() -> None:
    req = MCPRequest(method="tools/list")
    fp = fingerprint_mcp_request(req)
    assert isinstance(fp, str)
    assert len(fp) == 64


def test_fingerprint_tools_call_no_params_does_not_crash() -> None:
    req = MCPRequest(method="tools/call", id=1, params=None)
    fp = fingerprint_mcp_request(req)
    assert isinstance(fp, str)
