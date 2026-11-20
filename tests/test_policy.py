"""Tests for the rules file, the remembered decisions, and how they interact."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from mcp_gatekeeper.ipc import Action, DecisionRequest, Scope
from mcp_gatekeeper.policy import Decision, Policy, Rule, Source


def a_request(
    tool: str = "delete_file", server: str = "demo", method: str = "tools/call"
) -> DecisionRequest:
    return DecisionRequest(
        id=DecisionRequest.new_id(),
        server=server,
        direction="c2s",
        method=method,
        tool=tool,
        params={"path": "/home/me/important.docx"},
        request_id=1,
    )


# --------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------


def test_a_bare_rule_matches_everything():
    assert Rule().matches(a_request())


def test_matches_on_a_tool_glob():
    rule = Rule(tool="delete_*")
    assert rule.matches(a_request("delete_file"))
    assert not rule.matches(a_request("read_file"))


def test_matches_on_a_server_glob():
    rule = Rule(server="demo*")
    assert rule.matches(a_request(server="demo-two"))
    assert not rule.matches(a_request(server="production"))


def test_matches_on_a_method():
    rule = Rule(method="sampling/*")
    assert rule.matches(a_request(method="sampling/createMessage"))
    assert not rule.matches(a_request(method="tools/call"))


def test_falls_back_to_the_method_when_there_is_no_tool():
    # resources/read and the server-initiated requests have no tool name.
    request = DecisionRequest(
        id="x",
        server="demo",
        direction="s2c",
        method="sampling/createMessage",
        tool=None,
        params={},
        request_id=1,
    )
    assert Rule(tool="sampling/*").matches(request)


def test_matching_is_case_sensitive():
    assert not Rule(tool="Delete_*").matches(a_request("delete_file"))


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def test_a_rule_round_trips():
    rule = Rule(server="demo", tool="send_*", action=Decision.BLOCK)
    assert Rule.from_payload(rule.to_payload()) == rule


def test_missing_fields_become_wildcards():
    rule = Rule.from_payload({"action": "allow"})
    assert rule == Rule(server="*", tool="*", method="*", action=Decision.ALLOW)


def test_an_unusable_rule_is_dropped_rather_than_fatal():
    # A typo in the rules file must not stop the daemon starting -- that would
    # lock you out of your own tools.
    assert Rule.from_payload({"action": "sometimes"}) is None
    assert Rule.from_payload("not a mapping") is None
    assert Rule.from_payload(None) is None


def test_a_bad_rule_does_not_discard_the_good_ones(tmp_path: Path):
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(
        """\
default: ask
rules:
  - {server: "*", tool: "delete_*", action: nonsense}
  - {server: "*", tool: "send_*", action: block}
"""
    )

    policy = Policy.load(policy_path, tmp_path / "remembered.json")
    assert len(policy.rules) == 1
    assert policy.evaluate(a_request("send_email")).decision is Decision.BLOCK


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------


def test_missing_files_give_a_working_default(tmp_path: Path):
    policy = Policy.load(tmp_path / "nope.yaml", tmp_path / "nope.json")
    assert policy.default is Decision.ASK
    assert policy.evaluate(a_request()).decision is Decision.ASK


def test_unparseable_yaml_does_not_stop_startup(tmp_path: Path):
    policy_path = tmp_path / "policy.yaml"
    # A bare * is an alias marker in YAML: exactly the mistake the example
    # file warns about.
    policy_path.write_text("rules:\n  - {server: *, tool: x}\n")

    policy = Policy.load(policy_path, tmp_path / "remembered.json")
    assert policy.evaluate(a_request()).decision is Decision.ASK


def test_the_default_comes_from_the_file(tmp_path: Path):
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text("default: block\n")

    policy = Policy.load(policy_path, tmp_path / "remembered.json")
    assert policy.evaluate(a_request()).decision is Decision.BLOCK


# --------------------------------------------------------------------------
# resolution order
# --------------------------------------------------------------------------


def test_a_hand_written_allow_skips_the_prompt():
    policy = Policy(rules=[Rule(tool="read_file", action=Decision.ALLOW)])
    resolved = policy.evaluate(a_request("read_file"))
    assert resolved.decision is Decision.ALLOW
    assert resolved.source is Source.POLICY


def test_a_hand_written_block_beats_a_remembered_allow():
    # The security-relevant one: tightening the file afterwards must win over
    # an "always allow" clicked earlier.
    policy = Policy(
        rules=[Rule(tool="delete_*", action=Decision.BLOCK)],
        remembered=[Rule(server="demo", tool="delete_file", action=Decision.ALLOW)],
    )
    resolved = policy.evaluate(a_request("delete_file"))
    assert resolved.decision is Decision.BLOCK
    assert resolved.source is Source.POLICY


def test_a_hand_written_ask_falls_through_to_memory():
    policy = Policy(
        rules=[Rule(tool="delete_*", action=Decision.ASK)],
        remembered=[Rule(server="demo", tool="delete_file", action=Decision.ALLOW)],
    )
    resolved = policy.evaluate(a_request("delete_file"))
    assert resolved.decision is Decision.ALLOW
    assert resolved.source is Source.REMEMBERED


def test_this_session_beats_what_was_remembered():
    policy = Policy(remembered=[Rule(server="demo", tool="delete_file", action=Decision.ALLOW)])
    policy.remember(a_request(), Action.BLOCK, Scope.SESSION)

    resolved = policy.evaluate(a_request())
    assert resolved.decision is Decision.BLOCK
    assert resolved.source is Source.SESSION


def test_nothing_matching_lands_on_the_default():
    policy = Policy(rules=[Rule(tool="read_file", action=Decision.ALLOW)], default=Decision.ASK)
    resolved = policy.evaluate(a_request("delete_file"))
    assert resolved.decision is Decision.ASK
    assert resolved.source is Source.DEFAULT


# --------------------------------------------------------------------------
# remembering
# --------------------------------------------------------------------------


def test_allow_once_is_not_remembered(tmp_path: Path):
    remembered = tmp_path / "remembered.json"
    policy = Policy(remembered_path=remembered)
    policy.remember(a_request(), Action.ALLOW, Scope.ONCE)

    assert policy.evaluate(a_request()).source is Source.DEFAULT
    assert not remembered.exists()


def test_a_session_choice_is_never_written_to_disk(tmp_path: Path):
    remembered = tmp_path / "remembered.json"
    policy = Policy(remembered_path=remembered)
    policy.remember(a_request(), Action.ALLOW, Scope.SESSION)

    assert policy.evaluate(a_request()).decision is Decision.ALLOW
    assert not remembered.exists()


def test_always_survives_a_restart(tmp_path: Path):
    policy_path = tmp_path / "policy.yaml"
    remembered = tmp_path / "remembered.json"

    first = Policy.load(policy_path, remembered)
    first.remember(a_request("send_email"), Action.ALLOW, Scope.ALWAYS)

    reloaded = Policy.load(policy_path, remembered)
    resolved = reloaded.evaluate(a_request("send_email"))
    assert resolved.decision is Decision.ALLOW
    assert resolved.source is Source.REMEMBERED


def test_remembering_is_scoped_to_the_server_and_tool(tmp_path: Path):
    policy = Policy(remembered_path=tmp_path / "remembered.json")
    policy.remember(a_request("send_email", server="demo"), Action.ALLOW, Scope.ALWAYS)

    # Same tool on a different server is still a question.
    assert policy.evaluate(a_request("send_email", server="other")).source is Source.DEFAULT
    # A different tool on the same server too.
    assert policy.evaluate(a_request("delete_file", server="demo")).source is Source.DEFAULT


def test_remembering_ignores_the_arguments(tmp_path: Path):
    # The documented limitation, pinned by a test so nobody is surprised:
    # "always allow read_file" permits reading anything.
    policy = Policy(remembered_path=tmp_path / "remembered.json")
    policy.remember(a_request("read_file"), Action.ALLOW, Scope.ALWAYS)

    request = a_request("read_file")
    object.__setattr__(request, "params", {"path": "/home/me/.ssh/id_rsa"})
    assert policy.evaluate(request).decision is Decision.ALLOW


def test_the_remembered_file_is_private_and_valid_json(tmp_path: Path):
    remembered = tmp_path / "remembered.json"
    policy = Policy(remembered_path=remembered)
    policy.remember(a_request(), Action.BLOCK, Scope.ALWAYS)

    assert stat.S_IMODE(remembered.stat().st_mode) == 0o600
    assert stat.S_IMODE(remembered.parent.stat().st_mode) == 0o700

    document = json.loads(remembered.read_text())
    assert document["rules"][0]["action"] == "block"


def test_no_temporary_file_is_left_behind(tmp_path: Path):
    remembered = tmp_path / "remembered.json"
    policy = Policy(remembered_path=remembered)
    policy.remember(a_request(), Action.ALLOW, Scope.ALWAYS)

    assert list(tmp_path.glob("*.tmp")) == []
