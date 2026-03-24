from __future__ import annotations

import json

from agentbreak.behaviors import apply_response_behavior, empty, invalid_json, malformed_tool_calls, malformed_tool_use


def test_empty_returns_empty_bytes() -> None:
    assert empty(b'{"ok": true}') == b""


def test_invalid_json_returns_unparseable_bytes() -> None:
    payload = invalid_json(b'{"ok": true}')
    try:
        json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError:
        assert True
    else:
        raise AssertionError("invalid_json returned parseable JSON")


def test_malformed_tool_calls_corrupts_existing_field() -> None:
    body = json.dumps(
        {
            "id": "resp_1",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [{"id": "call_1", "type": "function"}],
                    }
                }
            ],
        }
    ).encode("utf-8")
    payload = json.loads(malformed_tool_calls(body).decode("utf-8"))
    assert payload["choices"][0]["message"]["tool_calls"] == "INVALID"


def test_malformed_tool_calls_injects_field_when_missing() -> None:
    body = json.dumps({"id": "resp_1"}).encode("utf-8")
    payload = json.loads(malformed_tool_calls(body).decode("utf-8"))
    assert payload["tool_calls"] == "INVALID"


def test_malformed_tool_calls_returns_invalid_json_on_parse_failure() -> None:
    assert malformed_tool_calls(b"{oops") == b"{not valid"


def test_malformed_tool_use_corrupts_content_blocks() -> None:
    body = json.dumps(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "Hello"}],
        }
    ).encode("utf-8")
    payload = json.loads(malformed_tool_use(body).decode("utf-8"))
    assert payload["content"][0]["type"] == "tool_use"
    assert payload["content"][0]["id"] == "INVALID"
    assert payload["content"][0]["input"] == "INVALID"


def test_malformed_tool_use_injects_invalid_when_no_content() -> None:
    body = json.dumps({"id": "msg_1", "type": "message"}).encode("utf-8")
    payload = json.loads(malformed_tool_use(body).decode("utf-8"))
    assert payload["content"] == "INVALID"


def test_malformed_tool_use_returns_invalid_json_on_parse_failure() -> None:
    assert malformed_tool_use(b"{oops") == b"{not valid"


def test_apply_response_behavior_malformed_tool_use_works() -> None:
    body = json.dumps(
        {"content": [{"type": "text", "text": "hi"}]}
    ).encode("utf-8")
    result = apply_response_behavior(body, "malformed_tool_use")
    payload = json.loads(result.decode("utf-8"))
    assert payload["content"][0]["type"] == "tool_use"


def test_apply_response_behavior_unknown_name_returns_original() -> None:
    original = b'{"ok": true}'
    assert apply_response_behavior(original, "nope") == original
