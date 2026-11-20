"""Tests for finding and rewriting Claude Desktop's config.

The wrap/unwrap pair is the risky part: getting it wrong either leaves the
gatekeeper silently out of the loop, or leaves Desktop with a config it cannot
run and no MCP servers at all.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from mcp_gatekeeper import locator
from mcp_gatekeeper.locator import LocatorError
from mcp_gatekeeper.protocol import JSONObject

FILESYSTEM_ARGS = ["-y", "@modelcontextprotocol/server-filesystem", "/home/me"]
FILESYSTEM: JSONObject = {"command": "npx", "args": FILESYSTEM_ARGS}


def write_config(path: Path, document: JSONObject) -> Path:
    path.write_text(json.dumps(document, indent=2))
    return path


# --------------------------------------------------------------------------
# wrapping
# --------------------------------------------------------------------------


def test_wrapping_puts_the_original_command_after_a_separator():
    wrapped = locator.wrap(FILESYSTEM, server_name="filesystem", launcher=["python3"])

    assert wrapped["command"] == "python3"
    args = wrapped["args"]
    assert isinstance(args, list)
    assert args[:4] == ["-m", locator.MODULE, "--server-name", "filesystem"]
    assert args[args.index("--") + 1 :] == ["npx", *FILESYSTEM_ARGS]


def test_wrapping_can_pin_the_socket():
    wrapped = locator.wrap(
        FILESYSTEM, server_name="fs", launcher=["python3"], socket=Path("/run/gk.sock")
    )
    args = wrapped["args"]
    assert isinstance(args, list)
    assert "--socket" in args
    assert "/run/gk.sock" in args


def test_wrapping_is_idempotent():
    # Running install twice must not nest interceptors.
    once = locator.wrap(FILESYSTEM, server_name="fs", launcher=["python3"])
    twice = locator.wrap(once, server_name="fs", launcher=["python3"])
    assert once == twice


def test_wrapping_keeps_other_keys():
    entry: JSONObject = dict(FILESYSTEM)
    entry["env"] = {"TOKEN": "x"}
    wrapped = locator.wrap(entry, server_name="fs", launcher=["python3"])
    assert wrapped["env"] == {"TOKEN": "x"}


def test_wrapping_an_entry_with_no_command_is_refused():
    with pytest.raises(LocatorError, match="no command"):
        locator.wrap({"args": ["x"]}, server_name="broken", launcher=["python3"])


def test_a_command_with_no_arguments_wraps():
    wrapped = locator.wrap({"command": "my-server"}, server_name="s", launcher=["python3"])
    args = wrapped["args"]
    assert isinstance(args, list)
    assert args[-1] == "my-server"


# --------------------------------------------------------------------------
# unwrapping
# --------------------------------------------------------------------------


def test_unwrap_restores_the_original_exactly():
    wrapped = locator.wrap(FILESYSTEM, server_name="fs", launcher=["python3"])
    assert locator.unwrap(wrapped) == FILESYSTEM


def test_unwrap_restores_through_a_wsl_launcher():
    launcher = ["wsl.exe", "-d", "Ubuntu", "--", "/home/me/.venv/bin/python"]
    wrapped = locator.wrap(FILESYSTEM, server_name="fs", launcher=launcher)
    assert locator.unwrap(wrapped) == FILESYSTEM


def test_unwrap_drops_args_when_there_were_none():
    wrapped = locator.wrap({"command": "my-server"}, server_name="s", launcher=["python3"])
    assert locator.unwrap(wrapped) == {"command": "my-server"}


def test_unwrapping_something_untouched_changes_nothing():
    assert locator.unwrap(FILESYSTEM) == FILESYSTEM


def test_a_mangled_wrapped_entry_is_refused_rather_than_guessed():
    # Better to send someone to the backup than to invent a command.
    broken: JSONObject = {
        "command": "python3",
        "args": ["-m", locator.MODULE, "--server-name", "x"],
    }
    with pytest.raises(LocatorError, match="separator"):
        locator.unwrap(broken)


def test_detects_whether_an_entry_is_wrapped():
    assert not locator.is_wrapped(FILESYSTEM)
    assert locator.is_wrapped(locator.wrap(FILESYSTEM, server_name="fs", launcher=["python3"]))
    assert not locator.is_wrapped({"command": "x"})


# --------------------------------------------------------------------------
# files
# --------------------------------------------------------------------------


def test_reads_the_server_table(tmp_path: Path):
    path = write_config(tmp_path / "c.json", {"mcpServers": {"filesystem": FILESYSTEM}})
    assert list(locator.servers(locator.load_config(path))) == ["filesystem"]


def test_a_config_without_servers_is_fine(tmp_path: Path):
    path = write_config(tmp_path / "c.json", {"preferences": {}})
    assert locator.servers(locator.load_config(path)) == {}


def test_invalid_json_is_reported_clearly(tmp_path: Path):
    path = tmp_path / "c.json"
    path.write_text("{not json")
    with pytest.raises(LocatorError, match="not valid JSON"):
        locator.load_config(path)


def test_saving_keeps_a_backup_of_the_original(tmp_path: Path):
    path = write_config(tmp_path / "c.json", {"mcpServers": {"fs": FILESYSTEM}})

    backup = locator.save_config(path, {"mcpServers": {}})

    assert backup.exists()
    assert json.loads(backup.read_text())["mcpServers"]["fs"] == FILESYSTEM


def test_the_backup_is_never_overwritten(tmp_path: Path):
    # It is the pre-Gatekeeper version. That is the one worth keeping.
    path = write_config(tmp_path / "c.json", {"mcpServers": {"fs": FILESYSTEM}})

    locator.save_config(path, {"mcpServers": {"one": FILESYSTEM}})
    backup = locator.save_config(path, {"mcpServers": {"two": FILESYSTEM}})

    assert "fs" in json.loads(backup.read_text())["mcpServers"]


def test_saving_leaves_no_temporary_file(tmp_path: Path):
    path = write_config(tmp_path / "c.json", {"mcpServers": {}})
    locator.save_config(path, {"mcpServers": {}})
    assert list(tmp_path.glob("*.tmp")) == []


def test_an_explicit_path_is_used_as_given(tmp_path: Path):
    path = write_config(tmp_path / "mine.json", {"mcpServers": {}})
    assert locator.find_config(path) == path


def test_a_missing_explicit_path_is_reported(tmp_path: Path):
    with pytest.raises(LocatorError, match="no config at"):
        locator.find_config(tmp_path / "nope.json")


# --------------------------------------------------------------------------
# launcher
# --------------------------------------------------------------------------


def test_a_native_config_is_launched_directly(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(locator, "is_wsl", lambda: False)
    assert locator.launcher_for(Path.home() / ".config" / "Claude" / "c.json") == [sys.executable]


def test_a_windows_config_from_wsl_goes_back_through_wsl(monkeypatch: pytest.MonkeyPatch):
    # Desktop is a Windows process, so the command it runs has to be one
    # Windows can start.
    monkeypatch.setattr(locator, "is_wsl", lambda: True)
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")

    launcher = locator.launcher_for(Path("/mnt/c/Users/me/AppData/Local/Claude-Data/x/c.json"))

    assert launcher[0] == "wsl.exe"
    assert launcher[1:3] == ["-d", "Ubuntu"]
    assert launcher[-1] == sys.executable


def test_a_linux_config_under_wsl_is_still_launched_directly(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(locator, "is_wsl", lambda: True)
    assert locator.launcher_for(Path("/home/me/.config/Claude/c.json")) == [sys.executable]


def test_a_candidate_we_cannot_stat_is_not_fatal(monkeypatch: pytest.MonkeyPatch):
    # Windows keeps junctions under /mnt/c/Users whose stat raises
    # PermissionError instead of returning False.
    def boom(_self: Path) -> bool:
        raise PermissionError("not for you")

    monkeypatch.setattr(Path, "exists", boom)

    assert locator.exists(Path("/mnt/c/Users/All Users/x")) is False
    with pytest.raises(LocatorError, match="could not find"):
        locator.find_config()
