"""The queue of requests waiting on an answer."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from mcp_gatekeeper.ipc import DecisionRequest, Verdict

Decider = Callable[[DecisionRequest], Awaitable[Verdict]]
"""Something that answers one held request.

At this stage it is a stub that returns a fixed verdict. Later it is the popup:
show the request, wait for a click, return what was clicked. The shape is the
same either way, which is the point of defining it this way now.
"""


class Withdrawn(Exception):
    """The request was taken back before anyone decided it.

    Raised when the client cancelled, which means there is nobody left to send
    a verdict to.
    """


class DecisionQueue:
    """Tracks what is outstanding and runs the decider against each one.

    Deciding happens in a task per request, never inline, so a slow answer to
    one request cannot hold up any other.
    """

    def __init__(self, decider: Decider) -> None:
        self._decider: Decider = decider
        self._items: dict[str, DecisionRequest] = {}
        self._tasks: dict[str, asyncio.Task[Verdict]] = {}
        self._withdrawn: set[str] = set()

    async def _decide(self, request: DecisionRequest) -> Verdict:
        # A thin coroutine wrapper: create_task needs a Coroutine, while the
        # public Decider type stays the looser Awaitable so implementations
        # aren't forced to be async def.
        return await self._decider(request)

    async def submit(self, request: DecisionRequest) -> Verdict:
        """Register a held request and wait for its answer."""
        task = asyncio.create_task(self._decide(request))
        self._items[request.id] = request
        self._tasks[request.id] = task

        try:
            return await task
        except asyncio.CancelledError:
            # Distinguish "the client took it back" from "we are shutting
            # down", because only the first one is routine.
            if request.id in self._withdrawn:
                raise Withdrawn(request.id) from None
            raise
        finally:
            self._items.pop(request.id, None)
            self._tasks.pop(request.id, None)
            self._withdrawn.discard(request.id)

    def withdraw(self, decision_id: str) -> bool:
        """Cancel a pending decision. True if there was one to cancel."""
        task = self._tasks.get(decision_id)
        if task is None:
            return False
        self._withdrawn.add(decision_id)
        return task.cancel()

    def snapshot(self) -> list[DecisionRequest]:
        """What is outstanding right now. This is what the popup will render."""
        return list(self._items.values())

    def __len__(self) -> int:
        return len(self._items)
