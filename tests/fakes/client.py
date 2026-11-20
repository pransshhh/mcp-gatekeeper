#!/usr/bin/env python3
"""Stands in for Claude Desktop so we can drive the proxy without it.

Spawns whatever command follows `--`, speaks the client half of MCP over that
process's stdio, and prints a timestamped transcript.

Straight at the fake server, to check the harness itself:

    uv run python tests/fakes/client.py -- uv run python tests/fakes/server.py

Or at the interceptor wrapping the fake server, which is the real use:

    uv run python tests/fakes/client.py -- \
        uv run python -m mcp_gatekeeper.interceptor -- uv run python tests/fakes/server.py

The scenario fires two calls at once on purpose. If the fast one only comes
back after the slow one has finished, something downstream is serialising.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Any

JSONObject = dict[str, Any]

PROTOCOL_VERSION = "2025-06-18"

_start = time.monotonic()


def log(message: str) -> None:
    print(f"[{time.monotonic() - _start:6.2f}s] {message}", flush=True)


class Client:
    """Minimal MCP client: writes requests, routes replies back to callers."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process: asyncio.subprocess.Process = process
        self._next_id: int = 0
        self._pending: dict[object, asyncio.Future[JSONObject]] = {}

    def _write(self, payload: JSONObject) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write((json.dumps(payload) + "\n").encode())

    async def request(self, method: str, params: JSONObject | None = None) -> JSONObject:
        self._next_id += 1
        request_id = self._next_id

        future: asyncio.Future[JSONObject] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future

        # Annotated, or the literal infers dict[str, str | int] and assigning
        # params below becomes a type error.
        payload: JSONObject = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)
        log(f"--> {method} (id={request_id})")

        return await future

    def notify(self, method: str, params: JSONObject | None = None) -> None:
        payload: JSONObject = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)
        log(f"--> {method} (notification)")

    async def read_loop(self) -> None:
        assert self.process.stdout is not None

        while True:
            line = await self.process.stdout.readline()
            if not line:
                log("server stream closed")
                break

            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue

            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                log(f"<-- (not JSON, ignoring) {text[:80]!r}")
                continue

            if isinstance(message, list):
                log(f"<-- batch of {len(message)}")
                continue

            if "method" in message:
                self._handle_server_request(message)
                continue

            request_id = message.get("id")
            future = self._pending.pop(request_id, None)
            if future is not None and not future.done():
                future.set_result(message)
            else:
                log(f"<-- reply for unknown id={request_id}")

        # Don't leave anyone awaiting a reply that can never arrive.
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError("server went away"))

    def _handle_server_request(self, message: JSONObject) -> None:
        """The server asking us for something, e.g. sampling/createMessage."""
        method = message.get("method")
        request_id = message.get("id")
        log(f"<== server request: {method} (id={request_id})")

        if request_id is None:
            return  # a notification, nothing to answer

        self._write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "role": "assistant",
                    "content": {"type": "text", "text": "(pretend model output)"},
                    "model": "fake-model",
                },
            }
        )


def summarise(reply: JSONObject) -> str:
    if "error" in reply:
        error = reply["error"]
        return f"ERROR {error.get('code')}: {error.get('message')}"

    result = reply.get("result") or {}
    blocks = result.get("content") or []
    text = blocks[0].get("text", "") if blocks and isinstance(blocks[0], dict) else ""

    if result.get("isError"):
        return f"BLOCKED/ERROR: {text}"
    return f"ok: {text}" if text else f"ok: {json.dumps(result)[:120]}"


async def scenario(client: Client) -> None:
    reply = await client.request(
        "initialize",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"sampling": {}},
            "clientInfo": {"name": "fake-client", "version": "0.1.0"},
        },
    )
    negotiated = (reply.get("result") or {}).get("protocolVersion")
    log(f"<-- initialize ok, negotiated protocolVersion={negotiated}")

    client.notify("notifications/initialized")

    reply = await client.request("tools/list")
    names = [tool["name"] for tool in (reply.get("result") or {}).get("tools", [])]
    log(f"<-- tools/list: {names}")

    log("")
    log("--- single safe call ---")
    reply = await client.request(
        "tools/call", {"name": "read_file", "arguments": {"path": "/home/me/notes.md"}}
    )
    log(f"<-- read_file: {summarise(reply)}")

    log("")
    log("--- single dangerous call ---")
    reply = await client.request(
        "tools/call", {"name": "delete_file", "arguments": {"path": "/home/me/important.docx"}}
    )
    log(f"<-- delete_file: {summarise(reply)}")

    log("")
    log("--- two at once (slow_task must not block send_email) ---")
    slow = asyncio.create_task(
        client.request("tools/call", {"name": "slow_task", "arguments": {"seconds": 3}})
    )
    fast = asyncio.create_task(
        client.request(
            "tools/call",
            {"name": "send_email", "arguments": {"to": "someone@example.com", "subject": "hi"}},
        )
    )

    log(f"<-- send_email: {summarise(await fast)}")
    log(f"<-- slow_task: {summarise(await slow)}")

    log("")
    log("--- ping (should still be responsive) ---")
    await client.request("ping")
    log("<-- ping ok")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command", nargs=argparse.REMAINDER, help="the server command, after a -- separator"
    )
    command = parser.parse_args().command

    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("usage: client.py -- <command to run>", file=sys.stderr)
        return 2

    log(f"spawning: {' '.join(command)}")
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        limit=8 * 1024 * 1024,
    )

    client = Client(process)
    reader = asyncio.create_task(client.read_loop())

    try:
        await asyncio.wait_for(scenario(client), timeout=120)
    except TimeoutError:
        log("scenario timed out")
        return 1
    except ConnectionError as exc:
        log(f"connection lost: {exc}")
        return 1
    finally:
        log("")
        log("closing stdin, waiting for the child to exit")
        if process.stdin is not None and not process.stdin.is_closing():
            process.stdin.close()
        _ = reader.cancel()
        try:
            _ = await asyncio.wait_for(process.wait(), timeout=5)
            log(f"child exited with code {process.returncode}")
        except TimeoutError:
            log("child did not exit, killing it")
            process.kill()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
