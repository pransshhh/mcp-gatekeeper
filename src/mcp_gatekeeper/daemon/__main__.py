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

from mcp_gatekeeper import audit as audit_module
from mcp_gatekeeper import ipc
from mcp_gatekeeper import policy as policy_module
from mcp_gatekeeper.audit import AuditLog
from mcp_gatekeeper.daemon.decisions import Decider, DecisionQueue
from mcp_gatekeeper.daemon.gate import make_gate
from mcp_gatekeeper.daemon.server import Daemon
from mcp_gatekeeper.ipc import Action, DecisionRequest, Verdict
from mcp_gatekeeper.policy import Policy

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


def run_with_ui(
    path: Path,
    timeout: float,
    on_timeout: Action,
    *,
    policy: Policy,
    audit: AuditLog,
) -> int:
    # Imported here so headless runs never need a display or tkinter.
    from mcp_gatekeeper.ui.prompt import PromptQueue, PromptWindow, UiDecider

    prompts = PromptQueue()
    gate = make_gate(
        policy,
        audit,
        UiDecider(prompts),
        timeout=timeout,
        on_timeout=on_timeout,
    )
    daemon = Daemon(DecisionQueue(gate), path)

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
    parser.add_argument(
        "--policy",
        default=None,
        help=f"rules file, read only (default: {policy_module.default_policy_path()})",
    )
    parser.add_argument(
        "--remembered",
        default=None,
        help=(
            "where 'always' choices are kept (default: "
            + str(policy_module.default_remembered_path())
            + ")"
        ),
    )
    parser.add_argument(
        "--audit",
        default=None,
        help=f"decision log (default: {audit_module.default_path()})",
    )
    args = parser.parse_args(argv)

    raw_socket = cast("str | None", args.socket)
    path = Path(raw_socket) if raw_socket else ipc.socket_path()

    raw_auto = cast("str | None", args.auto)
    if raw_auto is not None:
        # Headless: no rules, no log, no display. Keeps the plumbing tests
        # about the plumbing.
        return asyncio.run(run_headless(path, Action(raw_auto), cast("float", args.delay)))

    raw_policy = cast("str | None", args.policy)
    raw_remembered = cast("str | None", args.remembered)
    raw_audit = cast("str | None", args.audit)

    policy = Policy.load(
        Path(raw_policy) if raw_policy else policy_module.default_policy_path(),
        Path(raw_remembered) if raw_remembered else policy_module.default_remembered_path(),
    )
    audit = AuditLog(Path(raw_audit) if raw_audit else audit_module.default_path())

    return run_with_ui(
        path,
        cast("float", args.timeout),
        Action(cast("str", args.on_timeout)),
        policy=policy,
        audit=audit,
    )


if __name__ == "__main__":
    raise SystemExit(main())
