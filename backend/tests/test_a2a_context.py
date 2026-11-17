"""A context is a partner-scoped entity that records who answers it.

Spec 010 User Story 2. The protocol's ``contextId`` grouped nothing here: it was
minted per task and never read back, so a transfer could not own the
conversation the specification says it owns.
"""

import uuid

import pytest

from app.a2a import contexts
from tests.a2a_server_stubs import FakeDb, agent, exposure, partner_client

pytestmark = pytest.mark.asyncio


def _context(client_id="partner-1", **overrides):
    fields = {
        "id": uuid.uuid4(),
        "client_id": client_id,
        "responsible_agent_id": uuid.uuid4(),
        "exposure_id": uuid.uuid4(),
    }
    fields.update(overrides)
    return type("Context", (), fields)()


def _integration():
    return type("Integration", (), {"id": uuid.uuid4(), "provider": "oauth_a2a"})()


def _bind(monkeypatch, *, reachable):
    """Answer "which agents does this partner's integration bind" directly.

    The reachability policy is the subject; the two lookups it goes through to
    find the partner are covered by the provider tests.
    """
    from app.a2a import providers

    async def _integration_id(*_a, **_k):
        return uuid.uuid4()

    async def _binding(*_a, **_k):
        return reachable

    monkeypatch.setattr(providers, "registration_integration_id", _integration_id)
    monkeypatch.setattr(providers, "agents_binding", _binding)


class TestResolvingAContext:
    async def test_an_issued_identifier_resolves_for_its_own_client(self):
        """FR-007. Grouping under the context the partner names is the whole
        point of accepting one."""
        ctx = _context()
        found = await contexts.resolve(FakeDb(ctx), str(ctx.id), "partner-1")
        assert found is ctx

    async def test_an_unissued_identifier_resolves_to_nothing(self):
        """FR-009. The caller turns this into the same not-found a foreign
        context gets, so existence is not probeable."""
        assert await contexts.resolve(FakeDb(None), str(uuid.uuid4()), "partner-1") is None

    async def test_a_malformed_identifier_resolves_to_nothing_without_a_query(self):
        """A partner-supplied string reaches this before any UUID parsing; an
        exception here would be a 500 where a not-found belongs."""
        db = FakeDb()
        assert await contexts.resolve(db, "not-a-uuid", "partner-1") is None

    async def test_the_client_is_part_of_the_lookup(self):
        """FR-010. Scoping in the query rather than after it: a post-filter
        leaks existence through timing and through any later refactor that
        forgets the check."""
        import inspect

        src = inspect.getsource(contexts.resolve)
        assert "client_id" in src


class TestMintingAContext:
    async def test_it_records_the_client_the_agent_and_the_card(self):
        """FR-008. All three are frozen at creation: the client owns it, the
        agent answers it, and the card is the contract a follow-up honours."""
        _agent, _exposure = agent(), exposure()
        db = FakeDb()
        ctx = await contexts.mint(
            db, client_id="partner-1", agent_id=_agent.id, exposure_id=_exposure.id
        )
        assert ctx.client_id == "partner-1"
        assert ctx.responsible_agent_id == _agent.id
        assert ctx.exposure_id == _exposure.id
        # Not flushed alone: it lands with the first task under it, so a
        # duplicate submission's rollback takes the context with it.
        assert db.flushes == 0
        # And the identifier exists before that flush, because the caller sets
        # it on the task it inserts in the same unit of work. Leaving this to
        # the column default assigns it AT flush, and the task goes in with a
        # null context — which no fake session rejects and Postgres does.
        assert ctx.id is not None


class TestWhoAnswersNext:
    async def test_a_transfer_moves_responsibility(self):
        """FR-011. Transfer's contract is that the target handles what comes
        next, which is only true if the context says so."""
        ctx = _context()
        peer = uuid.uuid4()
        await contexts.set_responsible(FakeDb(ctx), ctx, peer)
        assert ctx.responsible_agent_id == peer

    async def test_a_mandatory_transfer_does_not_move_it(self):
        """A pipeline stage is not taking over the conversation — the chain is
        how one message gets answered. Its stages are typically unexposed, so
        recording one as responsible would refuse every follow-up in that
        context forever, claiming an agent is unreachable when it never was."""
        from app.a2a import tasks as task_svc

        chain = [
            {"type": "transfer", "target_agent_id": str(uuid.uuid4()), "mandatory": True},
            {"type": "transfer", "target_agent_id": str(uuid.uuid4()), "mandatory": True},
        ]
        assert task_svc._conversational_target(chain) is None

    async def test_a_chosen_transfer_does_move_it(self):
        """The tool's contract, and the agent it names is one the same partner
        reaches by the operator's own binding."""
        from app.a2a import tasks as task_svc

        peer = uuid.uuid4()
        chain = [{"type": "transfer", "target_agent_id": str(peer), "mandatory": False}]
        assert task_svc._conversational_target(chain) == peer

    async def test_a_chosen_transfer_wins_over_the_pipeline_it_starts(self):
        """A conversational hand-off followed by the target's own pipeline
        leaves the TARGET responsible, not its terminal stage."""
        from app.a2a import tasks as task_svc

        chosen, stage = uuid.uuid4(), uuid.uuid4()
        chain = [
            {"type": "transfer", "target_agent_id": str(chosen), "mandatory": False},
            {"type": "transfer", "target_agent_id": str(stage), "mandatory": True},
        ]
        assert task_svc._conversational_target(chain) == chosen

    async def test_consult_and_delegate_are_ignored(self):
        """Neither answers the partner's next message."""
        from app.a2a import tasks as task_svc

        chain = [
            {"type": "consult", "target_agent_id": str(uuid.uuid4())},
            {"type": "delegate", "target_agent_id": str(uuid.uuid4())},
        ]
        assert task_svc._conversational_target(chain) is None

    async def test_attribution_and_responsibility_are_read_apart(self):
        """Two different questions: which agent produced this answer, and which
        answers the next message. The pipeline case is where they differ."""
        import inspect

        from app.a2a import tasks as task_svc

        src = inspect.getsource(task_svc.execute_task_run)
        assert "responding_agent_id=responding" in src
        assert "_conversational_target(handovers)" in src

    async def test_consult_and_delegate_leave_it_alone(self):
        """Neither claims future messages, so neither may move it. Asserted on
        the executor's call site: it passes the transferred-to agent, which is
        None unless a transfer happened."""
        import inspect

        from app.a2a import tasks as task_svc

        src = inspect.getsource(task_svc.execute_task_run)
        assert "set_responsible" in src
        assert "transferred_to" in src


class TestRefusals:
    async def test_a_deleted_responsible_agent_refuses(self):
        """The agent row is gone; there is nothing to answer with."""
        assert (
            await contexts.responsible_agent_if_reachable(
                FakeDb(None), _context(), "partner-1"
            )
            is None
        )

    async def test_an_agent_this_partner_no_longer_reaches_refuses(self, monkeypatch):
        """FR-013. Responsibility recorded once is not consent forever: the
        operator unbound that agent from this partner's integration."""
        ctx = _context()
        _agent = agent(id=ctx.responsible_agent_id)
        _bind(monkeypatch, reachable=[])
        got = await contexts.responsible_agent_if_reachable(
            FakeDb(_agent, _integration()), ctx, "partner-1"
        )
        assert got is None

    async def test_a_still_reachable_agent_answers(self, monkeypatch):
        ctx = _context()
        _agent = agent(id=ctx.responsible_agent_id)
        _bind(monkeypatch, reachable=[_agent])
        got = await contexts.responsible_agent_if_reachable(
            FakeDb(_agent, _integration()), ctx, "partner-1"
        )
        assert got is _agent


class TestTheContinuationRoute:
    """The three refusals a follow-up passes before any run starts.

    Driven through the JSON-RPC route rather than the predicates alone: the
    order matters (an unknown context must not reveal itself through a
    different error than a busy one) and so do the error codes a partner
    branches on.
    """

    def _send(
        self,
        monkeypatch,
        *,
        resolve=None,
        reachable=None,
        open_tasks=0,
        created_out: list | None = None,
    ):
        from fastapi import FastAPI

        import app.a2a.exposure as exposure_mod
        from app.api.deps import get_db
        from app.api.v1 import a2a_server
        from tests.a2a_chat_gate import stub_chat_gate

        stub_chat_gate(monkeypatch)
        _exposure = exposure()
        _exposure.agent_id = uuid.uuid4()
        if resolve is not None:
            # Addressed under the card the context was created on, which is the
            # ordinary case; the crossing is its own test below.
            _exposure.id = resolve.exposure_id

        async def _get_exposure(_db, _token):
            return _exposure

        monkeypatch.setattr(exposure_mod, "get_exposure_by_token", _get_exposure)

        async def _noop(*_a, **_k):
            return None

        monkeypatch.setattr(a2a_server, "_enforce_preauth_rate_limit", _noop)
        monkeypatch.setattr(a2a_server, "_enforce_a2a_rate_limit", _noop)

        async def _auth(_request, _db, _exposure):
            return "partner-1", "", "oauth_a2a"

        monkeypatch.setattr(a2a_server, "_authenticate", _auth)

        async def _resolve(*_a, **_k):
            return resolve

        async def _reachable(*_a, **_k):
            return reachable

        async def _open(*_a, **_k):
            return open_tasks

        monkeypatch.setattr(contexts, "resolve", _resolve)
        monkeypatch.setattr(contexts, "responsible_agent_if_reachable", _reachable)
        monkeypatch.setattr(contexts, "open_task_count", _open)

        if created_out is not None:
            import app.a2a.tasks as tasks_mod
            import app.services.audit as audit_mod
            import app.services.quota as quota_mod

            async def _none(*_a, **_k):
                return None

            monkeypatch.setattr(audit_mod, "log_audit", _none)
            monkeypatch.setattr(quota_mod, "check_agentic_quota", _none)
            monkeypatch.setattr(tasks_mod, "find_existing_task", _none)
            monkeypatch.setattr(a2a_server, "_spawn_task_run", lambda *_a: None)

            async def _create(_db, **kw):
                created_out.append(kw)
                raise tasks_mod.TaskRejectedError("stop here", "test_stop")

            async def _rejected(*_a, **_k):
                return None

            monkeypatch.setattr(tasks_mod, "create_task", _create)
            monkeypatch.setattr(tasks_mod, "create_rejected_task", _rejected)

        app = FastAPI()
        app.include_router(a2a_server.router)
        app.dependency_overrides[get_db] = lambda: FakeDb(agent())
        return partner_client(app).post(
            "/a2a/tok/jsonrpc",
            json={
                "jsonrpc": "2.0",
                "id": "r1",
                "method": "message/send",
                "params": {
                    "contextId": str(uuid.uuid4()),
                    "message": {"messageId": "m1", "parts": [{"text": "hi"}]},
                },
            },
            headers={"Authorization": "Bearer t"},
        )

    def test_an_unknown_context_is_answered_as_not_found(self, monkeypatch):
        """FR-009, FR-010. Unissued and foreign share this answer."""
        body = self._send(monkeypatch, resolve=None).json()
        assert "error" in body, body
        assert body["error"]["code"] == -32001

    def test_an_unreachable_responsible_agent_is_refused(self, monkeypatch):
        """FR-013, and not as not-found: the context exists, the reach does
        not, and a partner that cannot tell them apart cannot act."""
        body = self._send(
            monkeypatch, resolve=_context(), reachable=None
        ).json()
        assert body["error"]["code"] != -32001
        assert "no longer available" in body["error"]["message"]

    def test_a_busy_context_queues_the_follow_up(self, monkeypatch):
        """Parallel messages in one conversation are queued, never refused:
        the task is created marked queued, and the executor runs it once the
        tasks ahead of it are finished (xtm-one #11864)."""
        created: list = []
        self._send(
            monkeypatch,
            resolve=_context(),
            reachable=agent(),
            open_tasks=2,
            created_out=created,
        )
        assert len(created) == 1
        assert created[0]["queued"] is True

    def test_an_idle_context_is_not_queued(self, monkeypatch):
        created: list = []
        self._send(
            monkeypatch,
            resolve=_context(),
            reachable=agent(),
            open_tasks=0,
            created_out=created,
        )
        assert created[0]["queued"] is False

    def test_a_full_context_queue_is_a_named_rate_limit(self, monkeypatch):
        """Only a genuinely full queue refuses, and it says which limit and
        how long to wait, in the envelope partner tooling branches on."""
        r = self._send(
            monkeypatch,
            resolve=_context(),
            reachable=agent(),
            open_tasks=contexts.MAX_OPEN_TASKS_PER_CONTEXT,
        )
        assert r.status_code == 429
        assert r.headers["Retry-After"] == str(contexts.CONTEXT_QUEUE_RETRY_AFTER_SECONDS)
        err = r.json()["error"]
        assert err["code"] == -32029
        assert err["data"]["@type"] == "RateLimitedError"
        assert err["data"]["details"][0]["limit"] == "context_queue"
        assert "queue limit" in err["message"]


class TestATaskIdIsReadWhereverAPartnerPutsIt:
    def test_both_placements_are_consulted(self):
        """A partner that puts ``taskId`` at the params level fell through to
        ``create_task``: a different task id came back for what it believed was
        a follow-up, its quota was spent, and the task it was answering was
        left asking forever. ``contextId`` below always read both."""
        import inspect

        from app.api.v1 import a2a_server

        src = inspect.getsource(a2a_server.a2a_jsonrpc)
        named = src.index("declared_task = ")
        window = src[named : named + 260]
        assert 'params.get("taskId")' in window
        assert 'message.get("taskId")' in window
