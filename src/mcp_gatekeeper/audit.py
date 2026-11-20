"""The record of what was asked for and what was decided.

Append-only JSONL, one object per decision, written as it happens. Every
decision lands here, including the ones a rule answered without asking --
those are precisely the ones you would otherwise have no record of.

What goes in is deliberately bounded: the full hash of the arguments plus a
short redacted preview, never the raw payload. That keeps the file from growing
without limit and keeps credentials out of a plain-text log that lives on disk
for months.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import cast

from mcp_gatekeeper.ipc import DecisionRequest, Verdict
from mcp_gatekeeper.protocol import JSONObject
from mcp_gatekeeper.ui import display

DEFAULT_MAX_BYTES = 5_000_000
PREVIEW_CHARS = 300


class DecidedBy(StrEnum):
    """Who or what produced a verdict."""

    USER = "user"
    POLICY = "policy"
    SESSION = "session"
    TIMEOUT = "timeout"
    UNAVAILABLE = "daemon-unavailable"


def default_path() -> Path:
    return Path.home() / ".mcp-gatekeeper" / "audit.jsonl"


class AuditLog:
    """Writes decision records, and reads the tail back for `log`."""

    def __init__(self, path: Path, *, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self.path: Path = path
        self.max_bytes: int = max_bytes

    def _prepare(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.path.exists():
            # Create it ourselves so the mode is right from the first write
            # rather than whatever the umask happens to be.
            self.path.touch(mode=0o600)
        else:
            self._rotate_if_needed()

    def _rotate_if_needed(self) -> None:
        if self.path.stat().st_size < self.max_bytes:
            return
        backup = self.path.with_suffix(self.path.suffix + ".1")
        os.replace(self.path, backup)
        self.path.touch(mode=0o600)

    def record(
        self,
        request: DecisionRequest,
        verdict: Verdict,
        *,
        decided_by: DecidedBy,
        latency_ms: int,
    ) -> JSONObject:
        """Append one decision. Returns the entry, which makes testing simple."""
        preview = display.to_text(request.params)

        entry: JSONObject = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "server": request.server,
            "direction": request.direction,
            "method": request.method,
            "tool": request.tool,
            "request_id": request.request_id,
            "decision_id": request.id,
            "params_sha256": display.digest(request.params, length=64),
            "params_preview": display.truncate(preview, PREVIEW_CHARS),
            "verdict": verdict.action.value,
            "scope": verdict.scope.value,
            "decided_by": decided_by.value,
            "latency_ms": latency_ms,
        }

        self._prepare()
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, separators=(",", ":")) + "\n")

        return entry

    def tail(self, limit: int = 20) -> list[JSONObject]:
        """The most recent entries, oldest first. Unreadable lines are skipped."""
        if not self.path.exists():
            return []

        lines = self.path.read_text(encoding="utf-8").splitlines()
        entries: list[JSONObject] = []
        for line in lines[-limit:]:
            try:
                parsed = cast(object, json.loads(line))
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                entries.append(cast(JSONObject, parsed))
        return entries
