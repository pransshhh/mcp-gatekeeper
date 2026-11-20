"""Tests for the stdio interceptor.

Fast unit tests for the session state, then tests that actually spawn a daemon
and an interceptor-wrapped fake server. The subprocess ones are slower but they
are the only way to prove what matters: that a blocked call never reaches the
server at all.

The fake server logs a `!!` marker to stderr when a dangerous tool really runs,
and the interceptor does not pipe its child's stderr, so both end up in the one
stream these tests read. Absence of that marker is the proof.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp_gatekeeper import framing, interceptor, protocol
from mcp_gatekeeper.daemon.__main__ import make_auto_decider
from mcp_gatekeeper.daemon.decisions import DecisionQueue
from mcp_gatekeeper.daemon.server import Daemon
from mcp_gatekeeper.ipc import Action

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


def call(request_id: int, tool: str, **arguments: object) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }


# --------------------------------------------------------------------------
# session state
# --------------------------------------------------------------------------


def note(session: interceptor.Session, payload: object) -> None:
    session.note(protocol.parse_line(framing.encode(payload)))


def test_note_captures_the_negotiated_version():
    session = interceptor.Session("demo")
    note(
        session,
        {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-06-18"}},
    )
    assert session.protocol_version == "2025-06-18"


def test_note_keeps_the_first_version_it_sees():
    session = interceptor.Session("demo")
    for version in ("2025-06-18", "2025-03-26"):
        note(
            session,
            {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": version}},
        )
    assert session.protocol_version == "2025-06-18"


def test_note_survives_a_batch():
    session = interceptor.Session("demo")
    note(
        session,
        [
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "x"}},
        ],
    )


def test_finds_the_id_a_cancellation_refers_to():
    session = interceptor.Session("demo")
    line = protocol.parse_line(
        framing.encode(
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": 7, "reason": "user stopped"},
            }
        )
    )
    assert session.cancelled_ids(line) == [7]


def test_ignores_a_cancellation_without_a_usable_id():
    session = interceptor.Session("demo")
    for params in ({}, {"requestId": None}, {"requestId": {"nested": 1}}):
        line = protocol.parse_line(
            framing.encode(
                {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": params}
            )
        )
        assert session.cancelled_ids(line) == []


# --------------------------------------------------------------------------
# the real thing
# --------------------------------------------------------------------------


@asynccontextmanager
async def running(
    *,
    socket: Path | None = None,
    server_args: Sequence[str] = (),
) -> AsyncGenerator[asyncio.subprocess.Process]:
    """Spawn the interceptor wrapped around the fake server."""
    argv = [
        sys.executable,
        "-m",
        "mcp_gatekeeper.interceptor",
        "--server-name",
        "test",
    ]
    if socket is not None:
        argv += ["--socket", str(socket)]
    argv += ["--", sys.executable, FAKE_SERVER, *server_args]

    process = await asyncio.create_subprocess_exec(
        *argv,
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


@asynccontextmanager
async def gatekeeper(
    tmp_path: Path, action: Action = Action.ALLOW, delay: float = 0.0
) -> AsyncGenerator[tuple[asyncio.subprocess.Process, Daemon]]:
    """A daemon plus an interceptor pointed at it."""
    socket = tmp_path / "d.sock"
    daemon = Daemon(DecisionQueue(make_auto_decider(action, delay)), socket)
    await daemon.start()
    serving = asyncio.create_task(daemon.serve())
    try:
        async with running(socket=socket) as process:
            yield process, daemon
    finally:
        await daemon.stop()
        serving.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await serving


async def send(process: asyncio.subprocess.Process, payload: object) -> None:
    assert process.stdin is not None
    process.stdin.write(framing.encode(payload))
    await process.stdin.drain()


async def frame(process: asyncio.subprocess.Process) -> protocol.Message:
    assert process.stdout is not None
    raw = await asyncio.wait_for(process.stdout.readline(), timeout=20)
    return protocol.parse_line(raw).single


async def finish(process: asyncio.subprocess.Process) -> str:
    """Close stdin, wait for exit, and return everything written to stderr."""
    assert process.stdin is not None
    assert process.stderr is not None
    if not process.stdin.is_closing():
        process.stdin.close()
    stderr = (await asyncio.wait_for(process.stderr.read(), timeout=20)).decode()
    await asyncio.wait_for(process.wait(), timeout=20)
    return stderr


def result_of(message: protocol.Message) -> dict[str, object]:
    result = message.payload.get("result")
    assert isinstance(result, dict), message.payload
    return result


def text_of(message: protocol.Message) -> str:
    content = result_of(message).get("content")
    assert isinstance(content, list)
    first = content[0]
    assert isinstance(first, dict)
    return str(first.get("text", ""))


async def handshake(process: asyncio.subprocess.Process) -> None:
    await send(process, INITIALIZE)
    await frame(process)


# -- the pair that matters -------------------------------------------------


async def test_an_allowed_call_reaches_the_server(tmp_path: Path):
    async with gatekeeper(tmp_path, Action.ALLOW) as (process, _daemon):
        await handshake(process)
        await send(process, call(2, "delete_file", path="/home/me/important.docx"))
        reply = await frame(process)
        stderr = await finish(process)

    assert result_of(reply).get("isError") is False
    assert "Deleted" in text_of(reply)
    # The server really ran it.
    assert "!! delete_file actually ran" in stderr


async def test_a_blocked_call_never_reaches_the_server(tmp_path: Path):
    async with gatekeeper(tmp_path, Action.BLOCK) as (process, _daemon):
        await handshake(process)
        await send(process, call(2, "delete_file", path="/home/me/important.docx"))
        reply = await frame(process)
        stderr = await finish(process)

    assert reply.id == 2
    assert result_of(reply).get("isError") is True
    assert "Blocked by MCP Gatekeeper" in text_of(reply)
    # The whole point: blocked means not executed, not executed-and-hidden.
    assert "!! delete_file actually ran" not in stderr


# -- everything around it --------------------------------------------------


async def test_discovery_is_never_gated(tmp_path: Path):
    # Gating tools/list would break startup or bury the user in prompts.
    async with gatekeeper(tmp_path, Action.BLOCK) as (process, _daemon):
        await handshake(process)
        await send(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        reply = await frame(process)
        await finish(process)

    tools = result_of(reply).get("tools")
    assert isinstance(tools, list)
    assert len(tools) == 4


async def test_ungated_traffic_flows_while_a_decision_is_pending(tmp_path: Path):
    # If a held request stalls the pump, pings stall too and Desktop decides
    # the server is dead.
    async with gatekeeper(tmp_path, Action.ALLOW, delay=1.5) as (process, _daemon):
        await handshake(process)
        await send(process, call(2, "delete_file", path="/tmp/x"))
        await send(process, {"jsonrpc": "2.0", "id": 3, "method": "ping"})

        first = await frame(process)
        second = await frame(process)
        await finish(process)

    # The ping overtakes the held call, which is the expected reordering.
    assert first.id == 3
    assert second.id == 2


async def test_fails_closed_when_there_is_no_daemon(tmp_path: Path):
    async with running(socket=tmp_path / "nobody-home.sock") as process:
        await handshake(process)
        await send(process, call(2, "delete_file", path="/tmp/x"))
        reply = await frame(process)
        stderr = await finish(process)

    assert result_of(reply).get("isError") is True
    assert "could not reach the approval daemon" in text_of(reply)
    assert "!! delete_file actually ran" not in stderr


async def test_a_cancelled_request_is_withdrawn(tmp_path: Path):
    async with gatekeeper(tmp_path, Action.ALLOW, delay=10.0) as (process, daemon):
        await handshake(process)
        await send(process, call(2, "delete_file", path="/tmp/x"))
        await asyncio.sleep(0.5)
        assert len(daemon.queue) == 1

        await send(
            process,
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": 2, "reason": "user stopped"},
            },
        )
        await asyncio.sleep(0.5)
        assert len(daemon.queue) == 0

        stderr = await finish(process)

    assert "cancelled: c2s id=2" in stderr
    assert "!! delete_file actually ran" not in stderr


async def test_stdout_carries_only_protocol_frames(tmp_path: Path):
    # The test that catches a stray print before Claude Desktop does.
    async with gatekeeper(tmp_path, Action.ALLOW) as (process, _daemon):
        await send(process, INITIALIZE)
        await send(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        await send(process, call(3, "read_file", path="/tmp/x"))

        assert process.stdin is not None
        process.stdin.close()

        assert process.stdout is not None
        lines: list[bytes] = []
        while True:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=20)
            if not line:
                break
            lines.append(line)

    assert len(lines) >= 3
    for line in lines:
        protocol.parse_line(line)  # raises MalformedLine if anything else got in


async def test_exits_cleanly_and_reaps_the_server(tmp_path: Path):
    async with gatekeeper(tmp_path, Action.ALLOW) as (process, _daemon):
        await handshake(process)
        stderr = await finish(process)

    assert process.returncode == 0
    # Proves we waited for the child rather than orphaning it.
    assert "server exited with code" in stderr


async def test_passes_through_output_it_cannot_parse(tmp_path: Path):
    # A banner is forwarded, not swallowed: a transparent proxy does not
    # quietly edit the stream.
    socket = tmp_path / "d.sock"
    daemon = Daemon(DecisionQueue(make_auto_decider(Action.ALLOW)), socket)
    await daemon.start()
    serving = asyncio.create_task(daemon.serve())
    try:
        async with running(socket=socket, server_args=["--banner"]) as process:
            await send(process, INITIALIZE)
            assert process.stdout is not None
            raw = [
                await asyncio.wait_for(process.stdout.readline(), timeout=20),
                await asyncio.wait_for(process.stdout.readline(), timeout=20),
            ]
            await finish(process)
    finally:
        await daemon.stop()
        serving.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await serving

    assert any(b"starting up" in line for line in raw)
