"""Tests for what the dialog is allowed to render.

Arguments come from whoever wrote the MCP server, so every one of these is
really a "can a hostile payload break or abuse the dialog" test.
"""

from __future__ import annotations

from mcp_gatekeeper.ui import display


def test_strips_control_characters():
    assert display.sanitise("a" + chr(0) + "b") == "a" + display.PLACEHOLDER + "b"
    assert display.sanitise("a" + chr(0x2028) + "b") == "a" + display.PLACEHOLDER + "b"


def test_keeps_tabs_and_newlines():
    # These are legitimate inside a rendered payload.
    assert display.sanitise("a\tb\nc") == "a\tb\nc"


def test_truncate_reports_how_much_was_hidden():
    out = display.truncate("x" * 50, 10)
    assert out.startswith("x" * 10)
    assert "40 more characters" in out


def test_truncate_leaves_short_text_alone():
    assert display.truncate("short", 100) == "short"


def test_redacts_anything_that_looks_like_a_credential():
    params = {
        "api_key": "sk-123",
        "apiKey": "sk-123",
        "Authorization": "Bearer x",
        "password": "hunter2",
        "session_token": "abc",
        "path": "/home/me/notes.md",
    }
    out = display.redact(params)
    assert isinstance(out, dict)

    assert out["path"] == "/home/me/notes.md"
    for key in ("api_key", "apiKey", "Authorization", "password", "session_token"):
        assert out[key] == display.REDACTED, key


def test_redacts_inside_nested_structures():
    out = display.redact({"outer": {"items": [{"token": "secret"}]}})
    assert display.REDACTED in repr(out)
    assert "secret" not in repr(out)


def test_caps_very_long_values():
    out = display.redact({"blob": "y" * 5000})
    assert isinstance(out, dict)
    assert len(str(out["blob"])) < 500


def test_caps_long_lists():
    out = display.redact({"many": list(range(500))})
    assert isinstance(out, dict)
    assert isinstance(out["many"], list)
    assert len(out["many"]) == 20


def test_stops_recursing_on_deeply_nested_input():
    # A payload nested a thousand deep should not take the dialog down.
    nested: dict[str, object] = {"end": 1}
    for _ in range(50):
        nested = {"next": nested}
    assert "..." in repr(display.redact(nested))


def test_digest_is_stable_and_order_independent():
    first = display.digest({"a": 1, "b": 2})
    second = display.digest({"b": 2, "a": 1})
    assert first == second
    assert first != display.digest({"a": 1, "b": 3})


def test_digest_survives_values_json_cannot_encode():
    assert len(display.digest({"weird": object()})) == 12


def test_to_text_handles_no_arguments():
    assert display.to_text({}) == "(no arguments)"


def test_to_text_is_bounded():
    # The attack this blocks: megabytes of padding pushing Block off screen.
    out = display.to_text({"blob": "z" * 100_000})
    assert len(out) <= display.MAX_TOTAL_CHARS + 100


def test_to_text_redacts():
    out = display.to_text({"api_key": "sk-live-123"})
    assert "sk-live-123" not in out
    assert display.REDACTED in out
