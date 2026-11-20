"""JSON-RPC parsing and the gating decision for MCP traffic.

Pure functions only. Nothing here opens a socket or a file: the stdio
interceptor and the HTTP gateway both hand bytes to this module, which is what
keeps one gating decision shared between the two transports.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

JSONObject = dict[str, object]
"""A decoded JSON-RPC object.

Values are `object`, not `Any`. This is other people's JSON, so nothing about
it is known until it has been checked — `object` makes the type checker insist
on that, where `Any` would quietly wave it through.
"""

RequestId = str | int | float | None
"""JSON-RPC allows a string, a number or null for `id`."""

BLOCK_TEXT = (
    "Blocked by MCP Gatekeeper: the user denied this call. Do not retry; ask the user first."
)

# JSON-RPC reserves -32000..-32099 for implementation-defined server errors.
JSONRPC_BLOCKED = -32001


class Direction(StrEnum):
    """Which way a message is travelling."""

    CLIENT_TO_SERVER = "c2s"
    SERVER_TO_CLIENT = "s2c"


class Kind(StrEnum):
    """What sort of JSON-RPC object this is."""

    REQUEST = "request"
    NOTIFICATION = "notification"
    RESPONSE = "response"
    ERROR = "error"


class MalformedLine(Exception):
    """Line was not valid JSON-RPC.

    Callers log this and carry on. Servers do print startup banners to stdout,
    and one junk line must not end the session.
    """


# Requests we hold and ask the user about.
GATED_CLIENT_TO_SERVER = frozenset({"tools/call", "resources/read"})
GATED_SERVER_TO_CLIENT = frozenset({"sampling/createMessage", "elicitation/create"})

# Low risk, so off unless switched on in config.
OPTIONAL_GATES = frozenset({"prompts/get"})

# Gating any of these either breaks startup or buries the user in popups for
# things that have no side effects.
NEVER_GATED = frozenset(
    {
        "initialize",
        "ping",
        "tools/list",
        "resources/list",
        "resources/templates/list",
        "resources/subscribe",
        "resources/unsubscribe",
        "prompts/list",
        "roots/list",
        "completion/complete",
        "logging/setLevel",
    }
)


@dataclass(frozen=True)
class Message:
    """One JSON-RPC object, already classified."""

    payload: JSONObject
    kind: Kind
    method: str | None
    id: RequestId

    @property
    def is_request(self) -> bool:
        return self.kind is Kind.REQUEST


@dataclass(frozen=True)
class Line:
    """One newline-delimited frame off the wire.

    `raw` is kept so an allowed request can be forwarded byte for byte rather
    than re-serialised. What the user approved is then exactly what runs.

    `messages` holds more than one entry only for a batch array, which the
    2025-03-26 revision allowed and 2025-06-18 removed again.
    """

    raw: bytes
    messages: tuple[Message, ...]
    is_batch: bool

    @property
    def single(self) -> Message:
        if len(self.messages) != 1:
            raise ValueError("line holds a batch, not a single message")
        return self.messages[0]


def classify(payload: object) -> Message:
    """Work out what kind of JSON-RPC object this is."""
    if not isinstance(payload, dict):
        raise MalformedLine(f"expected an object, got {type(payload).__name__}")

    obj = cast(JSONObject, payload)
    raw_method = obj.get("method")
    has_id = obj.get("id") is not None

    method: str | None = None
    if raw_method is not None:
        if not isinstance(raw_method, str):
            raise MalformedLine("method must be a string")
        method = raw_method
        kind = Kind.REQUEST if has_id else Kind.NOTIFICATION
    elif "result" in obj:
        kind = Kind.RESPONSE
    elif "error" in obj:
        kind = Kind.ERROR
    else:
        raise MalformedLine("no method, result or error field")

    request_id = cast(RequestId, obj.get("id"))
    return Message(payload=obj, kind=kind, method=method, id=request_id)


def parse_line(raw: bytes) -> Line:
    """Parse one frame, or raise MalformedLine if it isn't one."""
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        raise MalformedLine("empty line")

    try:
        payload = cast(object, json.loads(text))
    except json.JSONDecodeError as exc:
        raise MalformedLine(f"not JSON: {exc}") from exc

    if isinstance(payload, list):
        items = cast(list[object], payload)
        if not items:
            raise MalformedLine("empty batch array")
        return Line(raw=raw, messages=tuple(classify(item) for item in items), is_batch=True)

    return Line(raw=raw, messages=(classify(payload),), is_batch=False)


def should_gate(
    message: Message,
    direction: Direction,
    *,
    optional: Iterable[str] = (),
) -> bool:
    """True if this message needs a user decision before it goes anywhere."""
    if not message.is_request:
        return False

    method = message.method
    if method in NEVER_GATED:
        return False
    if method in set(optional):
        return True

    if direction is Direction.CLIENT_TO_SERVER:
        return method in GATED_CLIENT_TO_SERVER
    return method in GATED_SERVER_TO_CLIENT


def describe(message: Message) -> tuple[str, JSONObject]:
    """Best-effort (label, details) for the popup.

    Deliberately forgiving. A server can put anything in `params`, and the
    popup still has to render something rather than throw.
    """
    raw_params = message.payload.get("params")
    params: JSONObject = cast(JSONObject, raw_params) if isinstance(raw_params, dict) else {}

    if message.method == "tools/call":
        name = params.get("name")
        label = name if isinstance(name, str) else "(unnamed tool)"
        arguments = params.get("arguments")
        if isinstance(arguments, dict):
            return label, cast(JSONObject, arguments)
        return label, {"arguments": arguments}

    if message.method == "resources/read":
        return "resources/read", {"uri": params.get("uri")}

    return message.method or "(no method)", params


def extract_protocol_version(message: Message) -> str | None:
    """Pull protocolVersion from an initialize request or its response.

    The response is the authoritative one: that is the version both sides
    actually settled on.
    """
    if message.kind is Kind.RESPONSE:
        result = message.payload.get("result")
        if isinstance(result, dict):
            version = cast(JSONObject, result).get("protocolVersion")
            return version if isinstance(version, str) else None

    if message.method == "initialize":
        params = message.payload.get("params")
        if isinstance(params, dict):
            version = cast(JSONObject, params).get("protocolVersion")
            return version if isinstance(version, str) else None

    return None


def build_denial(
    request_id: RequestId, *, mode: str = "tool_error", text: str = BLOCK_TEXT
) -> JSONObject:
    """The reply sent back in place of a blocked request.

    `tool_error` is the default. The spec puts tool execution failures in the
    result with isError and keeps JSON-RPC errors for protocol faults, and a
    user saying no is the former. It also reads better downstream: the model
    sees the text and can explain itself instead of the server looking broken.

    `jsonrpc_error` is there for anyone who would rather have a hard error,
    but some clients treat that as a transport failure and retry.
    """
    if mode == "jsonrpc_error":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": JSONRPC_BLOCKED, "message": text},
        }

    if mode != "tool_error":
        raise ValueError(f"unknown denial mode: {mode}")

    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"isError": True, "content": [{"type": "text", "text": text}]},
    }


def encode(payload: object) -> bytes:
    """Serialise one message back to a frame.

    json.dumps escapes newlines inside strings, so a frame can never contain a
    stray newline and desync whoever is reading.
    """
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
