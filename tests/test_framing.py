"""Tests for newline-delimited JSON framing."""

from __future__ import annotations

import asyncio
import json

from mcp_gatekeeper import framing


def test_encode_never_emits_a_stray_newline():
    # A newline inside a string would split one frame into two and desync
    # whoever is reading.
    blob = framing.encode({"text": "line one\nline two"})
    assert blob.endswith(b"\n")
    assert blob.count(b"\n") == 1


def test_encode_round_trips():
    payload = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert json.loads(framing.encode(payload)) == payload


async def _reader_for(data: bytes, limit: int) -> asyncio.StreamReader:
    reader = asyncio.StreamReader(limit=limit)
    reader.feed_data(data)
    reader.feed_eof()
    return reader


async def test_iter_lines_yields_frames():
    reader = await _reader_for(b'{"a":1}\n{"b":2}\n', limit=64 * 1024)
    assert [line async for line in framing.iter_lines(reader)] == [b'{"a":1}\n', b'{"b":2}\n']


async def test_iter_lines_keeps_a_trailing_partial():
    # A peer that dies mid-write still sent us something the other side would
    # consider a frame.
    reader = await _reader_for(b'{"a":1}\n{"b":2}', limit=64 * 1024)
    lines = [line async for line in framing.iter_lines(reader)]
    assert lines[-1] == b'{"b":2}'


async def test_iter_lines_skips_blank_lines():
    reader = await _reader_for(b'\n\n{"a":1}\n\n', limit=64 * 1024)
    assert [line async for line in framing.iter_lines(reader)] == [b'{"a":1}\n']


async def test_iter_lines_skips_oversized_and_recovers():
    # The regression this guards: an earlier version drained a fixed chunk on
    # overflow, which swallowed the *next* frame along with the bad one.
    dropped: list[str] = []
    reader = await _reader_for(b"x" * 5000 + b"\n" + b'{"ok":1}\n', limit=1024)

    lines = [line async for line in framing.iter_lines(reader, on_drop=dropped.append)]

    assert lines == [b'{"ok":1}\n']
    assert len(dropped) == 1


async def test_iter_lines_survives_oversized_line_at_eof():
    dropped: list[str] = []
    reader = await _reader_for(b"x" * 5000, limit=1024)

    assert [line async for line in framing.iter_lines(reader, on_drop=dropped.append)] == []
