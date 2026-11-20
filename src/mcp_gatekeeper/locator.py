"""Finding Claude Desktop's config, and putting ourselves in front of its servers.

Desktop runs whatever command each `mcpServers` entry names. Installing means
rewriting that command to launch the interceptor with the original command
after a `--`, so Desktop still thinks it started the real server.

    before: {"command": "npx", "args": ["server-filesystem", "/home/me"]}
    after:  {"command": "python",
             "args": ["-m", "mcp_gatekeeper.interceptor", "--server-name", "filesystem",
                      "--", "npx", "server-filesystem", "/home/me"]}

Nothing extra is stored in the file. Uninstalling reconstructs the original
from everything after the `--`, which means the config stays something Desktop
would have accepted anyway and there is no private bookkeeping to go stale.

The awkward case is WSL: Desktop is a Windows process, so the command it runs
has to be a Windows one. When the config lives under /mnt and we are inside
WSL, the wrapper becomes `wsl.exe -d <distro> -- <linux python> ...` so the
whole MCP stack stays on the Linux side with one boundary crossing at the edge.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from mcp_gatekeeper.protocol import JSONObject

CONFIG_NAME = "claude_desktop_config.json"
MODULE = "mcp_gatekeeper.interceptor"
BACKUP_SUFFIX = ".gatekeeper-backup"
SEPARATOR = "--"


class LocatorError(Exception):
    """The config could not be found, read or written."""


def is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
    except OSError:
        return False


def exists(path: Path) -> bool:
    """exists() that cannot raise.

    The junctions Windows keeps under /mnt/c/Users -- "All Users", "Default
    User" -- raise PermissionError from stat instead of returning False, which
    takes down anything walking the candidate list.
    """
    try:
        return path.exists()
    except OSError:
        return False


def _glob(root: Path, pattern: str) -> list[Path]:
    """glob() that cannot raise, for the same reason."""
    try:
        return sorted(root.glob(pattern))
    except OSError:
        return []


def candidate_paths() -> list[Path]:
    """Where the config might be, best guess first.

    The documented location is %APPDATA%\\Claude, but recent Windows builds
    keep it under %LOCALAPPDATA%\\Claude-Data\\<install-id>\\ instead, so both
    are probed. From WSL we also look across at the Windows side, since that
    is where Desktop actually runs.
    """
    found: list[Path] = []

    def add(path: Path) -> None:
        if path not in found:
            found.append(path)

    appdata = os.environ.get("APPDATA")
    if appdata:
        add(Path(appdata) / "Claude" / CONFIG_NAME)

    local = os.environ.get("LOCALAPPDATA")
    if local:
        found.extend(_glob(Path(local), f"Claude-Data/*/{CONFIG_NAME}"))

    home = Path.home()
    add(home / "Library" / "Application Support" / "Claude" / CONFIG_NAME)
    add(home / ".config" / "Claude" / CONFIG_NAME)

    if is_wsl():
        for user in _glob(Path("/mnt/c/Users"), "*"):
            # Skip the junctions and any profile we cannot read, rather than
            # listing four paths that can never hold a config.
            if not exists(user / "AppData"):
                continue
            found.extend(_glob(user, f"AppData/Local/Claude-Data/*/{CONFIG_NAME}"))
            add(user / "AppData" / "Roaming" / "Claude" / CONFIG_NAME)

    return found


def find_config(explicit: Path | None = None) -> Path:
    """Locate the config, or explain what to pass instead."""
    if explicit is not None:
        if not exists(explicit):
            raise LocatorError(f"no config at {explicit}")
        return explicit

    for candidate in candidate_paths():
        if exists(candidate):
            return candidate

    message = (
        "could not find claude_desktop_config.json. Pass --config with its path; "
        + "Claude Desktop shows it under Settings > Developer."
    )
    raise LocatorError(message)


def load_config(path: Path) -> JSONObject:
    try:
        document = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except OSError as exc:
        raise LocatorError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise LocatorError(f"{path} is not valid JSON: {exc}") from exc

    if not isinstance(document, dict):
        raise LocatorError(f"{path} does not contain a JSON object")
    return cast(JSONObject, document)


def save_config(path: Path, document: JSONObject) -> Path:
    """Write the config back, keeping a one-time backup of the original.

    The backup is never overwritten: it is what the file looked like before
    Gatekeeper first touched it, which is the version worth being able to
    return to.
    """
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if not backup.exists():
        shutil.copy2(path, backup)

    # Write and rename, so an interrupted write cannot leave Desktop with a
    # truncated config and no MCP servers at all.
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return backup


def servers(document: JSONObject) -> JSONObject:
    entry = document.get("mcpServers")
    return cast(JSONObject, entry) if isinstance(entry, dict) else {}


def launcher_for(config_path: Path) -> list[str]:
    """The command Desktop should run to reach our interceptor.

    From WSL, a config on the Windows side needs a Windows-runnable command,
    so we route back in through wsl.exe. Everything inside that -- the
    interceptor and the real MCP server -- then stays on Linux.
    """
    if is_wsl() and str(config_path).startswith("/mnt/"):
        distro = os.environ.get("WSL_DISTRO_NAME", "Ubuntu")
        return ["wsl.exe", "-d", distro, SEPARATOR, sys.executable]
    return [sys.executable]


def is_wrapped(entry: JSONObject) -> bool:
    args = entry.get("args")
    if not isinstance(args, list):
        return False
    return MODULE in cast("list[object]", args)


def wrap(
    entry: JSONObject,
    *,
    server_name: str,
    launcher: list[str],
    socket: Path | None = None,
) -> JSONObject:
    """Put the interceptor in front of one server entry."""
    if is_wrapped(entry):
        return dict(entry)

    command = entry.get("command")
    if not isinstance(command, str) or not command:
        raise LocatorError(f"server {server_name!r} has no command to wrap")

    raw_args = entry.get("args")
    original_args = [str(item) for item in cast("list[object]", raw_args or [])]

    gatekeeper_args = ["-m", MODULE, "--server-name", server_name]
    if socket is not None:
        gatekeeper_args += ["--socket", str(socket)]
    gatekeeper_args += [SEPARATOR, command, *original_args]

    wrapped = dict(entry)
    wrapped["command"] = launcher[0]
    wrapped["args"] = [*launcher[1:], *gatekeeper_args]
    return wrapped


def unwrap(entry: JSONObject) -> JSONObject:
    """Restore a server entry to what it was before wrapping."""
    if not is_wrapped(entry):
        return dict(entry)

    args = [str(item) for item in cast("list[object]", entry.get("args") or [])]
    index = args.index(MODULE)

    try:
        separator = args.index(SEPARATOR, index)
    except ValueError as exc:
        raise LocatorError("wrapped entry has no -- separator; restore the backup") from exc

    original = args[separator + 1 :]
    if not original:
        raise LocatorError("wrapped entry has no original command; restore the backup")

    restored = dict(entry)
    restored["command"] = original[0]
    if len(original) > 1:
        restored["args"] = original[1:]
    else:
        restored.pop("args", None)
    return restored


@dataclass(frozen=True)
class Change:
    """What install or uninstall did to one server."""

    server: str
    changed: bool
    note: str = ""
