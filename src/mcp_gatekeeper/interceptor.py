"""The stdio proxy that sits between Claude Desktop and one MCP server.

Claude Desktop launches whatever command its config names, talks JSON-RPC over
that process's stdin and stdout, and has no idea what it actually started. So
we take that slot: Desktop launches us, we launch the real server, and every
frame passes through our hands.

    python -m mcp_gatekeeper.interceptor --server-name demo -- <real server cmd>

This stage is pass-through only. Nothing is held and nothing is blocked; the
point is to prove the plumbing is invisible before any decision logic goes in.
A session driven through here must produce output identical to driving the real
server directly.

Two rules that matter more than they look:

* Stdout carries protocol data and nothing else. One stray write corrupts the
  stream and the server dies in a way that is genuinely hard to diagnose. All
  logging goes to stderr, which Desktop captures in its own log.
* Frames are forwarded as the bytes they arrived as, never re-serialised. Once
  gating exists, that is what makes "what the user approved" and "what the
  server ran" the same thing.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
from collections.abc import Sequence
from typing import cast

from mcp_gatekeeper import protocol
from mcp_gatekeeper.framing import MAX_LINE_BYTES, iter_lines
from mcp_gatekeeper.protocol import Direction, MalformedLine

# How long to let the server finish replying after the client goes away.
DRAIN_SECONDS = 5.0

# How long to wait for the server to exit before killing it.
EXIT_SECONDS = 5.0


async def _wait_for_stop(event: asyncio.Event) -> None:
    """Wrapper so every task in the wait set shares one result type."""
    await event.wait()


class Session:
    """One MCP server, with us in the middle of its conversation."""

    def __init__(self, server_name: str) -> None:
        self.server_name: str = server_name
        self.protocol_version: str | None = None

    def log(self, message: str) -> None:
        """Stderr only. See the module docstring."""
        sys.stderr.write(f"[gatekeeper:{self.server_name}] {message}\n")
        sys.stderr.flush()

    def inspect(self, raw: bytes, direction: Direction) -> None:
        """Look at a frame on its way past. Never changes it."""
        try:
            line = protocol.parse_line(raw)
        except MalformedLine as exc:
            # Real servers print startup banners to stdout. Log it and pass it
            # on unchanged: a transparent proxy does not quietly edit the
            # stream, and the client is entitled to ignore it itself.
            self.log(f"{direction.value}: forwarding unparsed line ({exc})")
            return

        for message in line.messages:
            if self.protocol_version is None:
                version = protocol.extract_protocol_version(message)
                if version is not None:
                    self.protocol_version = version
                    self.log(f"negotiated protocolVersion={version}")

            if not message.is_request:
                continue

            label, _details = protocol.describe(message)
            # Reported but not acted on yet. When gating lands, this boolean is
            # what decides whether the frame waits for the user.
            would_gate = protocol.should_gate(message, direction)
            detail = f"({label}) would_gate={would_gate}"
            self.log(f"{direction.value} {message.method} id={message.id} {detail}")


async def _connect_stdin() -> asyncio.StreamReader:
    """Wrap our own stdin in an asyncio stream.

    connect_read_pipe does not work on stdin on native Windows. That is fine
    while the interceptor runs under Linux (directly, or reached from Windows
    through wsl.exe); running it as a Windows process means replacing this with
    a thread feeding a queue.
    """
    reader = asyncio.StreamReader(limit=MAX_LINE_BYTES)
    loop = asyncio.get_running_loop()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    return reader


def _write_stdout(raw: bytes) -> None:
    sys.stdout.buffer.write(raw if raw.endswith(b"\n") else raw + b"\n")
    sys.stdout.buffer.flush()


async def _pump_to_server(
    session: Session, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Client -> server."""
    async for raw in iter_lines(reader, on_drop=session.log):
        session.inspect(raw, Direction.CLIENT_TO_SERVER)
        try:
            writer.write(raw if raw.endswith(b"\n") else raw + b"\n")
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            session.log("server stopped reading, dropping the rest")
            return


async def _pump_to_client(session: Session, reader: asyncio.StreamReader) -> None:
    """Server -> client."""
    async for raw in iter_lines(reader, on_drop=session.log):
        session.inspect(raw, Direction.SERVER_TO_CLIENT)
        _write_stdout(raw)


async def _shutdown(session: Session, process: asyncio.subprocess.Process) -> int:
    """Stop the real server and reap it, so Desktop leaves nothing behind."""
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=EXIT_SECONDS)
        except TimeoutError:
            session.log("server did not exit, killing it")
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()

    code = process.returncode or 0
    session.log(f"server exited with code {code}")
    return code


async def run(server_name: str, command: Sequence[str]) -> int:
    session = Session(server_name)
    session.log(f"starting: {' '.join(command)}")

    # stderr is deliberately not piped, so the real server's own logging goes
    # straight through to whatever is watching ours.
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        limit=MAX_LINE_BYTES,
    )
    if process.stdin is None or process.stdout is None:
        session.log("could not open pipes to the server")
        return 1

    stdin_reader = await _connect_stdin()

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stopping.set)

    to_server = asyncio.create_task(_pump_to_server(session, stdin_reader, process.stdin))
    to_client = asyncio.create_task(_pump_to_client(session, process.stdout))
    signalled = asyncio.create_task(_wait_for_stop(stopping))

    done, pending = await asyncio.wait(
        {to_server, to_client, signalled}, return_when=asyncio.FIRST_COMPLETED
    )

    if signalled in done:
        session.log("stopping on signal")
    elif to_server in done:
        # Desktop closed our stdin, which is how it says it is finished. Give
        # the server a moment to flush any replies still in flight.
        session.log("client closed stdin")
        if not process.stdin.is_closing():
            process.stdin.close()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(to_client), timeout=DRAIN_SECONDS)
    else:
        session.log("server closed stdout")

    for task in pending:
        task.cancel()
    for task in pending:
        with contextlib.suppress(asyncio.CancelledError):
            await task

    return await _shutdown(session, process)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mcp_gatekeeper.interceptor",
        description="Proxy one MCP server's stdio through MCP Gatekeeper.",
    )
    parser.add_argument(
        "--server-name",
        default="server",
        help="label for this server in logs and prompts",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="the real server command, after a -- separator",
    )
    args = parser.parse_args(argv)

    server_name = cast(str, args.server_name)
    command: list[str] = list(cast("list[str]", args.command))
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("no server command given; put it after --")

    return asyncio.run(run(server_name, command))


if __name__ == "__main__":
    raise SystemExit(main())
