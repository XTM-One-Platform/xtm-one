"""The rules of the persona self-improvement loop (#1044), without a database:
the ``persona`` type of the improvement proposals (#8256).

Who may approve (someone who may manage the agent; a private agent without the
administrator's bypass), what a baseline must be (a completed run that
measured the current persona), where a cycle ends (the -2 point quality margin,
and a rollback only of the loop's own version), and what the generator must
return. The cycle against PostgreSQL is ``test_persona_improvement_postgres.py``.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.models.improvement_proposal import STATUS_KEPT, STATUS_REGRESSION_KEPT, STATUS_ROLLED_BACK
from app.services import improvement_proposals as proposals
from app.services import persona_improvement as loop
from app.services.evaluation_comparison import compare_metric
from app.services.improvement_proposals import lifecycle


def _user(*, admin=False):
    return SimpleNamespace(id=uuid.uuid4(), role="admin" if admin else "user", is_admin=admin)


def _agent(*, owner=None, company=False, builtin=False, shared_with=None, permission="config"):
    shared = [str(shared_with.id)] if shared_with else []
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="Agent",
        user_id=None if company else (owner.id if owner else uuid.uuid4()),
        is_company_managed=company,
        is_builtin=builtin,
        shared_group_ids=[],
        shared_group_permissions={},
        shared_user_ids=shared,
        shared_user_permissions={uid: permission for uid in shared},
        access_restricted=False,
    )


# ---------------------------------------------------------------------------
# Who approves
# ---------------------------------------------------------------------------


def test_a_private_agent_is_approved_by_its_owner():
    owner = _user()
    assert loop.approval_error(_agent(owner=owner), owner, []) is None


def test_an_admin_bypass_does_not_approve_a_private_agent():
    error = loop.approval_error(_agent(owner=_user()), _user(admin=True), [])
    assert error is not None
    assert "owner" in error


def test_a_config_grant_approves_and_a_read_only_one_does_not():
    grantee = _user()
    assert loop.approval_error(_agent(shared_with=grantee), grantee, []) is None
    assert loop.approval_error(_agent(shared_with=grantee, permission="read_only"), grantee, []) is not None


def test_losing_the_grant_loses_the_approval():
    grantee = _user()
    agent = _agent(shared_with=grantee)
    assert loop.approval_error(agent, grantee, []) is None

    agent.shared_user_ids, agent.shared_user_permissions = [], {}

    assert loop.approval_error(agent, grantee, []) is not None


def test_a_company_managed_agent_is_approved_by_admins_only():
    agent = _agent(company=True)
    assert loop.approval_error(agent, _user(admin=True), []) is None
    assert loop.approval_error(agent, _user(), []) is not None


def test_a_builtin_agent_is_never_rewritten(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.platform_mode", "xtm_one")
    assert loop.approval_error(_agent(company=True, builtin=True), _user(admin=True), []) is not None


# ---------------------------------------------------------------------------
# The baseline
# ---------------------------------------------------------------------------


def _run(*, score=70.0, version=3):
    return SimpleNamespace(score=score, persona_version_number=version)


def test_no_evaluation_baseline_yet_is_refused():
    problem = loop.baseline_problem(None, "score", 3)
    assert problem is not None
    assert "no completed run" in problem


def test_a_baseline_without_a_quality_score_is_refused():
    assert loop.baseline_problem(_run(score=None), "score", 3) is not None


def test_a_baseline_of_an_older_persona_is_refused():
    problem = loop.baseline_problem(_run(version=2), "score", 3)
    assert problem is not None
    assert "v2" in problem and "v3" in problem


def test_a_baseline_of_the_current_persona_is_accepted():
    assert loop.baseline_problem(_run(version=3), "score", 3) is None
    # A run from before persona snapshots, or of an agent without a persona.
    assert loop.baseline_problem(_run(version=None), "score", 3) is None
    assert loop.baseline_problem(_run(version=0), "score", 3) is None
    assert loop.baseline_problem(_run(version=1), "score", None) is None


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("baseline", "candidate", "still_active", "status"),
    [
        (70.0, 68.0, True, STATUS_KEPT),  # a tie inside the margin: -2.0 is neutral
        (70.0, 69.5, True, STATUS_KEPT),
        (70.0, 80.0, True, STATUS_KEPT),  # progression
        (70.0, 67.9, True, STATUS_ROLLED_BACK),  # beyond the margin
        (70.0, 50.0, False, STATUS_REGRESSION_KEPT),  # a human edited it meanwhile
        (70.0, 68.0, False, STATUS_KEPT),
    ],
)
def test_where_a_cycle_ends(baseline, candidate, still_active, status):
    verdict = compare_metric("score", baseline, candidate).verdict
    assert proposals.settle_status(verdict, still_active) == status


# ---------------------------------------------------------------------------
# The generator's answer
# ---------------------------------------------------------------------------


def test_a_json_rewrite_is_read():
    assert loop.parse_rewrite('{"persona": " You are terse. ", "summary": "Shorter."}') == (
        "You are terse.",
        "Shorter.",
    )


def test_a_fenced_rewrite_is_read():
    content = 'Here it is:\n```json\n{"persona": "You are kind.", "summary": ""}\n```'
    assert loop.parse_rewrite(content) == ("You are kind.", None)


@pytest.mark.parametrize(
    "content",
    ["", "You are kind.", '{"summary": "no persona"}', '{"persona": "   "}', '{"persona": 3}'],
)
def test_an_unusable_answer_is_refused(content):
    with pytest.raises(proposals.ProposalError) as caught:
        loop.parse_rewrite(content)
    assert caught.value.status_code == 502


def test_a_rewrite_longer_than_a_persona_is_refused():
    with pytest.raises(proposals.ProposalError):
        loop.parse_rewrite('{"persona": "' + "x" * (loop.MAX_PERSONA_CHARS + 1) + '"}')


async def test_a_rewrite_holds_no_database_connection_across_its_model_call(monkeypatch):
    """Resolving the model reads the provider configs and platform_settings on
    the request's session: that transaction ends before the model is called."""
    events: list[str] = []

    class _Llm:
        async def chat(self, _messages, **_kwargs):
            events.append("chat")
            return SimpleNamespace(content='{"persona": "Rewritten persona.", "summary": "Why."}')

    async def _get_llm_for_user(_db, _user_id):
        events.append("resolve")
        return _Llm(), "model-a"

    async def _release(_db):
        events.append("release")

    monkeypatch.setattr("app.llm.router.get_llm_for_user", _get_llm_for_user)
    monkeypatch.setattr(loop, "release_db_connection_before_loop", _release)

    agent = SimpleNamespace(id=uuid.uuid4(), name="Agent", persona="Old persona.")
    rewrite = await loop.generate_rewrite(object(), str(uuid.uuid4()), agent, {})

    assert events == ["resolve", "release", "chat"]
    assert rewrite == ("Rewritten persona.", "Why.")


def _propose_harness(monkeypatch, events: list[str]):
    """``propose`` up to its writes, every read stubbed: what it keeps or refuses."""
    baseline = SimpleNamespace(id=uuid.uuid4(), score=70.0, persona_version_number=3)

    class _Db:
        # In order: the agent is part of the evaluation; the target, as filing
        # loads it; no proposal under the same dedup key; none open on the agent.
        answers: list = []

        async def scalar(self, _query):
            return self.answers.pop(0)

        def add(self, _row):
            events.append("add")

        async def flush(self):
            events.append("flush")

    async def _none(*_args, **_kwargs):
        return None

    async def _group_ids(_db, _user_id):
        return []

    async def _baseline(_db, _evaluation_id, _agent_id):
        return baseline

    async def _active_version(_db, *, agent_id):
        return 3

    async def _evidence(_db, _agent, _baseline, _metric):
        events.append("evidence")
        return {}

    monkeypatch.setattr(loop, "get_user_group_ids", _group_ids)
    monkeypatch.setattr(loop, "writable_agent_error", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(loop, "open_improvement", _none)
    monkeypatch.setattr(loop, "latest_baseline_run", _baseline)
    monkeypatch.setattr(loop, "get_active_agent_persona_version_number", _active_version)
    monkeypatch.setattr(loop, "gather_evidence", _evidence)
    monkeypatch.setattr(lifecycle, "log_audit", _none)
    return _Db()


def _generator(events: list[str], persona: str):
    async def _generate(_db, _user_id, _agent, _evidence_payload):
        events.append("generate")
        return persona, "Why."

    return _generate


async def _propose(monkeypatch, *, base: str, rewrite: str):
    events: list[str] = []
    db = _propose_harness(monkeypatch, events)
    agent = SimpleNamespace(id=uuid.uuid4(), name="Agent", persona=base, is_company_managed=False, user_id=None)
    db.answers = [uuid.uuid4(), agent, None, None]
    evaluation = SimpleNamespace(id=uuid.uuid4(), name="Eval", quality_source=None)
    try:
        improvement = await loop.propose(
            db, user=_user(admin=True), agent=agent, evaluation=evaluation, generate=_generator(events, rewrite)
        )
    except proposals.ProposalError as exc:
        return events, exc
    return events, improvement


async def test_a_targeted_rewrite_is_kept(monkeypatch):
    events, improvement = await _propose(monkeypatch, base="Old persona.", rewrite="Rewritten persona.")

    assert events == ["evidence", "generate", "add", "flush"]
    assert (improvement.base_content, improvement.proposed_content) == ("Old persona.", "Rewritten persona.")
    assert (improvement.proposal_type, improvement.target_type) == ("persona", "agent")
    assert improvement.target_id == improvement.agent_id
    # A person's request: measured by its re-evaluation, never by an experiment.
    assert (improvement.experiment_verdict, improvement.author_agent_id) == (None, None)
    assert improvement.evidence_refs == [{"kind": "evaluation_run", "id": str(improvement.baseline_run_id)}]


async def test_a_rewrite_that_inflates_the_persona_is_refused(monkeypatch):
    base = "You answer questions about invoices. " * 100  # 3,700 characters
    events, refused = await _propose(monkeypatch, base=base, rewrite=base + "x" * 3_000)

    assert isinstance(refused, proposals.ProposalError)
    assert refused.status_code == 502
    assert "targeted changes" in refused.detail
    assert "add" not in events


async def test_a_persona_over_the_loop_limit_is_refused_before_any_model_call(monkeypatch):
    events, refused = await _propose(monkeypatch, base="x" * (loop.MAX_PERSONA_CHARS + 1), rewrite="Short.")

    assert isinstance(refused, proposals.ProposalError)
    assert refused.status_code == 422
    assert events == []


@pytest.mark.parametrize(
    ("base_length", "limit"),
    [(0, 2_000), (1_000, 3_000), (6_000, 9_000), (15_000, loop.MAX_PERSONA_CHARS)],
)
def test_a_rewrite_grows_a_persona_by_half_or_2000_characters_at_most(base_length, limit):
    assert loop.rewrite_length_limit("x" * base_length) == limit
