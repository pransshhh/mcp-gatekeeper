"""Command line entry point.

    mcp-gatekeeper install      put the interceptor in front of every server
    mcp-gatekeeper uninstall    take it back out
    mcp-gatekeeper status       what is wrapped, and is the daemon up
    mcp-gatekeeper log          recent decisions
    mcp-gatekeeper doctor       where everything is, for when it is not working
    mcp-gatekeeper daemon       run the daemon (forwards its own flags)

This is the one module allowed to print: it is talking to a person at a
terminal. Everything else logs to stderr, because stdout elsewhere carries
protocol data.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

from mcp_gatekeeper import audit as audit_module
from mcp_gatekeeper import ipc, locator
from mcp_gatekeeper import policy as policy_module
from mcp_gatekeeper.audit import AuditLog
from mcp_gatekeeper.daemon.__main__ import main as daemon_main
from mcp_gatekeeper.locator import LocatorError
from mcp_gatekeeper.protocol import JSONObject


def _config_path(raw: str | None) -> Path:
    return locator.find_config(Path(raw) if raw else None)


def _optional_path(raw: str | None) -> Path | None:
    return Path(raw) if raw else None


def cmd_install(args: argparse.Namespace) -> int:
    path = _config_path(cast("str | None", args.config))
    document = locator.load_config(path)
    table = locator.servers(document)

    if not table:
        print(f"{path} has no mcpServers to wrap.")
        print("Add an MCP server in Claude Desktop first, then run install again.")
        return 1

    only = set(cast("list[str]", args.only or []))
    socket = _optional_path(cast("str | None", args.socket))
    launcher = locator.launcher_for(path)

    wrapped: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []

    for name in list(table):
        if only and name not in only:
            continue
        entry = table[name]
        if not isinstance(entry, dict):
            failed.append(f"{name} (not an object)")
            continue

        server = cast(JSONObject, entry)
        if locator.is_wrapped(server):
            skipped.append(name)
            continue

        try:
            table[name] = locator.wrap(server, server_name=name, launcher=launcher, socket=socket)
        except LocatorError as exc:
            failed.append(f"{name} ({exc})")
            continue
        wrapped.append(name)

    if not wrapped:
        print(f"Nothing to do in {path}.")
        if skipped:
            print(f"Already wrapped: {', '.join(skipped)}")
        if failed:
            print(f"Could not wrap: {', '.join(failed)}")
        return 0 if skipped and not failed else 1

    document["mcpServers"] = table
    backup = locator.save_config(path, document)

    print(f"Config:  {path}")
    print(f"Backup:  {backup}")
    print(f"Launch:  {' '.join(launcher)}")
    print(f"Wrapped: {', '.join(wrapped)}")
    if skipped:
        print(f"Already wrapped: {', '.join(skipped)}")
    if failed:
        print(f"Could not wrap: {', '.join(failed)}")
    print()
    print("Now start the daemon, then restart Claude Desktop completely")
    print("(quit it, do not just close the window).")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    path = _config_path(cast("str | None", args.config))
    document = locator.load_config(path)
    table = locator.servers(document)

    restored: list[str] = []
    for name in list(table):
        entry = table[name]
        if not isinstance(entry, dict):
            continue
        server = cast(JSONObject, entry)
        if not locator.is_wrapped(server):
            continue
        table[name] = locator.unwrap(server)
        restored.append(name)

    if not restored:
        print(f"Nothing wrapped in {path}.")
        return 0

    document["mcpServers"] = table
    locator.save_config(path, document)

    print(f"Restored: {', '.join(restored)}")
    print("Restart Claude Desktop completely for this to take effect.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    path = _config_path(cast("str | None", args.config))
    table = locator.servers(locator.load_config(path))

    print(f"Config: {path}")
    if not table:
        print("  (no MCP servers configured)")
    for name in sorted(table):
        entry = table[name]
        state = (
            "wrapped"
            if isinstance(entry, dict) and locator.is_wrapped(cast(JSONObject, entry))
            else "not wrapped"
        )
        print(f"  {name:<24} {state}")

    socket = _optional_path(cast("str | None", args.socket)) or ipc.socket_path()
    # The socket file existing is not proof anyone is listening, but its
    # absence is proof nobody is.
    print()
    print(f"Socket: {socket}  {'present' if socket.exists() else 'MISSING - daemon not running'}")
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    path = _optional_path(cast("str | None", args.audit)) or audit_module.default_path()
    entries = AuditLog(path).tail(cast("int", args.number))

    if not entries:
        print(f"No decisions recorded in {path}.")
        return 0

    print(f"{'time':<25} {'tool':<20} {'verdict':<7} {'by':<10} {'ms':>6}")
    for entry in entries:
        columns = [
            f"{entry.get('ts', '')!s:<25}",
            f"{entry.get('tool', '')!s:<20}",
            f"{entry.get('verdict', '')!s:<7}",
            f"{entry.get('decided_by', '')!s:<10}",
            f"{entry.get('latency_ms', '')!s:>6}",
        ]
        print(" ".join(columns))
    return 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    print("Claude Desktop config candidates:")
    candidates = locator.candidate_paths()
    if not candidates:
        print("  (none - no known location applies on this machine)")
    for candidate in candidates:
        print(f"  [{'x' if locator.exists(candidate) else ' '}] {candidate}")

    print()
    print(f"Running under WSL: {'yes' if locator.is_wsl() else 'no'}")

    socket = ipc.socket_path()
    print(f"Daemon socket:     {socket}  {'present' if socket.exists() else 'missing'}")

    policy_path = policy_module.default_policy_path()
    remembered = policy_module.default_remembered_path()
    audit_path = audit_module.default_path()
    for label, path in (
        ("Rules file", policy_path),
        ("Remembered", remembered),
        ("Audit log", audit_path),
    ):
        print(f"{label + ':':<18} {path}  {'present' if path.exists() else 'not created yet'}")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    path = _optional_path(cast("str | None", args.policy)) or policy_module.default_policy_path()
    if path.exists():
        print(f"{path} already exists; leaving it alone.")
        return 0

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(policy_module.EXAMPLE_POLICY, encoding="utf-8")
    print(f"Wrote a starter rules file to {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-gatekeeper",
        description="Ask before any MCP tool call runs.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    install = sub.add_parser("install", help="put the interceptor in front of every server")
    install.add_argument("--config", default=None, help="path to claude_desktop_config.json")
    install.add_argument("--socket", default=None, help="pin the daemon socket path")
    install.add_argument(
        "--only", action="append", default=None, help="wrap only this server (repeatable)"
    )
    install.set_defaults(func=cmd_install)

    uninstall = sub.add_parser("uninstall", help="take the interceptor back out")
    uninstall.add_argument("--config", default=None)
    uninstall.set_defaults(func=cmd_uninstall)

    status = sub.add_parser("status", help="what is wrapped, and is the daemon up")
    status.add_argument("--config", default=None)
    status.add_argument("--socket", default=None)
    status.set_defaults(func=cmd_status)

    log = sub.add_parser("log", help="recent decisions")
    log.add_argument("-n", "--number", type=int, default=20)
    log.add_argument("--audit", default=None)
    log.set_defaults(func=cmd_log)

    init = sub.add_parser("init", help="write a starter rules file")
    init.add_argument("--policy", default=None)
    init.set_defaults(func=cmd_init)

    doctor = sub.add_parser("doctor", help="where everything is")
    doctor.set_defaults(func=cmd_doctor)

    daemon = sub.add_parser("daemon", help="run the daemon (forwards its own flags)")
    daemon.add_argument("rest", nargs=argparse.REMAINDER)
    daemon.set_defaults(func=None)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    command = cast("str", args.command)
    if command == "daemon":
        rest = list(cast("list[str]", args.rest))
        if rest and rest[0] == "--":
            rest = rest[1:]
        return daemon_main(rest)

    handler = cast("Callable[[argparse.Namespace], int]", args.func)
    try:
        return handler(args)
    except LocatorError as exc:
        print(f"error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
