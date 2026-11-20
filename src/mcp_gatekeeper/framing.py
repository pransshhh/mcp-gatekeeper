"""Newline-delimited JSON framing.

One JSON object per line, UTF-8. Shared by the MCP proxy and the daemon's own
IPC -- two unrelated protocols that happen to agree on how a frame ends, which
is the only thing they have in common at this level.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable

# Nothing legitimate comes close. It exists so a broken or hostile peer cannot
# make us buffer without limit.
MAX_LINE_BYTES = 8 * 1024 * 1024


def encode(payload: object) -> bytes:
    """Serialise one message to a frame.

    json.dumps escapes newlines inside strings, so a frame can never contain a
    stray newline and desync whoever is reading.
    """
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")


async def iter_lines(
    reader: asyncio.StreamReader,
    *,
    on_drop: Callable[[str], None] | None = None,
) -> AsyncIterator[bytes]:
    """Yield complete frames from a stream until EOF.

    An oversized line is skipped rather than fatal: we resync at the next
    newline and report it through `on_drop`. One hostile message should not
    take down the session.
    """
    while True:
        try:
            line = await reader.readuntil(b"\n")
        except asyncio.IncompleteReadError as exc:
            # EOF. Anything buffered without a trailing newline is still a
            # frame as far as the peer is concerned.
            if exc.partial.strip():
                yield exc.partial
            return
        except asyncio.LimitOverrunError as exc:
            await _skip_to_newline(reader)
            if on_drop is not None:
                on_drop(f"dropped oversized line (over {exc.consumed} bytes)")
            continue

        if line.strip():
            yield line


async def _skip_to_newline(reader: asyncio.StreamReader) -> None:
    """Discard bytes up to and including the next frame boundary.

    The care here is deliberate. LimitOverrunError.consumed is the offset of
    the separator when one was found, or the buffer size when it was not.
    Dropping exactly that much and retrying resyncs us on the newline; reading
    a fixed chunk instead swallows the *next* frame along with the bad one,
    which looks fine until you test for it.
    """
    while True:
        try:
            await reader.readuntil(b"\n")
            return
        except asyncio.LimitOverrunError as exc:
            await reader.readexactly(exc.consumed)
        except asyncio.IncompleteReadError:
            return
