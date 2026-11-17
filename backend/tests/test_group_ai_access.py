"""AI switched off for a group: the reader, the request gates, the background
gate and the LLM router, each on its own surface.

The database is a stand-in: these tests pin who asks what and what they answer;
the SQL itself runs in ``test_group_ai_access_postgres.py``.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import deps
from app.llm import provider_access
from app.llm.provider_access import (
    ProviderAudience,
    ProviderNotUsableError,
    bind_acting_user,
    ensure_not_excluded,
    load_provider_audience,
    provider_usable_by,
    routing_for,
)
from app.services import agentic_gate, ai_state, group_ai_access

GROUP = str(uuid.uuid4())
PERSON = str(uuid.uuid4())


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _Db:
    """A session answering each ``execute`` from a queue and counting them."""

    def __init__(self, *answers):
        self._answers = list(answers)
        self.executed = 0

    async def execute(self, _statement):
        self.executed += 1
        return _Rows(self._answers.pop(0) if self._answers else [])


@pytest.fixture
def disabled_group():
    group_ai_access._cache = (frozenset({GROUP}), float("inf"))


def _config(**fields):
    return SimpleNamespace(
        id=uuid.uuid4(),
        access_restricted=False,
        shared_group_ids=None,
        shared_user_ids=None,
        excluded_group_ids=None,
        excluded_user_ids=None,
        available_models=["m"],
        **fields,
    )


# --- the reader ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_disabled_group_costs_no_query():
    db = _Db()
    assert await group_ai_access.member_of_disabled_group(db, PERSON) is False
    assert await group_ai_access.disabling_group_names(db, PERSON) == []
    assert db.executed == 0


@pytest.mark.asyncio
async def test_the_set_is_read_once_per_ttl_then_again_after_an_invalidation():
    group_ai_access.invalidate_cache()
    db = _Db([uuid.UUID(GROUP)], [])
    assert await group_ai_access.disabled_group_ids(db) == frozenset({GROUP})
    assert await group_ai_access.disabled_group_ids(db) == frozenset({GROUP})
    assert db.executed == 1
    group_ai_access.invalidate_cache()
    assert await group_ai_access.disabled_group_ids(db) == frozenset()
    assert db.executed == 2


@pytest.mark.asyncio
async def test_membership_is_asked_of_the_database_only_when_a_group_disables_ai(disabled_group):
    member = _Db([object()])
    assert await group_ai_access.member_of_disabled_group(member, PERSON) is True
    assert member.executed == 1
    outsider = _Db([])
    assert await group_ai_access.member_of_disabled_group(outsider, PERSON) is False
    assert outsider.executed == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("nobody", [None, "anonymous", "system", "", "not-a-uuid"])
async def test_a_caller_with_no_account_belongs_to_no_group(disabled_group, nobody):
    db = _Db()
    assert await group_ai_access.member_of_disabled_group(db, nobody) is False
    assert db.executed == 0


@pytest.mark.asyncio
async def test_groups_already_loaded_spare_the_membership_query(disabled_group):
    db = _Db()
    assert await group_ai_access.member_of_disabled_group(db, PERSON, group_ids=frozenset({GROUP})) is True
    assert await group_ai_access.member_of_disabled_group(db, PERSON, group_ids=frozenset({"other"})) is False
    assert db.executed == 0


# --- the request gates ----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_request_gate_refuses_the_bound_member_with_the_platform_code(disabled_group):
    bind_acting_user(PERSON)
    with pytest.raises(HTTPException) as refused:
        await deps.refuse_if_group_ai_disabled(_Db([object()]))
    assert refused.value.status_code == 403
    # The platform's code, so every client branching on it keeps working; the
    # message is a constant, so no group name reaches whatever client called.
    assert refused.value.detail == {
        "code": "ai_disabled",
        "message": group_ai_access.AI_DISABLED_BY_GROUP_MESSAGE,
        "scope": "group",
    }
    assert agentic_gate.refusal_code(refused.value) == "ai_disabled"
    assert agentic_gate.refusal_is_personal(refused.value) is True


@pytest.mark.asyncio
async def test_the_request_gate_lets_a_person_outside_the_group_through(disabled_group):
    bind_acting_user(PERSON)
    await deps.refuse_if_group_ai_disabled(_Db([]))


@pytest.mark.asyncio
async def test_a_public_route_binds_nobody_and_is_left_to_the_platform_switch(disabled_group):
    from contextvars import copy_context

    db = _Db()

    async def _anonymous():
        provider_access._acting_user.set(None)
        await deps.refuse_if_group_ai_disabled(db)

    await copy_context().run(_anonymous)
    assert db.executed == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["require_ai_enabled", "require_agentic_ai", "require_chat_enabled"])
async def test_every_ai_gate_asks_the_group_after_the_platform_switch(monkeypatch, disabled_group, gate):
    ai_state.prime_cache(True)
    from app.api.v1 import platform

    async def _yes(_db):
        return True

    monkeypatch.setattr(platform, "is_agentic_ai_enabled", _yes)
    monkeypatch.setattr(platform, "is_chat_enabled", _yes)
    bind_acting_user(PERSON)
    with pytest.raises(HTTPException) as refused:
        await getattr(deps, gate)(_Db([object()]))
    assert refused.value.detail["scope"] == "group"


@pytest.mark.asyncio
async def test_the_platform_switch_is_still_asked_first(disabled_group):
    ai_state.prime_cache(False)
    bind_acting_user(PERSON)
    db = _Db()
    with pytest.raises(HTTPException) as refused:
        await deps.require_ai_enabled(db)
    assert "scope" not in refused.value.detail
    assert db.executed == 0


# --- the background gate ---------------------------------------------------------


@pytest.fixture
def ai_on(monkeypatch):
    ai_state.prime_cache(True)

    async def _licensed():
        return True, True

    monkeypatch.setattr(agentic_gate, "_licence_answer", _licensed)


@pytest.mark.asyncio
async def test_a_member_s_work_is_refused_with_the_platform_code_and_marked_as_the_group(ai_on, disabled_group):
    verdict = await agentic_gate.group_verdict(_Db([object()]), PERSON)
    assert (verdict.refusal, verdict.transient, verdict.by_group) == ("ai_disabled", False, True)
    # The platform half says nothing about a person.
    assert (await agentic_gate.agentic_verdict(_Db())).refusal is None


@pytest.mark.asyncio
async def test_work_with_no_person_or_an_outsider_is_not_refused(ai_on, disabled_group):
    assert (await agentic_gate.group_verdict(_Db(), None)).refusal is None
    assert (await agentic_gate.group_verdict(_Db([]), PERSON)).refusal is None


@pytest.mark.asyncio
async def test_an_unreadable_group_state_defers_the_work_and_never_fails_it(ai_on, disabled_group):
    from sqlalchemy.exc import OperationalError

    class _Broken:
        async def execute(self, _statement):
            raise OperationalError("select", {}, Exception("down"))

    verdict = await agentic_gate.group_verdict(_Broken(), PERSON)
    assert (verdict.refusal, verdict.transient) == ("ai_disabled", True)


def test_a_platform_refusal_is_not_personal():
    for code in ("ai_disabled", "license_required"):
        platform = HTTPException(403, detail={"code": code, "message": "off"})
        assert agentic_gate.refusal_is_personal(platform) is False
    assert agentic_gate.refusal_is_personal(ValueError("no detail")) is False


@pytest.mark.asyncio
async def test_the_person_s_half_asks_nothing_for_work_with_no_person(disabled_group):
    db = _Db()
    assert (await agentic_gate.group_verdict(db, None)).refusal is None
    assert db.executed == 0


class _IsolatedDb(_Db):
    """A session with ``connection`` / ``no_autoflush``, recording the order."""

    def __init__(self, *answers):
        super().__init__(*answers)
        self.events: list[str] = []
        outer = self

        class _NoAutoflush:
            def __enter__(self):
                outer.events.append("no_autoflush")

            def __exit__(self, *exc):
                return False

        class _Savepoint:
            async def __aenter__(self):
                outer.events.append("savepoint")

            async def __aexit__(self, *exc):
                outer.events.append("savepoint_end")
                return False

        class _Conn:
            def begin_nested(self):
                return _Savepoint()

        self.no_autoflush = _NoAutoflush()
        self._conn = _Conn()

    async def connection(self):
        return self._conn

    async def execute(self, statement):
        self.events.append("execute")
        return await super().execute(statement)


@pytest.mark.asyncio
async def test_the_background_read_runs_in_a_savepoint_without_autoflush(monkeypatch, ai_on, disabled_group):
    # A run's session mid-transaction: its pending changes stay unflushed and a
    # failed SELECT cannot abort its transaction. The double stands in for a
    # real ``AsyncSession``, the only kind the isolated path is taken for.
    monkeypatch.setattr(group_ai_access, "AsyncSession", _IsolatedDb)
    db = _IsolatedDb([object()])
    verdict = await agentic_gate.group_verdict(db, PERSON)
    assert verdict.by_group is True
    assert db.events == ["no_autoflush", "savepoint", "execute", "savepoint_end"]


@pytest.mark.asyncio
async def test_the_platform_switch_off_is_not_marked_as_the_group(ai_on, disabled_group):
    ai_state.prime_cache(False)
    verdict = await agentic_gate.agentic_verdict(_Db())
    assert (verdict.refusal, verdict.by_group) == ("ai_disabled", False)


@pytest.mark.asyncio
async def test_the_person_s_half_takes_no_connection_while_no_group_disables_ai(monkeypatch):
    # The steady state of every install that does not use the feature: the
    # trigger funnels open a session for it, which must stay unused.
    class _Untouchable:
        async def connection(self):
            raise AssertionError("no connection may be taken")

        async def execute(self, _statement):
            raise AssertionError("no query may run")

    monkeypatch.setattr(group_ai_access, "AsyncSession", _Untouchable)
    assert (await agentic_gate.group_verdict(_Untouchable(), PERSON)).refusal is None


def test_the_wire_is_positive_and_only_the_service_flips_it():
    group = SimpleNamespace(ai_disabled=False)
    assert group_ai_access.group_ai_enabled(group) is True
    assert group_ai_access.set_group_ai_enabled(group, False) is True
    assert (group.ai_disabled, group_ai_access.group_ai_enabled(group)) == (True, False)
    # A group-like object from before the column reads as AI on.
    assert group_ai_access.group_ai_enabled(SimpleNamespace()) is True


# --- the LLM router ----------------------------------------------------------------


def test_an_ai_disabled_audience_may_use_no_provider_whatever_it_is_granted():
    audience = ProviderAudience(user_id=PERSON, group_ids=frozenset({GROUP}), ai_disabled=True)
    open_config = _config()
    granted = _config()
    granted.access_restricted = True
    granted.shared_user_ids = [PERSON]
    assert not provider_usable_by(open_config, audience)
    assert not provider_usable_by(granted, audience)
    routing = routing_for(audience, [open_config, granted])
    assert routing.usable == []
    assert routing.no_usable_message() == group_ai_access.AI_DISABLED_BY_GROUP_MESSAGE
    assert routing.reserved_model_message("m") == group_ai_access.AI_DISABLED_BY_GROUP_MESSAGE


def test_an_audience_with_ai_keeps_the_usual_refusals():
    routing = routing_for(ProviderAudience(user_id=PERSON), [_config()])
    assert routing.no_usable_message() == provider_access.NO_USABLE_PROVIDER_MESSAGE
    assert len(routing.usable) == 1


@pytest.mark.asyncio
async def test_the_audience_reads_the_groups_when_one_disables_ai_even_without_provider_restrictions(
    disabled_group,
):
    audience = await load_provider_audience(_Db([uuid.UUID(GROUP)]), PERSON, with_groups=False)
    assert audience.ai_disabled is True


@pytest.mark.asyncio
async def test_the_audience_stays_free_of_queries_while_no_group_disables_ai():
    db = _Db()
    audience = await load_provider_audience(db, PERSON, with_groups=False)
    assert (audience.ai_disabled, db.executed) == (False, 0)


@pytest.mark.asyncio
async def test_a_member_s_content_is_never_embedded_or_drawn_by_a_platform_capability(disabled_group):
    # Embeddings and images stay platform-level for everyone else, and a config
    # with no exclusion used to be waved through before any audience was read.
    with pytest.raises(ProviderNotUsableError) as refused:
        await ensure_not_excluded(_Db([uuid.UUID(GROUP)]), _config(), "Embedding", PERSON)
    assert str(refused.value) == group_ai_access.AI_DISABLED_BY_GROUP_MESSAGE
    await ensure_not_excluded(_Db([]), _config(), "Embedding", PERSON)
