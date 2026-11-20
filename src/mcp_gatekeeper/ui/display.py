"""Turning a held request into something safe to put on screen.

Arguments are attacker-controlled. A tool call can carry prompt injection
aimed at whoever reads the dialog, control characters that scramble the layout,
or megabytes of whitespace to push the Block button off the screen. Everything
here exists to make that harmless: strip, truncate, redact, and show a digest
so a repeat of the same call is recognisable.

Pure functions only, so all of this is testable without a display.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import cast

from mcp_gatekeeper.protocol import JSONObject

# Per-value and whole-dialog limits. Generous enough to read a real path or
# query, small enough that nothing can shove the buttons out of view.
MAX_VALUE_CHARS = 300
MAX_TOTAL_CHARS = 2000

REDACTED = "***"
PLACEHOLDER = chr(0xFFFD)

SECRET_KEY = re.compile(
    r"(?i)(token|secret|password|passwd|passphrase|authorization|api[_-]?key|credential|cookie)"
)

# C0 controls except tab and newline, then the C1 range, then LINE SEPARATOR
# and PARAGRAPH SEPARATOR -- which reflow a label in surprising ways. Built
# with chr() so this source file stays plain ASCII.
_CONTROL_RANGES = "\\x00-\\x08\\x0b-\\x1f\\x7f-\\x9f"
CONTROL_CHARS = re.compile("[" + _CONTROL_RANGES + chr(0x2028) + chr(0x2029) + "]")


def sanitise(text: str) -> str:
    """Make a string safe to render as a flat label."""
    return CONTROL_CHARS.sub(PLACEHOLDER, text)


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [{len(text) - limit} more characters]"


def redact(value: object, *, _depth: int = 0) -> object:
    """Replace anything that looks like a credential, recursively."""
    if _depth > 6:
        return "..."

    if isinstance(value, dict):
        source = cast("dict[object, object]", value)
        out: dict[str, object] = {}
        for raw_key, raw_value in source.items():
            key = str(raw_key)
            out[key] = REDACTED if SECRET_KEY.search(key) else redact(raw_value, _depth=_depth + 1)
        return out

    if isinstance(value, list):
        # Capped: a thousand-element array is not something anyone reads off a
        # dialog, and rendering it is how the buttons end up off screen.
        items = cast("list[object]", value)
        return [redact(item, _depth=_depth + 1) for item in items[:20]]

    if isinstance(value, str):
        return truncate(sanitise(value), MAX_VALUE_CHARS)

    return value


DIGEST_CHARS = 12


def digest(params: JSONObject, *, length: int = DIGEST_CHARS) -> str:
    """Stable fingerprint of the *original* arguments.

    Shown short in the dialog so a user can tell "this again" from "something
    new" without reading the whole payload. The audit log stores the full hash,
    and the dialog's 12 characters are its prefix, so the two can be matched up.
    """
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
    full = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return full[:length]


def to_text(params: JSONObject) -> str:
    """Pretty-print arguments for the dialog, redacted and bounded."""
    if not params:
        return "(no arguments)"

    safe = redact(params)
    try:
        rendered = json.dumps(safe, indent=2, sort_keys=True, default=str)
    except (TypeError, ValueError):
        rendered = repr(safe)

    return truncate(sanitise(rendered), MAX_TOTAL_CHARS)
