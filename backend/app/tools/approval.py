# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Pure decision layer for per-tool human-in-the-loop approval.

This module is deliberately free of I/O: no database, no network, no clock.
Everything it needs is passed in, already resolved, so the gate decision can be
unit-tested exhaustively and so it can be evaluated on the hot path of every
tool call without a round-trip.

Two things live here:

* :func:`fingerprint` — a stable digest of a tool's *approved shape*, used to
  detect that a tool has materially changed since a user granted a standing
  approval for it.
* :func:`evaluate_gate` — the decision table that answers "does this call pause
  for a human?".

Persistence for whitelist membership lives in
``app.services.hitl_whitelist``; persistence for standing approvals lives in
``app.services.tool_approval_service``.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

# Gate outcomes recorded against every tool call. Every call is logged with one
# of these, including the ones that never paused — "why did this run without
# asking me?" must be answerable from the audit trail alone.
OUTCOME_HITL_OFF = "auto_run_hitl_off"
OUTCOME_WHITELISTED = "auto_run_whitelisted"
OUTCOME_INTEGRATION_EXEMPT = "auto_run_integration_exempt"
OUTCOME_STANDING_EXCEPTION = "auto_run_standing_exception"
OUTCOME_PAUSED = "paused_for_approval"

#: The gate could not be built — an unreadable whitelist, a dropped connection.
#: Distinct from :data:`OUTCOME_PAUSED` so "we could not decide, so we asked"
#: is never read back as "the policy said ask".
OUTCOME_GATE_UNAVAILABLE = "paused_gate_unavailable"

#: A call the human already approved, re-paused because the tool is no longer
#: the one they were shown (FR-002c4). Distinct from :data:`OUTCOME_PAUSED` so
#: the trail says the decision was outrun rather than never taken.
OUTCOME_PAUSED_TOOL_CHANGED = "paused_tool_changed_since_decision"

# The two wholesale suppressions, named apart because they answer
# "why did this run without asking?" with different responsible parties: an
# administrator switched approvals off for the whole platform, or this user
# chose to allow every tool for themselves. Folding either into
# ``OUTCOME_HITL_OFF`` would misattribute a deliberate policy to the
# agent-level switch.
OUTCOME_APPROVALS_DISABLED = "auto_run_approvals_disabled"
OUTCOME_USER_ALLOWS_ALL = "auto_run_user_allows_all"

# A turn an administrator marked uninterrupted because of the bot that routed
# it. Named apart from ``OUTCOME_HITL_OFF`` for exactly the reason the
# two above are: that stamp means "the agent does not gate", and reusing it here
# told the trail an agent was ungated when the agent gates everywhere else and
# one trusted bot was the reason.
OUTCOME_BOT_UNINTERRUPTED = "auto_run_bot_uninterrupted"
# The agent sets nothing and the install default is off: the
# administrator's install-wide choice, not the agent's, let the call run.
OUTCOME_INSTALL_DEFAULT_OFF = "auto_run_install_default_off"

# Not every recorded call ran. These two are the resume path, where the reason
# is a fact about the *resume* rather than a gate decision, and calling either
# ``auto_run_whitelisted`` told the trail a call auto-ran on a whitelist it was
# never checked against.
#
# ``OUTCOME_NOT_OFFERED``: the call was answered with a refusal because the
# assignment no longer offers that tool — an approval is not a licence to run
# something the deny list has since removed.
OUTCOME_NOT_OFFERED = "not_run_tool_not_offered"

#: A gated call on a run the reviewer discarded instead of answering. The call
#: never ran and never got a verdict, and that is a third thing — not an
#: auto-run, not a human decision. Named so it cannot be read as either, and so
#: the FR-020 blank stays reserved for a gate that failed to install.
OUTCOME_DISMISSED = "not_run_dismissed"
#: A gated call nobody answered before the decision's deadline: the platform
#: refused it, no person did (``decision_waits``). Never ``human_reject``.
OUTCOME_EXPIRED = "not_run_expired"
#: A gated call the decision set carried no verdict for: refused by default,
#: never credited to a reviewer.
OUTCOME_UNDECIDED = "not_run_undecided"

#: Key of an approval decision naming who refused a call when no person did:
#: ``DECISION_SOURCE_DEADLINE`` (the deadline passed) or
#: ``DECISION_SOURCE_NOT_OFFERED`` (the tool left the run's surface). Absent: a
#: person's verdict.
DECISION_SOURCE_KEY = "source"
DECISION_SOURCE_DEADLINE = "deadline"
DECISION_SOURCE_NOT_OFFERED = "not_offered"
# ``OUTCOME_CLEARED_PRE_PAUSE``: the gate cleared this call *before* the pause,
# so it runs on resume without being re-gated — but the outcome sink is fresh
# after the pause unwinds, so the original reason (whitelist, integration
# exemption, standing approval) is genuinely not known here. Naming that is
# honest; picking one of the three is not.
OUTCOME_CLEARED_PRE_PAUSE = "auto_run_cleared_before_pause"

# A call the agent loop answers itself and strips before dispatch, so the gate
# is never consulted. Recorded rather than left blank: FR-020 asks for every
# call and its outcome, and a null is indistinguishable from the one thing that
# must stay legible — a gate that failed to install and let calls run ungated.
OUTCOME_META_NOT_DISPATCHED = "auto_run_meta_not_dispatched"

#: Tools the loop handles inline instead of dispatching to an executor.
#:
#: Spelled as literals to keep this module free of imports from the rest of the
#: package (see the module docstring); ``tests/test_gate_outcome_recording.py``
#: pins them against their defining constants, so a rename is loud rather than
#: silently turning these rows blank again.
LOOP_HANDLED_TOOLS = frozenset({"load_tools"})


@dataclass(frozen=True)
class GateDecision:
    """The outcome of evaluating one tool call against the approval gate."""

    action: Literal["execute", "pause"]

    # The FR-020 audit outcome for this call. Populated for executed calls;
    # a paused call gets its outcome later, once the human decides.
    log_outcome: str | None = None

    # True when a standing approval existed but no longer describes the tool.
    # The caller revokes the stale preference; the decision itself stays pure.
    invalidated_preference: bool = False


def fingerprint(input_schema: Mapping[str, Any] | None) -> str:
    """Digest a tool's *approved shape* for standing-approval comparison.

    Hashes the input schema and nothing else. This is what draws the line
    between a cosmetic change and a material one: rewording a description or
    renaming a display label leaves consent intact, while changing the
    parameters — adding a ``force`` flag, widening an enum — invalidates it,
    because the approved call and the proposed call are no longer the same
    shape.

    Whitelist membership is deliberately excluded. Whitelisting a tool makes a
    standing approval *moot* rather than *invalid* (a whitelisted tool never
    gates for anyone), so folding membership in would churn fingerprints and
    force pointless re-approvals when an administrator edits the list.

    Serialisation is key-order-stable so an unchanged schema always yields the
    same digest regardless of how the dict was constructed.
    """
    canonical = json.dumps(
        input_schema or {}, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_action_proposal(
    *,
    tool_call_id: str,
    tool_name: str,
    arguments: Mapping[str, Any] | None,
    description: str = "",
    input_schema: Mapping[str, Any] | None = None,
    source: str = "unknown",
    entity_id: str | None = None,
    always_asks: bool = False,
) -> dict[str, Any]:
    """Build the structured payload a reviewer sees for one proposed call.

    ``input_schema`` is carried alongside the values so the UI can render a
    typed form and, crucially, show each argument's own description — a
    reviewer needs to know what ``cascade: true`` *means*, not just that it is
    set. ``tool_call_id`` is the match key on resume; the tool name must never
    be used for that, because a turn can contain several calls to the same tool.

    ``tool_entity_id`` is ``ToolDefinition.entity_id``, the connection the call
    goes through: a "Yes, always" records it, because an MCP tool's source is a
    server name its owner chooses and only the server's id says which tool was
    approved (:func:`standing_approval_covers`).

    ``always_asks`` (only set when true) marks a call no standing approval or
    rule can clear (:func:`definition_always_asks`), so what explains the pause
    does not offer one (:func:`no_approval_channel_message`).
    """
    proposal = {
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "tool_description": description,
        "arguments": dict(arguments or {}),
        "input_schema": dict(input_schema or {}),
        "source": source,
        "tool_entity_id": str(entity_id) if entity_id else None,
    }
    if always_asks:
        proposal["always_asks"] = True
    return proposal


#: Proposal fields a reviewer needs and a bystander who can merely see the run
#: does not: the call itself, and the id of the connection it goes through.
DECIDER_ONLY_PROPOSAL_FIELDS = frozenset(
    {"arguments", "input_schema", "tool_entity_id"}
)


def redact_proposals(proposals: list | None) -> list | None:
    """Strip the decider-only fields from *proposals*.

    For the paths that reach people who cannot decide: the company SSE channel,
    which every authenticated user subscribes to, and the run serializers, which
    render for anyone who can see the run. What survives — id, name, description,
    source — is what the list views count and label. The full payload stays
    behind ``GET /pending-approvals``, which admin-gates it.
    """
    if not proposals:
        return proposals
    return [
        (
            {k: v for k, v in p.items() if k not in DECIDER_ONLY_PROPOSAL_FIELDS}
            if isinstance(p, dict)
            else p
        )
        for p in proposals
    ]


#: Reach of an exemption the product ships or derives from its own tools: the
#: built-ins, the integrations and the registered platforms.
REACH_PRODUCT = "product"
#: Reach of an administrator's addition, which names a tool rather than where
#: it comes from: anything carrying that name.
REACH_ANY = "*"


def names_its_own_tools(tool_source: str) -> bool:
    """Whether a tool of *tool_source* carries a name chosen outside the product.

    An MCP server advertises whatever names it likes and a custom tool's author
    types one, and the executor lets the last tool registered under a name
    replace the first. Such a tool must not inherit an exemption decided for
    another tool of the same name.
    """
    return tool_source.startswith(("mcp:", "custom"))


def whitelist_reaches(
    tool_key: str,
    *,
    tool_source: str,
    tool_entity_id: str | None,
    reach: Mapping[str, frozenset[str]] | None,
) -> bool:
    """Whether the whitelist entry for *tool_key* covers this tool.

    *reach* maps each exempt name to who it covers: :data:`REACH_PRODUCT`,
    :data:`REACH_ANY`, or the id of the MCP server whose read it was derived
    from. ``None`` is a caller that resolved no reach, which keeps membership by
    name alone for product tools; a tool that names itself is never exempted by
    name alone, so no tool name has to be reserved. A name with no recorded
    reach covers nothing.
    """
    if reach is None:
        return not names_its_own_tools(tool_source)
    owners = reach.get(tool_key)
    if not owners:
        return False
    if REACH_ANY in owners:
        return True
    if names_its_own_tools(tool_source):
        return bool(tool_entity_id) and str(tool_entity_id) in owners
    return REACH_PRODUCT in owners


def standing_approval_covers(
    tool_source: str | None,
    granted_source: str | None,
    *,
    tool_entity_id: str | None = None,
    granted_entity_id: str | None = None,
) -> bool:
    """Whether an approval granted to *granted_source* covers a tool of *tool_source*.

    Approvals are keyed by bare name, and a tool that names itself (see
    :func:`names_its_own_tools`) can take any name and copy any schema, so name
    and fingerprint say nothing about which tool the user approved:

    * a custom tool registers as ``custom:<id>`` and is covered only by an
      approval granted to that very tool; a bare ``custom`` names no tool;
    * an MCP tool's source is ``mcp:<server name>``, a name the server's owner
      chooses, so a server's tool (one carrying its server's id) is covered
      only by an approval granted on that same server, by id
      (*granted_entity_id* against *tool_entity_id*); an approval recorded
      without one names no server. The product's own tool among them
      (``read_mcp_resource``, ``mcp:resources``) carries no id, and is covered
      only by an approval that carries none either and names its source;
    * a product tool keeps matching by name, except for an approval granted to
      a tool that names itself: consent given to a discovered or authored tool
      is not consent to the product's tool of the same name.
    """
    if tool_source and tool_source.startswith("custom"):
        return tool_source.startswith("custom:") and tool_source == granted_source
    if tool_source and tool_source.startswith("mcp:"):
        if not (granted_source and str(granted_source).startswith("mcp:")):
            return False
        if tool_entity_id or granted_entity_id:
            return bool(tool_entity_id) and str(tool_entity_id) == str(
                granted_entity_id or ""
            )
        return tool_source == granted_source
    return not (granted_source and names_its_own_tools(granted_source))


def call_always_asks(
    always_ask_argument: str | None, arguments: Mapping[str, Any] | None
) -> bool:
    """Whether a call sets its tool's ``always_ask_argument``.

    Truthiness, as the tool reads its own flag: a value the tool would act on
    (a ``"true"`` string, a 1) must ask too. Arguments that are not a mapping
    are a malformed call, and ask.
    """
    if not always_ask_argument:
        return False
    if not isinstance(arguments, Mapping):
        return True
    return bool(arguments.get(always_ask_argument))


def definition_always_asks(definition: Any, call: Any) -> bool:
    """Whether *call* of the tool *definition* must ask a person.

    The tool asks on every call (``ToolDefinition.always_ask``), or *call*
    sets its ``always_ask_argument`` (:func:`call_always_asks`); the call's
    arguments are read only for the latter. ``None`` - a name no tool answers
    to - asks nothing here: dispatch refuses it.
    """
    if definition is None:
        return False
    if getattr(definition, "always_ask", False):
        return True
    argument = getattr(definition, "always_ask_argument", None)
    return bool(argument) and call_always_asks(argument, call.arguments)


def evaluate_gate(
    tool_key: str,
    *,
    hitl_enabled: bool,
    whitelist: frozenset[str],
    active_exceptions: Mapping[str, str],
    current_fingerprint: str,
    tool_source: str = "",
    tool_entity_id: str = "",
    exempt_sources: frozenset[str] = frozenset(),
    vetoed: frozenset[str] = frozenset(),
    whitelist_reach: Mapping[str, frozenset[str]] | None = None,
    exception_sources: Mapping[str, str] | None = None,
    exception_entity_ids: Mapping[str, str] | None = None,
) -> GateDecision:
    """Decide whether one tool call pauses for human approval.

    This function is TOTAL: it returns a decision for every input combination
    and never raises. That is a deliberate contract, not an accident of the
    implementation — it sits on the path of every tool call, and an exception
    escaping here would surface as a tool failure rather than as a gating
    decision, which is exactly the ambiguity the feature exists to remove.
    Callers resolve ``whitelist`` and ``active_exceptions`` beforehand and treat
    *their* failure as "pause" (fail closed).

    Note what is absent: run interactivity. A standing approval applies to
    interactive and scheduled runs alike, so whether a human happens to be
    watching does not change whether a call gates — it only changes whether a
    pause notifies the assignment owner. Reintroducing interactivity here would
    silently make scheduled runs behave differently from the run in which the
    user granted their approval.

    Args:
        tool_key: Canonical tool name, post-alias-resolution.
        tool_source: ``ToolDefinition.source``, e.g. ``integration:notion``.
        tool_entity_id: ``ToolDefinition.entity_id`` — which *connection* this
            call goes through. A source names a provider and several
            connections of one provider share it, so an exemption an operator
            set on one of them is keyed by both.
        exempt_sources: Sources an operator has exempted wholesale — currently
            integrations whose single approval toggle is off. Coarser than the
            whitelist by design: per-tool curation across ~2,350 integration
            tools is deferred, so the operator's unit of trust is the
            integration.
        hitl_enabled: The resolved agent/assignment HITL switch.
        whitelist: Effective whitelist membership for this deployment.
        vetoed: Tools an administrator has explicitly re-gated. Outranks every
            exemption below, which absence from ``whitelist`` could not express.
        whitelist_reach: Which tools each whitelist entry covers (see
            :func:`whitelist_reaches`). The whitelist is keyed by bare name, so
            without it an MCP or custom tool taking a whitelisted name ran
            unattended, and a company-managed MCP server advertising a gated
            built-in's name exempted the built-in.
        active_exceptions: ``{tool_key: schema_fingerprint}`` for the run's user.
        exception_sources: ``{tool_key: tool_source}`` each of those was granted
            to (see :func:`standing_approval_covers`). ``None`` covers no custom
            tool, so a caller that resolved none fails closed.
        exception_entity_ids: ``{tool_key: entity id}`` each of those was
            granted to — for an MCP tool, its server's id. ``None`` covers no
            MCP tool, for the same reason.
        current_fingerprint: Fingerprint of the tool as it exists right now.
    """
    # 1. The HITL switch is a hard override. Off means nothing gates, whatever
    #    the whitelist or anyone's standing approvals say.
    if not hitl_enabled:
        return GateDecision(action="execute", log_outcome=OUTCOME_HITL_OFF)

    # 2. An administrator's explicit re-gate is a VETO, not an absence. It has
    #    to short-circuit here: a removal only made the tool absent from the
    #    whitelist, and absence is indistinguishable from "never whitelisted",
    #    so control fell through to the per-connection check below — which any
    #    user can set on a connection they own. The decision order puts the
    #    whitelist (3) above per-connection switches (4) precisely so that
    #    cannot happen, and only a veto expresses it.
    if tool_key in vetoed:
        return GateDecision(action="pause", log_outcome=None)

    # 3. Whitelisted tools run unattended. This is the common path.
    if tool_key in whitelist and whitelist_reaches(
        tool_key,
        tool_source=tool_source,
        tool_entity_id=tool_entity_id,
        reach=whitelist_reach,
    ):
        return GateDecision(action="execute", log_outcome=OUTCOME_WHITELISTED)

    # 3. An integration the operator has exempted wholesale. Checked before
    #    standing approvals because it is an administrator decision about the
    #    whole connection, not one user's preference about one tool.
    # Two shapes, and the difference is who the operator was deciding for. A
    # bare ``integration:<provider>`` is a platform policy, which governs every
    # connection of that provider by design. ``integration:<provider>:<id>`` is
    # one connection's own switch — and matching only the bare form meant
    # turning off a single Notion connection exempted everybody's.
    if (
        tool_source
        and tool_entity_id
        and f"{tool_source}:{tool_entity_id}" in exempt_sources
    ):
        return GateDecision(
            action="execute", log_outcome=OUTCOME_INTEGRATION_EXEMPT
        )
    if tool_source and tool_source in exempt_sources:
        return GateDecision(
            action="execute", log_outcome=OUTCOME_INTEGRATION_EXEMPT
        )

    # 4/5. A standing approval only counts if it still describes this tool. One
    #      granted to another tool of the same name is not this tool's, and is
    #      left alone: gating here must not revoke what the user gave elsewhere.
    approved_fingerprint = active_exceptions.get(tool_key)
    if approved_fingerprint is not None and standing_approval_covers(
        tool_source,
        (exception_sources or {}).get(tool_key),
        tool_entity_id=tool_entity_id,
        granted_entity_id=(exception_entity_ids or {}).get(tool_key),
    ):
        if approved_fingerprint == current_fingerprint:
            return GateDecision(
                action="execute", log_outcome=OUTCOME_STANDING_EXCEPTION
            )
        # The tool changed materially since consent was given. Gate, and tell
        # the caller to revoke the stale preference so the user sees why the
        # prompt came back.
        return GateDecision(action="pause", invalidated_preference=True)

    # 5. Gated by default. Absence from the whitelist is the safe state, so a
    #    tool nobody has considered — a new integration, a freshly authored
    #    custom tool, one introduced by an upgrade — lands here.
    return GateDecision(action="pause")


# ── Reviewer verdicts ─────────────────────────────────────────────

VERDICT_APPROVE = "approve"
VERDICT_APPROVE_ALWAYS = "approve_always"
VERDICT_REJECT = "reject"

#: Every verdict a reviewer may return. A decision carries no arguments — the
#: reviewer answers the call the agent proposed — so this is the whole surface.
APPROVAL_VERDICTS = frozenset(
    {VERDICT_APPROVE, VERDICT_APPROVE_ALWAYS, VERDICT_REJECT}
)

#: Set on a decision by the route that received it, never by a client: the
#: administrator behind the approving session, or ``None``. The turn applying
#: it may have been sent by another session (another tab, the owner while an
#: administrator answers), so its own impersonation cannot stand in (FR-017b).
DECISION_IMPERSONATED_BY = "approver_impersonated_by"


def approver_admin_id(
    decision: Mapping[str, Any] | None, turn_impersonated_by: object
) -> uuid.UUID | None:
    """The administrator a "Yes, always" in *decision* is attributed to (FR-017b).

    A decision without the field falls back to the turn's impersonation: a
    resume, decided on the request that runs the turn, or a decision pushed by
    a process that predates the field.
    """
    if decision is not None and DECISION_IMPERSONATED_BY in decision:
        source = decision[DECISION_IMPERSONATED_BY]
    else:
        source = turn_impersonated_by
    return uuid.UUID(str(source)) if source else None


def resolve_log_outcome(
    tool_name: str,
    call_id: str,
    *,
    outcomes: Mapping[str, str],
    hitl_enabled: bool,
    hitl_off_outcome: str = OUTCOME_HITL_OFF,
) -> str | None:
    """What to record against one executed call (FR-020).

    Shared by the chat path and the run path so the two cannot drift — the same
    reason ``build_approval_gate`` is shared. They had the same expression
    copied into both, which meant the same blank in both.

    Order matters:

    1. What the gate decided, if it decided anything.
    2. The switch being off, the only way a call runs with no gate installed.
    3. A meta-call the loop answered itself, which never reaches the gate.
    4. ``None`` — and this remaining blank is meaningful. It is what a gate that
       failed to install looks like, and it must stay distinguishable from
       every named reason a call ran unattended. Do not add a catch-all here.

    A call that *paused* is stamped by whoever handled the pause
    (:func:`mark_paused`), never inferred, so it arrives through ``outcomes``.
    """
    recorded = outcomes.get(call_id)
    if recorded:
        return recorded
    if not hitl_enabled:
        # Which switch was off, not merely that one was: an agent that gates
        # everywhere except through one trusted bot must not be recorded as an
        # ungated agent.
        return hitl_off_outcome
    if tool_name in LOOP_HANDLED_TOOLS:
        return OUTCOME_META_NOT_DISPATCHED
    return None


def mark_paused(outcomes: dict, proposals: object) -> None:
    """Record that these calls paused for approval and were never decided.

    Called by whoever handles a pause that ends the turn — a surface with no way
    to ask, a client that did not opt in. Every other reason a call ran
    unattended is named in the trail; this one, where the call did *not* run, was
    a blank, and a blank is the same shape as a bug.

    Stamped at the pause rather than inferred from a missing outcome later,
    because one other thing produces a missing outcome: a gate that failed to
    install lets calls run ungated, and labelling those "paused" would claim a
    tool did not run when it did — a worse lie than saying nothing.
    """
    for proposal in proposals or []:
        call_id = (proposal or {}).get("tool_call_id")
        if call_id and call_id not in outcomes:
            outcomes[call_id] = OUTCOME_PAUSED


def no_approval_channel_message(
    exc: object, *, mention_admin_setting: bool = True
) -> str:
    """What to tell someone whose surface cannot collect an approval.

    Some surfaces install a gate but no way to answer it — a client that did not
    opt into mid-turn approval, the general assistant, the public embed. The
    gated call unwinds the turn there, and what reaches the person must explain
    that rather than read as a malfunction: they asked for something reasonable
    and the product declined to do it without a human, which is the feature
    working.

    Plain prose on purpose, with no structured data behind it, because the
    clients that reach this are exactly the ones with no UI for anything else.
    Shared so the wording cannot drift between the surfaces that need it.

    ``mention_admin_setting`` is off for the public embed: an anonymous visitor
    has no administrator screen to visit, and naming one would both mislead them
    and describe internal configuration to someone outside the deployment.

    The way out named here is the reader's own: a user can grant their own
    standing approval for a tool from the Tools page, so sending everybody to
    an administrator would describe a lever other than the one they can pull
    themselves. The administrator's remains named beside it, because it is the
    answer when the tool should stop asking for everyone.
    """
    proposals = getattr(exc, "proposals", None) or []
    if proposals and all(isinstance(p, dict) and p.get("requires_consent") for p in proposals):
        # A consent no setting waives: the Tools page cannot lift it.
        return (
            "I need your own approval for this, and I can't collect it here, so "
            "I've stopped rather than acting without it. Approve it from XTM One's "
            "web chat, or do it yourself there - a conversation moves into a "
            "workspace from its menu."
        )
    names = ", ".join(p.get("tool_name", "?") for p in proposals if isinstance(p, dict))
    if not names:
        # A gate that raised without proposals still names the tool in its
        # question; falling back to that beats saying "a tool".
        names = str(getattr(exc, "question", "") or "a tool it needs approval for")
    if not mention_admin_setting:
        return (
            f"I need approval before running: {names}.\n\n"
            "I can't collect that approval here, so I've stopped rather than "
            "acting without it. Please ask me to do something else."
        )
    if proposals and all(isinstance(p, dict) and p.get("always_asks") for p in proposals):
        # No rule or standing approval clears these calls: naming
        # the Tools page would send the reader to a lever that does nothing.
        return (
            f"I need approval before running: {names}.\n\n"
            "A person must approve every such call, and this chat can't "
            "collect that approval, so I've stopped rather than acting without "
            "it. Ask me again where approvals can be answered, such as the "
            "web chat, or ask me to do something else."
        )
    return (
        f"I need approval before running: {names}.\n\n"
        "This chat can't collect that approval, so I've stopped rather than "
        "acting without it. You can allow this tool for yourself from the Tools "
        "page, or an administrator can allow it for everyone from the same "
        "place — or you can ask me to do something else."
    )


def is_approval(verdict: object) -> bool:
    """Whether ``verdict`` authorises the call to run.

    Deliberately an allow-list, not ``!= "reject"``. Anything unrecognised — a
    typo, a stale verdict from an older client, a field a proxy mangled — must
    NOT execute a call nobody approved. Treating "not exactly reject" as consent
    made the safest-looking default the most dangerous one: a decision reading
    ``"rejected"`` would have run a destructive tool.
    """
    return verdict in (VERDICT_APPROVE, VERDICT_APPROVE_ALWAYS)


# ── Platform-level approval policy ────────────────────────────────

POLICY_REQUIRE = "require"
POLICY_SKIP = "skip"

SET_BY_DEFAULT = "default"
SET_BY_DEPLOYMENT = "deployment"
SET_BY_ADMIN = "admin"

# Why a nudge is being offered. The two are not interchangeable: one is the
# platform speaking for itself, the other is a single client of it.
NUDGE_DECLARED = "declared"
NUDGE_OBSERVED = "observed"

# ``openbas`` is OpenAEV's legacy identifier. It appears as a registration
# identifier on older installs, as an integration provider, and as the
# ``X-Platform-Product`` header value, and the three do not always agree — a
# registration made under the old name creates its integration under the new
# one. Kept in one place so every consumer resolves the pair identically.
_PLATFORM_IDENTIFIER_ALIASES = {
    "openbas": {"openaev"},
    "openaev": {"openbas"},
}


def platform_identifiers(identifier: str) -> set[str]:
    """Every name this platform is known by, including ``identifier`` itself.

    Lower-cased, because a registration stores the identifier verbatim from the
    request body while integration providers are canonical keys. A platform
    registered as ``OpenCTI`` otherwise spoke for the source
    ``integration:OpenCTI``, which no tool carries — so the operator's platform
    policy governed nothing and the per-connection switch quietly took over.
    The capability-update path normalises for the same reason.
    """
    key = (identifier or "").strip().lower()
    return {key} | _PLATFORM_IDENTIFIER_ALIASES.get(key, set())


def platform_gates_tools(
    *,
    supports_approval_prompts: bool | None,
    approval_policy: str,
    approval_policy_set_by: str,
) -> bool:
    """Should this registered platform's tools ask for approval?

    Two questions get answered here, and keeping them apart is the whole
    point of the function.

    *Can* a prompt be shown is a **fact** the platform reports about itself.
    *What to do when it cannot* is a **policy** only an operator can set. An
    earlier design tried to answer both from a table of "product X version Y
    supports approvals", which hardcoded other teams' release schedules into
    ours and lied the moment anyone backported.

    Precedence:

    1. An administrator's explicit choice wins over everything. A platform
       upgrade must never silently re-arm a control somebody deliberately
       switched off — the UI nudges them to reconsider instead.
    2. Otherwise, a platform that says it can prompt gets gated normally.
       This is what makes the arrangement self-correcting: ship approval
       support, re-register, and gating resumes with nobody touching a
       setting.
    3. Otherwise the seeded policy decides, and its own default is to gate.
    """
    if approval_policy_set_by == SET_BY_ADMIN:
        return approval_policy != POLICY_SKIP
    if supports_approval_prompts is True:
        return True
    return approval_policy != POLICY_SKIP


def platform_capability_nudge(
    *,
    supports_approval_prompts: bool | None,
    approval_policy: str,
    approval_policy_set_by: str,
    approval_support_seen: bool = False,
) -> str | None:
    """Why this platform could be gating again, or ``None``.

    The one case where "never override a human" and "fail safe" pull against
    each other: an operator turned approval off because the platform could
    not ask, and now something can. Resolved by doing neither silently — the
    choice stands, and the UI offers to restore the safer posture in one click.

    Two kinds of evidence reach this, and the caller must be able to tell them
    apart because they warrant different words:

    ``NUDGE_DECLARED``
        The platform itself declared the capability at registration. It speaks
        for its whole assistant.

    ``NUDGE_OBSERVED``
        A client of the platform sent ``supports_tool_approval`` on a chat
        message. Weaker: it proves *that* caller will answer a prompt, not
        that every caller will — an unattended one of the same platform may
        send nothing. Enough to offer the choice, never enough to make it.

    Without this second source the evidence is thrown away exactly where it is
    most needed: a platform that never declared the capability produces no
    nudge however many capable clients it has, so an operator who set *skip*
    at install is never told the reason has expired.
    """
    if supports_approval_prompts is not True and not approval_support_seen:
        return None
    if platform_gates_tools(
        supports_approval_prompts=supports_approval_prompts,
        approval_policy=approval_policy,
        approval_policy_set_by=approval_policy_set_by,
    ):
        return None
    # The platform's own word outranks one client's when both are in hand.
    return NUDGE_DECLARED if supports_approval_prompts is True else NUDGE_OBSERVED
