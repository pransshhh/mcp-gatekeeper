"""Tests for the decision log."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from mcp_gatekeeper.audit import AuditLog, DecidedBy
from mcp_gatekeeper.ipc import Action, DecisionRequest, Scope, Verdict


def a_request(tool: str = "delete_file", **params: object) -> DecisionRequest:
    return DecisionRequest(
        id=DecisionRequest.new_id(),
        server="demo",
        direction="c2s",
        method="tools/call",
        tool=tool,
        params=params or {"path": "/home/me/important.docx"},
        request_id=4,
    )


def test_records_a_decision(tmp_path: Path):
    log = AuditLog(tmp_path / "audit.jsonl")
    entry = log.record(
        a_request(),
        Verdict(action=Action.BLOCK),
        decided_by=DecidedBy.USER,
        latency_ms=2143,
    )

    assert entry["verdict"] == "block"
    assert entry["decided_by"] == "user"
    assert entry["tool"] == "delete_file"
    assert entry["latency_ms"] == 2143
    assert entry["scope"] == Scope.ONCE.value


def test_the_file_is_readable_only_by_its_owner(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.record(a_request(), Verdict(action=Action.ALLOW), decided_by=DecidedBy.POLICY, latency_ms=0)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_entries_are_one_json_object_per_line(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    for _ in range(3):
        log.record(
            a_request(), Verdict(action=Action.ALLOW), decided_by=DecidedBy.USER, latency_ms=1
        )

    lines = path.read_text().splitlines()
    assert len(lines) == 3
    for line in lines:
        assert isinstance(json.loads(line), dict)


def test_arguments_are_stored_as_a_hash_and_a_preview(tmp_path: Path):
    log = AuditLog(tmp_path / "audit.jsonl")
    entry = log.record(
        a_request(path="/home/me/notes.md"),
        Verdict(action=Action.ALLOW),
        decided_by=DecidedBy.USER,
        latency_ms=1,
    )

    assert len(str(entry["params_sha256"])) == 64
    assert "notes.md" in str(entry["params_preview"])


def test_secrets_never_reach_the_log(tmp_path: Path):
    # This file sits on disk for months. A token in it is a token leaked.
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.record(
        a_request("send_email", api_key="sk-live-SECRET"),
        Verdict(action=Action.ALLOW),
        decided_by=DecidedBy.USER,
        latency_ms=1,
    )

    assert "sk-live-SECRET" not in path.read_text()


def test_a_huge_payload_does_not_bloat_the_log(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.record(
        a_request(blob="x" * 200_000),
        Verdict(action=Action.ALLOW),
        decided_by=DecidedBy.USER,
        latency_ms=1,
    )

    assert path.stat().st_size < 2000


def test_rotates_when_it_gets_too_big(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, max_bytes=400)

    for _ in range(10):
        log.record(
            a_request(), Verdict(action=Action.ALLOW), decided_by=DecidedBy.USER, latency_ms=1
        )

    backup = path.with_suffix(".jsonl.1")
    assert backup.exists()
    assert path.stat().st_size < 1200


def test_tail_returns_recent_entries_oldest_first(tmp_path: Path):
    log = AuditLog(tmp_path / "audit.jsonl")
    for index in range(5):
        log.record(
            a_request(f"tool{index}"),
            Verdict(action=Action.ALLOW),
            decided_by=DecidedBy.USER,
            latency_ms=index,
        )

    recent = log.tail(limit=3)
    assert [entry["tool"] for entry in recent] == ["tool2", "tool3", "tool4"]


def test_tail_on_a_missing_file_is_empty(tmp_path: Path):
    assert AuditLog(tmp_path / "nothing.jsonl").tail() == []


def test_tail_skips_a_corrupted_line(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.record(a_request(), Verdict(action=Action.ALLOW), decided_by=DecidedBy.USER, latency_ms=1)

    with path.open("a") as handle:
        handle.write("{not json\n")

    assert len(log.tail()) == 1
