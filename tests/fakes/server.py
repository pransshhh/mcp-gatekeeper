#!/usr/bin/env python3
"""A stand-in MCP server for developing against without Claude Desktop.

Hand-rolled on purpose: no SDK, so there are no dependencies and we control
exactly what goes on the wire, including the malformed cases a real server
would never produce on demand.

Each request is handled on its own thread, so two overlapping calls really do
overlap. Without that you can't tell whether a stall is the interceptor's
fault or just this server answering one call at a time.

The tools are deliberately alarming so the demo has visible stakes. Nothing
here touches the disk or the network -- delete_file only claims it did, and
logs a marker to stderr. That marker is the proof that blocking works: block
the call and it must not appear.

    python tests/fakes/server.py [--banner] [--emit-sampling]
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from typing import Any

JSONObject = dict[str, Any]

PROTOCOL_VERSION = "2025-06-18"

_stdout_lock = threading.Lock()

TOOLS: list[JSONObject] = [
    {
        "name": "read_file",
        "description": "Read a file from disk.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "delete_file",
        "description": "Permanently delete a file.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "send_email",
        "description": "Send an email to an external address.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["to", "subject"],
        },
    },
    {
        "name": "slow_task",
        "description": "Sleep for a while. Useful for testing concurrency.",
        "inputSchema": {
            "type": "object",
            "properties": {"seconds": {"type": "number"}},
        },
    },
]


def log(message: str) -> None:
    """Always stderr. Anything on stdout has to be protocol data."""
    print(f"[fake-server] {message}", file=sys.stderr, flush=True)


def send(payload: JSONObject) -> None:
    line = json.dumps(payload, separators=(",", ":"))
    with _stdout_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def reply(request_id: object, result: JSONObject) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "result": result})


def reply_error(request_id: object, code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def text_result(text: str, is_error: bool = False) -> JSONObject:
    return {"isError": is_error, "content": [{"type": "text", "text": text}]}


def run_tool(name: str, arguments: JSONObject) -> JSONObject:
    if name == "read_file":
        return text_result(f"(pretend contents of {arguments.get('path', '?')})")

    if name == "delete_file":
        path = arguments.get("path", "?")
        log(f"!! delete_file actually ran for {path}")
        return text_result(f"Deleted {path}.")

    if name == "send_email":
        to = arguments.get("to", "?")
        log(f"!! send_email actually ran, to={to}")
        return text_result(f"Email sent to {to}.")

    if name == "slow_task":
        seconds = float(arguments.get("seconds", 2))
        time.sleep(seconds)
        return text_result(f"Finished after {seconds}s.")

    return text_result(f"No such tool: {name}", is_error=True)


def handle(message: JSONObject, emit_sampling: bool) -> None:
    method = message.get("method")
    request_id = message.get("id")

    if method == "initialize":
        reply(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": {"name": "fake-server", "version": "0.1.0"},
            },
        )
        if emit_sampling:
            # The server asking the client to run an inference for it. This is
            # the direction people forget to gate.
            send(
                {
                    "jsonrpc": "2.0",
                    "id": "srv-1",
                    "method": "sampling/createMessage",
                    "params": {
                        "messages": [
                            {"role": "user", "content": {"type": "text", "text": "summarise"}}
                        ],
                        "maxTokens": 100,
                    },
                }
            )
        return

    if method == "notifications/initialized":
        return  # notifications get no reply

    if method == "ping":
        reply(request_id, {})
        return

    if method == "tools/list":
        reply(request_id, {"tools": TOOLS})
        return

    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name", "")
        log(f"running {name}")
        reply(request_id, run_tool(name, params.get("arguments") or {}))
        return

    if request_id is not None:
        reply_error(request_id, -32601, f"Method not found: {method}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--emit-sampling",
        action="store_true",
        help="send an unsolicited sampling/createMessage after initialize",
    )
    parser.add_argument(
        "--banner",
        action="store_true",
        help="print a non-JSON line to stdout at startup, like some real servers do",
    )
    args = parser.parse_args()

    if args.banner:
        sys.stdout.write("fake-server starting up...\n")
        sys.stdout.flush()

    log("ready")

    for raw in sys.stdin:
        text = raw.strip()
        if not text:
            continue

        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            log(f"ignoring non-JSON line: {text[:80]!r}")
            continue

        if isinstance(message, list):
            for item in message:
                threading.Thread(
                    target=handle, args=(item, args.emit_sampling), daemon=True
                ).start()
            continue

        if "method" not in message:
            # A reply to one of our own server -> client requests.
            log(f"got a response to our request id={message.get('id')}")
            continue

        threading.Thread(target=handle, args=(message, args.emit_sampling), daemon=True).start()

    log("stdin closed, exiting")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
