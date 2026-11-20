"""Puts the pieces together: rules, then the user, then the record.

This is the decider the daemon actually runs. For each held request:

1. Ask the policy. An allow or block from a rule is answered immediately --
   no popup, which is what makes the tool usable past the tenth prompt.
2. Otherwise raise the dialog and wait, bounded by a timeout. No answer means
   refusal.
3. Remember the choice if the user asked us to, then write the decision to the
   audit log -- including the ones a rule answered silently, since those are
   the ones there would otherwise be no record of.

The timeout lives here rather than in a wrapper so the log can say *why* a
request was refused: a click, a rule, a timeout, or an unreachable daemon are
four very different things to read back later.
"""

from __future__ import annotations

import asyncio
import time

from mcp_gatekeeper.audit import AuditLog, DecidedBy
from mcp_gatekeeper.daemon.decisions import Decider
from mcp_gatekeeper.ipc import Action, DecisionRequest, Scope, Verdict
from mcp_gatekeeper.policy import Decision, Policy, Source

_SOURCE_TO_DECIDED_BY = {
    Source.POLICY: DecidedBy.POLICY,
    Source.REMEMBERED: DecidedBy.POLICY,
    Source.SESSION: DecidedBy.SESSION,
    Source.DEFAULT: DecidedBy.POLICY,
}


def make_gate(
    policy: Policy,
    audit: AuditLog,
    ask: Decider,
    *,
    timeout: float,
    on_timeout: Action,
) -> Decider:
    """Build the daemon's decider from its parts."""

    async def decide(request: DecisionRequest) -> Verdict:
        started = time.monotonic()
        resolved = policy.evaluate(request)

        if resolved.decision is not Decision.ASK:
            action = Action.ALLOW if resolved.decision is Decision.ALLOW else Action.BLOCK
            reason = None if action is Action.ALLOW else _rule_block_text(resolved.source)
            verdict = Verdict(action=action, reason=reason)
            decided_by = _SOURCE_TO_DECIDED_BY[resolved.source]
        else:
            try:
                verdict = await asyncio.wait_for(ask(request), timeout)
                decided_by = DecidedBy.USER
                # Only a real answer is worth remembering. A timeout is not a
                # preference.
                policy.remember(request, verdict.action, verdict.scope)
            except TimeoutError:
                verdict = Verdict(action=on_timeout, reason=_timeout_text(timeout))
                decided_by = DecidedBy.TIMEOUT

        audit.record(
            request,
            verdict,
            decided_by=decided_by,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        return verdict

    return decide


def _timeout_text(timeout: float) -> str:
    return (
        "Blocked by MCP Gatekeeper: nobody answered within "
        + f"{timeout:.0f} seconds, so the call was refused."
    )


def _rule_block_text(source: Source) -> str:
    if source is Source.SESSION:
        return "Blocked by MCP Gatekeeper: refused for the rest of this session."
    if source is Source.REMEMBERED:
        return "Blocked by MCP Gatekeeper: you chose to always refuse this."
    return "Blocked by MCP Gatekeeper: refused by a rule in your policy file."


def summarise(verdict: Verdict) -> str:
    """One line for the daemon's own log."""
    scope = "" if verdict.scope is Scope.ONCE else f" ({verdict.scope.value})"
    return f"{verdict.action.value}{scope}"
