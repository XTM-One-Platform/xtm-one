"""Authorization tests for the routes that act on an autonomous run.

``POST /api/v1/platform/autonomous/runs/{openaev_run_id}/cancel`` used to check
only that the deployment was licensed: any signed-in user of a licensed install
who knew (or guessed) an ``openaev_run_id`` could terminally cancel that run's
orchestration and, with ``purge=true``, delete its coordination state and clear
the shared orchestrator's cross-run memory - dropping memory other concurrent
engagements rely on. ``start`` on a known run id resets that run and records its
caller as the starter, and ``wake`` told anyone whether a run existed.

Every route that acts on a known run now asks one rule - the starter, an admin,
or the registered platform that started the run acting for an operator - and
answers anyone else exactly as an unknown run, doing nothing. These tests pin:

1. the pure entitlement matrix (:func:`caller_may_manage_autonomous_run`), and
2. the three routes: a refused caller gets the unknown-run answer, nothing is
   cancelled, purged, reset, created or woken, and the attempt against an
   existing run is audited as a security alert; the entitled callers keep
   working; idempotency for a missing / terminal execution; the row lock the
   decision is made under; and that ``start`` records the issuer the rule reads.

All stubbed - no database, Redis or LLM is touched.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services.autonomous_pentest import caller_may_manage_autonomous_run

ISSUER = "https://openaev.example.test"
OTHER_ISSUER = "https://opencti.example.test"
# The URL the same OpenAEV registration carries once the platform moved.
MOVED_ISSUER = "https://aev.new-domain.example.test"
_IDENTIFIERS = {ISSUER: "openaev", MOVED_ISSUER: "openaev", OTHER_ISSUER: "opencti"}


def _user(
    *,
    user_id: str | None = None,
    is_admin: bool = False,
    kind: str | None = "session_jwt",
    issuer: str | None = None,
    identifier: str | None = None,
    impersonated_by: str | None = None,
):
    return SimpleNamespace(
        id=user_id or str(uuid.uuid4()),
        email="op@example.test",
        is_admin=is_admin,
        auth_credential_kind=kind,
        platform_issuer=issuer,
        platform_identifier=identifier,
        impersonated_by=impersonated_by,
    )


def _platform(issuer: str | None = ISSUER, *, identifier: str | None = None, **kwargs):
    """A cross-platform JWT caller, tagged as ``deps`` tags it: the issuer and
    the identifier of the active registration that issuer belongs to."""
    return _user(
        kind="platform_jwt",
        issuer=issuer,
        identifier=identifier or _IDENTIFIERS.get(issuer),
        **kwargs,
    )


def _payload(**overrides):
    payload = {
        "openaev_run_id": "run-1",
        "user_id": str(uuid.uuid4()),
        "started_by_platform": "openaev",
        "started_by_issuer": ISSUER,
        "callback_base_url": ISSUER,
        "agent_slug": "openaev-attack-path-orchestrator",
    }
    payload.update(overrides)
    return payload


def _issuer_only_payload(**overrides):
    """A run that recorded its platform's URL but not its identifier."""
    payload = _payload(**overrides)
    payload.pop("started_by_platform", None)
    return payload


def _legacy_payload(**overrides):
    """A run started before the starting platform was recorded at all."""
    payload = _issuer_only_payload(**overrides)
    payload.pop("started_by_issuer", None)
    return payload


class TestCallerEntitlement:
    def test_admin_is_entitled_for_an_existing_run(self):
        assert caller_may_manage_autonomous_run(user=_user(is_admin=True), payload=_payload())

    def test_admin_is_entitled_when_the_execution_is_missing(self):
        assert caller_may_manage_autonomous_run(user=_user(is_admin=True), payload=None)

    def test_the_starter_is_entitled_whatever_the_credential(self):
        started_by = str(uuid.uuid4())
        for kind in ("session_jwt", "api_key"):
            assert caller_may_manage_autonomous_run(
                user=_user(user_id=started_by, kind=kind),
                payload=_payload(user_id=started_by),
            )

    def test_a_different_signed_in_user_is_not_entitled(self):
        # The exact hole closed here: a plain signed-in user who is not the
        # starter, not an admin and not the platform.
        assert not caller_may_manage_autonomous_run(user=_user(), payload=_payload())

    def test_an_api_key_caller_is_not_entitled_by_credential_alone(self):
        assert not caller_may_manage_autonomous_run(user=_user(kind="api_key"), payload=_payload())

    def test_the_starting_platform_is_entitled_for_another_operator(self):
        # OpenAEV's normal path: a cross-platform JWT whose issuer is the one
        # recorded at start. A DIFFERENT authorized operator (so user_id does not
        # match) must still be able to stop the run.
        assert caller_may_manage_autonomous_run(user=_platform(), payload=_payload())

    def test_the_starting_platform_is_entitled_after_it_changed_its_url(self):
        # OpenAEV moved (new domain, scheme, port or a trailing slash) and
        # re-registered: the SAME registration now carries the new URL, so its
        # JWTs carry a new issuer. Every run started before the move must stay
        # manageable by all its operators - restart, resume, pause, stop, delete.
        assert caller_may_manage_autonomous_run(user=_platform(MOVED_ISSUER), payload=_payload())

    def test_another_registered_platform_is_not_entitled(self):
        assert not caller_may_manage_autonomous_run(user=_platform(OTHER_ISSUER), payload=_payload())

    def test_the_platform_identity_decides_not_the_url(self):
        # A different registration is refused even at the URL the run was
        # started under.
        assert not caller_may_manage_autonomous_run(
            user=_platform(ISSUER, identifier="opencti"), payload=_payload()
        )

    def test_a_platform_credential_without_an_issuer_is_not_entitled(self):
        assert not caller_may_manage_autonomous_run(user=_platform(issuer=None), payload=_payload())

    def test_a_run_recorded_by_url_only_compares_it_normalized(self):
        assert caller_may_manage_autonomous_run(
            user=_platform("https://OpenAEV.example.test:443/", identifier="openaev"),
            payload=_issuer_only_payload(started_by_issuer="https://openaev.example.test"),
        )
        assert not caller_may_manage_autonomous_run(
            user=_platform(OTHER_ISSUER), payload=_issuer_only_payload()
        )

    def test_an_issuer_tag_without_a_platform_credential_grants_nothing(self):
        # Only the credential the request authenticated with counts.
        assert not caller_may_manage_autonomous_run(user=_user(kind="session_jwt", issuer=ISSUER), payload=_payload())

    def test_a_legacy_run_admits_the_platform_it_calls_back(self):
        # No recorded issuer: OpenAEV has always sent its own base URL - the
        # URL it signs its JWTs with - as callback_base_url.
        assert caller_may_manage_autonomous_run(user=_platform(), payload=_legacy_payload())

    def test_a_legacy_run_compares_the_callback_url_normalized(self):
        assert caller_may_manage_autonomous_run(
            user=_platform("https://OpenAEV.example.test:443/"),
            payload=_legacy_payload(callback_base_url="https://openaev.example.test"),
        )

    def test_a_legacy_run_refuses_any_other_platform(self):
        assert not caller_may_manage_autonomous_run(user=_platform(OTHER_ISSUER), payload=_legacy_payload())

    def test_a_legacy_run_without_a_callback_refuses_every_platform(self):
        assert not caller_may_manage_autonomous_run(user=_platform(), payload=_legacy_payload(callback_base_url=None))

    def test_a_legacy_run_with_a_malformed_callback_refuses_every_platform(self):
        assert not caller_may_manage_autonomous_run(
            user=_platform(), payload=_legacy_payload(callback_base_url="ftp://openaev.example.test")
        )

    def test_a_missing_execution_admits_only_an_admin(self):
        # Executions are never deleted: a missing one is a run that never ran
        # here, so there is no starter to match and nothing to protect. A
        # platform included - otherwise any registered platform could purge a
        # made-up run id and clear the shared orchestrator's memory.
        for user in (_platform(), _platform(OTHER_ISSUER), _user(), _user(kind="api_key")):
            assert not caller_may_manage_autonomous_run(user=user, payload=None)

    def test_impersonation_only_narrows(self):
        # An impersonated session is the impersonated user: the impersonating
        # admin gets that user's standing, never their own.
        admin_id = str(uuid.uuid4())
        assert not caller_may_manage_autonomous_run(user=_user(impersonated_by=admin_id), payload=_payload())
        started_by = str(uuid.uuid4())
        assert caller_may_manage_autonomous_run(
            user=_user(user_id=started_by, impersonated_by=admin_id),
            payload=_payload(user_id=started_by),
        )


class _StubDB:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def _request():
    return SimpleNamespace(headers={}, client=None)


def _execution(*, state="pending", payload=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        state=state,
        payload=payload if payload is not None else _payload(),
    )


@pytest.fixture
def env(monkeypatch):
    """Stub every collaborator the three routes touch and record what they did."""
    from app.api.v1 import platform as platform_module
    from app.services import audit as audit_module
    from app.services import autonomous_pentest as ap_module
    from app.services import orchestration_engine as engine_module

    rec = SimpleNamespace(
        execution=None,
        lookups=[],
        cancelled=[],
        purged=[],
        resolved_slugs=[],
        audits=[],
        resets=[],
        created=[],
        events=[],
        wakes=0,
    )

    async def fake_licensing(db):
        return None

    async def fake_get_execution(db, dedup_key, *, for_update=False):
        rec.lookups.append({"dedup_key": dedup_key, "for_update": for_update})
        return rec.execution

    async def fake_cancel_execution(db, *, execution_id):
        rec.cancelled.append(execution_id)
        return rec.execution

    async def fake_reset_execution(db, *, execution_id, payload, deadline_at, run_at=None):
        rec.resets.append({"execution_id": execution_id, "payload": payload})
        return SimpleNamespace(id=execution_id, payload=payload)

    async def fake_create_execution(db, **kwargs):
        rec.created.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4(), payload=kwargs.get("payload"))

    async def fake_emit_outbox_event(db, **kwargs):
        rec.events.append(kwargs)

    async def fake_notify():
        rec.wakes += 1

    async def fake_purge(db, openaev_run_id, *, orchestrator_agent_id=None):
        rec.purged.append((openaev_run_id, orchestrator_agent_id))

    async def fake_resolve_orchestrator(db, slug):
        rec.resolved_slugs.append(slug)
        return SimpleNamespace(id=uuid.uuid4(), slug=slug)

    async def fake_log_audit(db, **kwargs):
        rec.audits.append(kwargs)

    monkeypatch.setattr(platform_module, "_require_autonomous_licensing", fake_licensing)
    monkeypatch.setattr(platform_module, "_resolve_orchestrator_agent", fake_resolve_orchestrator)
    monkeypatch.setattr(engine_module, "get_execution_by_dedup_key", fake_get_execution)
    monkeypatch.setattr(engine_module, "cancel_execution", fake_cancel_execution)
    monkeypatch.setattr(engine_module, "reset_execution", fake_reset_execution)
    monkeypatch.setattr(engine_module, "create_execution", fake_create_execution)
    monkeypatch.setattr(engine_module, "emit_outbox_event", fake_emit_outbox_event)
    monkeypatch.setattr(engine_module, "notify_orchestration_wake", fake_notify)
    monkeypatch.setattr(ap_module, "purge_autonomous_run_state", fake_purge)
    monkeypatch.setattr(audit_module, "log_audit", fake_log_audit)

    def assert_nothing_done():
        assert rec.cancelled == []
        assert rec.purged == []
        assert rec.resets == []
        assert rec.created == []
        assert rec.events == []
        assert rec.wakes == 0

    return SimpleNamespace(rec=rec, module=platform_module, assert_nothing_done=assert_nothing_done)


def _assert_refusal_audited(rec, *, operation: str, execution):
    assert len(rec.audits) == 1
    entry = rec.audits[0]
    assert entry["action"] == "security_alert"
    assert entry["entity_type"] == "security"
    assert entry["entity_id"] == execution.id
    assert entry["details"]["source"] == "autonomous_run"
    assert entry["details"]["operation"] == operation
    return entry


class TestCancelRoute:
    async def _cancel(self, env, user, *, purge=False, run_id="run-1", db=None):
        return await env.module.cancel_autonomous_run(
            run_id, request=_request(), purge=purge, db=db or _StubDB(), user=user
        )

    @pytest.mark.asyncio
    async def test_non_entitled_user_cancels_nothing_and_gets_ignored(self, env):
        env.rec.execution = _execution()
        db = _StubDB()
        resp = await self._cancel(env, _user(), db=db)
        assert resp == {"status": "ignored", "openaev_run_id": "run-1"}
        env.assert_nothing_done()
        entry = _assert_refusal_audited(env.rec, operation="cancel", execution=env.rec.execution)
        assert entry["details"]["purge"] is False
        assert entry["details"]["credential"] == "session_jwt"
        # The only write is the audit entry.
        assert db.commits == 1

    @pytest.mark.asyncio
    async def test_non_entitled_user_purges_nothing_but_answers_like_unknown(self, env):
        env.rec.execution = _execution()
        resp = await self._cancel(env, _user(), purge=True)
        # Same shape an unknown run's purge yields (no existence oracle).
        assert resp == {"status": "purged", "openaev_run_id": "run-1", "purged": True}
        env.assert_nothing_done()
        _assert_refusal_audited(env.rec, operation="purge", execution=env.rec.execution)

    @pytest.mark.asyncio
    async def test_another_platform_cancels_and_purges_nothing(self, env):
        env.rec.execution = _execution()
        resp = await self._cancel(env, _platform(OTHER_ISSUER), purge=True)
        assert resp == {"status": "purged", "openaev_run_id": "run-1", "purged": True}
        env.assert_nothing_done()
        entry = _assert_refusal_audited(env.rec, operation="purge", execution=env.rec.execution)
        assert entry["details"]["platform_issuer"] == OTHER_ISSUER
        assert entry["details"]["platform"] == "opencti"

    @pytest.mark.asyncio
    async def test_a_refusal_is_logged_for_whoever_diagnoses_the_platform(self, env, caplog):
        # A platform ignores the answer it gets, so the server log is where a
        # misrouted call shows up - with what was refused and who asked.
        env.rec.execution = _execution()
        with caplog.at_level("WARNING", logger=env.module.__name__):
            await self._cancel(env, _platform(OTHER_ISSUER), purge=True)
        refusals = [r for r in caplog.records if r.levelname == "WARNING" and "Refused purge" in r.getMessage()]
        assert len(refusals) == 1
        message = refusals[0].getMessage()
        assert "'run-1'" in message
        assert "credential=platform_jwt" in message
        assert "platform=opencti" in message
        assert OTHER_ISSUER in message

    @pytest.mark.asyncio
    async def test_the_platform_cancels_a_run_started_before_it_changed_its_url(self, env):
        env.rec.execution = _execution()
        resp = await self._cancel(env, _platform(MOVED_ISSUER), purge=True)
        assert resp == {"status": "cancelled", "openaev_run_id": "run-1", "purged": True}
        assert env.rec.cancelled == [env.rec.execution.id]
        assert len(env.rec.purged) == 1
        assert env.rec.audits == []

    @pytest.mark.asyncio
    async def test_impersonated_session_of_a_stranger_is_refused_and_says_who(self, env):
        env.rec.execution = _execution()
        admin_id = str(uuid.uuid4())
        await self._cancel(env, _user(impersonated_by=admin_id), purge=True)
        env.assert_nothing_done()
        entry = _assert_refusal_audited(env.rec, operation="purge", execution=env.rec.execution)
        assert entry["details"]["impersonated_by"] == admin_id

    @pytest.mark.asyncio
    async def test_the_refusal_audit_bounds_the_caller_supplied_run_id(self, env):
        env.rec.execution = _execution()
        await self._cancel(env, _user(), run_id="r" * 5_000)
        entry = env.rec.audits[0]
        assert len(entry["entity_name"]) <= 500
        assert len(entry["details"]["openaev_run_id"]) <= 500

    @pytest.mark.asyncio
    async def test_the_decision_is_made_on_the_locked_row(self, env):
        env.rec.execution = _execution()
        await self._cancel(env, _platform())
        assert env.rec.lookups == [{"dedup_key": "autonomous_run:run-1", "for_update": True}]

    @pytest.mark.asyncio
    async def test_starter_cancels_and_purges_with_the_run_orchestrator(self, env):
        started_by = str(uuid.uuid4())
        env.rec.execution = _execution(payload=_payload(user_id=started_by, agent_slug="custom-orchestrator"))
        resp = await self._cancel(env, _user(user_id=started_by), purge=True)
        assert resp == {"status": "cancelled", "openaev_run_id": "run-1", "purged": True}
        assert env.rec.cancelled == [env.rec.execution.id]
        assert [run for run, _ in env.rec.purged] == ["run-1"]
        assert env.rec.resolved_slugs == ["custom-orchestrator"]
        assert env.rec.audits == []

    @pytest.mark.asyncio
    async def test_admin_cancels(self, env):
        env.rec.execution = _execution()
        resp = await self._cancel(env, _user(is_admin=True))
        assert resp["status"] == "cancelled"
        assert env.rec.cancelled == [env.rec.execution.id]
        assert env.rec.purged == []

    @pytest.mark.asyncio
    async def test_platform_acting_for_another_operator_cancels_and_purges(self, env):
        env.rec.execution = _execution()
        resp = await self._cancel(env, _platform(), purge=True)
        assert resp["status"] == "cancelled"
        assert len(env.rec.cancelled) == 1
        assert len(env.rec.purged) == 1
        assert env.rec.audits == []

    @pytest.mark.asyncio
    async def test_legacy_run_is_cancelled_by_the_platform_it_calls_back(self, env):
        env.rec.execution = _execution(payload=_legacy_payload())
        resp = await self._cancel(env, _platform(), purge=True)
        assert resp["status"] == "cancelled"
        assert len(env.rec.purged) == 1

    @pytest.mark.asyncio
    async def test_terminal_execution_stays_a_noop_cancel_and_still_purges(self, env):
        # cancel_execution itself no-ops on a terminal run; the purge still runs.
        env.rec.execution = _execution(state="cancelled")
        resp = await self._cancel(env, _platform(), purge=True)
        assert resp["status"] == "cancelled"
        assert len(env.rec.cancelled) == 1
        assert len(env.rec.purged) == 1

    @pytest.mark.asyncio
    async def test_missing_execution_is_a_noop_for_everyone(self, env):
        for user in (_user(is_admin=True), _platform(), _user()):
            db = _StubDB()
            resp = await self._cancel(env, user, db=db)
            assert resp == {"status": "ignored", "openaev_run_id": "run-1"}
            assert db.commits == 0
        env.assert_nothing_done()
        assert env.rec.audits == []

    @pytest.mark.asyncio
    async def test_missing_execution_purge_is_honoured_for_an_admin(self, env):
        resp = await self._cancel(env, _user(is_admin=True), purge=True)
        assert resp == {"status": "purged", "openaev_run_id": "run-1", "purged": True}
        assert env.rec.cancelled == []
        assert len(env.rec.purged) == 1

    @pytest.mark.asyncio
    async def test_missing_execution_purge_by_a_platform_does_nothing(self, env):
        # A run that never ran here has nothing a platform needs purged, and the
        # purge would clear the shared orchestrator's memory. Same answer, no
        # write, and no audit: it is the benign no-op of a never-engaged run.
        for user in (_platform(), _platform(OTHER_ISSUER), _user()):
            db = _StubDB()
            resp = await self._cancel(env, user, purge=True, db=db)
            assert resp == {"status": "purged", "openaev_run_id": "run-1", "purged": True}
            assert db.commits == 0
        env.assert_nothing_done()
        assert env.rec.audits == []


class TestWakeRoute:
    async def _wake(self, env, user, *, db=None):
        return await env.module.wake_autonomous_run(
            "run-1",
            body=env.module.AutonomousRunWakeRequest(reason="finding arrived"),
            request=_request(),
            db=db or _StubDB(),
            user=user,
        )

    @pytest.mark.asyncio
    async def test_non_entitled_caller_wakes_nothing_and_gets_ignored(self, env):
        env.rec.execution = _execution(state="waiting")
        resp = await self._wake(env, _user())
        assert resp == {"status": "ignored", "openaev_run_id": "run-1"}
        env.assert_nothing_done()
        _assert_refusal_audited(env.rec, operation="wake", execution=env.rec.execution)

    @pytest.mark.asyncio
    async def test_non_entitled_caller_is_refused_on_a_terminal_run_too(self, env):
        env.rec.execution = _execution(state="cancelled")
        resp = await self._wake(env, _platform(OTHER_ISSUER))
        assert resp == {"status": "ignored", "openaev_run_id": "run-1"}
        env.assert_nothing_done()
        _assert_refusal_audited(env.rec, operation="wake", execution=env.rec.execution)

    @pytest.mark.asyncio
    async def test_the_starting_platform_wakes_the_run(self, env):
        env.rec.execution = _execution(state="waiting")
        resp = await self._wake(env, _platform())
        assert resp == {"status": "woken", "openaev_run_id": "run-1"}
        assert len(env.rec.events) == 1
        assert env.rec.events[0]["execution_id"] == env.rec.execution.id
        assert env.rec.wakes == 1
        assert env.rec.audits == []

    @pytest.mark.asyncio
    async def test_entitled_wake_of_a_terminal_run_stays_a_noop(self, env):
        env.rec.execution = _execution(state="completed")
        db = _StubDB()
        resp = await self._wake(env, _platform(), db=db)
        assert resp == {"status": "ignored", "openaev_run_id": "run-1"}
        env.assert_nothing_done()
        assert env.rec.audits == []
        # The row lock is released before answering, not at session teardown.
        assert db.rollbacks == 1

    @pytest.mark.asyncio
    async def test_the_decision_and_the_event_share_the_locked_row(self, env):
        env.rec.execution = _execution(state="waiting")
        await self._wake(env, _platform())
        assert env.rec.lookups == [{"dedup_key": "autonomous_run:run-1", "for_update": True}]

    @pytest.mark.asyncio
    async def test_a_reengage_racing_a_stale_wake_leaves_no_live_event(self, env, monkeypatch):
        # The interleaving: the previous starter reads the run (and passes the
        # rule on the payload it read), a platform operator then re-engages it -
        # the reset replaces user_id and neutralizes the prior engagement's
        # pending events - and only then does the stale wake insert its event.
        # Without the lock the event lands after the reset and wakes the newly
        # owned run for a caller the current payload refuses. With it, the reset
        # waits for the wake's commit and neutralizes that event too.
        from app.services import orchestration_engine as engine_module

        previous_starter = str(uuid.uuid4())
        row = SimpleNamespace(
            id=uuid.uuid4(),
            state="waiting",
            payload=_payload(user_id=previous_starter, started_by_issuer=None),
            events=[],
            locked=False,
            waiting=[],
        )

        def reengage_by_another_operator():
            row.payload = _payload(user_id=str(uuid.uuid4()))
            for event in row.events:
                if event["status"] == "pending":
                    event["status"] = "processed"

        async def fake_get_execution(db, dedup_key, *, for_update=False):
            seen = SimpleNamespace(id=row.id, state=row.state, payload=dict(row.payload))
            if for_update:
                row.locked = True
            if row.locked:
                row.waiting.append(reengage_by_another_operator)
            else:
                reengage_by_another_operator()
            return seen

        async def fake_emit_outbox_event(db, **kwargs):
            row.events.append({"status": "pending", **kwargs})

        class _RowDB(_StubDB):
            async def _release(self):
                row.locked = False
                while row.waiting:
                    row.waiting.pop(0)()

            async def commit(self):
                await super().commit()
                await self._release()

            async def rollback(self):
                await super().rollback()
                await self._release()

        monkeypatch.setattr(engine_module, "get_execution_by_dedup_key", fake_get_execution)
        monkeypatch.setattr(engine_module, "emit_outbox_event", fake_emit_outbox_event)

        await self._wake(env, _user(user_id=previous_starter), db=_RowDB())

        assert row.waiting == []
        assert row.payload["user_id"] != previous_starter
        assert [event for event in row.events if event["status"] == "pending"] == []

    @pytest.mark.asyncio
    async def test_a_reengage_that_lands_first_refuses_the_stale_wake(self, env):
        previous_starter = str(uuid.uuid4())
        # The operator's re-engage already replaced the owner when the old
        # starter's wake reads the row.
        env.rec.execution = _execution(state="waiting", payload=_payload(user_id=str(uuid.uuid4())))
        resp = await self._wake(env, _user(user_id=previous_starter))
        assert resp == {"status": "ignored", "openaev_run_id": "run-1"}
        env.assert_nothing_done()
        _assert_refusal_audited(env.rec, operation="wake", execution=env.rec.execution)

    @pytest.mark.asyncio
    async def test_missing_execution_is_ignored_for_everyone_without_audit(self, env):
        for user in (_user(), _platform(), _user(is_admin=True)):
            resp = await self._wake(env, user)
            assert resp == {"status": "ignored", "openaev_run_id": "run-1"}
        env.assert_nothing_done()
        assert env.rec.audits == []


class TestStartRoute:
    async def _start(self, env, user, *, db=None):
        body = env.module.AutonomousRunStartRequest(
            objective="map the path",
            openaev_run_id="run-1",
            simulation_id="sim-1",
            callback_base_url=ISSUER,
        )
        return await env.module.start_autonomous_run(body=body, request=_request(), db=db or _StubDB(), user=user)

    @pytest.mark.asyncio
    async def test_start_records_the_platform_issuer_and_user(self, env):
        starter = str(uuid.uuid4())
        await self._start(env, _platform(user_id=starter))
        payload = env.rec.created[0]["payload"]
        assert payload["user_id"] == starter
        assert payload["started_by_platform"] == "openaev"
        assert payload["started_by_issuer"] == ISSUER
        assert env.rec.resets == []

    @pytest.mark.asyncio
    async def test_start_records_no_platform_for_a_session_start(self, env):
        await self._start(env, _user())
        payload = env.rec.created[0]["payload"]
        assert payload["started_by_platform"] is None
        assert payload["started_by_issuer"] is None

    @pytest.mark.asyncio
    async def test_a_new_run_id_is_started_by_any_licensed_caller(self, env):
        resp = await self._start(env, _user())
        assert len(env.rec.created) == 1
        assert resp.session_id
        assert env.rec.audits == []

    @pytest.mark.asyncio
    async def test_a_stranger_cannot_reengage_and_take_over_a_run(self, env):
        # The takeover the cancel gate alone left open: a reset rewrites the
        # recorded starter, after which cancel and purge would admit the caller.
        env.rec.execution = _execution()
        db = _StubDB()
        with pytest.raises(HTTPException) as exc:
            await self._start(env, _user(), db=db)
        assert exc.value.status_code == 404
        env.assert_nothing_done()
        _assert_refusal_audited(env.rec, operation="re-engage", execution=env.rec.execution)
        assert db.commits == 1
        assert env.rec.lookups == [{"dedup_key": "autonomous_run:run-1", "for_update": True}]

    @pytest.mark.asyncio
    async def test_another_platform_cannot_reengage_a_run(self, env):
        env.rec.execution = _execution()
        with pytest.raises(HTTPException) as exc:
            await self._start(env, _platform(OTHER_ISSUER))
        assert exc.value.status_code == 404
        env.assert_nothing_done()

    @pytest.mark.asyncio
    async def test_the_starting_platform_reengages_for_another_operator(self, env):
        env.rec.execution = _execution()
        operator = str(uuid.uuid4())
        await self._start(env, _platform(user_id=operator))
        assert len(env.rec.resets) == 1
        assert env.rec.resets[0]["execution_id"] == env.rec.execution.id
        assert env.rec.resets[0]["payload"]["user_id"] == operator
        assert env.rec.resets[0]["payload"]["started_by_issuer"] == ISSUER
        assert env.rec.created == []
        assert env.rec.audits == []

    @pytest.mark.asyncio
    async def test_the_starter_and_an_admin_reengage(self, env):
        started_by = str(uuid.uuid4())
        for user in (_user(user_id=started_by), _user(is_admin=True)):
            env.rec.execution = _execution(payload=_payload(user_id=started_by))
            await self._start(env, user)
        assert len(env.rec.resets) == 2
        assert env.rec.audits == []

    @pytest.mark.asyncio
    async def test_a_legacy_run_is_reengaged_by_the_platform_it_calls_back(self, env):
        env.rec.execution = _execution(payload=_legacy_payload())
        await self._start(env, _platform())
        assert len(env.rec.resets) == 1
        assert env.rec.resets[0]["payload"]["started_by_platform"] == "openaev"
        assert env.rec.resets[0]["payload"]["started_by_issuer"] == ISSUER

    @pytest.mark.asyncio
    async def test_another_operator_restarts_a_run_started_before_the_platform_moved(self, env):
        # OpenAEV's restart / resume re-engages the SAME run id. After a URL
        # change it must not 404 for anyone but the operator who started it.
        env.rec.execution = _execution()
        operator = str(uuid.uuid4())
        await self._start(env, _platform(MOVED_ISSUER, user_id=operator))
        assert len(env.rec.resets) == 1
        payload = env.rec.resets[0]["payload"]
        assert payload["user_id"] == operator
        assert payload["started_by_platform"] == "openaev"
        assert payload["started_by_issuer"] == MOVED_ISSUER
        assert env.rec.audits == []


class TestPlatformIdentity:
    """The identity the rule matches on comes from the verified registration."""

    @pytest.mark.asyncio
    async def test_a_platform_jwt_caller_is_tagged_with_its_registration(self, monkeypatch):
        import jwt as pyjwt

        from app.api import deps
        from app.core.xtm_auth import CrossJWTPayload

        async def _verify(_token, _db):
            return CrossJWTPayload(
                iss=ISSUER,
                sub="operator",
                email="op@example.test",
                aud="https://xtm.example.test",
                jti="jti",
                platform_identifier="openaev",
            )

        operator = SimpleNamespace(id=uuid.uuid4(), email="op@example.test", is_active=True)

        class _EmailLookup:
            async def execute(self, _stmt):
                return SimpleNamespace(scalar_one_or_none=lambda: operator)

        monkeypatch.setattr("app.core.xtm_auth.verify_xtm_jwt", _verify)
        monkeypatch.setattr(pyjwt, "get_unverified_header", lambda _t: {"alg": "EdDSA"})

        user = await deps.get_current_user_ungated(
            credentials=SimpleNamespace(credentials="platform-jwt"), db=_EmailLookup()
        )

        assert user.auth_credential_kind == "platform_jwt"
        assert user.platform_issuer == ISSUER
        assert user.platform_identifier == "openaev"
