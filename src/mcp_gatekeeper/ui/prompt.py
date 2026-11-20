"""The approval popup, and the bridge between it and the asyncio loop.

Tk insists on owning the main thread and asyncio wants a loop of its own, so
they run in separate threads and talk over two one-way channels:

    asyncio -> Tk    a thread-safe queue the window polls with root.after()
    Tk -> asyncio    loop.call_soon_threadsafe, to resolve the waiting future

Nothing on the Tk side is awaited and nothing on the asyncio side touches a
widget. That rule is what keeps this from becoming the usual Tk-and-threads
horror story.

The window shows one request at a time with a "1 of 3" counter rather than
stacking dialogs: stacked modals are how people click the wrong button.
"""

from __future__ import annotations

import asyncio
import queue
import time
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import ttk
from typing import cast

from mcp_gatekeeper.ipc import Action, DecisionRequest, Scope, Verdict
from mcp_gatekeeper.ui import display

TITLE = "MCP Gatekeeper"


@dataclass
class Prompt:
    """One held request, waiting on a click."""

    request: DecisionRequest
    future: asyncio.Future[Verdict]
    loop: asyncio.AbstractEventLoop
    shown_at: float | None = field(default=None)

    @property
    def settled(self) -> bool:
        """True once somebody answered, or it was withdrawn or timed out."""
        return self.future.done()

    def answer(self, action: Action, scope: Scope = Scope.ONCE) -> None:
        """Resolve this prompt. Safe to call from the Tk thread."""
        verdict = Verdict(action=action, scope=scope)
        self.loop.call_soon_threadsafe(self._resolve, verdict)

    def _resolve(self, verdict: Verdict) -> None:
        # Back on the asyncio thread by now. The future may already be done if
        # the request was withdrawn or timed out while the dialog was open.
        if not self.future.done():
            self.future.set_result(verdict)


class PromptQueue:
    """Thread-safe handoff from the asyncio loop to the window."""

    def __init__(self) -> None:
        self._queue: queue.Queue[Prompt] = queue.Queue()

    def submit(self, prompt: Prompt) -> None:
        self._queue.put(prompt)

    def drain(self) -> list[Prompt]:
        """Take everything waiting, without blocking."""
        taken: list[Prompt] = []
        while True:
            try:
                taken.append(self._queue.get_nowait())
            except queue.Empty:
                return taken


class UiDecider:
    """A Decider that asks the person at the keyboard."""

    def __init__(self, prompts: PromptQueue) -> None:
        self._prompts: PromptQueue = prompts

    async def __call__(self, request: DecisionRequest) -> Verdict:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Verdict] = loop.create_future()
        self._prompts.submit(Prompt(request=request, future=future, loop=loop))
        return await future


class PromptWindow:
    """The popup. Runs on the main thread and polls for work."""

    def __init__(
        self,
        prompts: PromptQueue,
        *,
        poll_ms: int = 100,
        timeout_seconds: float = 45.0,
    ) -> None:
        self._prompts: PromptQueue = prompts
        self._poll_ms: int = poll_ms
        self._timeout: float = timeout_seconds

        self._waiting: list[Prompt] = []
        self._current: Prompt | None = None
        self._stopping: bool = False

        self._root: tk.Tk = tk.Tk()
        self._build()
        self._root.withdraw()

    # -- construction -----------------------------------------------------

    def _build(self) -> None:
        root = self._root
        root.title(TITLE)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.resizable(width=False, height=False)

        frame = ttk.Frame(root, padding=16)
        frame.grid(sticky="nsew")

        self._headline: ttk.Label = ttk.Label(frame, text="", font=("TkDefaultFont", 12, "bold"))
        self._headline.grid(row=0, column=0, columnspan=2, sticky="w")

        self._subtitle: ttk.Label = ttk.Label(frame, text="")
        self._subtitle.grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 10))

        ttk.Label(frame, text="Arguments").grid(row=2, column=0, sticky="w")

        # Read-only, fixed size, plain text. Never a rendered widget: these
        # are someone else's strings.
        self._arguments: tk.Text = tk.Text(frame, width=64, height=12, wrap="word")
        self._arguments.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=(2, 10))
        self._arguments.configure(state="disabled")

        self._footer: ttk.Label = ttk.Label(frame, text="")
        self._footer.grid(row=4, column=0, sticky="w")

        buttons = ttk.Frame(frame)
        buttons.grid(row=4, column=1, sticky="e")

        self._block_button: ttk.Button = ttk.Button(buttons, text="Block", command=self._on_block)
        self._block_button.grid(row=0, column=0, padx=(0, 8))

        self._allow_button: ttk.Button = ttk.Button(buttons, text="Allow", command=self._on_allow)
        self._allow_button.grid(row=0, column=1)

        # Escape blocks. There is deliberately no Return binding for Allow:
        # the safe action is the one you can reach without looking.
        root.bind("<Escape>", lambda _event: self._on_block())

    # -- lifecycle --------------------------------------------------------

    def run(self) -> None:
        self._root.after(self._poll_ms, self.poll)
        self._root.mainloop()

    def close(self) -> None:
        """Tear the window down. Used by tests and at shutdown."""
        self._root.destroy()

    @property
    def headline(self) -> str:
        """The question currently on screen, for tests."""
        return cast("str", self._headline.cget("text"))

    @property
    def subtitle(self) -> str:
        return cast("str", self._subtitle.cget("text"))

    @property
    def arguments(self) -> str:
        return self._arguments.get("1.0", "end")

    def request_stop(self) -> None:
        """Ask the window to close. Safe from another thread."""
        self._stopping = True

    # -- polling ----------------------------------------------------------

    def poll(self) -> None:
        """Run one polling pass.

        Public because driving a single pass by hand is the only sane way to
        test rendering without a mainloop.
        """
        if self._stopping:
            self._root.quit()
            return

        self._waiting.extend(self._prompts.drain())

        # Drop anything resolved elsewhere: withdrawn by the client, or timed
        # out by the daemon while this dialog sat open.
        self._waiting = [prompt for prompt in self._waiting if not prompt.settled]
        if self._current is not None and self._current.settled:
            self._current = None

        if self._current is None and self._waiting:
            self._show(self._waiting[0])
        elif self._current is None:
            self._root.withdraw()

        self._refresh_footer()
        self._root.after(self._poll_ms, self.poll)

    # -- rendering --------------------------------------------------------

    def _show(self, prompt: Prompt) -> None:
        self._current = prompt
        prompt.shown_at = time.monotonic()

        request = prompt.request
        label = request.tool or request.method
        direction = "server -> Claude" if request.direction == "s2c" else "Claude -> server"

        self._headline.configure(text=f"Allow {label}?")
        self._subtitle.configure(text=f"{request.server}  |  {request.method}  |  {direction}")

        self._arguments.configure(state="normal")
        self._arguments.delete("1.0", "end")
        self._arguments.insert("1.0", display.to_text(request.params))
        self._arguments.configure(state="disabled")

        self._root.deiconify()
        # typeshed's tkinter stubs leave these partially unknown.
        self._root.attributes("-topmost", True)  # pyright: ignore[reportUnknownMemberType]
        self._root.lift()  # pyright: ignore[reportUnknownMemberType]
        self._root.focus_force()
        # Focus the safe button, so a stray Return or Space blocks.
        self._block_button.focus_set()

    def _refresh_footer(self) -> None:
        prompt = self._current
        if prompt is None:
            self._footer.configure(text="")
            return

        parts = [f"#{display.digest(prompt.request.params)}"]

        outstanding = len(self._waiting)
        if outstanding > 1:
            parts.append(f"1 of {outstanding}")

        if prompt.shown_at is not None:
            left = self._timeout - (time.monotonic() - prompt.shown_at)
            parts.append(f"blocks in {max(0, int(left))}s")

        self._footer.configure(text="   ".join(parts))

    # -- actions ----------------------------------------------------------

    def _answer(self, action: Action) -> None:
        prompt = self._current
        if prompt is None:
            return
        prompt.answer(action)
        self._current = None
        if prompt in self._waiting:
            self._waiting.remove(prompt)

        # Show the next one straight away rather than waiting for a tick.
        if self._waiting:
            self._show(self._waiting[0])
        else:
            self._root.withdraw()
        self._refresh_footer()

    def _on_allow(self) -> None:
        self._answer(Action.ALLOW)

    def _on_block(self) -> None:
        self._answer(Action.BLOCK)

    def _on_close(self) -> None:
        # Closing the window is not consent.
        self._answer(Action.BLOCK)
