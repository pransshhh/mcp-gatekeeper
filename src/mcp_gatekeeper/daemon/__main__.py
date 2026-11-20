"""Run the daemon.

    python -m mcp_gatekeeper.daemon                  # with the popup
    python -m mcp_gatekeeper.daemon --auto allow     # headless, for tests

With no --auto, every held request raises a window and waits for a click.
--auto answers everything the same way instead, which is what the test suite
and CI use: no display needed, and it keeps those tests about the plumbing.

Threading, because Tk insists on the main thread: the window runs there and
the asyncio loop runs on a worker. The two only speak through the queue in
mcp_gatekeeper.ui.prompt.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from mcp_gatekeeper import ipc
from mcp_gatekeeper.daemon.decisions import Decider, DecisionQueue, with_timeout
from mcp_gatekeeper.daemon.server import Daemon
from mcp_gatekeeper.ipc import Action, DecisionRequest, Verdict

# Below the client's own request timeout (around 60s in the MCP SDK), so the
# user is never answering a question nobody is waiting for any more.
DEFAULT_TIMEOUT = 45.0


def make_auto_decider(action: Action, delay: float = 0.0) -> Decider:
    """A stand-in for the user. Answers everything the same way."""

    async def decide(_request: DecisionRequest) -> Verdict:
        if delay > 0:
            await asyncio.sleep(delay)
        # No reason: there is nothing to tell the user beyond the refusal
        # itself, and a reason here would replace the standard wording.
        return Verdict(action=action)

    return decide


async def _serve_until(daemon: Daemon, stopping: asyncio.Event) -> None:
    await daemon.start()
    serving = asyncio.create_task(daemon.serve())
    try:
        await stopping.wait()
    finally:
        # stop() first: it drops live connections, which is what allows the
        # serve task to unwind instead of blocking on wait_closed().
        await daemon.stop()
        serving.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await serving


async def run_headless(path: Path, action: Action, delay: float) -> int:
    queue = DecisionQueue(make_auto_decider(action, delay))
    daemon = Daemon(queue, path)

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stopping.set)

    await _serve_until(daemon, stopping)
    return 0


def run_with_ui(path: Path, timeout: float, on_timeout: Action) -> int:
    # Imported here so headless runs never need a display or tkinter.
    from mcp_gatekeeper.ui.prompt import PromptQueue, PromptWindow, UiDecider

    prompts = PromptQueue()
    queue = DecisionQueue(with_timeout(UiDecider(prompts), timeout, on_timeout))
    daemon = Daemon(queue, path)

    # Event has no loop affinity at construction, so the main thread can make
    # it and the worker can wait on it.
    stopping = asyncio.Event()
    loop = asyncio.new_event_loop()

    def worker() -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_serve_until(daemon, stopping))
        finally:
            loop.close()

    thread = threading.Thread(target=worker, name="gatekeeper-daemon", daemon=True)
    thread.start()

    window = PromptWindow(prompts, timeout_seconds=timeout)

    def on_signal(_signum: int, _frame: object) -> None:
        window.request_stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(OSError, ValueError):
            signal.signal(sig, on_signal)

    try:
        window.run()
    except KeyboardInterrupt:
        pass
    finally:
        loop.call_soon_threadsafe(stopping.set)
        thread.join(timeout=10)
        with contextlib.suppress(Exception):
            window.close()

    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mcp_gatekeeper.daemon")
    parser.add_argument(
        "--auto",
        choices=[Action.ALLOW.value, Action.BLOCK.value],
        default=None,
        help="skip the popup and answer every request this way (for tests)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="with --auto, seconds to wait before answering, to fake a slow human",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help="seconds to wait for a click before refusing",
    )
    parser.add_argument(
        "--on-timeout",
        choices=[Action.BLOCK.value, Action.ALLOW.value],
        default=Action.BLOCK.value,
        help="what an unanswered request becomes; allow is dangerous",
    )
    parser.add_argument(
        "--socket",
        default=None,
        help=f"socket path (default: {ipc.socket_path()})",
    )
    args = parser.parse_args(argv)

    raw_socket = cast("str | None", args.socket)
    path = Path(raw_socket) if raw_socket else ipc.socket_path()

    raw_auto = cast("str | None", args.auto)
    if raw_auto is not None:
        return asyncio.run(run_headless(path, Action(raw_auto), cast("float", args.delay)))

    return run_with_ui(
        path,
        cast("float", args.timeout),
        Action(cast("str", args.on_timeout)),
    )


if __name__ == "__main__":
    raise SystemExit(main())
