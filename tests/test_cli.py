"""Tests for the command line.

The locator is tested on its own; these check the glue, and above all that
install followed by uninstall leaves the config exactly as it was. A failed
experiment must not be able to leave Claude Desktop with no working servers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from mcp_gatekeeper import cli, locator
from mcp_gatekeeper.protocol import JSONObject

ORIGINAL: JSONObject = {
    "mcpServers": {
        "demo": {"command": "python3", "args": ["server.py"]},
        "filesystem": {
            "command": "npx",
            "args": ["-y", "server-filesystem", "/home/me"],
            "env": {"TOKEN": "x"},
        },
    },
    "preferences": {"keepMe": True},
}


def a_config(tmp_path: Path, document: JSONObject | None = None) -> Path:
    path = tmp_path / "claude_desktop_config.json"
    path.write_text(json.dumps(document if document is not None else ORIGINAL, indent=2))
    return path


def servers_in(path: Path) -> JSONObject:
    return locator.servers(locator.load_config(path))


def entry_of(path: Path, name: str) -> JSONObject:
    """One server entry, narrowed so it can actually be indexed."""
    value = servers_in(path)[name]
    assert isinstance(value, dict)
    return cast(JSONObject, value)


def test_install_wraps_every_server(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    path = a_config(tmp_path)

    assert cli.main(["install", "--config", str(path)]) == 0

    assert all(locator.is_wrapped(entry_of(path, name)) for name in servers_in(path))
    assert "Wrapped: demo, filesystem" in capsys.readouterr().out


def test_install_leaves_unrelated_settings_alone(tmp_path: Path):
    path = a_config(tmp_path)
    cli.main(["install", "--config", str(path)])

    assert locator.load_config(path)["preferences"] == {"keepMe": True}


def test_install_is_idempotent(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    path = a_config(tmp_path)
    cli.main(["install", "--config", str(path)])
    first = path.read_text()
    capsys.readouterr()

    assert cli.main(["install", "--config", str(path)]) == 0

    assert path.read_text() == first
    assert "Already wrapped" in capsys.readouterr().out


def test_uninstall_restores_the_original_exactly(tmp_path: Path):
    # The property that makes installing safe to try.
    path = a_config(tmp_path)
    before = json.loads(path.read_text())

    cli.main(["install", "--config", str(path)])
    cli.main(["uninstall", "--config", str(path)])

    assert json.loads(path.read_text()) == before


def test_only_wraps_the_servers_you_name(tmp_path: Path):
    path = a_config(tmp_path)

    cli.main(["install", "--config", str(path), "--only", "demo"])

    assert locator.is_wrapped(entry_of(path, "demo"))
    assert not locator.is_wrapped(entry_of(path, "filesystem"))


def test_install_pins_the_socket_when_asked(tmp_path: Path):
    path = a_config(tmp_path)
    cli.main(["install", "--config", str(path), "--socket", "/run/gk.sock"])

    args = entry_of(path, "demo")["args"]
    assert isinstance(args, list)
    assert "/run/gk.sock" in args


def test_install_says_so_when_there_is_nothing_to_wrap(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    path = a_config(tmp_path, {"preferences": {}})

    assert cli.main(["install", "--config", str(path)]) == 1
    assert "no mcpServers" in capsys.readouterr().out


def test_install_skips_a_malformed_entry_without_dying(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    path = a_config(
        tmp_path,
        {"mcpServers": {"good": {"command": "x"}, "bad": {"args": ["no command"]}}},
    )

    assert cli.main(["install", "--config", str(path)]) == 0

    out = capsys.readouterr().out
    assert "Wrapped: good" in out
    assert "Could not wrap" in out
    assert locator.is_wrapped(entry_of(path, "good"))


def test_uninstall_on_an_untouched_config_is_a_no_op(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    path = a_config(tmp_path)

    assert cli.main(["uninstall", "--config", str(path)]) == 0
    assert "Nothing wrapped" in capsys.readouterr().out


def test_status_reports_what_is_wrapped(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    path = a_config(tmp_path)
    cli.main(["install", "--config", str(path), "--only", "demo"])
    capsys.readouterr()

    assert cli.main(["status", "--config", str(path), "--socket", str(tmp_path / "x.sock")]) == 0

    out = capsys.readouterr().out
    assert "demo" in out
    assert "wrapped" in out
    assert "MISSING" in out  # no daemon running


def test_a_missing_config_is_an_error_not_a_crash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    assert cli.main(["status", "--config", str(tmp_path / "nope.json")]) == 1
    assert "error: no config at" in capsys.readouterr().out


def test_log_is_calm_when_there_is_nothing_to_show(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    assert cli.main(["log", "--audit", str(tmp_path / "audit.jsonl")]) == 0
    assert "No decisions recorded" in capsys.readouterr().out


def test_init_writes_a_starter_rules_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    path = tmp_path / "policy.yaml"

    assert cli.main(["init", "--policy", str(path)]) == 0

    body = path.read_text()
    assert "default: ask" in body
    # The wildcard warning is the point of shipping an example at all.
    assert "Quote every wildcard" in body
    capsys.readouterr()

    # Running it twice must not clobber what someone has edited.
    path.write_text("default: block\n")
    assert cli.main(["init", "--policy", str(path)]) == 0
    assert path.read_text() == "default: block\n"


def test_doctor_runs(capsys: pytest.CaptureFixture[str]):
    assert cli.main(["doctor"]) == 0
    assert "config candidates" in capsys.readouterr().out
