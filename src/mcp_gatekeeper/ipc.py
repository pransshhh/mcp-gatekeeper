"""Transport between each interceptor and the one daemon.

This is our own protocol, not MCP. An interceptor describes a request it is
holding; the daemon answers with a verdict. One JSON object per line over a
Unix socket that only the current user can open.

Those file permissions are the security boundary, not housekeeping: anything
that can connect to this socket can forge an "allow" for any request, so the
socket lives in a 0700 directory and is itself 0600.

Decision ids are generated here and are deliberately *not* JSON-RPC ids. Two
MCP servers will both happily use `id: 1`, so the daemon can never key on that
alone.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

from mcp_gatekeeper import framing
from mcp_gatekeeper.protocol import JSONObject, RequestId

SOCKET_NAME = "mcp-gatekeeper.sock"
SOCKET_ENV = "MCP_GATEKEEPER_SOCKET"

# sun_path is 108 bytes on Linux and 104 on macOS, and going over it fails deep
# inside asyncio with a bare "AF_UNIX path too long". Check it ourselves so the
# message says what to do about it.
MAX_SOCKET_PATH_BYTES = 100


class IpcError(Exception):
    """Something went wrong talking to the daemon.

    Callers are expected to treat this as a reason to block, not to carry on.
    """


class Action(StrEnum):
    ALLOW = "allow"
    BLOCK = "block"


class Scope(StrEnum):
    """How long a decision lasts. Only ONCE is honoured before the policy layer."""

    ONCE = "once"
    SESSION = "session"
    ALWAYS = "always"


@dataclass(frozen=True)
class Verdict:
    action: Action
    scope: Scope = Scope.ONCE
    reason: str | None = None

    @property
    def allowed(self) -> bool:
        return self.action is Action.ALLOW

    def to_payload(self) -> JSONObject:
        return {"action": self.action.value, "scope": self.scope.value, "reason": self.reason}

    @classmethod
    def from_payload(cls, payload: JSONObject) -> Verdict:
        raw_action = payload.get("action")
        raw_scope = payload.get("scope")
        reason = payload.get("reason")

        # Anything we don't recognise means block. A verdict we can't read is
        # not permission.
        action = Action.ALLOW if raw_action == Action.ALLOW.value else Action.BLOCK
        try:
            scope = Scope(raw_scope)
        except ValueError:
            scope = Scope.ONCE

        return cls(action=action, scope=scope, reason=reason if isinstance(reason, str) else None)


@dataclass(frozen=True)
class DecisionRequest:
    """One held request, as described to the daemon."""

    id: str
    server: str
    direction: str
    method: str
    tool: str | None
    params: JSONObject
    request_id: RequestId

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex

    def to_payload(self) -> JSONObject:
        return {
            "type": "decide",
            "id": self.id,
            "server": self.server,
            "direction": self.direction,
            "method": self.method,
            "tool": self.tool,
            "params": self.params,
            "request_id": self.request_id,
        }

    @classmethod
    def from_payload(cls, payload: JSONObject) -> DecisionRequest:
        decision_id = payload.get("id")
        if not isinstance(decision_id, str) or not decision_id:
            raise IpcError("decide message has no usable id")

        params = payload.get("params")
        tool = payload.get("tool")
        request_id = payload.get("request_id")

        return cls(
            id=decision_id,
            server=_as_text(payload.get("server"), "?"),
            direction=_as_text(payload.get("direction"), "?"),
            method=_as_text(payload.get("method"), "?"),
            tool=tool if isinstance(tool, str) else None,
            params=cast(JSONObject, params) if isinstance(params, dict) else {},
            request_id=request_id if isinstance(request_id, str | int | float) else None,
        )


def _as_text(value: object, fallback: str) -> str:
    return value if isinstance(value, str) else fallback


def decode(raw: bytes) -> JSONObject | None:
    """Parse one IPC frame, or None if it isn't readable.

    Unreadable frames are dropped rather than raised: a confused peer should
    not be able to kill the daemon.
    """
    try:
        payload = cast(object, json.loads(raw.decode("utf-8", errors="replace")))
    except json.JSONDecodeError:
        return None
    return cast(JSONObject, payload) if isinstance(payload, dict) else None


def socket_path() -> Path:
    """Where the daemon listens.

    The environment override exists so tests never touch a real daemon.
    """
    override = os.environ.get(SOCKET_ENV)
    if override:
        return Path(override)

    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / SOCKET_NAME

    return Path.home() / ".mcp-gatekeeper" / SOCKET_NAME


def check_socket_path(path: Path) -> None:
    """Reject a path the operating system cannot bind."""
    length = len(str(path).encode("utf-8"))
    if length > MAX_SOCKET_PATH_BYTES:
        message = (
            f"socket path is {length} bytes, over the {MAX_SOCKET_PATH_BYTES} byte limit "
            + f"for Unix sockets: {path}. Use a shorter path, e.g. under $XDG_RUNTIME_DIR."
        )
        raise IpcError(message)


def require_unix() -> None:
    if sys.platform == "win32":
        message = (
            "The daemon speaks a Unix domain socket. Windows needs a named pipe; "
            + "run the daemon under WSL for now."
        )
        raise NotImplementedError(message)


def ensure_socket_dir(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Tighten it when we can. A shared directory such as /tmp is not ours to
    # chmod, and the socket's own 0600 is the boundary that actually matters;
    # the directory mode is defence in depth.
    with contextlib.suppress(OSError):
        os.chmod(path.parent, 0o700)


class DaemonClient:
    """The interceptor's side of the connection.

    Several decisions can be outstanding at once, so replies are matched back
    to callers by decision id rather than by arrival order.
    """

    def __init__(self, path: Path, server_name: str) -> None:
        self.path: Path = path
        self.server_name: str = server_name
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[Verdict]] = {}
        self._write_lock: asyncio.Lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        writer = self._writer
        return writer is not None and not writer.is_closing()

    async def ensure_connected(self) -> None:
        """Connect if we are not already. Safe to call on every request."""
        if self.connected:
            return
        await self.connect()

    async def connect(self) -> None:
        require_unix()
        check_socket_path(self.path)
        # Re-callable: a daemon restart leaves a dead reader task and a closing
        # writer behind, and both have to go before we open a new connection.
        await self._teardown()
        try:
            reader, writer = await asyncio.open_unix_connection(str(self.path))
        except OSError as exc:
            raise IpcError(f"cannot reach the daemon at {self.path}: {exc}") from exc

        self._writer = writer
        self._reader_task = asyncio.create_task(self._read_loop(reader))
        await self._send({"type": "hello", "server": self.server_name, "pid": os.getpid()})

    async def ask(self, request: DecisionRequest) -> Verdict:
        """Hand a held request to the daemon and wait for the answer."""
        future: asyncio.Future[Verdict] = asyncio.get_running_loop().create_future()
        self._pending[request.id] = future
        try:
            await self._send(request.to_payload())
        except IpcError:
            self._pending.pop(request.id, None)
            raise
        return await future

    async def withdraw(self, decision_id: str) -> None:
        """Tell the daemon nobody is waiting for this decision any more."""
        future = self._pending.pop(decision_id, None)
        if future is not None and not future.done():
            future.cancel()
        with contextlib.suppress(IpcError):
            await self._send({"type": "withdraw", "id": decision_id})

    async def close(self) -> None:
        await self._teardown()

    async def _teardown(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None

        writer = self._writer
        if writer is not None:
            if not writer.is_closing():
                writer.close()
            with contextlib.suppress(OSError, asyncio.CancelledError):
                await writer.wait_closed()
            self._writer = None

        self._fail_pending(IpcError("connection closed"))

    async def _send(self, payload: JSONObject) -> None:
        writer = self._writer
        if writer is None or writer.is_closing():
            raise IpcError("not connected to the daemon")
        async with self._write_lock:
            try:
                writer.write(framing.encode(payload))
                await writer.drain()
            except (OSError, ConnectionError) as exc:
                raise IpcError(f"lost the daemon connection: {exc}") from exc

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        try:
            async for raw in framing.iter_lines(reader):
                payload = decode(raw)
                if payload is None or payload.get("type") != "verdict":
                    continue

                decision_id = payload.get("id")
                if not isinstance(decision_id, str):
                    continue

                future = self._pending.pop(decision_id, None)
                if future is not None and not future.done():
                    future.set_result(Verdict.from_payload(payload))
        finally:
            # The daemon going away must not leave callers waiting forever.
            # They fail closed instead.
            self._fail_pending(IpcError("daemon closed the connection"))
            # Mark the connection dead so `connected` is honest and the next
            # request reconnects. Losing the read side does not close the write
            # side on its own, so without this we would look connected forever.
            writer = self._writer
            if writer is not None and not writer.is_closing():
                writer.close()

    def _fail_pending(self, exc: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()
