"""Standing rules, and the decisions you asked us to remember.

Two files, deliberately:

    policy.yaml       hand-written, read only. Comments survive because
                      nothing here ever writes to it.
    remembered.json   machine-written. "Always allow" appends here.

PyYAML cannot write comments, so a single file that the app rewrote would
silently destroy whatever the user had annotated. Splitting it costs one extra
path and makes that impossible.

How a request resolves:

1. A hand-written rule saying allow or block wins outright. It is the user's
   standing instruction and nothing may override it -- not even a remembered
   click, which matters if they tighten the file after clicking "always".
2. Otherwise (a rule said ask, or nothing matched): this session's choices,
   then remembered choices, then the default.

Remembered decisions are scoped to (server, tool) and apply to *any*
arguments. "Always allow read_file" therefore also permits reading
~/.ssh/id_rsa. That is a real limitation, stated in the dialog and in the
README, and the reason "allow once" is the default button.
"""

from __future__ import annotations

import fnmatch
import json
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

import yaml

from mcp_gatekeeper.ipc import Action, DecisionRequest, Scope
from mcp_gatekeeper.protocol import JSONObject

EXAMPLE_POLICY = """\
# MCP Gatekeeper rules.
#
# Quote every wildcard: a bare * starts an alias in YAML and will not parse.
# Likewise quote a tool literally named no, yes, on or off -- YAML reads those
# as booleans.
#
# action is allow, block or ask. An allow or block here is final: it overrides
# anything you later click "always" on. ask means "consult what I remembered,
# otherwise prompt me".

default: ask

rules:
  # Never silently delete or send anything.
  - server: "*"
    tool: "delete_*"
    action: ask

  - server: "*"
    tool: "send_*"
    action: ask

  # Reading from the demo server is fine.
  # - server: "demo"
  #   tool: "read_file"
  #   action: allow
"""


class Decision(StrEnum):
    ALLOW = "allow"
    BLOCK = "block"
    ASK = "ask"


class Source(StrEnum):
    """Where a resolution came from. Goes straight into the audit log."""

    POLICY = "policy"
    REMEMBERED = "remembered"
    SESSION = "session"
    DEFAULT = "default"


@dataclass(frozen=True)
class Rule:
    server: str = "*"
    tool: str = "*"
    method: str = "*"
    action: Decision = Decision.ASK

    def matches(self, request: DecisionRequest) -> bool:
        tool = request.tool or request.method
        return (
            fnmatch.fnmatchcase(request.server, self.server)
            and fnmatch.fnmatchcase(tool, self.tool)
            and fnmatch.fnmatchcase(request.method, self.method)
        )

    def to_payload(self) -> JSONObject:
        return {
            "server": self.server,
            "tool": self.tool,
            "method": self.method,
            "action": self.action.value,
        }

    @classmethod
    def from_payload(cls, payload: object) -> Rule | None:
        """Build a rule from file data, or None if it is not usable.

        Forgiving on purpose: one malformed rule should not stop the daemon
        starting, because then a typo locks you out of your own tools.
        """
        if not isinstance(payload, dict):
            return None
        source = cast("dict[str, object]", payload)

        raw_action = source.get("action", Decision.ASK.value)
        try:
            action = Decision(raw_action)
        except ValueError:
            return None

        return cls(
            server=_as_glob(source.get("server")),
            tool=_as_glob(source.get("tool")),
            method=_as_glob(source.get("method")),
            action=action,
        )


def _as_glob(value: object) -> str:
    return value if isinstance(value, str) and value else "*"


@dataclass(frozen=True)
class Resolution:
    """What the policy decided, and which layer decided it."""

    decision: Decision
    source: Source


def default_policy_path() -> Path:
    return Path.home() / ".mcp-gatekeeper" / "policy.yaml"


def default_remembered_path() -> Path:
    return Path.home() / ".mcp-gatekeeper" / "remembered.json"


class Policy:
    """Resolves a request against rules, memory and the default."""

    def __init__(
        self,
        *,
        rules: list[Rule] | None = None,
        remembered: list[Rule] | None = None,
        default: Decision = Decision.ASK,
        remembered_path: Path | None = None,
    ) -> None:
        self.rules: list[Rule] = rules or []
        self.remembered: list[Rule] = remembered or []
        self.default: Decision = default
        self.remembered_path: Path | None = remembered_path
        # Lives and dies with the process, which is what "this session" means.
        self._session: list[Rule] = []

    # -- loading ----------------------------------------------------------

    @classmethod
    def load(cls, policy_path: Path, remembered_path: Path) -> Policy:
        document = _read_yaml(policy_path)

        raw_default = document.get("default", Decision.ASK.value)
        try:
            default = Decision(raw_default)
        except ValueError:
            default = Decision.ASK

        return cls(
            rules=_rules_from(document.get("rules")),
            remembered=_rules_from(_read_json(remembered_path).get("rules")),
            default=default,
            remembered_path=remembered_path,
        )

    # -- resolving --------------------------------------------------------

    def evaluate(self, request: DecisionRequest) -> Resolution:
        for rule in self.rules:
            if rule.matches(request):
                # An explicit allow or block in the hand-written file is final.
                if rule.action is not Decision.ASK:
                    return Resolution(rule.action, Source.POLICY)
                break

        for rule in self._session:
            if rule.matches(request):
                return Resolution(rule.action, Source.SESSION)

        for rule in self.remembered:
            if rule.matches(request):
                return Resolution(rule.action, Source.REMEMBERED)

        return Resolution(self.default, Source.DEFAULT)

    # -- remembering ------------------------------------------------------

    def remember(self, request: DecisionRequest, action: Action, scope: Scope) -> None:
        """Record a click that asked to be remembered."""
        if scope is Scope.ONCE:
            return

        decision = Decision.ALLOW if action is Action.ALLOW else Decision.BLOCK
        rule = Rule(
            server=request.server,
            tool=request.tool or request.method,
            method="*",
            action=decision,
        )

        if scope is Scope.SESSION:
            self._session.append(rule)
            return

        self.remembered.append(rule)
        self._save_remembered()

    def _save_remembered(self) -> None:
        path = self.remembered_path
        if path is None:
            return

        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = {"rules": [rule.to_payload() for rule in self.remembered]}

        # Write and rename, so a crash mid-write cannot leave a truncated file
        # that would fail to parse on the next start.
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)


def _rules_from(value: object) -> list[Rule]:
    if not isinstance(value, list):
        return []
    items = cast("list[object]", value)
    return [rule for rule in (Rule.from_payload(item) for item in items) if rule is not None]


def _read_yaml(path: Path) -> JSONObject:
    """Read a YAML mapping, or an empty one.

    safe_load, never load: the unsafe loader constructs arbitrary Python from
    file contents, which would be a poor look in a tool whose job is refusing
    untrusted actions.
    """
    if not path.exists():
        return {}
    try:
        document = cast(object, yaml.safe_load(path.read_text(encoding="utf-8")))
    except yaml.YAMLError:
        return {}
    return cast(JSONObject, document) if isinstance(document, dict) else {}


def _read_json(path: Path) -> JSONObject:
    if not path.exists():
        return {}
    try:
        document = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError:
        return {}
    return cast(JSONObject, document) if isinstance(document, dict) else {}
