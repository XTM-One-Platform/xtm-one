"""Emergency stop ("panic button") — the parts that need no database.

* The ``/api/v1/panic`` contract: a user may stop / resume their own scope,
  the platform scope is 403 for a non-admin (and the service is never
  reached), the counts come back as the response body.
* The pause reason the stop stamps is a registered one, so the provenance
  module does not log it as unknown.
* The four long-running adapters opt into the reconciler's fast cancel poll
  from the new setting, and opt out when it is 0.
* The cancel event is threaded to the engines that drive an agent loop.
* The Redis marker scan parses conversation ids out of the marker keys.

The sweep itself runs real SQL against real rows and is covered by
``test_panic_stop_postgres.py``.
"""

from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1 import panic as panic_api
from app.core.config import settings
from app.core.database import get_db
from app.services import panic
from app.services.assignment_pause import PANIC_STOP_REASON, PAUSE_REASONS

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _user(*, role: str = "user") -> SimpleNamespace:
    uid = uuid.uuid4()
    return SimpleNamespace(
        id=uid,
        email=f"{uid.hex[:8]}@test.local",
        full_name="Panic Tester",
        role=role,
        is_admin=role == "admin",
    )


def _client(user) -> TestClient:
    app = FastAPI()
    app.include_router(panic_api.router, prefix="/api/v1/panic")
    db = SimpleNamespace(commit=AsyncMock())

    async def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[deps.get_current_user] = lambda: user
    client = TestClient(app)
    client.fake_db = db  # type: ignore[attr-defined]
    return client


# ---------------------------------------------------------------------------
# Route contract
# ---------------------------------------------------------------------------


def test_stop_self_arms_the_barrier_commits_then_sweeps_commits_and_returns_the_counts():
    """Two transactions: the door is closed (and durable) before the room is
    cleared, so what the sweep selects is the complete set of live work."""
    user = _user()
    armed = panic.EmergencyStopOutcome(scope="self", assignments_paused=2)
    outcome = panic.EmergencyStopOutcome(
        scope="self", runs_cancelled=3, assignments_paused=2, chat_turns_cancelled=1
    )
    order: list[str] = []
    client = _client(user)
    client.fake_db.commit = AsyncMock(side_effect=lambda: order.append("commit"))

    async def _arm(*_a, **_k):
        order.append("arm")
        return armed

    async def _stop(*_a, **kw):
        order.append("stop")
        assert kw["outcome"] is armed  # the sweep continues the armed outcome
        return outcome

    with (
        patch.object(panic, "arm_admission_barrier", _arm),
        patch.object(panic, "emergency_stop", _stop),
        patch.object(panic, "after_commit", AsyncMock()) as after,
    ):
        resp = client.post("/api/v1/panic/stop", json={"scope": "self"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scope"] == "self"
    assert body["runs_cancelled"] == 3
    assert body["assignments_paused"] == 2
    assert body["chat_turns_cancelled"] == 1
    assert body["ai_disabled"] is False
    assert body["late_admissions_cancelled"] == 0
    assert body["audit_id"] is None  # no audit row in this stubbed outcome
    assert order == ["arm", "commit", "stop", "commit"]
    after.assert_awaited_once_with(outcome)


def test_stop_defaults_to_the_self_scope():
    user = _user()
    outcome = panic.EmergencyStopOutcome(scope="self")
    with (
        patch.object(panic, "arm_admission_barrier", AsyncMock(return_value=outcome)),
        patch.object(panic, "emergency_stop", AsyncMock(return_value=outcome)) as stop,
        patch.object(panic, "after_commit", AsyncMock()),
    ):
        resp = _client(user).post("/api/v1/panic/stop", json={})
    assert resp.status_code == 200
    assert stop.await_args.kwargs["scope"] == "self"


def test_platform_stop_invalidates_the_ai_cache_between_the_two_phases():
    """The barrier is only a barrier once every replica gates on it: the
    local cache is dropped right after the first commit, not after the sweep."""
    from app.services import ai_state

    admin = _user(role="admin")
    armed = panic.EmergencyStopOutcome(scope="platform", ai_disabled=True)
    order: list[str] = []
    client = _client(admin)
    client.fake_db.commit = AsyncMock(side_effect=lambda: order.append("commit"))

    async def _stop(*_a, **_k):
        order.append("stop")
        return armed

    with (
        patch.object(panic, "arm_admission_barrier", AsyncMock(return_value=armed)),
        patch.object(panic, "emergency_stop", _stop),
        patch.object(panic, "after_commit", AsyncMock()),
        patch.object(ai_state, "invalidate_cache", side_effect=lambda: order.append("invalidate")),
    ):
        resp = client.post("/api/v1/panic/stop", json={"scope": "platform"})
    assert resp.status_code == 200
    assert order[:3] == ["commit", "invalidate", "stop"]


def test_platform_scope_is_forbidden_to_a_regular_user_before_any_work():
    user = _user(role="user")
    with (
        patch.object(panic, "arm_admission_barrier", AsyncMock()) as stop,
        patch.object(panic, "resume_after_emergency_stop", AsyncMock()) as resume,
    ):
        client = _client(user)
        stopped = client.post("/api/v1/panic/stop", json={"scope": "platform"})
        resumed = client.post(
            "/api/v1/panic/resume", json={"scope": "platform", "reason": "over"}
        )

    assert stopped.status_code == 403
    assert resumed.status_code == 403
    stop.assert_not_awaited()
    resume.assert_not_awaited()
    client.fake_db.commit.assert_not_awaited()


def test_platform_scope_is_allowed_to_an_admin():
    admin = _user(role="admin")
    outcome = panic.EmergencyStopOutcome(scope="platform", ai_disabled=True)
    with (
        patch.object(panic, "arm_admission_barrier", AsyncMock(return_value=outcome)),
        patch.object(panic, "emergency_stop", AsyncMock(return_value=outcome)),
        patch.object(panic, "after_commit", AsyncMock()),
    ):
        resp = _client(admin).post("/api/v1/panic/stop", json={"scope": "platform"})
    assert resp.status_code == 200
    assert resp.json()["ai_disabled"] is True


def test_unknown_scope_is_a_validation_error():
    resp = _client(_user()).post("/api/v1/panic/stop", json={"scope": "everyone"})
    assert resp.status_code == 422


def test_resume_returns_its_counts_and_invalidates_after_commit():
    admin = _user(role="admin")
    outcome = panic.ResumeOutcome(scope="platform", assignments_resumed=4, ai_enabled=True)
    with (
        patch.object(
            panic, "resume_after_emergency_stop", AsyncMock(return_value=outcome)
        ),
        patch.object(panic, "after_resume_commit", AsyncMock()) as after,
    ):
        client = _client(admin)
        resp = client.post(
            "/api/v1/panic/resume",
            json={"scope": "platform", "reason": "Incident closed"},
        )
    assert resp.status_code == 200
    assert resp.json() == {
        "scope": "platform",
        "assignments_resumed": 4,
        "ai_enabled": True,
    }
    client.fake_db.commit.assert_awaited_once()
    after.assert_awaited_once_with(outcome)


def test_self_resume_under_a_platform_stop_is_a_409():
    """The platform stop's boundary is lifted as a whole, by an administrator —
    never one user's slice at a time."""
    user = _user()
    with patch.object(
        panic,
        "resume_after_emergency_stop",
        AsyncMock(side_effect=panic.PlatformStopActiveError("platform stop open")),
    ):
        client = _client(user)
        resp = client.post(
            "/api/v1/panic/resume", json={"scope": "self", "reason": "please"}
        )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "platform_stop_active"
    client.fake_db.commit.assert_not_awaited()


def test_resume_requires_a_reason():
    """A resume is not an emergency: the audit trail gets the reason up front."""
    admin = _user(role="admin")
    with patch.object(panic, "resume_after_emergency_stop", AsyncMock()) as resume:
        client = _client(admin)
        missing = client.post("/api/v1/panic/resume", json={"scope": "platform"})
        blank = client.post(
            "/api/v1/panic/resume", json={"scope": "platform", "reason": "   "}
        )
    assert missing.status_code == 422
    assert blank.status_code == 422
    resume.assert_not_awaited()


def test_resume_hands_the_reason_to_the_service():
    admin = _user(role="admin")
    outcome = panic.ResumeOutcome(scope="self", reason="done", assignments_resumed=1)
    with (
        patch.object(
            panic, "resume_after_emergency_stop", AsyncMock(return_value=outcome)
        ) as resume,
        patch.object(panic, "after_resume_commit", AsyncMock()),
    ):
        resp = _client(admin).post(
            "/api/v1/panic/resume", json={"scope": "self", "reason": "done"}
        )
    assert resp.status_code == 200
    assert resume.await_args.kwargs["reason"] == "done"


def test_stop_reason_is_recorded_after_the_fact_and_committed():
    user = _user()
    audit_id = uuid.uuid4()
    with patch.object(
        panic, "record_stop_reason", AsyncMock(return_value=3)
    ) as record:
        client = _client(user)
        resp = client.post(
            f"/api/v1/panic/stop/{audit_id}/reason", json={"reason": "Runaway agent"}
        )
    assert resp.status_code == 200
    assert resp.json() == {"audit_id": str(audit_id), "assignments_annotated": 3}
    assert record.await_args.kwargs["audit_id"] == audit_id
    assert record.await_args.kwargs["reason"] == "Runaway agent"
    assert record.await_args.kwargs["actor"] is user
    client.fake_db.commit.assert_awaited_once()


def test_stop_reason_refuses_a_stop_that_is_not_the_callers():
    user = _user()
    with patch.object(
        panic, "record_stop_reason", AsyncMock(side_effect=LookupError("nope"))
    ):
        client = _client(user)
        resp = client.post(
            f"/api/v1/panic/stop/{uuid.uuid4()}/reason", json={"reason": "x"}
        )
    assert resp.status_code == 404
    client.fake_db.commit.assert_not_awaited()


def test_stop_reason_must_not_be_blank():
    user = _user()
    with patch.object(panic, "record_stop_reason", AsyncMock()) as record:
        resp = _client(user).post(
            f"/api/v1/panic/stop/{uuid.uuid4()}/reason", json={"reason": "   "}
        )
    # ``min_length`` counts characters, so whitespace would pass it; the
    # boundary validator refuses a blank reason before the service is reached.
    assert resp.status_code == 422
    record.assert_not_awaited()


def test_status_passes_the_service_answer_through():
    status = {
        "self_paused_assignments": 2,
        "self_stop_active": True,
        "platform_paused_assignments": None,
        "ai_disabled": None,
        "platform_stop_active": False,
        "active": True,
        "last_stop": {"at": None, "by": "a@b.c", "scope": "self", "reason": "x"},
    }
    with patch.object(panic, "emergency_stop_status", AsyncMock(return_value=status)):
        resp = _client(_user()).get("/api/v1/panic/status")
    assert resp.status_code == 200
    assert resp.json() == status


# ---------------------------------------------------------------------------
# Service guards
# ---------------------------------------------------------------------------


def test_owner_for_refuses_the_platform_scope_to_a_non_admin():
    with pytest.raises(PermissionError):
        panic._owner_for(_user(role="user"), "platform")


def test_owner_for_resolves_the_scopes():
    user = _user(role="admin")
    assert panic._owner_for(user, "self") == user.id
    assert panic._owner_for(user, "platform") is None


def test_panic_stop_is_a_registered_pause_reason():
    """``pause_assignment`` warns on an unknown reason and the provenance UI
    translates known codes — the stop must be one of them."""
    assert PANIC_STOP_REASON == "panic_stop"
    assert PANIC_STOP_REASON in PAUSE_REASONS


def test_after_commit_invalidates_the_ai_cache_only_when_ai_was_disabled():
    from app.services import ai_state

    with patch.object(ai_state, "invalidate_cache") as invalidate:
        import asyncio

        asyncio.run(panic.after_commit(panic.EmergencyStopOutcome(scope="self")))
        invalidate.assert_not_called()
        asyncio.run(
            panic.after_commit(
                panic.EmergencyStopOutcome(scope="platform", ai_disabled=True)
            )
        )
        invalidate.assert_called_once()


# ---------------------------------------------------------------------------
# Fast cancel: adapters + engine plumbing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module_name, class_name",
    [
        ("assignment", "AssignmentAdapter"),
        ("sub_agent", "SubAgentAdapter"),
        ("flow", "FlowAdapter"),
        ("evaluation", "EvaluationAdapter"),
    ],
)
def test_long_running_adapters_opt_into_the_fast_cancel_poll(
    monkeypatch, module_name, class_name
):
    import importlib

    module = importlib.import_module(
        f"app.services.orchestration_adapters.{module_name}"
    )
    adapter = getattr(module, class_name)()

    monkeypatch.setattr(settings, "orchestration_cancel_poll_seconds", 3)
    assert adapter.cancel_poll_seconds == 3.0
    monkeypatch.setattr(settings, "orchestration_cancel_poll_seconds", 0)
    assert adapter.cancel_poll_seconds is None


def test_reconciler_starts_a_watcher_for_an_opted_in_adapter(monkeypatch):
    """The property is only useful if the reconciler reads it."""
    import asyncio

    from app.services import orchestration_reconciler as reconciler
    from app.services.orchestration_adapters.assignment import AssignmentAdapter

    monkeypatch.setattr(settings, "orchestration_cancel_poll_seconds", 2)

    async def _go():
        # The caller owns the event, which the lease heartbeat fires too.
        event = asyncio.Event()
        watcher = reconciler._start_cancel_watcher(AssignmentAdapter(), uuid.uuid4(), event)
        try:
            assert watcher is not None
        finally:
            await reconciler._stop_cancel_watcher(watcher)

    asyncio.run(_go())


def test_cancel_event_is_threaded_to_the_engines_that_drive_an_agent_loop():
    from app.services.assignment_engine import execute_assignment_run
    from app.services.sub_agent import _execute_sub_agent_task

    assert "cancel_event" in inspect.signature(execute_assignment_run).parameters
    assert "cancel_event" in inspect.signature(_execute_sub_agent_task).parameters


# ---------------------------------------------------------------------------
# Redis marker scan
# ---------------------------------------------------------------------------


def test_list_processing_conversations_parses_marker_keys(monkeypatch):
    import asyncio

    from app.services import chat_turn_guard

    keys = [
        b"chat:processing:11111111-1111-1111-1111-111111111111",
        "chat:processing:22222222-2222-2222-2222-222222222222",
        b"chat:processing:",  # a bare prefix must not yield an empty id
    ]

    class _FakeRedis:
        def __init__(self):
            self.patterns: list[str] = []

        async def scan_iter(self, *, match: str, count: int):
            self.patterns.append(match)
            for key in keys:
                yield key

    fake = _FakeRedis()

    async def _redis():
        return fake

    monkeypatch.setattr(chat_turn_guard, "_redis", _redis)
    ids = asyncio.run(chat_turn_guard.list_processing_conversations())

    assert ids == [
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    ]
    assert fake.patterns == ["chat:processing:*"]


# ---------------------------------------------------------------------------
# The token path
# ---------------------------------------------------------------------------


def _admin_client(admin) -> TestClient:
    client = _client(admin)
    client.app.dependency_overrides[deps.require_admin] = lambda: admin
    return client


def test_token_management_is_admin_only():
    user = _user(role="user")
    client = _client(user)  # get_current_user is the plain user; require_admin is real
    assert client.get("/api/v1/panic/token").status_code == 403
    assert client.post("/api/v1/panic/token").status_code == 403
    assert client.delete("/api/v1/panic/token").status_code == 403


def test_token_generate_returns_the_raw_token_once_and_commits():
    admin = _user(role="admin")
    created = {
        "token": "xes-" + "a" * 48,
        "prefix": "xes-aaaaaaaa...",
        "created_at": "2026-09-18T00:00:00+00:00",
        "created_by": admin.email,
    }
    with patch.object(
        panic, "generate_emergency_token", AsyncMock(return_value=created)
    ) as gen:
        client = _admin_client(admin)
        resp = client.post("/api/v1/panic/token")
    assert resp.status_code == 200
    assert resp.json() == created
    assert gen.await_args.kwargs["actor"] is admin
    client.fake_db.commit.assert_awaited_once()


def test_token_revoke_is_404_when_none_is_configured():
    admin = _user(role="admin")
    with patch.object(panic, "revoke_emergency_token", AsyncMock(return_value=False)):
        client = _admin_client(admin)
        resp = client.delete("/api/v1/panic/token")
    assert resp.status_code == 404
    client.fake_db.commit.assert_not_awaited()


def test_token_revoke_commits_when_one_existed():
    admin = _user(role="admin")
    with patch.object(panic, "revoke_emergency_token", AsyncMock(return_value=True)):
        client = _admin_client(admin)
        resp = client.delete("/api/v1/panic/token")
    assert resp.status_code == 204
    client.fake_db.commit.assert_awaited_once()


def test_emergency_by_token_is_rate_limited_before_the_token_is_read():
    """A probe against the token is throttled per client IP — and the token
    is never even looked at once the ceiling is hit."""
    from fastapi import HTTPException

    from app.api.v1 import panic as panic_api

    async def _too_many(*_a, **kw):
        assert kw["fail_closed"] is True  # never unlimited when Redis is down
        raise HTTPException(status_code=429, detail="Too many requests")

    with (
        patch.object(panic_api, "enforce_rate_limit", _too_many),
        patch.object(
            panic, "carries_current_token_prefix", AsyncMock(return_value=False)
        ),
        patch.object(panic, "resolve_emergency_token", AsyncMock()) as resolve,
    ):
        resp = _client(_user()).post(
            "/api/v1/panic/emergency", headers={"Authorization": "Bearer xes-x"}
        )
    assert resp.status_code == 429
    resolve.assert_not_awaited()


@pytest.mark.parametrize("limiter_status", [429, 503])
def test_the_current_token_gets_through_a_spent_or_unreadable_ceiling(limiter_status):
    """Ten wrong attempts from a shared egress, or a Redis outage, must not
    lock the holder of the real token out of the stop: a request carrying the
    current token's prefix is still checked."""
    from fastapi import HTTPException

    from app.api.v1 import panic as panic_api

    admin = _user(role="admin")
    outcome = panic.EmergencyStopOutcome(scope="platform", audit_id=uuid.uuid4())
    token = "xes-" + "c" * 48

    async def _refused(*_a, **_kw):
        raise HTTPException(status_code=limiter_status, detail="limited")

    with (
        patch.object(panic_api, "enforce_rate_limit", _refused),
        patch.object(
            panic, "carries_current_token_prefix", AsyncMock(return_value=True)
        ) as prefix,
        patch.object(panic, "resolve_emergency_token", AsyncMock(return_value=admin)),
        patch.object(
            panic, "emergency_stop_with_token", AsyncMock(return_value=outcome)
        ),
        patch.object(panic, "after_commit", AsyncMock()),
    ):
        resp = _client(_user()).post(
            "/api/v1/panic/emergency", headers={"Authorization": f"Bearer {token}"}
        )
    assert resp.status_code == 200, resp.text
    assert prefix.await_args.args[1] == token


def test_the_token_prefix_check_needs_the_configured_prefix():
    import asyncio

    from app.api.v1 import platform_settings_helpers

    token = "xes-" + "d" * 48
    ps = SimpleNamespace(
        emergency_stop_token_hash="$2b$hash",
        emergency_stop_token_prefix=panic._display_prefix(token),
    )
    with patch.object(
        platform_settings_helpers, "get_platform_settings", AsyncMock(return_value=ps)
    ):
        assert asyncio.run(panic.carries_current_token_prefix(None, token)) is True
        assert (
            asyncio.run(panic.carries_current_token_prefix(None, "xes-" + "e" * 48))
            is False
        )
        assert asyncio.run(panic.carries_current_token_prefix(None, None)) is False
        ps.emergency_stop_token_hash = None
        assert asyncio.run(panic.carries_current_token_prefix(None, token)) is False


def test_emergency_by_token_refuses_a_missing_or_wrong_token_and_records_it():
    """No session, no token: 401 — and the refusal is committed so the
    security event stays in the trail."""
    from app.api.v1 import panic as panic_api

    with (
        patch.object(panic_api, "enforce_rate_limit", AsyncMock()),
        patch.object(panic, "resolve_emergency_token", AsyncMock(return_value=None)) as resolve,
        patch.object(panic, "emergency_stop_with_token", AsyncMock()) as stop,
    ):
        client = _client(_user())
        missing = client.post("/api/v1/panic/emergency")
        wrong = client.post(
            "/api/v1/panic/emergency", headers={"Authorization": "Bearer xes-nope"}
        )
    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert resolve.await_args_list[0].args[1] is None
    assert resolve.await_args_list[1].args[1] == "xes-nope"
    stop.assert_not_awaited()
    assert client.fake_db.commit.await_count == 2


def test_emergency_by_token_runs_the_platform_stop_as_the_minting_admin():
    admin = _user(role="admin")
    outcome = panic.EmergencyStopOutcome(
        scope="platform", runs_cancelled=2, ai_disabled=True, audit_id=uuid.uuid4()
    )
    from app.api.v1 import panic as panic_api

    with (
        patch.object(panic_api, "enforce_rate_limit", AsyncMock()),
        patch.object(panic, "resolve_emergency_token", AsyncMock(return_value=admin)),
        patch.object(
            panic, "emergency_stop_with_token", AsyncMock(return_value=outcome)
        ) as stop,
        patch.object(panic, "after_commit", AsyncMock()) as after,
    ):
        client = _client(_user())  # the session dependency is irrelevant here
        resp = client.post(
            "/api/v1/panic/emergency",
            headers={"Authorization": "Bearer xes-" + "b" * 48},
            json={"reason": "Runaway agent, on-call runbook step 3"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scope"] == "platform"
    assert body["ai_disabled"] is True
    assert body["audit_id"] == str(outcome.audit_id)
    assert stop.await_args.kwargs["actor"] is admin
    assert stop.await_args.kwargs["reason"] == "Runaway agent, on-call runbook step 3"
    # ``emergency_stop_with_token`` commits both phases itself.
    client.fake_db.commit.assert_not_awaited()
    after.assert_awaited_once_with(outcome)


# ---------------------------------------------------------------------------
# Review follow-ups: the stop's boundary holds, and the fast cancel is real
# ---------------------------------------------------------------------------


def test_generic_resume_of_an_emergency_stopped_assignment_is_refused():
    """The power toggle (and the agent-builder tool) must not undo the stop
    one assignment at a time: only the emergency-stop resume may."""
    from fastapi import HTTPException

    from app.services.assignment_pause import ensure_not_emergency_stopped

    stopped = SimpleNamespace(enabled=False, paused_reason=PANIC_STOP_REASON)
    with pytest.raises(HTTPException) as excinfo:
        ensure_not_emergency_stopped(stopped)
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["code"] == "emergency_stop"

    # A manual pause, an enabled row and an unstamped pause pass untouched.
    ensure_not_emergency_stopped(SimpleNamespace(enabled=False, paused_reason="user_toggle"))
    ensure_not_emergency_stopped(SimpleNamespace(enabled=True, paused_reason=PANIC_STOP_REASON))
    ensure_not_emergency_stopped(SimpleNamespace(enabled=False, paused_reason=None))


def test_fast_cancel_reaches_every_adapter_that_advertises_it():
    """An adapter that sets ``cancel_poll_seconds`` must hand the event to the
    loop it drives — otherwise the watcher fires into the void.

    An evaluation's loop is its batch: the fan-out stops the runs from outside.
    A run never takes the event, which also fires on lease loss."""
    from app.services.evaluation_engine import execute_evaluation, execute_evaluation_run
    from app.services.flow_runner import FlowWalker

    assert "cancel_event" in inspect.signature(execute_evaluation).parameters
    assert "cancel_event" not in inspect.signature(execute_evaluation_run).parameters
    assert "cancel_event" in inspect.signature(FlowWalker.__init__).parameters


def test_nested_handovers_inside_an_assignment_run_get_the_cancel_event():
    """Consult / transfer turns spawned by an assignment run call
    ``run_agent_chat`` themselves; a stop on the parent must reach them."""
    from app.services import assignment_engine

    src = inspect.getsource(assignment_engine.execute_assignment_run)
    consult = src.index('"type": "consult"')
    transfer = src.index('"type": "transfer"')
    # The kwarg sits in the same ``run_agent_chat(`` call as the marker: look
    # from the marker to that call's closing paren (the call spans many
    # commented keyword lines, so a fixed window is not enough).
    def _call_tail(start: int) -> str:
        depth, k = 1, start
        while depth and k < len(src):
            depth += {"(": 1, ")": -1}.get(src[k], 0)
            k += 1
        return src[start:k]

    assert "cancel_event=cancel_event" in _call_tail(consult)
    assert "cancel_event=cancel_event" in _call_tail(transfer)


def test_resume_reports_the_effective_ai_state_not_the_cleared_override():
    """Clearing the override means "follow PLATFORM_MODE"; in an AI-disabled
    deployment that is still OFF, and the UI must not say "switched back on"."""
    from app.services import ai_state

    with patch.object(ai_state, "resolve", return_value=False):
        assert ai_state.resolve(None) is False
    src = inspect.getsource(panic.resume_after_emergency_stop)
    # Resolved from what is actually restored (the pre-stop override), never
    # a bare "True" because an override went away.
    assert "outcome.ai_enabled = ai_state.resolve(restore_to)" in src
    assert "outcome.ai_enabled = True" not in src


def test_after_commit_runs_the_late_admission_sweep_now_and_after_the_cache_lifetime():
    """One pass right away, one more once no replica can still hold a warm
    AI-gate cache (process-local, one TTL) — the second is detached."""
    import asyncio

    from app.services import ai_state

    outcome = panic.EmergencyStopOutcome(
        scope="self",
        partner_cancel_run_ids=[uuid.uuid4()],
        runner_commands_cancelled=1,
        finished_runner_command_ids=[uuid.uuid4()],
    )
    order: list[str] = []

    async def _fake_sleep(delay):
        order.append(f"sleep:{delay}")

    from app.a2a.client import delegation
    from app.services import runner_transport

    with (
        patch.object(panic, "sweep_late_admissions", AsyncMock(return_value=0)) as sweep,
        patch.object(panic, "_resumed_since", AsyncMock(return_value=False)),
        patch.object(panic.asyncio, "sleep", _fake_sleep),
        patch.object(delegation, "spawn_run_cancel", side_effect=lambda ids: order.append("partners")),
        patch.object(
            runner_transport,
            "announce_finished_commands",
            AsyncMock(side_effect=lambda *_a: order.append("runners")),
        ),
    ):

        async def _run():
            await panic.after_commit(outcome)
            order.append("after_commit returned")
            await asyncio.gather(*list(panic._background))

        asyncio.run(_run())
    assert sweep.await_count == 2
    # The partner and runner work runs NOW, inside after_commit — never behind
    # the detached delay (a restart during the sleep would lose it).
    assert order.index("partners") < order.index("after_commit returned")
    assert order.index("runners") < order.index("after_commit returned")
    assert f"sleep:{ai_state.CACHE_TTL_SECONDS + 1.0}" in order


def test_the_delayed_sweep_is_skipped_once_the_stop_was_lifted():
    """A resume inside the window re-opens the door on purpose; the second
    pass must not cancel the runs it legitimately started."""
    import asyncio

    outcome = panic.EmergencyStopOutcome(scope="platform")
    with (
        patch.object(panic, "sweep_late_admissions", AsyncMock(return_value=0)) as sweep,
        patch.object(panic, "_resumed_since", AsyncMock(return_value=True)),
        patch.object(panic.asyncio, "sleep", AsyncMock()),
    ):
        asyncio.run(panic._delayed_sweep(outcome, 0))
    sweep.assert_not_awaited()


def test_the_immediate_sweep_is_skipped_when_a_resume_already_landed():
    """A resume between the stop's commit and its after-commit work re-opened
    the door: what started after it is legitimate, even to the first pass."""
    import asyncio

    outcome = panic.EmergencyStopOutcome(scope="platform")
    with (
        patch.object(panic, "sweep_late_admissions", AsyncMock(return_value=0)) as sweep,
        patch.object(panic, "_resumed_since", AsyncMock(return_value=True)),
        patch.object(panic.asyncio, "sleep", AsyncMock()),
    ):

        async def _run():
            await panic.after_commit(outcome)
            await asyncio.gather(*list(panic._background))

        asyncio.run(_run())
    sweep.assert_not_awaited()


def test_a_platform_resume_takes_only_what_a_platform_stop_paused():
    """Matched on the platform stops' own timestamps, never on the words of a
    pause: a user's own stop is not the platform's to lift."""
    where = str(panic._panic_paused_query(None, platform_stop_times=[])).split("WHERE", 1)[1]
    assert "paused_at IN" in where
    self_where = str(panic._panic_paused_query(uuid.uuid4())).split("WHERE", 1)[1]
    assert "paused_at" not in self_where


def test_stop_and_resume_transitions_share_one_lock():
    """The self-resume check and its writes must not straddle a platform stop
    being armed on another request (TOCTOU)."""
    arm = inspect.getsource(panic.arm_admission_barrier)
    resume = inspect.getsource(panic.resume_after_emergency_stop)
    assert "await serialize_stop_transitions(db)" in arm
    assert "await serialize_stop_transitions(db)" in resume
    # The lock is taken BEFORE the platform-stop check in the resume.
    assert resume.index("serialize_stop_transitions") < resume.index("platform_stop_open(db)")
    assert "pg_advisory_xact_lock" in inspect.getsource(panic.serialize_stop_transitions)


def test_the_evaluation_loop_checks_cancellation_after_the_agent_call_too():
    """A cancel landing during the agent call ends the run before the judge
    call. The engine tests' fake session cannot reach this probe, so the
    source is the only place it is checked."""
    from app.services import evaluation_engine

    src = inspect.getsource(evaluation_engine.execute_evaluation_run)
    # Between the agent's latency measure and the first duration statement.
    after_call = src.index("agent_wall_clock_ms: int | None = _ms(")
    scoring = src.index("agent_duration_ms = _step_total_ms(agent_local_traces)", after_call)
    between = src[after_call:scoring]
    assert "_evaluation_run_cancelled_out_of_band(db, run.id)" in between
    assert "break" in between


def test_late_admission_count_is_folded_into_the_response():
    """The route serialises the outcome AFTER ``after_commit``: what the sweep
    caught must be in the summary the operator reads."""
    import asyncio

    async def _fake_sweep(outcome):
        outcome.runs_cancelled += 2  # what ``sweep_late_admissions`` does itself
        return 2

    outcome = panic.EmergencyStopOutcome(scope="platform", runs_cancelled=3)
    with patch.object(panic, "sweep_late_admissions", _fake_sweep):
        asyncio.run(panic.after_commit(outcome))
    assert outcome.response()["runs_cancelled"] == 5
    src = inspect.getsource(panic.sweep_late_admissions)
    assert "outcome.runs_cancelled += cancelled" in src
    assert "outcome.late_admissions_cancelled += cancelled" in src


def _sweep_harness(outcome, *, live_conversations, tasks=(0, 0)):
    """Run ``_sweep_platform_live_work`` with its session and arms stubbed."""
    import asyncio
    from contextlib import asynccontextmanager

    from app.services import chat_turn_guard, sub_agent

    db = SimpleNamespace(commit=AsyncMock())

    @asynccontextmanager
    async def _session():
        yield db

    cascaded: list[str] = []

    async def _cascade(conversation_id):
        cascaded.append(conversation_id)

    with (
        patch("app.core.database.async_session", _session),
        patch.object(
            sub_agent, "cancel_sub_agent_tasks_for_scope", AsyncMock(return_value=tasks)
        ) as cancel_tasks,
        patch.object(
            chat_turn_guard,
            "list_processing_conversations",
            AsyncMock(return_value=list(live_conversations)),
        ),
        patch.object(chat_turn_guard, "request_turn_cancel", AsyncMock(return_value=True)),
        patch.object(panic, "_add_to_audit_counts", AsyncMock()) as audit,
        patch.object(panic, "_cancel_conversation_dependants", _cascade),
    ):

        async def _run():
            await panic._sweep_platform_live_work(outcome)
            await asyncio.gather(*list(panic._background))

        asyncio.run(_run())
    return cancel_tasks, audit, cascaded


def test_a_platform_sweep_cancels_the_tasks_and_turns_admitted_after_the_stop():
    """With AI off nothing agentic may be live: a turn admitted through a warm
    AI cache would run to its end, and a task it spawned would be held and
    replayed at the resume. The turns the stop already cancelled are asked
    again but counted once, and cascaded once."""
    outcome = panic.EmergencyStopOutcome(
        scope="platform",
        chat_turns_cancelled=1,
        cancelled_conversation_ids=["already-stopped"],
    )
    cancel_tasks, audit, cascaded = _sweep_harness(
        outcome, live_conversations=["already-stopped", "late"], tasks=(2, 2)
    )

    assert cancel_tasks.await_args.kwargs["user_id"] is None
    assert outcome.sub_agent_tasks_cancelled == 2
    assert outcome.executions_cancelled == 2
    assert outcome.chat_turns_cancelled == 2
    assert outcome.cancelled_conversation_ids == ["already-stopped", "late"]
    assert cascaded == ["late"]
    assert audit.await_args.kwargs == {
        "sub_agent_tasks_cancelled": 2,
        "executions_cancelled": 2,
        "chat_turns_cancelled": 1,
    }


def test_only_a_platform_stop_sweeps_live_tasks_and_turns():
    """A ``self`` stop does not gate new work: what the user starts after it
    is theirs, so the late pass leaves their new turns and tasks alone."""
    src = inspect.getsource(panic.sweep_late_admissions)
    assert "if outcome.owner_id is None:\n        await _sweep_platform_live_work(outcome)" in src


def test_the_platform_only_arm_matches_the_adapters_own_names():
    """A spelled-out adapter name that drifts from the one executions are
    created with matches nothing, and the stop leaves those runs going."""
    src = inspect.getsource(panic._cancel_platform_only)
    assert "orchestration_adapters.autonomous import" in src
    assert "orchestration_adapters.evaluation import" in src
    assert '"autonomous"' not in src


def test_partner_cancels_are_never_sent_twice_for_one_run():
    """Every active run is a root AND a child may also be a root's descendant,
    so the cascade's tree carries duplicates; the partner list must not."""
    src = inspect.getsource(panic._cancel_runs)
    assert "list(dict.fromkeys(tree))" in src
    sweep = inspect.getsource(panic.sweep_late_admissions)
    assert "spawn_run_cancel(list(dict.fromkeys(tree)))" in sweep


# ---------------------------------------------------------------------------
# Review follow-ups (fred-lr)
# ---------------------------------------------------------------------------


def test_a_failed_phase_two_is_rolled_back_reported_and_still_swept():
    """The barrier is already committed when phase 2 runs: a failure there is
    rolled back, the trail says the stop is incomplete, the response says so,
    and the after-commit sweeps still run."""
    user = _user()
    armed = panic.EmergencyStopOutcome(
        scope="self", assignments_paused=2, audit_id=uuid.uuid4()
    )
    entry = SimpleNamespace(details={"phase": "armed"})
    client = _client(user)
    client.fake_db.rollback = AsyncMock()
    client.fake_db.execute = AsyncMock(
        return_value=SimpleNamespace(scalar_one_or_none=lambda: entry)
    )

    async def _stop(*_a, **kw):
        outcome = kw["outcome"]
        outcome.runs_cancelled += 5  # rolled back with the failure
        outcome.chat_turns_cancelled += 1  # a Redis request: it happened
        outcome.cancelled_conversation_ids.append("c-1")
        raise RuntimeError("database went away")

    with (
        patch.object(panic, "arm_admission_barrier", AsyncMock(return_value=armed)),
        patch.object(panic, "emergency_stop", _stop),
        patch.object(panic, "after_commit", AsyncMock()) as after,
    ):
        resp = client.post("/api/v1/panic/stop", json={"scope": "self"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["completed"] is False
    assert body["runs_cancelled"] == 0
    assert body["assignments_paused"] == 2
    assert body["chat_turns_cancelled"] == 1
    client.fake_db.rollback.assert_awaited()
    assert entry.details["phase"] == "incomplete"
    reported = after.await_args.args[0]
    assert reported.completed is False
    assert reported.cancelled_conversation_ids == ["c-1"]


def test_an_assignment_another_open_stop_covers_is_held():
    """A stop covers what was paused when it was pressed; the user lifting
    their own stop never releases what someone else's still covers."""
    from datetime import UTC, datetime, timedelta

    owner, run_as = uuid.uuid4(), uuid.uuid4()
    t0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=5)
    paused = SimpleNamespace(user_id=owner, run_as_user_id=run_as, paused_at=t0)

    # The owner's stop paused it, the run-as user lifts theirs: held.
    assert panic._covered_by_open_stop(paused, {owner: t0, run_as: t1}, lifting=run_as)
    # The other order: the owner's stop came later and found it paused.
    assert panic._covered_by_open_stop(paused, {owner: t1}, lifting=run_as)
    # The user lifting their own stop is never held by it.
    assert not panic._covered_by_open_stop(paused, {run_as: t1}, lifting=run_as)
    # A stop pressed before the pause did not cover it.
    earlier = t0 - timedelta(minutes=5)
    assert not panic._covered_by_open_stop(paused, {owner: earlier}, lifting=None)


def test_a_cancelled_assignment_run_still_records_its_usage():
    """A run stopped mid-loop spent its model calls: the cancelled branch
    writes the token totals before leaving the status the cancel set."""
    from app.services import assignment_engine

    src = inspect.getsource(assignment_engine)
    branch = src[src.index('if _terminal_stop == "cancelled":') :]
    branch = branch[: branch.index("return\n")]
    assert "apply_run_usage_accounting(run, loop_result.usage, wf_trace_ctx)" in branch
