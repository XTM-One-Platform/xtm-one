"""Approval payloads reach only deciders, and one pause takes one decision.

Both defects share a premise: a gate whose
proposals every tenant member can read, or whose verdict depends on which of two
racing writers landed last, does not provide the property it advertises.
"""

import inspect
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.tools.approval import build_action_proposal, redact_proposals


def _proposal(name="delete_everything"):
    return build_action_proposal(
        tool_call_id="call_1",
        tool_name=name,
        arguments={"target": "prod", "cascade": True},
        description="Deletes things",
        input_schema={"type": "object"},
        source="integration",
    )


class _User:
    def __init__(self, role="user", uid=None):
        self.role = role
        self.id = uid or uuid.uuid4()


class _Run:
    def __init__(self, *, is_company, owner, proposals):
        self.is_company_managed = is_company
        self.user_id = owner
        self.pending_approvals = proposals
        # What the deciding paths re-read once they hold the row: a paused run
        # that nobody has answered yet.
        self.status = "awaiting_human"
        self.approval_decisions = None


# --- the payload ----------------------------------------------------------


def test_redaction_drops_the_call_and_keeps_the_label():
    [out] = redact_proposals([_proposal()])

    assert "arguments" not in out, "the call itself is the secret"
    assert "input_schema" not in out
    assert out["tool_name"] == "delete_everything", "lists still label proposals"
    assert out["tool_call_id"] == "call_1"
    assert out["tool_description"] == "Deletes things"


@pytest.mark.parametrize("empty", [None, []])
def test_redaction_passes_empty_through(empty):
    assert redact_proposals(empty) == empty


def test_a_non_admin_seeing_a_company_run_does_not_see_the_arguments():
    """The exposure: these serializers render for anyone who can see the run,
    and a company-managed run is visible to every authenticated user, while
    only an admin may decide it."""
    from app.api.v1.assignments import _visible_pending_approvals

    run = _Run(is_company=True, owner=uuid.uuid4(), proposals=[_proposal()])

    [seen] = _visible_pending_approvals(run, _User(role="user"))

    assert "arguments" not in seen


def test_an_admin_deciding_a_company_run_sees_everything():
    from app.api.v1.assignments import _visible_pending_approvals

    run = _Run(is_company=True, owner=uuid.uuid4(), proposals=[_proposal()])

    [seen] = _visible_pending_approvals(run, _User(role="admin"))

    assert seen["arguments"] == {"target": "prod", "cascade": True}


def test_the_owner_of_a_personal_run_sees_everything():
    from app.api.v1.assignments import _visible_pending_approvals

    owner = _User()
    run = _Run(is_company=False, owner=owner.id, proposals=[_proposal()])

    [seen] = _visible_pending_approvals(run, owner)

    assert seen["arguments"]


def test_an_unidentified_caller_gets_the_redacted_form():
    """Fail closed: a serializer that cannot say who is asking does not get the
    payload. ``_run_to_response`` defaults ``user`` to ``None``."""
    from app.api.v1.assignments import _visible_pending_approvals

    run = _Run(is_company=False, owner=uuid.uuid4(), proposals=[_proposal()])

    [seen] = _visible_pending_approvals(run, None)

    assert "arguments" not in seen


@pytest.mark.asyncio
async def test_the_company_broadcast_carries_no_arguments(monkeypatch):
    """``activity:company`` is subscribed by every authenticated user, so the
    event is a prompt to go look — the decider's client reads the proposals
    from the endpoint that gates them."""
    import app.services.assignment_helpers as helpers

    published: dict[str, str] = {}

    class _Redis:
        async def publish(self, channel, payload):
            published[channel] = payload

    async def _client():
        return _Redis()

    monkeypatch.setattr(helpers, "redis_client", _client)

    await helpers.publish_action_proposal(
        user_id=str(uuid.uuid4()),
        run_id=str(uuid.uuid4()),
        assignment_name="nightly",
        proposals=[_proposal()],
        is_company=True,
    )

    body = json.loads(published["activity:company"])
    assert body["proposals"], "the prompt still says what is waiting"
    assert "arguments" not in body["proposals"][0]


@pytest.mark.asyncio
async def test_a_personal_run_still_streams_the_full_proposal(monkeypatch):
    """That channel has one subscriber, its owner, who is also its decider."""
    import app.services.assignment_helpers as helpers

    published: dict[str, str] = {}

    class _Redis:
        async def publish(self, channel, payload):
            published[channel] = payload

    async def _client():
        return _Redis()

    monkeypatch.setattr(helpers, "redis_client", _client)
    uid = str(uuid.uuid4())

    await helpers.publish_action_proposal(
        user_id=uid,
        run_id=str(uuid.uuid4()),
        assignment_name="nightly",
        proposals=[_proposal()],
        is_company=False,
    )

    body = json.loads(published[f"activity:{uid}"])
    assert body["proposals"][0]["arguments"] == {"target": "prod", "cascade": True}


# --- the race -------------------------------------------------------------


# Every path that reads a pause and then decides it. The bulk three reach the
# lock through a helper, so this asserts "locks, or delegates to something that
# does" rather than the literal call — otherwise factoring the call out breaks
# the test that exists to protect it.
_LOCK_HELPERS = ("_locked_run", "lock=True")


@pytest.mark.parametrize(
    "endpoint",
    [
        # The respond endpoint delegates to it, and so does the Teams card.
        "answer_paused_run",
        "dismiss_run",
        "release_covered_runs",
        "batch_respond_to_approvals",
        "approve_by_tool",
    ],
)
def test_the_pause_is_read_under_a_row_lock(endpoint):
    """Two reviewers opening one pause both read ``awaiting_human``. Without the
    lock both pass the status check and write verdicts, and the stored decision
    need not be the one the worker consumed. With it, the status check *is* the
    transition: the second waits and is refused as a late decision.
    """
    import app.api.v1.assignments as mod

    source = inspect.getsource(getattr(mod, endpoint))

    assert ".with_for_update()" in source or any(h in source for h in _LOCK_HELPERS), (
        f"{endpoint} must lock the run row before deciding its status"
    )


def test_the_selection_query_never_locks():
    """``_answerable_pending_runs`` must not lock, and both callers rely on it.

    The summary path only counts, and a counting query holding ``FOR UPDATE`` on
    every answerable run in the tenant would block each single-run decision for
    the statement timeout. The decision path locks one row at a time instead,
    because a lock taken by this query is released by the first answer's commit
    and the rest of the sweep would then act on a pre-commit snapshot.
    """
    import app.api.v1.assignments as mod

    assert ".with_for_update()" not in inspect.getsource(mod._answerable_pending_runs)


def test_the_by_tool_sweep_re_reads_each_run_under_the_lock():
    """The corollary: because the selection does not lock, the sweep has to take
    the row itself before deciding it, and re-check what it reads there.
    """
    import app.api.v1.assignments as mod

    source = inspect.getsource(mod.approve_by_tool)

    assert "_locked_run(db, candidate.id)" in source
    assert "run.approval_decisions" in source, (
        "the sweep must refuse a run answered between the select and the lock"
    )


@pytest.mark.parametrize(
    "endpoint", ["answer_paused_run", "batch_respond_to_approvals"]
)
def test_an_answered_run_is_refused_even_though_it_still_reads_awaiting(endpoint):
    """The lock serialises but does not decide. ``resume_assignment_run`` leaves
    the run in ``awaiting_human`` until the worker drains it, so a status check
    under the lock reads exactly what the first reviewer read. Every deciding
    path has to consult ``approval_decisions`` as well.
    """
    import app.api.v1.assignments as mod

    assert "_refuse_already_answered" in inspect.getsource(getattr(mod, endpoint))


# --- whose decision is it -------------------------------------------------


class _SuppressionDB:
    """Answers the two scalars ``resolve_gate_suppression`` reads, in order."""

    def __init__(self, *, approvals_enabled=True, allows_all=True):
        self.calls = 0
        self.approvals_enabled = approvals_enabled
        self.allows_all = allows_all

    async def scalar(self, _stmt):
        self.calls += 1
        return self.approvals_enabled if self.calls == 1 else self.allows_all

    async def rollback(self):
        return None


@pytest.mark.asyncio
async def test_a_personal_allow_all_suppresses_a_personal_run():
    from app.services.hitl_whitelist import resolve_gate_suppression
    from app.tools.approval import OUTCOME_USER_ALLOWS_ALL

    outcome = await resolve_gate_suppression(_SuppressionDB(), uuid.uuid4())

    assert outcome == OUTCOME_USER_ALLOWS_ALL


@pytest.mark.asyncio
async def test_a_personal_allow_all_does_not_suppress_a_company_managed_run():
    """The run-as user of a company-managed fan-out cannot approve that run —
    ``require_run_access`` refuses a non-admin any interaction with one. Letting
    their own preference cancel the question would hand the person who may not
    answer it the power to remove it."""
    from app.services.hitl_whitelist import resolve_gate_suppression

    outcome = await resolve_gate_suppression(
        _SuppressionDB(), uuid.uuid4(), is_company_managed=True
    )

    assert outcome is None


@pytest.mark.asyncio
async def test_the_platform_kill_switch_still_applies_to_a_company_run():
    """That switch is the administrator's own, unlike the personal one."""
    from app.services.hitl_whitelist import resolve_gate_suppression
    from app.tools.approval import OUTCOME_APPROVALS_DISABLED

    outcome = await resolve_gate_suppression(
        _SuppressionDB(approvals_enabled=False),
        uuid.uuid4(),
        is_company_managed=True,
    )

    assert outcome == OUTCOME_APPROVALS_DISABLED


@pytest.mark.asyncio
async def test_a_sweep_does_not_write_standing_consent_onto_another_account(
    monkeypatch,
):
    """``approve_always`` grants to the RUN's owner, so an admin clearing the
    queue would convert a queue-clearing click into permanent, deployment-wide
    consent for someone who never clicked anything (FR-008a)."""
    import app.api.v1.assignments as mod
    import app.services.assignment_engine as engine
    from app.api.v1.schemas.assignment import ApproveByToolRequest

    admin = _User(role="admin")
    someone_else = uuid.uuid4()
    run = _Run(is_company=True, owner=someone_else, proposals=[_proposal("read_x")])
    run.id = uuid.uuid4()

    granted: list = []

    async def _runs(*_a, **_k):
        return [run]

    async def _grant(_db, _run, decisions, _user):
        granted.extend(d.decision for d in decisions)

    async def _resume(*_a, **_k):
        return None

    async def _locked(_db, _run_id):
        # The sweep now re-reads each candidate under its row lock before
        # deciding it; this test is about whose account the grant lands on.
        return run

    monkeypatch.setattr(mod, "_answerable_pending_runs", _runs)
    monkeypatch.setattr(mod, "_locked_run", _locked)
    monkeypatch.setattr(mod, "_grant_standing_approvals", _grant)
    monkeypatch.setattr(mod, "_apply_decisions_to_run", lambda *_a, **_k: None)
    monkeypatch.setattr(engine, "resume_assignment_run", _resume)

    await mod.approve_by_tool(
        ApproveByToolRequest(tool_names=["read_x"], decision="approve_always"),
        user=admin,
        db=SimpleNamespace(commit=AsyncMock()),
    )

    assert granted == ["approve"], (
        "the run is answered, but the durable half is withheld"
    )


# --- unknown means gated --------------------------------------------------


def test_the_fallback_gate_pauses_every_call():
    """``build_approval_gate`` fails closed internally; a caller that catches
    its exception and installs nothing throws that away and runs every tool
    unattended *because* the control was unavailable."""
    from app.services.hitl_whitelist import make_fail_closed_gate
    from app.tools.approval import OUTCOME_GATE_UNAVAILABLE
    from app.tools.base import HumanInputRequestedError

    sink: dict[str, str] = {}
    gate = make_fail_closed_gate(sink)

    class _Call:
        id = "call_1"
        name = "delete_everything"

    with pytest.raises(HumanInputRequestedError):
        gate(_Call())

    assert sink == {"call_1": OUTCOME_GATE_UNAVAILABLE}, (
        "'we could not decide, so we asked' must not read back as policy"
    )


@pytest.mark.parametrize(
    "module_path",
    ["app/api/v1/public_chat.py", "app/services/general_chat.py"],
)
def test_the_unwatched_surfaces_install_the_fallback(module_path):
    """The anonymous embed has nobody watching, and the spec makes the General
    Assistant permanently gated. Agent chat deliberately keeps the other trade —
    that divergence is asserted here so a later copy-paste is loud."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    source = (root / module_path).read_text(encoding="utf-8")

    assert "make_fail_closed_gate" in source


def test_agent_chat_keeps_the_other_trade_on_purpose():
    """It names the helper in a comment explaining why it does NOT use it, so
    the pin has to look at calls, not text."""
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    source = (root / "app/services/agent_chat.py").read_text(encoding="utf-8")

    called = {
        n.func.id
        for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }

    assert "make_fail_closed_gate" not in called
    assert "Divergent on purpose" in source, "and says why, at the site"


# --- an explicit re-gate is a veto, not an absence -------------------------


def _gate(**over):
    from app.tools.approval import evaluate_gate

    kwargs = dict(
        hitl_enabled=True,
        whitelist=frozenset(),
        active_exceptions={},
        current_fingerprint="fp",
        tool_source="integration",
        tool_entity_id="conn-1",
        exempt_sources=frozenset(),
        vetoed=frozenset(),
    )
    kwargs.update(over)
    return evaluate_gate(kwargs.pop("tool_key", "download_jira_attachment"), **kwargs)


def test_a_re_gate_outranks_a_per_connection_switch():
    """The hole: an admin's "require approval again" wrote a `remove` override,
    which only made the tool ABSENT from the whitelist — indistinguishable from
    "never whitelisted". Control then fell through to the per-connection check,
    which any user can set on a connection they own, and the tool ran.
    """
    decision = _gate(
        exempt_sources=frozenset({"integration:conn-1"}),
        vetoed=frozenset({"download_jira_attachment"}),
    )

    assert decision.action == "pause"


def test_a_re_gate_outranks_a_standing_approval():
    decision = _gate(
        active_exceptions={"download_jira_attachment": "fp"},
        vetoed=frozenset({"download_jira_attachment"}),
    )

    assert decision.action == "pause"


def test_a_re_gate_outranks_whitelist_membership():
    """Both can be true at once: the derived layer re-adds a name an admin
    removed. The removal is the decision somebody made on purpose."""
    decision = _gate(
        whitelist=frozenset({"download_jira_attachment"}),
        vetoed=frozenset({"download_jira_attachment"}),
    )

    assert decision.action == "pause"


def test_a_re_gate_does_not_survive_the_hitl_switch_being_off():
    """That one is documented as the hard override and stays so."""
    decision = _gate(
        hitl_enabled=False, vetoed=frozenset({"download_jira_attachment"})
    )

    assert decision.action == "execute"


def test_an_unvetoed_tool_still_takes_its_connection_exemption():
    decision = _gate(exempt_sources=frozenset({"integration:conn-1"}))

    assert decision.action == "execute"


# --- the decision must still describe the tool ----------------------------


class _Composite:
    def __init__(self, schema):
        self._schema = schema

    def get_tool_definition(self, _name):
        return SimpleNamespace(
            input_schema=self._schema, source="builtin", entity_id=None
        )


@pytest.mark.asyncio
async def test_an_approved_call_still_runs_when_the_tool_is_unchanged(monkeypatch):
    from app.services.chat_approval_resume import approved_shapes
    from app.tools.approval import fingerprint

    schema = {"type": "object", "properties": {"target": {"type": "string"}}}
    proposals = [{"tool_call_id": "c1", "tool_name": "t", "input_schema": schema}]
    decisions = {"c1": {"decision": "approve"}}

    shapes = approved_shapes(proposals, decisions)
    assert shapes == {"c1": fingerprint(schema)}

    gate = await _build_gate(monkeypatch, schema, shapes)
    gate(SimpleNamespace(id="c1", name="t"))  # does not raise


@pytest.mark.asyncio
async def test_an_approved_call_re_pauses_when_the_tool_changed(monkeypatch):
    """The bypass never expired on a clock, so a tab reopened days later
    executed against whatever the tool had since become (FR-002c4)."""
    from app.services.chat_approval_resume import approved_shapes
    from app.tools.approval import OUTCOME_PAUSED_TOOL_CHANGED
    from app.tools.base import HumanInputRequestedError

    shown = {"type": "object", "properties": {"target": {"type": "string"}}}
    changed = {
        "type": "object",
        "properties": {"target": {"type": "string"}, "cascade": {"type": "boolean"}},
    }
    proposals = [{"tool_call_id": "c1", "tool_name": "t", "input_schema": shown}]
    shapes = approved_shapes(proposals, {"c1": {"decision": "approve"}})

    sink: dict[str, str] = {}
    gate = await _build_gate(monkeypatch, changed, shapes, sink=sink)

    with pytest.raises(HumanInputRequestedError):
        gate(SimpleNamespace(id="c1", name="t"))

    assert sink["c1"] == OUTCOME_PAUSED_TOOL_CHANGED


async def _build_gate(monkeypatch, live_schema, shapes, sink=None):
    """A gate over a composite whose tool has ``live_schema`` right now."""
    import app.services.hitl_whitelist as mod

    async def _whitelist(_db, **_kw):
        return frozenset()

    async def _vetoed(_db):
        return frozenset()

    async def _exempt(_db):
        return frozenset()

    async def _exceptions(_db, _uid, **_k):
        return {}

    monkeypatch.setattr(mod, "resolve_whitelist", _whitelist)
    monkeypatch.setattr(mod, "resolve_vetoed", _vetoed)
    monkeypatch.setattr(mod, "resolve_exempt_sources", _exempt)
    monkeypatch.setattr(
        "app.services.tool_approval_service.list_active_exceptions", _exceptions
    )

    return await mod.build_approval_gate(
        object(),
        _Composite(live_schema),
        user_id=None,
        hitl_enabled=True,
        approved_call_ids={"c1"},
        approved_shapes=shapes,
        outcome_sink=sink,
    )
