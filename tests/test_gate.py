"""Tests for the composed decider: rules, then the user, then the record."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from mcp_gatekeeper.audit import AuditLog
from mcp_gatekeeper.daemon.decisions import Decider
from mcp_gatekeeper.daemon.gate import make_gate
from mcp_gatekeeper.ipc import Action, DecisionRequest, Scope, Verdict
from mcp_gatekeeper.policy import Decision, Policy, Rule


def a_request(tool: str = "delete_file") -> DecisionRequest:
    return DecisionRequest(
        id=DecisionRequest.new_id(),
        server="demo",
        direction="c2s",
        method="tools/call",
        tool=tool,
        params={"path": "/home/me/important.docx"},
        request_id=1,
    )


class FakeAsk:
    """Stands in for the dialog."""

    def __init__(self, verdict: Verdict | None = None, delay: float = 0.0) -> None:
        self.verdict: Verdict = verdict or Verdict(action=Action.ALLOW)
        self.delay: float = delay
        self.calls: list[DecisionRequest] = []

    async def __call__(self, request: DecisionRequest) -> Verdict:
        self.calls.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.verdict


def entries(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def build(
    tmp_path: Path,
    *,
    policy: Policy | None = None,
    ask: FakeAsk | None = None,
    timeout: float = 5.0,
    on_timeout: Action = Action.BLOCK,
) -> tuple[Decider, FakeAsk, Path]:
    log_path = tmp_path / "audit.jsonl"
    asker = ask or FakeAsk()
    gate = make_gate(
        policy or Policy(remembered_path=tmp_path / "remembered.json"),
        AuditLog(log_path),
        asker,
        timeout=timeout,
        on_timeout=on_timeout,
    )
    return gate, asker, log_path


async def test_a_rule_answers_without_asking(tmp_path: Path):
    policy = Policy(rules=[Rule(tool="read_file", action=Decision.ALLOW)])
    gate, ask, log_path = build(tmp_path, policy=policy)

    verdict = await gate(a_request("read_file"))

    assert verdict.allowed
    assert ask.calls == []  # the whole point: no popup
    assert entries(log_path)[0]["decided_by"] == "policy"


async def test_a_blocking_rule_explains_itself(tmp_path: Path):
    policy = Policy(rules=[Rule(tool="delete_*", action=Decision.BLOCK)])
    gate, ask, log_path = build(tmp_path, policy=policy)

    verdict = await gate(a_request())

    assert not verdict.allowed
    assert "policy file" in (verdict.reason or "")
    assert ask.calls == []
    assert entries(log_path)[0]["verdict"] == "block"


async def test_an_unmatched_request_reaches_the_dialog(tmp_path: Path):
    gate, ask, log_path = build(tmp_path, ask=FakeAsk(Verdict(action=Action.BLOCK)))

    verdict = await gate(a_request())

    assert not verdict.allowed
    assert len(ask.calls) == 1
    assert entries(log_path)[0]["decided_by"] == "user"


async def test_no_answer_fails_closed_and_says_so(tmp_path: Path):
    gate, _ask, log_path = build(tmp_path, ask=FakeAsk(delay=5.0), timeout=0.2)

    verdict = await gate(a_request())

    assert not verdict.allowed
    assert "nobody answered" in (verdict.reason or "")
    # Attribution matters: a timeout reads very differently from a click.
    assert entries(log_path)[0]["decided_by"] == "timeout"


async def test_on_timeout_can_be_configured_to_allow(tmp_path: Path):
    gate, _ask, _log = build(tmp_path, ask=FakeAsk(delay=5.0), timeout=0.2, on_timeout=Action.ALLOW)
    assert (await gate(a_request())).allowed


async def test_always_is_remembered_and_skips_the_next_prompt(tmp_path: Path):
    policy = Policy(remembered_path=tmp_path / "remembered.json")
    ask = FakeAsk(Verdict(action=Action.ALLOW, scope=Scope.ALWAYS))
    gate, _ask, _log = build(tmp_path, policy=policy, ask=ask)

    assert (await gate(a_request("send_email"))).allowed
    assert (await gate(a_request("send_email"))).allowed

    # Asked once, then answered from memory.
    assert len(ask.calls) == 1


async def test_once_is_not_remembered(tmp_path: Path):
    ask = FakeAsk(Verdict(action=Action.ALLOW, scope=Scope.ONCE))
    gate, _ask, _log = build(tmp_path, ask=ask)

    await gate(a_request())
    await gate(a_request())

    assert len(ask.calls) == 2


async def test_a_timeout_is_never_remembered(tmp_path: Path):
    # Walking away is not a preference.
    remembered = tmp_path / "remembered.json"
    policy = Policy(remembered_path=remembered)
    gate, _ask, _log = build(tmp_path, policy=policy, ask=FakeAsk(delay=5.0), timeout=0.2)

    await gate(a_request())

    assert not remembered.exists()


async def test_a_session_choice_is_logged_as_such(tmp_path: Path):
    policy = Policy(remembered_path=tmp_path / "remembered.json")
    ask = FakeAsk(Verdict(action=Action.ALLOW, scope=Scope.SESSION))
    gate, _ask, log_path = build(tmp_path, policy=policy, ask=ask)

    await gate(a_request())
    await gate(a_request())

    logged = entries(log_path)
    assert logged[0]["decided_by"] == "user"
    assert logged[1]["decided_by"] == "session"


async def test_every_decision_is_logged_with_a_latency(tmp_path: Path):
    gate, _ask, log_path = build(tmp_path, ask=FakeAsk(delay=0.15))

    await gate(a_request())

    entry = entries(log_path)[0]
    assert isinstance(entry["latency_ms"], int)
    assert entry["latency_ms"] >= 100
    assert entry["tool"] == "delete_file"


async def test_concurrent_requests_do_not_interfere(tmp_path: Path):
    gate, ask, log_path = build(tmp_path, ask=FakeAsk(delay=0.3))

    results = await asyncio.gather(
        gate(a_request("delete_file")),
        gate(a_request("send_email")),
    )

    assert all(verdict.allowed for verdict in results)
    assert len(ask.calls) == 2
    assert len(entries(log_path)) == 2
