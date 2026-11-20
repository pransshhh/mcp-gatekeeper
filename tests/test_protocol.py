"""Tests for the wire format and the gating decision."""

from __future__ import annotations

import json
from typing import Any

import pytest

from mcp_gatekeeper import protocol as p


def test_classify_request():
    message = p.classify({"jsonrpc": "2.0", "id": 1, "method": "tools/call"})
    assert message.kind is p.Kind.REQUEST
    assert message.is_request
    assert message.method == "tools/call"
    assert message.id == 1


def test_classify_request_with_id_zero():
    # id 0 is valid JSON-RPC and falsy in Python. Treating it as a
    # notification would leave the caller waiting for a reply forever.
    message = p.classify({"jsonrpc": "2.0", "id": 0, "method": "ping"})
    assert message.kind is p.Kind.REQUEST


def test_classify_notification():
    message = p.classify({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert message.kind is p.Kind.NOTIFICATION
    assert not message.is_request


def test_classify_response_and_error():
    assert p.classify({"jsonrpc": "2.0", "id": 1, "result": {}}).kind is p.Kind.RESPONSE
    assert p.classify({"jsonrpc": "2.0", "id": 1, "error": {}}).kind is p.Kind.ERROR


@pytest.mark.parametrize(
    "payload",
    [
        {"jsonrpc": "2.0", "id": 1},  # nothing useful in it
        {"jsonrpc": "2.0", "method": 42},  # method must be a string
        ["not", "an", "object"],
        "a bare string",
    ],
)
def test_classify_rejects_junk(payload):
    with pytest.raises(p.MalformedLine):
        p.classify(payload)


def test_parse_single_line():
    raw = b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
    line = p.parse_line(raw)
    assert not line.is_batch
    assert line.raw == raw
    assert line.single.method == "ping"


def test_parse_batch():
    raw = (
        json.dumps(
            [
                {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            ]
        ).encode()
        + b"\n"
    )

    line = p.parse_line(raw)
    assert line.is_batch
    assert len(line.messages) == 2
    with pytest.raises(ValueError):
        _ = line.single


@pytest.mark.parametrize(
    "raw",
    [
        b"fake-server starting up...\n",  # banner text, happens in real life
        b"\n",
        b"[]\n",
        b"{not json}\n",
    ],
)
def test_parse_rejects_junk_lines(raw):
    with pytest.raises(p.MalformedLine):
        p.parse_line(raw)


def gated(method, direction=p.Direction.CLIENT_TO_SERVER, **kwargs):
    message = p.classify({"jsonrpc": "2.0", "id": 1, "method": method})
    return p.should_gate(message, direction, **kwargs)


def test_gates_dangerous_client_requests():
    assert gated("tools/call")
    assert gated("resources/read")


@pytest.mark.parametrize(
    "method",
    ["initialize", "ping", "tools/list", "resources/list", "prompts/list", "roots/list"],
)
def test_never_gates_the_handshake_or_discovery(method):
    assert not gated(method)


def test_gates_server_initiated_requests():
    # The direction people forget. A server asking the client to run an
    # inference is at least as dangerous as a tool call.
    assert gated("sampling/createMessage", p.Direction.SERVER_TO_CLIENT)
    assert gated("elicitation/create", p.Direction.SERVER_TO_CLIENT)


def test_direction_matters():
    assert not gated("sampling/createMessage", p.Direction.CLIENT_TO_SERVER)
    assert not gated("tools/call", p.Direction.SERVER_TO_CLIENT)


def test_optional_gates_are_off_by_default():
    assert not gated("prompts/get")
    assert gated("prompts/get", optional={"prompts/get"})


def test_optional_cannot_override_never_gated():
    # A config typo must not be able to gate the handshake.
    assert not gated("initialize", optional={"initialize"})


def test_never_gates_responses_or_notifications():
    response = p.classify({"jsonrpc": "2.0", "id": 1, "result": {}})
    notification = p.classify({"jsonrpc": "2.0", "method": "notifications/cancelled"})
    assert not p.should_gate(response, p.Direction.SERVER_TO_CLIENT)
    assert not p.should_gate(notification, p.Direction.CLIENT_TO_SERVER)


def test_describe_tool_call():
    message = p.classify(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "delete_file", "arguments": {"path": "/tmp/x"}},
        }
    )
    label, details = p.describe(message)
    assert label == "delete_file"
    assert details == {"path": "/tmp/x"}


def test_describe_survives_rubbish_params():
    # The popup must render something no matter what the server sends.
    message = p.classify({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": "nope"})
    label, details = p.describe(message)
    assert label == "(unnamed tool)"
    assert isinstance(details, dict)


def test_describe_resource_read():
    message = p.classify(
        {"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": "file:///x"}}
    )
    label, details = p.describe(message)
    assert label == "resources/read"
    assert details == {"uri": "file:///x"}


def test_denial_defaults_to_tool_error():
    denial: Any = p.build_denial(7)
    assert denial["id"] == 7
    assert denial["result"]["isError"] is True
    assert "Blocked by MCP Gatekeeper" in denial["result"]["content"][0]["text"]
    assert "error" not in denial


def test_denial_as_jsonrpc_error():
    denial: Any = p.build_denial(7, mode="jsonrpc_error")
    assert denial["error"]["code"] == p.JSONRPC_BLOCKED
    assert "result" not in denial


def test_denial_rejects_unknown_mode():
    with pytest.raises(ValueError):
        p.build_denial(1, mode="shrug")


def test_denial_keeps_string_ids():
    assert p.build_denial("srv-1")["id"] == "srv-1"


def test_extract_protocol_version():
    request = p.classify(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26"},
        }
    )
    response = p.classify({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-06-18"}})

    assert p.extract_protocol_version(request) == "2025-03-26"
    assert p.extract_protocol_version(response) == "2025-06-18"
    assert (
        p.extract_protocol_version(p.classify({"jsonrpc": "2.0", "id": 2, "method": "ping"}))
        is None
    )
