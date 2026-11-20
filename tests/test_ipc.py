"""Tests for the interceptor <-> daemon transport and the decision queue."""

from __future__ import annotations

import asyncio
import contextlib
import stat
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from mcp_gatekeeper import ipc
from mcp_gatekeeper.daemon.__main__ import make_auto_decider
from mcp_gatekeeper.daemon.decisions import DecisionQueue
from mcp_gatekeeper.daemon.server import Daemon
from mcp_gatekeeper.ipc import Action, DaemonClient, DecisionRequest, IpcError, Scope, Verdict


def a_request(tool: str = "delete_file", request_id: int = 1) -> DecisionRequest:
    return DecisionRequest(
        id=DecisionRequest.new_id(),
        server="demo",
        direction="c2s",
        method="tools/call",
        tool=tool,
        params={"path": "/home/me/important.docx"},
        request_id=request_id,
    )


@asynccontextmanager
async def running_daemon(
    path: Path, action: Action = Action.ALLOW, delay: float = 0.0
) -> AsyncGenerator[Daemon]:
    daemon = Daemon(DecisionQueue(make_auto_decider(action, delay)), path)
    await daemon.start()
    server = asyncio.create_task(daemon.serve())
    try:
        yield daemon
    finally:
        await daemon.stop()
        server.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await server


@asynccontextmanager
async def connected(path: Path, name: str = "demo") -> AsyncGenerator[DaemonClient]:
    client = DaemonClient(path, name)
    await client.connect()
    try:
        yield client
    finally:
        await client.close()


# --------------------------------------------------------------------------
# verdict parsing -- fail closed
# --------------------------------------------------------------------------


def test_only_an_explicit_allow_means_allow():
    assert Verdict.from_payload({"action": "allow"}).allowed


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "block"},
        {"action": "maybe"},
        {"action": None},
        {},
    ],
)
def test_anything_else_means_block(payload: dict[str, object]):
    # A verdict we cannot read is not permission. This is the whole safety
    # posture of the tool in one assertion.
    assert not Verdict.from_payload(payload).allowed


def test_unknown_scope_falls_back_to_once():
    assert Verdict.from_payload({"action": "allow", "scope": "forever"}).scope is Scope.ONCE


def test_decision_request_round_trips():
    original = a_request()
    assert DecisionRequest.from_payload(original.to_payload()) == original


def test_decision_request_rejects_a_missing_id():
    with pytest.raises(IpcError):
        DecisionRequest.from_payload({"type": "decide", "server": "demo"})


def test_decision_ids_are_unique():
    assert DecisionRequest.new_id() != DecisionRequest.new_id()


# --------------------------------------------------------------------------
# socket plumbing
# --------------------------------------------------------------------------


def test_socket_path_honours_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv(ipc.SOCKET_ENV, str(tmp_path / "custom.sock"))
    assert ipc.socket_path() == tmp_path / "custom.sock"


async def test_socket_is_private_to_the_current_user(tmp_path: Path):
    # Anything that can connect can forge an allow, so these bits matter.
    path = tmp_path / "test.sock"
    async with running_daemon(path):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


async def test_replaces_a_socket_left_behind_by_a_crash(tmp_path: Path):
    # The file existing says nothing about whether anyone is listening.
    path = tmp_path / "test.sock"
    path.write_text("stale")

    async with running_daemon(path), connected(path) as client:
        assert (await client.ask(a_request())).allowed


async def test_socket_is_removed_on_shutdown(tmp_path: Path):
    path = tmp_path / "test.sock"
    async with running_daemon(path):
        assert path.exists()
    assert not path.exists()


def test_an_over_long_socket_path_is_rejected_clearly(tmp_path: Path):
    # The OS limit is about 108 bytes and blowing it fails deep inside asyncio
    # with "AF_UNIX path too long". Catch it where we can explain it.
    too_long = tmp_path / ("d" * 120 + ".sock")
    with pytest.raises(IpcError, match="byte limit"):
        ipc.check_socket_path(too_long)


def test_a_reasonable_socket_path_is_accepted():
    ipc.check_socket_path(Path("/run/user/1000/mcp-gatekeeper.sock"))


async def test_connecting_without_a_daemon_raises(tmp_path: Path):
    client = DaemonClient(tmp_path / "nobody-home.sock", "demo")
    with pytest.raises(IpcError):
        await client.connect()


# --------------------------------------------------------------------------
# decisions
# --------------------------------------------------------------------------


async def test_allow_round_trip(tmp_path: Path):
    path = tmp_path / "test.sock"
    async with running_daemon(path, Action.ALLOW), connected(path) as client:
        verdict = await client.ask(a_request())

    assert verdict.allowed
    assert verdict.action is Action.ALLOW


async def test_block_round_trip(tmp_path: Path):
    path = tmp_path / "test.sock"
    async with running_daemon(path, Action.BLOCK), connected(path) as client:
        verdict = await client.ask(a_request())

    assert not verdict.allowed


async def test_two_decisions_resolve_in_parallel(tmp_path: Path):
    # The one that matters. If decisions queue up behind each other here they
    # will queue up behind a real dialog later, and the tool feels broken.
    path = tmp_path / "test.sock"
    async with running_daemon(path, Action.ALLOW, delay=0.4), connected(path) as client:
        started = time.monotonic()
        first, second = await asyncio.gather(
            client.ask(a_request(request_id=1)),
            client.ask(a_request(tool="send_email", request_id=2)),
        )
        elapsed = time.monotonic() - started

    assert first.allowed
    assert second.allowed
    assert elapsed < 0.75, f"decisions serialised: {elapsed:.2f}s for two 0.4s answers"


async def test_replies_are_matched_by_decision_id_not_arrival_order(tmp_path: Path):
    path = tmp_path / "test.sock"
    slow = a_request(tool="slow", request_id=1)
    fast = a_request(tool="fast", request_id=2)

    async def decider(request: DecisionRequest) -> Verdict:
        if request.tool == "slow":
            await asyncio.sleep(0.3)
        return Verdict(action=Action.ALLOW, reason=request.tool)

    daemon = Daemon(DecisionQueue(decider), path)
    await daemon.start()
    server = asyncio.create_task(daemon.serve())
    try:
        async with connected(path) as client:
            results = await asyncio.gather(client.ask(slow), client.ask(fast))
    finally:
        await daemon.stop()
        server.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await server

    # Each caller gets its own answer even though the fast one came back first.
    assert [verdict.reason for verdict in results] == ["slow", "fast"]


async def test_snapshot_lists_what_is_outstanding(tmp_path: Path):
    path = tmp_path / "test.sock"
    async with running_daemon(path, delay=5.0) as daemon, connected(path) as client:
        request = a_request(tool="send_email")
        asking = asyncio.create_task(client.ask(request))
        await asyncio.sleep(0.15)

        assert [item.tool for item in daemon.queue.snapshot()] == ["send_email"]
        assert len(daemon.queue) == 1

        asking.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asking


async def test_withdraw_cancels_a_pending_decision(tmp_path: Path):
    path = tmp_path / "test.sock"
    async with running_daemon(path, delay=5.0) as daemon, connected(path) as client:
        request = a_request()
        asking = asyncio.create_task(client.ask(request))
        await asyncio.sleep(0.15)
        assert len(daemon.queue) == 1

        await client.withdraw(request.id)

        with pytest.raises(asyncio.CancelledError):
            await asking

        await asyncio.sleep(0.15)
        assert len(daemon.queue) == 0


async def test_withdrawing_something_unknown_is_harmless(tmp_path: Path):
    path = tmp_path / "test.sock"
    async with running_daemon(path) as daemon, connected(path) as client:
        await client.withdraw("not-a-real-decision")
        await asyncio.sleep(0.1)
        # Still working afterwards.
        assert (await client.ask(a_request())).allowed
        assert len(daemon.queue) == 0


# --------------------------------------------------------------------------
# robustness
# --------------------------------------------------------------------------


async def test_a_confused_peer_cannot_kill_the_daemon(tmp_path: Path):
    path = tmp_path / "test.sock"
    async with running_daemon(path):
        reader, writer = await asyncio.open_unix_connection(str(path))
        writer.write(b"not json at all\n")
        writer.write(b'{"type":"nonsense"}\n')
        writer.write(b'{"type":"decide"}\n')  # no id
        await writer.drain()
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        del reader

        # A second client still gets served.
        async with connected(path) as client:
            assert (await client.ask(a_request())).allowed


async def test_pending_callers_fail_closed_when_the_daemon_dies(tmp_path: Path):
    # Not left hanging, and not silently allowed either.
    path = tmp_path / "test.sock"
    daemon = Daemon(DecisionQueue(make_auto_decider(Action.ALLOW, delay=5.0)), path)
    await daemon.start()
    server = asyncio.create_task(daemon.serve())

    client = DaemonClient(path, "demo")
    await client.connect()
    asking = asyncio.create_task(client.ask(a_request()))
    await asyncio.sleep(0.15)

    await daemon.stop()
    server.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await server

    with pytest.raises(IpcError):
        await asyncio.wait_for(asking, timeout=5)

    await client.close()
