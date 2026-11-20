"""Run the daemon.

    python -m mcp_gatekeeper.daemon --auto allow

There is no popup yet. `--auto` answers every request the same way, which
keeps this stage about the plumbing: if the round trip works under --auto,
what is left is purely a UI problem.

`--delay` fakes a slow human. Use it to prove that holding one request does
not stall anything else -- the failure that would make the finished tool feel
broken.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from mcp_gatekeeper import ipc
from mcp_gatekeeper.daemon.decisions import Decider, DecisionQueue
from mcp_gatekeeper.daemon.server import Daemon
from mcp_gatekeeper.ipc import Action, DecisionRequest, Verdict


def make_auto_decider(action: Action, delay: float = 0.0) -> Decider:
    """A stand-in for the user. Answers everything the same way."""

    async def decide(_request: DecisionRequest) -> Verdict:
        if delay > 0:
            await asyncio.sleep(delay)
        return Verdict(action=action, reason="auto")

    return decide


async def run(path: Path, action: Action, delay: float) -> int:
    queue = DecisionQueue(make_auto_decider(action, delay))
    daemon = Daemon(queue, path)

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stopping.set)

    await daemon.start()
    server = asyncio.create_task(daemon.serve())
    try:
        await stopping.wait()
    finally:
        # stop() first: it drops live connections, which is what allows the
        # serve task to unwind instead of blocking on wait_closed().
        await daemon.stop()
        server.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await server
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mcp_gatekeeper.daemon")
    parser.add_argument(
        "--auto",
        choices=[Action.ALLOW.value, Action.BLOCK.value],
        default=Action.ALLOW.value,
        help="answer every request this way (there is no popup yet)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="seconds to wait before answering, to fake a slow human",
    )
    parser.add_argument(
        "--socket",
        default=None,
        help=f"socket path (default: {ipc.socket_path()})",
    )
    args = parser.parse_args(argv)

    raw_socket = cast(str | None, args.socket)
    path = Path(raw_socket) if raw_socket else ipc.socket_path()
    action = Action(cast(str, args.auto))
    delay = cast(float, args.delay)

    return asyncio.run(run(path, action, delay))


if __name__ == "__main__":
    raise SystemExit(main())
