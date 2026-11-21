"""The stdio proxy that sits between Claude Desktop and one MCP server.

Claude Desktop launches whatever command its config names, talks JSON-RPC over
that process's stdin and stdout, and has no idea what it actually started. So
we take that slot: Desktop launches us, we launch the real server, and every
frame passes through our hands.

    python -m mcp_gatekeeper.interceptor --server-name demo -- <real server cmd>

Frames that need a decision are held here while the daemon is asked. Everything
else goes straight through, which is what keeps `tools/list` and `ping` snappy
while a dialog is open.

Three rules that matter more than they look:

* Stdout carries protocol data and nothing else. One stray write corrupts the
  stream and the server dies in a way that is genuinely hard to diagnose. All
  logging goes to stderr, which Desktop captures in its own log.
* An allowed frame is forwarded as the bytes it arrived as, never
  re-serialised, so "what the user approved" and "what the server ran" are the
  same thing.
* A refusal travels back the way the request came. Blocking a client's
  tools/call means answering the client; blocking a server's sampling request
  means answering the server.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from mcp_gatekeeper import framing, ipc, protocol
from mcp_gatekeeper.framing import MAX_LINE_BYTES, iter_lines
from mcp_gatekeeper.ipc import Action, DaemonClient, DecisionRequest, IpcError, Verdict
from mcp_gatekeeper.protocol import Direction, Kind, Line, MalformedLine, Message, RequestId

# How long to let the server finish replying after the client goes away.
DRAIN_SECONDS = 5.0

# How long to wait for the server to exit before killing it.
EXIT_SECONDS = 5.0

BATCH_BLOCK_TEXT = (
    "Blocked by MCP Gatekeeper: this batch contained a request needing approval, "
    "and batches are refused as a whole. Send the calls individually."
)


def _opposite(direction: Direction) -> Direction:
    """Where a reply to a frame travelling `direction` has to go."""
    if direction is Direction.CLIENT_TO_SERVER:
        return Direction.SERVER_TO_CLIENT
    return Direction.CLIENT_TO_SERVER


class Session:
    """Per-server state: what was negotiated, and what is currently held."""

    def __init__(self, server_name: str) -> None:
        self.server_name: str = server_name
        # What the client asked for, and what the server actually agreed to.
        # These differ in practice: Claude Desktop proposes 2025-11-25 and a
        # server may well answer with something older.
        self.proposed_version: str | None = None
        self.protocol_version: str | None = None
        # (direction, jsonrpc id) -> decision id, so a cancellation can find
        # the decision it belongs to. Two servers both use id 1, which is why
        # the interceptor is per-server and the key is not the id alone.
        self.held: dict[tuple[Direction, RequestId], str] = {}

    def log(self, message: str) -> None:
        """Stderr only. See the module docstring."""
        sys.stderr.write(f"[gatekeeper:{self.server_name}] {message}\n")
        sys.stderr.flush()

    def note(self, line: Line) -> None:
        """Record what can be learned from a frame on its way past."""
        for message in line.messages:
            version = protocol.extract_protocol_version(message)
            if version is None:
                continue

            if message.kind is Kind.RESPONSE:
                # The response is authoritative. It has to be allowed to
                # overwrite the request's value, because the initialize
                # request arrives first -- an earlier version of this took
                # whatever it saw first and so always reported the client's
                # proposal while calling it "negotiated".
                if self.protocol_version != version:
                    self.protocol_version = version
                    self.log(f"negotiated protocolVersion={version}")
            elif self.proposed_version is None:
                self.proposed_version = version
                self.log(f"client proposed protocolVersion={version}")

    def cancelled_ids(self, line: Line) -> list[RequestId]:
        """Request ids this frame is cancelling, if any."""
        cancelled: list[RequestId] = []
        for message in line.messages:
            if message.method != "notifications/cancelled":
                continue
            params = message.payload.get("params")
            if not isinstance(params, dict):
                continue
            raw_id = cast("dict[str, object]", params).get("requestId")
            if isinstance(raw_id, str | int | float):
                cancelled.append(raw_id)
        return cancelled


class Proxy:
    """Moves frames between the client and the server, holding what needs a decision."""

    def __init__(
        self,
        session: Session,
        process: asyncio.subprocess.Process,
        client: DaemonClient | None,
        *,
        denial_mode: str = "tool_error",
        on_unavailable: Action = Action.BLOCK,
    ) -> None:
        self.session: Session = session
        self.process: asyncio.subprocess.Process = process
        self.client: DaemonClient | None = client
        self.denial_mode: str = denial_mode
        self.on_unavailable: Action = on_unavailable

        self._to_server_lock: asyncio.Lock = asyncio.Lock()
        self._to_client_lock: asyncio.Lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()

    # -- plumbing ---------------------------------------------------------

    def _track(self, task: asyncio.Task[None]) -> None:
        # Without a reference the loop may garbage-collect a running task.
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def flush_held(self, timeout: float) -> None:
        """Give decisions already in flight a chance to finish.

        Called before the server's stdin is closed. Without this, a call that
        has just been approved is dropped on the way out, because the server
        exits before the frame reaches it.
        """
        outstanding = {task for task in self._tasks if not task.done()}
        if not outstanding:
            return
        self.session.log(f"waiting for {len(outstanding)} held frame(s)")
        await asyncio.wait(outstanding, timeout=timeout)

    async def drain_tasks(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

    async def forward(self, raw: bytes, to: Direction) -> None:
        frame = raw if raw.endswith(b"\n") else raw + b"\n"

        if to is Direction.CLIENT_TO_SERVER:
            stdin = self.process.stdin
            if stdin is None or stdin.is_closing():
                return
            async with self._to_server_lock:
                try:
                    stdin.write(frame)
                    await stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    self.session.log("server stopped reading, dropping the frame")
            return

        async with self._to_client_lock:
            sys.stdout.buffer.write(frame)
            sys.stdout.buffer.flush()

    # -- pumps ------------------------------------------------------------

    async def pump(self, reader: asyncio.StreamReader, direction: Direction) -> None:
        """Read frames travelling `direction` and deal with each one.

        A gated frame is handed to a task, never awaited here, or one pending
        decision would stall everything else on this stream.
        """
        async for raw in iter_lines(reader, on_drop=self.session.log):
            try:
                line = protocol.parse_line(raw)
            except MalformedLine as exc:
                # Real servers print startup banners. Log it and pass it on
                # unchanged: a transparent proxy does not quietly edit the
                # stream, and the peer is entitled to ignore it itself.
                self.session.log(f"{direction.value}: forwarding unparsed line ({exc})")
                await self.forward(raw, direction)
                continue

            self.session.note(line)
            self._withdraw_cancelled(line, direction)

            gated = [m for m in line.messages if protocol.should_gate(m, direction)]
            if not gated:
                await self.forward(raw, direction)
                continue

            self._track(asyncio.create_task(self._hold(line, gated, direction)))

    def _withdraw_cancelled(self, line: Line, direction: Direction) -> None:
        """Drop any decision whose request the peer has just cancelled."""
        client = self.client
        for request_id in self.session.cancelled_ids(line):
            decision_id = self.session.held.pop((direction, request_id), None)
            if decision_id is None or client is None:
                continue
            self.session.log(f"cancelled: {direction.value} id={request_id}")
            self._track(asyncio.create_task(client.withdraw(decision_id)))

    # -- decisions --------------------------------------------------------

    async def _hold(self, line: Line, gated: list[Message], direction: Direction) -> None:
        try:
            if line.is_batch:
                await self._refuse_batch(line, gated, direction)
                return

            message = gated[0]
            verdict = await self._ask(message, direction)

            if verdict.allowed:
                await self.forward(line.raw, direction)
            else:
                await self._deny(message, direction, verdict)
        except asyncio.CancelledError:
            # Either the peer cancelled the request or we are shutting down.
            # Nobody is waiting for an answer, so there is nothing to send.
            raise

    async def _ask(self, message: Message, direction: Direction) -> Verdict:
        label, details = protocol.describe(message)
        request = DecisionRequest(
            id=DecisionRequest.new_id(),
            server=self.session.server_name,
            direction=direction.value,
            method=message.method or "?",
            tool=label,
            params=details,
            request_id=message.id,
        )

        client = self.client
        if client is None:
            return self._unavailable("no daemon configured for this platform")

        key = (direction, message.id)
        self.session.held[key] = request.id
        self.session.log(f"holding {direction.value} {message.method} id={message.id} ({label})")

        try:
            verdict = await self._ask_daemon(client, request)
        finally:
            self.session.held.pop(key, None)

        self.session.log(f"{verdict.action.value}: {label} id={message.id}")
        return verdict

    async def _ask_daemon(self, client: DaemonClient, request: DecisionRequest) -> Verdict:
        """Ask the daemon, reconnecting if the connection turns out to be dead.

        Two attempts, because connection state cannot be predicted: a Unix
        socket whose peer has gone still looks writable until you actually
        write to it, so the only reliable signal is a failure. The daemon may
        also have started *after* us, or restarted since -- neither should mean
        refusing everything until Claude Desktop is restarted.
        """
        detail = "daemon not reachable"

        for final in (False, True):
            try:
                was_connected = client.connected
                await client.ensure_connected()
                if not was_connected:
                    self.session.log(f"connected to the daemon at {client.path}")
                return await client.ask(request)
            except (IpcError, NotImplementedError) as exc:
                detail = str(exc)
                if final:
                    break
                # Drop whatever we have so the next attempt genuinely
                # reconnects rather than reusing a corpse.
                with contextlib.suppress(Exception):
                    await client.close()

        return self._unavailable(detail)

    def _unavailable(self, detail: str) -> Verdict:
        self.session.log(f"daemon unavailable ({detail}); failing {self.on_unavailable.value}")
        reason = (
            "Blocked by MCP Gatekeeper: could not reach the approval daemon, "
            + f"so the call was refused ({detail})."
        )
        return Verdict(action=self.on_unavailable, reason=reason)

    async def _deny(self, message: Message, direction: Direction, verdict: Verdict) -> None:
        text = verdict.reason or protocol.BLOCK_TEXT
        payload = protocol.build_denial(message.id, mode=self.denial_mode, text=text)
        # The refusal goes back the way the request came.
        await self.forward(framing.encode(payload), _opposite(direction))

    async def _refuse_batch(self, line: Line, gated: list[Message], direction: Direction) -> None:
        """Refuse a whole batch containing anything that needs approval.

        Batching only existed in the 2025-03-26 revision and was removed in
        2025-06-18, so this is a safety net rather than a feature. Approving
        part of a batch and forwarding the rest would change its meaning, and
        answering each member separately breaks the batch contract -- so the
        whole thing is refused and the client is told to retry individually.
        """
        self.session.log(f"refusing a batch containing {len(gated)} gated request(s)")
        denials = [
            protocol.build_denial(message.id, mode=self.denial_mode, text=BATCH_BLOCK_TEXT)
            for message in line.messages
            if message.is_request
        ]
        if denials:
            await self.forward(framing.encode(denials), _opposite(direction))


async def _wait_for_stop(event: asyncio.Event) -> None:
    """Wrapper so every task in the wait set shares one result type."""
    await event.wait()


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


async def run(
    server_name: str,
    command: Sequence[str],
    *,
    socket: Path,
    denial_mode: str = "tool_error",
    on_unavailable: Action = Action.BLOCK,
) -> int:
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

    # Not fatal if the daemon is missing: discovery and pings still work, and
    # anything needing approval fails closed with an explanation the model can
    # relay. A dead interceptor would take the whole server down instead.
    client: DaemonClient | None = DaemonClient(socket, server_name)
    try:
        await client.connect()
        session.log(f"connected to the daemon at {socket}")
    except NotImplementedError as exc:
        # No transport on this platform at all, so there is nothing to retry.
        session.log(f"daemon unsupported here ({exc}); gated requests will be refused")
        client = None
    except IpcError as exc:
        # Keep the client: it reconnects when a request actually needs it, so
        # starting the daemon later does not mean restarting Desktop.
        session.log(f"no daemon at {socket} yet ({exc}); will retry on demand")

    proxy = Proxy(
        session,
        process,
        client,
        denial_mode=denial_mode,
        on_unavailable=on_unavailable,
    )

    stdin_reader = await _connect_stdin()

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stopping.set)

    from_client = asyncio.create_task(proxy.pump(stdin_reader, Direction.CLIENT_TO_SERVER))
    from_server = asyncio.create_task(proxy.pump(process.stdout, Direction.SERVER_TO_CLIENT))
    signalled = asyncio.create_task(_wait_for_stop(stopping))

    done, pending = await asyncio.wait(
        {from_client, from_server, signalled}, return_when=asyncio.FIRST_COMPLETED
    )

    if signalled in done:
        session.log("stopping on signal")
    elif from_client in done:
        # Desktop closed our stdin, which is how it says it is finished. Give
        # the server a moment to flush any replies still in flight.
        session.log("client closed stdin")
        # Let anything already approved reach the server before we shut its
        # input, or the frame is lost.
        await proxy.flush_held(DRAIN_SECONDS)
        # Then tell the server, and only then wait: it closes its stdout once
        # its own stdin is done, so draining before this just burns the timeout.
        if not process.stdin.is_closing():
            process.stdin.close()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(from_server), timeout=DRAIN_SECONDS)
    else:
        session.log("server closed stdout")

    for task in pending:
        task.cancel()
    for task in pending:
        with contextlib.suppress(asyncio.CancelledError):
            await task

    await proxy.drain_tasks()
    if client is not None:
        await client.close()

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
        "--socket",
        default=None,
        help=f"daemon socket path (default: {ipc.socket_path()})",
    )
    parser.add_argument(
        "--denial-mode",
        choices=["tool_error", "jsonrpc_error"],
        default="tool_error",
        help="how a refusal is reported back",
    )
    parser.add_argument(
        "--on-daemon-unavailable",
        choices=[Action.BLOCK.value, Action.ALLOW.value],
        default=Action.BLOCK.value,
        help="what to do when the daemon cannot be reached; allow is dangerous",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="the real server command, after a -- separator",
    )
    args = parser.parse_args(argv)

    command: list[str] = list(cast("list[str]", args.command))
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("no server command given; put it after --")

    raw_socket = cast("str | None", args.socket)
    socket = Path(raw_socket) if raw_socket else ipc.socket_path()

    return asyncio.run(
        run(
            cast("str", args.server_name),
            command,
            socket=socket,
            denial_mode=cast("str", args.denial_mode),
            on_unavailable=Action(cast("str", args.on_daemon_unavailable)),
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
