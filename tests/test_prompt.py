"""Tests for the popup's bridge to the asyncio loop.

The window itself needs a display, so only one test here touches Tk. The part
worth testing anyway is the handoff: a request goes onto a queue, something
else answers it, and the waiting coroutine wakes up with the right verdict.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from mcp_gatekeeper.daemon.decisions import Decider, with_timeout
from mcp_gatekeeper.ipc import Action, DecisionRequest, Scope, Verdict
from mcp_gatekeeper.ui.prompt import PromptQueue, UiDecider


def a_request(tool: str = "delete_file") -> DecisionRequest:
    return DecisionRequest(
        id=DecisionRequest.new_id(),
        server="demo",
        direction="c2s",
        method="tools/call",
        tool=tool,
        params={"path": "/home/me/important.docx"},
        request_id=1,
    )


async def ask(decider: Decider, request: DecisionRequest) -> Verdict:
    """Adapter: Decider returns an Awaitable, create_task wants a Coroutine."""
    return await decider(request)


def test_queue_drains_everything_without_blocking():
    prompts = PromptQueue()
    assert prompts.drain() == []


async def test_a_prompt_resolves_when_answered():
    prompts = PromptQueue()
    asking = asyncio.create_task(UiDecider(prompts)(a_request()))
    await asyncio.sleep(0)

    (prompt,) = prompts.drain()
    assert not prompt.settled
    prompt.answer(Action.ALLOW)

    verdict = await asking
    assert verdict.allowed
    assert verdict.scope is Scope.ONCE
    assert prompt.settled


async def test_blocking_is_reported_as_blocking():
    prompts = PromptQueue()
    asking = asyncio.create_task(UiDecider(prompts)(a_request()))
    await asyncio.sleep(0)

    (prompt,) = prompts.drain()
    prompt.answer(Action.BLOCK)

    assert not (await asking).allowed


async def test_two_prompts_are_answered_independently():
    # Two dialogs outstanding must not get each other's answers.
    prompts = PromptQueue()
    decider = UiDecider(prompts)
    first = asyncio.create_task(decider(a_request("delete_file")))
    second = asyncio.create_task(decider(a_request("send_email")))
    await asyncio.sleep(0)

    pending = {prompt.request.tool: prompt for prompt in prompts.drain()}
    assert set(pending) == {"delete_file", "send_email"}

    pending["delete_file"].answer(Action.BLOCK)
    pending["send_email"].answer(Action.ALLOW)

    assert not (await first).allowed
    assert (await second).allowed


async def test_answering_twice_is_harmless():
    prompts = PromptQueue()
    asking = asyncio.create_task(UiDecider(prompts)(a_request()))
    await asyncio.sleep(0)

    (prompt,) = prompts.drain()
    prompt.answer(Action.ALLOW)
    prompt.answer(Action.BLOCK)  # a double click, or a race with a timeout

    assert (await asking).allowed


async def test_an_unanswered_prompt_fails_closed():
    decider = with_timeout(UiDecider(PromptQueue()), 0.2, Action.BLOCK)
    verdict = await decider(a_request())

    assert not verdict.allowed
    assert "nobody answered" in (verdict.reason or "")


async def test_a_timeout_dismisses_the_dialog():
    # The window watches `settled` to know when to close itself, so the
    # timeout has to mark the prompt, not just return a verdict.
    prompts = PromptQueue()
    decider = with_timeout(UiDecider(prompts), 0.2, Action.BLOCK)

    asking = asyncio.create_task(ask(decider, a_request()))
    await asyncio.sleep(0.05)
    (prompt,) = prompts.drain()
    assert not prompt.settled

    await asking
    assert prompt.settled


async def test_a_prompt_answered_in_time_is_not_timed_out():
    prompts = PromptQueue()
    decider = with_timeout(UiDecider(prompts), 5.0, Action.BLOCK)

    asking = asyncio.create_task(ask(decider, a_request()))
    await asyncio.sleep(0.05)
    (prompt,) = prompts.drain()
    prompt.answer(Action.ALLOW)

    assert (await asking).allowed


@pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="needs a display")
def test_the_window_renders_a_prompt():
    from mcp_gatekeeper.ui.prompt import Prompt, PromptWindow

    loop = asyncio.new_event_loop()
    try:
        prompts = PromptQueue()
        window = PromptWindow(prompts, timeout_seconds=30.0)
        try:
            prompts.submit(Prompt(request=a_request(), future=loop.create_future(), loop=loop))
            window.poll()

            assert "delete_file" in window.headline
            assert "demo" in window.subtitle
            assert "important.docx" in window.arguments
        finally:
            window.close()
    finally:
        loop.close()
