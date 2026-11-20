"""The daemon's socket listener."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

from mcp_gatekeeper import framing, ipc
from mcp_gatekeeper.daemon.decisions import DecisionQueue, Withdrawn
from mcp_gatekeeper.ipc import DecisionRequest, IpcError
from mcp_gatekeeper.protocol import JSONObject

Send = Callable[[JSONObject], Awaitable[None]]


class Daemon:
    """Accepts interceptor connections and answers their decide messages."""

    def __init__(self, queue: DecisionQueue, path: Path) -> None:
        self.queue: DecisionQueue = queue
        self.path: Path = path
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    def log(self, message: str) -> None:
        sys.stderr.write(f"[gatekeeper-daemon] {message}\n")
        sys.stderr.flush()

    async def start(self) -> None:
        ipc.require_unix()
        ipc.check_socket_path(self.path)
        ipc.ensure_socket_dir(self.path)

        # A socket left behind by a crash would make bind() fail, and the file
        # existing says nothing about whether anyone is listening.
        if self.path.exists():
            self.path.unlink()

        self._server = await asyncio.start_unix_server(self._handle, path=str(self.path))
        os.chmod(self.path, 0o600)
        self.log(f"listening on {self.path}")

    async def serve(self) -> None:
        """Serve a listener that start() has already opened.

        Deliberately not `async with server:`. That form's __aexit__ awaits
        wait_closed(), which since 3.12 waits for every live connection to
        finish -- so cancelling this task would hang exactly when a client is
        still connected. stop() owns teardown instead.
        """
        server = self._server
        if server is None:
            raise RuntimeError("start() has not run")
        await server.serve_forever()

    async def serve_forever(self) -> None:
        await self.start()
        await self.serve()

    async def stop(self) -> None:
        """Shut down, dropping live connections.

        Call this *before* cancelling whatever task is running serve(). Both
        Server.close() and Server.serve_forever() await wait_closed(), which
        since 3.12 waits for every connection handler to finish -- so a serve
        task cancelled while a client is still connected will hang. Cancelling
        the handlers here first is what lets it unwind.
        """
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

        if self._server is not None:
            self._server.close()
            # Only returns promptly because the handlers above are gone.
            # The timeout is a backstop, not the plan.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), timeout=2.0)
            self._server = None

        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        self.log("stopped")

    def _track(self, task: asyncio.Task[None]) -> None:
        # Without a reference the loop may garbage-collect a running task.
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Track our own task so stop() can drop live connections. Without this
        # a connected client keeps the daemon from ever shutting down.
        current = asyncio.current_task()
        if current is not None:
            self._track(cast("asyncio.Task[None]", current))

        write_lock = asyncio.Lock()

        async def send(payload: JSONObject) -> None:
            async with write_lock:
                if writer.is_closing():
                    return
                with contextlib.suppress(OSError, ConnectionError):
                    writer.write(framing.encode(payload))
                    await writer.drain()

        peer = "?"
        try:
            async for raw in framing.iter_lines(reader, on_drop=self.log):
                payload = ipc.decode(raw)
                if payload is None:
                    self.log("ignoring unreadable frame")
                    continue

                kind = payload.get("type")

                if kind == "hello":
                    peer = str(payload.get("server", "?"))
                    self.log(f"connected: {peer} (pid {payload.get('pid')})")

                elif kind == "decide":
                    # A task per decision, so one slow answer does not hold up
                    # anything else arriving on this same connection.
                    self._track(asyncio.create_task(self._decide(payload, send)))

                elif kind == "withdraw":
                    decision_id = payload.get("id")
                    if isinstance(decision_id, str) and self.queue.withdraw(decision_id):
                        self.log(f"withdrawn: {decision_id}")

                else:
                    self.log(f"ignoring unknown message type: {kind!r}")
        finally:
            self.log(f"disconnected: {peer}")
            if not writer.is_closing():
                writer.close()
                with contextlib.suppress(OSError, asyncio.CancelledError):
                    await writer.wait_closed()

    async def _decide(self, payload: JSONObject, send: Send) -> None:
        try:
            request = DecisionRequest.from_payload(payload)
        except IpcError as exc:
            self.log(f"bad decide message: {exc}")
            return

        label = request.tool or request.method
        self.log(f"pending: {request.server} {label} (id={request.id[:8]})")

        try:
            verdict = await self.queue.submit(request)
        except Withdrawn:
            # Nobody is waiting for an answer any more.
            return

        self.log(f"{verdict.action.value}: {request.server} {label}")
        await send({"type": "verdict", "id": request.id, **verdict.to_payload()})
