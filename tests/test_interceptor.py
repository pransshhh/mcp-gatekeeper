"""Tests for the stdio interceptor.

Split in two: fast unit tests for the frame reader and the inspector, then a
handful that actually spawn the interceptor wrapped around the fake server.
The subprocess ones are slower but they are the only way to prove the things
that matter in practice -- that stdout carries nothing but protocol data, and
that the process exits cleanly instead of leaving a server behind.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp_gatekeeper import interceptor, protocol
from mcp_gatekeeper.protocol import Direction

FAKE_SERVER = str(Path(__file__).parent / "fakes" / "server.py")

INITIALIZE: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0.1.0"},
    },
}


# --------------------------------------------------------------------------
# iter_lines
# --------------------------------------------------------------------------


async def _reader_for(data: bytes, limit: int) -> asyncio.StreamReader:
    reader = asyncio.StreamReader(limit=limit)
    reader.feed_data(data)
    reader.feed_eof()
    return reader


async def test_iter_lines_yields_frames():
    reader = await _reader_for(b'{"a":1}\n{"b":2}\n', limit=64 * 1024)
    lines = [line async for line in interceptor.iter_lines(reader)]
    assert lines == [b'{"a":1}\n', b'{"b":2}\n']


async def test_iter_lines_keeps_a_trailing_partial():
    # A peer that dies mid-write still sent us something the other side would
    # consider a frame.
    reader = await _reader_for(b'{"a":1}\n{"b":2}', limit=64 * 1024)
    lines = [line async for line in interceptor.iter_lines(reader)]
    assert lines[-1] == b'{"b":2}'


async def test_iter_lines_skips_blank_lines():
    reader = await _reader_for(b'\n\n{"a":1}\n\n', limit=64 * 1024)
    lines = [line async for line in interceptor.iter_lines(reader)]
    assert lines == [b'{"a":1}\n']


async def test_iter_lines_skips_oversized_and_recovers():
    # The regression this guards: an earlier version drained a fixed chunk on
    # overflow, which swallowed the *next* frame along with the bad one.
    dropped: list[str] = []
    reader = await _reader_for(b"x" * 5000 + b"\n" + b'{"ok":1}\n', limit=1024)

    lines = [line async for line in interceptor.iter_lines(reader, on_drop=dropped.append)]

    assert lines == [b'{"ok":1}\n']
    assert len(dropped) == 1


async def test_iter_lines_survives_oversized_line_at_eof():
    dropped: list[str] = []
    reader = await _reader_for(b"x" * 5000, limit=1024)

    lines = [line async for line in interceptor.iter_lines(reader, on_drop=dropped.append)]

    assert lines == []


# --------------------------------------------------------------------------
# Session.inspect
# --------------------------------------------------------------------------


def test_inspect_captures_negotiated_version():
    session = interceptor.Session("demo")
    session.inspect(
        protocol.encode({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-06-18"}}),
        Direction.SERVER_TO_CLIENT,
    )
    assert session.protocol_version == "2025-06-18"


def test_inspect_keeps_the_first_version_it_sees():
    session = interceptor.Session("demo")
    for version in ("2025-06-18", "2025-03-26"):
        session.inspect(
            protocol.encode({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": version}}),
            Direction.SERVER_TO_CLIENT,
        )
    assert session.protocol_version == "2025-06-18"


def test_inspect_tolerates_junk():
    # Servers print banners. Inspecting one must not raise, or the pump dies.
    session = interceptor.Session("demo")
    session.inspect(b"fake-server starting up...\n", Direction.SERVER_TO_CLIENT)
    assert session.protocol_version is None


def test_inspect_never_raises_on_a_batch():
    session = interceptor.Session("demo")
    session.inspect(
        protocol.encode(
            [
                {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "x"}},
            ]
        ),
        Direction.CLIENT_TO_SERVER,
    )


# --------------------------------------------------------------------------
# the real thing, in a subprocess
# --------------------------------------------------------------------------


@asynccontextmanager
async def running(*server_args: str) -> AsyncGenerator[asyncio.subprocess.Process]:
    """Spawn the interceptor wrapped around the fake server."""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "mcp_gatekeeper.interceptor",
        "--server-name",
        "test",
        "--",
        sys.executable,
        FAKE_SERVER,
        *server_args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        yield process
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def _send(process: asyncio.subprocess.Process, payload: Any) -> None:
    assert process.stdin is not None
    process.stdin.write(protocol.encode(payload))
    await process.stdin.drain()


async def _read_frame(process: asyncio.subprocess.Process) -> bytes:
    assert process.stdout is not None
    return await asyncio.wait_for(process.stdout.readline(), timeout=15)


async def _read_until_eof(process: asyncio.subprocess.Process) -> list[bytes]:
    assert process.stdout is not None
    lines: list[bytes] = []
    while True:
        line = await asyncio.wait_for(process.stdout.readline(), timeout=15)
        if not line:
            return lines
        lines.append(line)


async def test_forwards_a_request_and_returns_the_reply():
    async with running() as process:
        await _send(process, INITIALIZE)
        message = protocol.parse_line(await _read_frame(process)).single

        assert message.kind is protocol.Kind.RESPONSE
        assert message.id == 1


async def test_stdout_carries_only_protocol_frames():
    # The test that catches a stray print before Claude Desktop does.
    async with running() as process:
        await _send(process, INITIALIZE)
        await _send(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        await _send(
            process,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "read_file", "arguments": {"path": "/tmp/x"}},
            },
        )

        assert process.stdin is not None
        process.stdin.close()

        lines = await _read_until_eof(process)

    assert len(lines) >= 3
    for line in lines:
        protocol.parse_line(line)  # raises MalformedLine if anything else got in


async def test_exits_cleanly_when_the_client_goes_away():
    async with running() as process:
        await _send(process, INITIALIZE)
        await _read_frame(process)

        assert process.stdin is not None
        process.stdin.close()

        code = await asyncio.wait_for(process.wait(), timeout=15)

    assert code == 0


async def test_reaps_the_server_on_shutdown():
    async with running() as process:
        await _send(process, INITIALIZE)
        await _read_frame(process)

        assert process.stdin is not None
        process.stdin.close()
        await asyncio.wait_for(process.wait(), timeout=15)

        assert process.stderr is not None
        stderr = (await process.stderr.read()).decode()

    # Proves we waited for the child rather than orphaning it.
    assert "server exited with code" in stderr


async def test_passes_through_output_it_cannot_parse():
    # A banner is forwarded, not swallowed: a transparent proxy does not
    # quietly edit the stream.
    async with running("--banner") as process:
        await _send(process, INITIALIZE)

        assert process.stdin is not None
        lines: list[bytes] = [await _read_frame(process), await _read_frame(process)]
        process.stdin.close()

    assert any(b"starting up" in line for line in lines)


async def test_gates_nothing_yet():
    # Pass-through stage: a dangerous call still reaches the server. When
    # gating lands this test flips, and that flip is the feature.
    async with running() as process:
        await _send(process, INITIALIZE)
        await _read_frame(process)
        await _send(
            process,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "delete_file", "arguments": {"path": "/tmp/x"}},
            },
        )
        message = protocol.parse_line(await _read_frame(process)).single

    result = message.payload.get("result")
    assert isinstance(result, dict)
    assert result.get("isError") is False
