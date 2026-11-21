"""``input-required``: control returns to the model, never to a human.

FR-031, FR-046, FR-048, FR-049. The decision this file pins is the one the
clarification session spent the most effort on: when a partner asks for more
input, the tool returns to the calling agent with the question and the task
id, and the agent answers by calling the tool again. Parking the run — asking
the USER to answer the partner — was rejected as a different feature needing a
UX surface that does not exist.

Two consequences of that choice are accepted rather than solved, and both are
tested here because "accepted" only means anything if the bound is real:

* a continuation must be verified to belong to a delegation THIS run made, or
  a partner could talk the model into resuming a task it never owned; and
* a partner can farm turns by asking forever, bounded by the run's existing
  budgets rather than by a new limit.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.a2a.client import delegation
from app.models.a2a import OUTCOME_COMPLETED, OUTCOME_REFUSED_PREFLIGHT
from tests.a2a_client_stubs import (
    RecordingDb,
    client_for,
    integration_row,
    rpc,
    settings_row,
)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    from tests.a2a_chat_gate import stub_chat_gate

    stub_chat_gate(monkeypatch)
    monkeypatch.setattr(delegation, "verdict_is_stale", lambda _s: False)

    async def _headers(_i, _c):
        return {"Authorization": "Bearer minted"}

    monkeypatch.setattr(delegation, "outbound_headers", _headers)


class TestControlReturnsToTheModel:
    @pytest.mark.asyncio
    async def test_input_required_returns_the_question_and_the_task_id(
        self, monkeypatch
    ):
        def handler(_request):
            return rpc(
                {
                    "id": "task-77",
                    "status": {
                        "state": "input-required",
                        "message": {"parts": [{"text": "Which tenant?"}]},
                    },
                }
            )

        monkeypatch.setattr(httpx, "AsyncClient", client_for(handler))
        result = await delegation.delegate(
            RecordingDb(), integration_row(), settings_row(), message="triage"
        )
        assert result.needs_input
        assert result.question == "Which tenant?"
        # The task id is what makes a continuation possible at all.
        assert result.partner_task_id == "task-77"

    @pytest.mark.asyncio
    async def test_no_human_is_waited_on(self, monkeypatch):
        # The whole point of the decision. If this ever parks, a partner's
        # task sits open for hours and the run holds resources waiting for a
        # person who was never told they were needed.
        def handler(_request):
            return rpc(
                {"id": "t", "status": {"state": "input-required", "message": {"parts": [{"text": "?"}]}}}
            )

        monkeypatch.setattr(httpx, "AsyncClient", client_for(handler))
        result = await delegation.delegate(
            RecordingDb(), integration_row(), settings_row(), message="x"
        )
        # It RETURNS — it does not block, and it does not raise.
        assert result.outcome == OUTCOME_COMPLETED
        assert result.needs_input

    @pytest.mark.asyncio
    async def test_a_question_with_no_text_still_returns_something_answerable(
        self, monkeypatch
    ):
        # A partner asking for input without saying what it wants is unhelpful
        # but not a failure; the agent gets a usable prompt rather than None.
        def handler(_request):
            return rpc({"id": "t", "status": {"state": "input-required"}})

        monkeypatch.setattr(httpx, "AsyncClient", client_for(handler))
        result = await delegation.delegate(
            RecordingDb(), integration_row(), settings_row(), message="x"
        )
        assert result.needs_input and result.question


class TestContinuationOwnership:
    """FR-046: a run may only continue a task it started.

    ``continue_task_id`` is the one field of a delegation the calling model
    supplies verbatim. Unchecked, a model could resume a task another run — or
    another partner — started, and read the answer.
    """

    @pytest.mark.asyncio
    async def test_a_task_this_run_did_not_start_is_refused(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            httpx, "AsyncClient", client_for(lambda r: called.append(r) or rpc({}))
        )
        result = await delegation.delegate(
            RecordingDb(owns_continuation=False),
            integration_row(),
            settings_row(),
            message="resume it",
            continue_task_id="someone-elses-task",
        )
        assert result.outcome == OUTCOME_REFUSED_PREFLIGHT
        assert result.partner_reason == "continuation_not_owned"
        # Refused before the partner is contacted: the id itself is the thing
        # in question, so asking about it would already have leaked it.
        assert called == []

    @pytest.mark.asyncio
    async def test_the_ownership_lookup_is_scoped_to_the_run_when_there_is_one(self):
        # ``run_ref`` is NULL on the chat path, so matching it unconditionally
        # would refuse every continuation there. Constrained only when the
        # caller has one.
        import inspect

        src = inspect.getsource(delegation._may_continue)
        assert "A2ADelegation.integration_id == integration.id" in src
        assert "_scoped_to_caller(stmt, calling)" in src
        assert "if run_ref is None:" in inspect.getsource(delegation._scoped_to_caller)

    @pytest.mark.asyncio
    async def test_the_lookup_is_scoped_to_the_user_and_the_target(self):
        # ``run_ref`` is NULL on the chat path, so the user is what keeps one user
        # out of another's task. Asserted on the statement issued, not the source.
        import uuid

        issued = []

        class _Db(RecordingDb):
            async def execute(self, stmt):
                issued.append(stmt)
                return await super().execute(stmt)

        calling = delegation.CallingRun(agent_id=uuid.uuid4(), user_id=uuid.uuid4())
        target = settings_row(id=uuid.uuid4())
        assert await delegation._may_continue(
            _Db(), integration_row(), target, calling, "t-1"
        )
        where = str(issued[0].compile())
        for column in ("integration_id", "partner_task_id", "user_id", "target_id"):
            assert f"a2a_delegations.{column}" in where, column
        # Chat path: no run to match.
        assert "a2a_delegations.run_ref" not in where

    def test_a_run_may_collect_a_task_its_agent_started_in_a_run_that_has_ended(self) -> None:
        """#12079: a scheduled run handed a task back and ended; a later run of
        the SAME agent for the SAME user may collect it. Another agent, another
        user, or a run still going stays refused (FR-046)."""
        import uuid

        from sqlalchemy import select

        from app.models.a2a import A2ADelegation

        calling = delegation.CallingRun(agent_id=uuid.uuid4(), user_id=uuid.uuid4(), run_ref=uuid.uuid4())
        stmt = delegation._scoped_to_caller(select(A2ADelegation.id), calling)
        where = str(stmt.compile(compile_kwargs={"literal_binds": False}))
        assert "a2a_delegations.user_id" in where
        assert "a2a_delegations.run_ref = " in where
        # The alternative: this agent, and a run in a terminal status.
        assert "a2a_delegations.agent_id" in where
        assert "a2a_delegations.run_ref IN (SELECT assignment_runs.id" in where
        assert "assignment_runs.status IN" in where
        params = stmt.compile().params
        assert calling.agent_id in params.values()
        assert calling.user_id in params.values()

    def test_a_later_turn_of_the_same_conversation_may_continue(self) -> None:
        """#448: a chat turn is its own run, so the turn that receives the
        partner's question is never the turn that started the task. The same
        agent and user in the SAME conversation may continue it."""
        import uuid

        from sqlalchemy import select

        from app.models.a2a import A2ADelegation

        calling = delegation.CallingRun(
            agent_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            run_ref=uuid.uuid4(),
            conversation_ref=uuid.uuid4(),
        )
        stmt = delegation._scoped_to_caller(select(A2ADelegation.id), calling)
        where = str(stmt.compile())
        # Still scoped to the user, whatever the branch.
        assert "a2a_delegations.user_id = " in where
        assert "a2a_delegations.conversation_ref = " in where
        params = stmt.compile().params
        assert calling.conversation_ref in params.values()
        assert calling.agent_id in params.values()
        assert calling.user_id in params.values()

    def test_no_conversation_means_no_conversation_branch(self) -> None:
        # A scheduled run has no conversation: the run and ended-run rules
        # stay the only ways in.
        import uuid

        from sqlalchemy import select

        from app.models.a2a import A2ADelegation

        calling = delegation.CallingRun(agent_id=uuid.uuid4(), user_id=uuid.uuid4(), run_ref=uuid.uuid4())
        where = str(delegation._scoped_to_caller(select(A2ADelegation.id), calling).compile())
        assert "a2a_delegations.conversation_ref" not in where

    def test_a_caller_without_an_agent_keeps_the_strict_run_match(self) -> None:
        import uuid

        from sqlalchemy import select

        from app.models.a2a import A2ADelegation

        calling = SimpleNamespace(
            agent_id=None, user_id=uuid.uuid4(), run_ref=uuid.uuid4(), conversation_ref=uuid.uuid4()
        )
        where = str(delegation._scoped_to_caller(select(A2ADelegation.id), calling).compile())
        assert "assignment_runs" not in where
        assert "a2a_delegations.conversation_ref" not in where
        assert "a2a_delegations.run_ref = " in where

    def test_a_run_of_the_same_work_item_may_collect_while_the_starter_runs(self) -> None:
        """#600: a run wound down by the iteration backstop releases its work
        item before its own end is written, so the next run of that item
        starts while the starting run still reads as running. The same agent
        dispatched for the SAME item may collect; the item id is matched in
        the runs' dispatcher record, never a free payload."""
        import uuid

        from sqlalchemy import select

        from app.models.a2a import A2ADelegation

        item = uuid.uuid4()
        calling = delegation.CallingRun(
            agent_id=uuid.uuid4(), user_id=uuid.uuid4(), run_ref=uuid.uuid4(), work_item_ref=item
        )
        stmt = delegation._scoped_to_caller(select(A2ADelegation.id), calling)
        where = str(stmt.compile())
        assert "a2a_delegations.user_id = " in where
        assert "assignment_runs.trigger_context" in where
        params = stmt.compile().params
        assert str(item) in params.values()
        assert "work_item" in params.values()
        assert calling.agent_id in params.values()

    def test_no_work_item_means_no_work_item_branch(self) -> None:
        import uuid

        from sqlalchemy import select

        from app.models.a2a import A2ADelegation

        calling = delegation.CallingRun(agent_id=uuid.uuid4(), user_id=uuid.uuid4(), run_ref=uuid.uuid4())
        where = str(delegation._scoped_to_caller(select(A2ADelegation.id), calling).compile())
        assert "trigger_context" not in where

    def test_the_refusal_names_the_owning_run_and_never_says_resend(self) -> None:
        """#600: the old text told the model to send the work as a new
        delegation, which duplicated it on the partner's side."""
        import uuid

        owner = uuid.uuid4()
        named = delegation._not_owned_detail(owner)
        assert str(owner) in named
        assert "Do not resend" in named
        assert "a2a.delegation.completed" in named
        generic = delegation._not_owned_detail(None)
        assert "Do not resend" in generic
        assert "Send the work as a new delegation" not in generic

    @pytest.mark.asyncio
    async def test_the_owner_lookup_is_scoped_to_the_callers_agent_and_user(self) -> None:
        import uuid

        issued: list[Any] = []
        owner = uuid.uuid4()

        class _Result:
            def scalar_one_or_none(self) -> uuid.UUID:
                return owner

        class _Db:
            async def execute(self, stmt: Any) -> _Result:
                issued.append(stmt)
                return _Result()

        calling = delegation.CallingRun(agent_id=uuid.uuid4(), user_id=uuid.uuid4(), run_ref=uuid.uuid4())
        found = await delegation._sibling_owner_run(
            _Db(), integration_row(), settings_row(id=uuid.uuid4()), calling, "t-1"
        )
        assert found == owner
        where = str(issued[0].compile())
        for column in ("integration_id", "partner_task_id", "agent_id", "user_id", "target_id"):
            assert f"a2a_delegations.{column}" in where, column

    @pytest.mark.asyncio
    async def test_an_unreadable_owner_keeps_the_generic_refusal(self) -> None:
        import uuid

        class _Db:
            async def execute(self, _stmt: object) -> None:
                raise RuntimeError("aborted transaction")

        calling = delegation.CallingRun(agent_id=uuid.uuid4(), user_id=uuid.uuid4())
        assert await delegation._sibling_owner_run(_Db(), integration_row(), settings_row(), calling, "t-1") is None


class TestContinuation:
    @pytest.mark.asyncio
    async def test_a_continuation_resumes_the_same_task(self, monkeypatch):
        # FR-031: a continuation MUST NOT create a new task. Creating one
        # would answer the partner's question in a conversation it is not
        # having, and return an unrelated task id to its follow-up.
        bodies = []

        def handler(request):
            bodies.append(request.content.decode())
            return rpc(
                {
                    "id": "task-77",
                    "status": {"state": "completed"},
                    "artifacts": [{"parts": [{"text": "done"}]}],
                }
            )

        monkeypatch.setattr(httpx, "AsyncClient", client_for(handler))
        result = await delegation.delegate(
            RecordingDb(),
            integration_row(),
            settings_row(),
            message="the tenant is contoso",
            continue_task_id="task-77",
        )
        assert result.outcome == OUTCOME_COMPLETED
        assert result.result_text == "done"
        # The submission names the task being continued.
        assert "task-77" in bodies[0]


class TestAuthorisationRequired:
    @pytest.mark.asyncio
    async def test_an_unsupplyable_authorisation_fails_without_retry(
        self, monkeypatch
    ):
        # FR-049. Distinct from input-required: the partner wants something
        # the PLATFORM cannot supply, so asking the agent to try again would
        # loop until a budget ran out for no reason.
        calls = []

        def handler(_request):
            calls.append(1)
            return rpc(
                {"id": "t", "status": {"state": "failed", "reason": "auth-required"}}
            )

        monkeypatch.setattr(httpx, "AsyncClient", client_for(handler))
        result = await delegation.delegate(
            RecordingDb(), integration_row(), settings_row(), message="x"
        )
        assert result.outcome == "partner_failed"
        assert result.partner_reason == "auth-required"
        # One submission, no retry loop.
        assert len(calls) == 1


class TestTurnFarmingIsBounded:
    @pytest.mark.asyncio
    async def test_a_partner_asking_forever_is_bounded_by_the_run_not_by_us(
        self, monkeypatch
    ):
        # FR-048: no new per-delegation turn limit. Each round is a separate
        # tool call, so the run's existing iteration, token, spend and
        # wall-clock budgets are what stop it — and each round returns
        # promptly rather than blocking, which is what makes those budgets
        # able to act at all.
        def handler(_request):
            return rpc(
                {"id": "t", "status": {"state": "input-required", "message": {"parts": [{"text": "again?"}]}}}
            )

        monkeypatch.setattr(httpx, "AsyncClient", client_for(handler))
        for _ in range(3):
            result = await delegation.delegate(
                RecordingDb(), integration_row(), settings_row(), message="x", continue_task_id="t"
            )
            assert result.needs_input
        # No limit of our own was introduced.
        import inspect

        src = inspect.getsource(delegation.delegate)
        assert "max_continuations" not in src
        assert "turn_limit" not in src


class TestTaskIdPrefix:
    """#978: a shortened id of the caller's own task names it; anything else
    is refused with the full-id remedy, never read as another caller's."""

    class _PrefixDb(RecordingDb):
        """Exact lookup misses; the prefix lookup answers *matches*."""

        def __init__(self, matches):
            super().__init__(owns_continuation=False)
            self.matches = matches
            self.issued = []

        async def execute(self, stmt):
            self.issued.append(stmt)
            matches = self.matches

            class _Scalars:
                def all(self):
                    return matches

                def first(self):
                    return None

            class _Result:
                def scalar_one_or_none(self):
                    return None

                def scalars(self):
                    return _Scalars()

            return _Result()

    @pytest.mark.asyncio
    async def test_a_unique_owned_prefix_resolves(self):
        full = "99c39a2e-a078-42d8-9196-a6319f2d5bfa"
        db = self._PrefixDb([full])
        got = await delegation._resolve_owned_prefix(db, integration_row(), settings_row(), None, "99c39a2e")
        assert got == full

    @pytest.mark.asyncio
    async def test_an_ambiguous_prefix_does_not_resolve(self):
        db = self._PrefixDb(["99c39a2e-aaaa", "99c39a2e-bbbb"])
        assert await delegation._resolve_owned_prefix(db, integration_row(), settings_row(), None, "99c39a2e") is None

    @pytest.mark.asyncio
    async def test_a_short_prefix_is_never_looked_up(self):
        db = self._PrefixDb(["99c39a2e-aaaa"])
        assert await delegation._resolve_owned_prefix(db, integration_row(), settings_row(), None, "99c3") is None
        assert db.issued == []

    @pytest.mark.asyncio
    async def test_the_prefix_lookup_is_scoped_like_the_ownership_check(self):
        import uuid

        calling = delegation.CallingRun(agent_id=uuid.uuid4(), user_id=uuid.uuid4(), run_ref=uuid.uuid4())
        db = self._PrefixDb([])
        await delegation._resolve_owned_prefix(
            db, integration_row(), settings_row(id=uuid.uuid4()), calling, "99c39a2e"
        )
        where = str(db.issued[0].compile())
        for column in ("integration_id", "partner_task_id", "user_id", "target_id", "run_ref"):
            assert f"a2a_delegations.{column}" in where, column
        assert "LIKE" in where

    @pytest.mark.asyncio
    async def test_delegate_collects_through_a_resolved_prefix(self, monkeypatch):
        full = "99c39a2e-a078-42d8-9196-a6319f2d5bfa"
        sent = []

        def handler(request):
            sent.append(request.content.decode())
            return rpc({"id": full, "status": {"state": "input-required", "message": {"parts": [{"text": "?"}]}}})

        monkeypatch.setattr(httpx, "AsyncClient", client_for(handler))
        result = await delegation.delegate(
            self._PrefixDb([full]), integration_row(), settings_row(), message="go on", continue_task_id="99c39a2e"
        )
        assert result.partner_reason != "continuation_not_owned"
        assert sent and full in sent[0]

    @pytest.mark.asyncio
    async def test_an_unresolved_id_names_the_full_id_remedy(self, monkeypatch):
        monkeypatch.setattr(httpx, "AsyncClient", client_for(lambda r: rpc({})))
        result = await delegation.delegate(
            self._PrefixDb([]), integration_row(), settings_row(), message="x", continue_task_id="99c39a2e"
        )
        assert result.partner_reason == "continuation_not_owned"
        assert "full task id" in result.detail
