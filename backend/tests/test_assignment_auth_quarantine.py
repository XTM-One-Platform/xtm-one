"""Tests for the cross-run integration auth-failure quarantine.

When an assignment's integration tools repeatedly return authentication /
permission failures (e.g. an expired Notion OAuth token), the assignment is
auto-disabled after ``settings.assignment_auth_fail_quarantine_threshold``
consecutive runs with auth failures, and the owner is notified.

Covered:
  * ``_messages_contain_auth_failure`` — detection helper.
  * ``_record_assignment_auth_failure`` / ``_reset_assignment_auth_failures``
    — Redis counter helpers (fail-open on Redis outage).
  * ``_track_assignment_auth_failures`` — the post-run gate: threshold == 0
    disables the feature; only scheduler runs advance the counter; below
    threshold the assignment stays enabled; at threshold it is disabled;
    a clean run of any trigger type resets the counter.
  * ``_handle_assignment_auth_failure`` — Activity + SSE notification
    (best-effort: failures are swallowed; SSE scoped company vs personal).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.services.assignment_engine import (
    _ASSIGNMENT_AUTH_FAIL_KEY_PREFIX,
    _ASSIGNMENT_AUTH_REPORT_FAIL_KEY_PREFIX,
    _ASSIGNMENT_AUTH_REPORT_INCR_LUA,
    _auth_failure_kind,
    _auth_notice_key,
    _auth_report_key,
    _claim_auth_failure_notice,
    _handle_assignment_auth_failure,
    _identity_ran_recently,
    _messages_contain_auth_failure,
    _record_assignment_auth_failure,
    _record_assignment_auth_report_failure,
    _report_unquarantined_auth_failure,
    _reset_assignment_auth_failures,
    _track_assignment_auth_failures,
)

# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------


@dataclass
class _Msg:
    """Minimal ChatMessage stub — only ``role`` and ``content`` matter here."""

    role: str
    content: str | None = None
    tool_calls: list[Any] = field(default_factory=list)


def _tool_msg(content: str) -> _Msg:
    return _Msg(role="tool", content=content)


def _assistant_msg(content: str) -> _Msg:
    return _Msg(role="assistant", content=content)


def _system_msg(content: str) -> _Msg:
    return _Msg(role="system", content=content)


# ---------------------------------------------------------------------------
# _messages_contain_auth_failure
# ---------------------------------------------------------------------------


def test_empty_messages_returns_false():
    assert _messages_contain_auth_failure([]) is False


def test_no_tool_messages_returns_false():
    msgs = [
        _system_msg("You are an agent."),
        _assistant_msg("Let me check that for you."),
    ]
    assert _messages_contain_auth_failure(msgs) is False


def test_tool_message_with_no_auth_error_returns_false():
    msgs = [
        _tool_msg("Here are your Notion pages: [Page 1, Page 2]"),
        _assistant_msg("Found 2 pages."),
    ]
    assert _messages_contain_auth_failure(msgs) is False


def test_tool_message_with_auth_failure_keyword_returns_true():
    """'authentication failed' is a known keyword in _AUTH_FAILURE_KEYWORDS."""
    msgs = [
        _tool_msg(
            "Error: Notion authentication failed — the token may be expired "
            "or revoked. The user should re-authorize this integration."
        ),
    ]
    assert _messages_contain_auth_failure(msgs) is True


def test_tool_message_401_returns_true():
    """HTTP 401 in the error string triggers the auth-failure classifier."""
    msgs = [
        _tool_msg("401 Unauthorized: your API key is invalid."),
    ]
    assert _messages_contain_auth_failure(msgs) is True


def test_tool_message_unauthorized_keyword_returns_true():
    msgs = [
        _tool_msg("Error: unauthorized — check your API credentials."),
    ]
    assert _messages_contain_auth_failure(msgs) is True


def test_only_assistant_message_with_auth_text_returns_false():
    """Auth error text in an assistant (non-tool) message must NOT trigger."""
    msgs = [
        _assistant_msg(
            "The tool returned: authentication failed. Please re-authorize."
        ),
    ]
    assert _messages_contain_auth_failure(msgs) is False


def test_mix_of_ok_and_auth_failure_tool_messages_returns_true():
    """A single auth-failure tool result among many good ones is enough."""
    msgs = [
        _tool_msg("Page content retrieved successfully."),
        _assistant_msg("Got the page, now querying the database."),
        _tool_msg(
            "Error: Notion authentication failed — the token may be expired "
            "or revoked. The user should re-authorize this integration."
        ),
        _assistant_msg("Could not query the database."),
    ]
    assert _messages_contain_auth_failure(msgs) is True


def test_tool_message_with_none_content_skipped():
    """None content must not raise — treated as empty (no auth failure)."""
    msgs = [_Msg(role="tool", content=None)]
    assert _messages_contain_auth_failure(msgs) is False


def test_token_may_be_expired_keyword_returns_true():
    """'token may be expired' is an explicit keyword in _AUTH_FAILURE_KEYWORDS."""
    msgs = [
        _tool_msg("Error: the OAuth token may be expired or invalid."),
    ]
    assert _messages_contain_auth_failure(msgs) is True


# ---------------------------------------------------------------------------
# _record_assignment_auth_failure / _reset_assignment_auth_failures
# (Redis fail-open behaviour)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_returns_incremented_count():
    """Happy path: Redis INCR returns new count as an integer."""
    assignment_id = str(uuid.uuid4())
    expected_key = f"{_ASSIGNMENT_AUTH_FAIL_KEY_PREFIX}{assignment_id}"

    fake_redis = AsyncMock()
    fake_redis.eval = AsyncMock(return_value=2)

    # Patch the lazily imported redis_client used inside the function.
    async def _fake_redis_client():
        return fake_redis

    with patch("app.core.redis.redis_client", new=_fake_redis_client):
        count = await _record_assignment_auth_failure(assignment_id)

    assert count == 2
    fake_redis.eval.assert_awaited_once()
    call_args = fake_redis.eval.call_args
    # Key argument is the second positional argument passed to eval().
    assert expected_key in str(call_args)


@pytest.mark.asyncio
async def test_record_returns_zero_on_redis_error():
    """When Redis is unavailable, _record_assignment_auth_failure returns 0 (fail-open)."""
    assignment_id = str(uuid.uuid4())

    async def _boom():
        raise ConnectionError("Redis is down")

    with patch("app.core.redis.redis_client", new=_boom):
        count = await _record_assignment_auth_failure(assignment_id)

    assert count == 0


@pytest.mark.asyncio
async def test_reset_deletes_key():
    """Happy path: reset calls Redis DEL on the correct key."""
    assignment_id = str(uuid.uuid4())
    expected_key = f"{_ASSIGNMENT_AUTH_FAIL_KEY_PREFIX}{assignment_id}"

    fake_redis = AsyncMock()
    fake_redis.delete = AsyncMock(return_value=1)

    async def _fake_redis_client():
        return fake_redis

    with patch("app.core.redis.redis_client", new=_fake_redis_client):
        await _reset_assignment_auth_failures(assignment_id)

    # The notice claims are part of the episode: recovery must clear them too,
    # or a credential that breaks again inside the cooldown is never reported.
    fake_redis.delete.assert_awaited_once_with(
        expected_key,
        f"{_ASSIGNMENT_AUTH_REPORT_FAIL_KEY_PREFIX}{assignment_id}",
        _auth_notice_key(assignment_id, "report"),
        _auth_notice_key(assignment_id, "quarantine"),
    )


@pytest.mark.asyncio
async def test_reset_is_silent_on_redis_error():
    """When Redis is unavailable, reset is a no-op (fail-open)."""
    assignment_id = str(uuid.uuid4())

    async def _boom():
        raise ConnectionError("Redis is down")

    with patch("app.core.redis.redis_client", new=_boom):
        # Must not raise.
        await _reset_assignment_auth_failures(assignment_id)


# ---------------------------------------------------------------------------
# _handle_assignment_auth_failure — notification best-effort
# ---------------------------------------------------------------------------


def _make_mock_db() -> AsyncMock:
    db = AsyncMock()
    db.flush = AsyncMock()
    db.add = MagicMock()
    db.begin_nested = MagicMock()
    db.begin_nested.return_value.__aenter__ = AsyncMock(return_value=None)
    db.begin_nested.return_value.__aexit__ = AsyncMock(return_value=False)
    return db


@pytest.mark.asyncio
async def test_handle_quarantine_personal_assignment_notifies_owner_only(caplog):
    """Personal assignment: activity written, SSE goes ONLY to the owner.

    Publishing a private assignment's name/failure details to the shared
    ``activity:company`` channel would leak them to every connected user.
    """
    from app.services.assignment_engine import _handle_assignment_auth_failure

    assignment_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()

    assignment = MagicMock()
    assignment.id = assignment_id
    assignment.name = "Agents Inventory Sync"
    assignment.user_id = user_id
    assignment.is_company_managed = False

    agent = MagicMock()
    agent.id = agent_id
    agent.name = "[Legal] Agents Inventory Sync Agent"

    db = _make_mock_db()

    fake_redis = AsyncMock()
    fake_redis.publish = AsyncMock(return_value=1)

    async def _fake_redis_client():
        return fake_redis

    with patch("app.core.redis.redis_client", new=_fake_redis_client):
        await _handle_assignment_auth_failure(
            db,
            assignment=assignment,
            agent=agent,
            run_id="run-abc-123",
            consecutive=3,
        )

    # Activity row was added to the session.
    db.add.assert_called_once()
    added_obj = db.add.call_args[0][0]
    assert added_obj.activity_type == "assignment_auth_quarantine"
    assert "re-authorize" in added_obj.summary.lower()
    assert added_obj.extra_data["consecutive_auth_failures"] == 3

    # SSE published ONLY to the owner's personal channel — never company.
    publish_channels = [c.args[0] for c in fake_redis.publish.call_args_list]
    assert publish_channels == [f"activity:{user_id}"]


@pytest.mark.asyncio
async def test_handle_quarantine_company_assignment_broadcasts_company():
    """Company-managed assignment: SSE broadcast to the company feed + owner."""
    from app.services.assignment_engine import _handle_assignment_auth_failure

    user_id = uuid.uuid4()

    assignment = MagicMock()
    assignment.id = uuid.uuid4()
    assignment.name = "Company Inventory Sync"
    assignment.user_id = user_id
    assignment.is_company_managed = True

    agent = MagicMock()
    agent.id = uuid.uuid4()
    agent.name = "Company Agent"

    db = _make_mock_db()

    fake_redis = AsyncMock()
    fake_redis.publish = AsyncMock(return_value=1)

    async def _fake_redis_client():
        return fake_redis

    with patch("app.core.redis.redis_client", new=_fake_redis_client):
        await _handle_assignment_auth_failure(
            db,
            assignment=assignment,
            agent=agent,
            run_id="run-company-owned",
            consecutive=3,
        )

    publish_channels = [c.args[0] for c in fake_redis.publish.call_args_list]
    assert "activity:company" in publish_channels
    assert f"activity:{user_id}" in publish_channels


@pytest.mark.asyncio
async def test_handle_quarantine_survives_db_error(caplog):
    """A DB failure inside the notification must not propagate."""
    from app.services.assignment_engine import _handle_assignment_auth_failure

    assignment = MagicMock()
    assignment.id = uuid.uuid4()
    assignment.name = "Test Assignment"
    assignment.user_id = uuid.uuid4()
    assignment.is_company_managed = False

    agent = MagicMock()
    agent.id = uuid.uuid4()
    agent.name = "Test Agent"

    db = AsyncMock()
    db.flush = AsyncMock(side_effect=RuntimeError("DB down"))

    fake_redis = AsyncMock()
    fake_redis.publish = AsyncMock(return_value=1)

    async def _fake_redis_client():
        return fake_redis

    with patch("app.core.redis.redis_client", new=_fake_redis_client):
        # Must not raise even though db.flush raises.
        await _handle_assignment_auth_failure(
            db,
            assignment=assignment,
            agent=agent,
            run_id="run-xyz",
            consecutive=3,
        )

    # SSE is still attempted after the DB failure.
    assert fake_redis.publish.called


@pytest.mark.asyncio
async def test_handle_quarantine_no_owner_broadcasts_company_only():
    """When the assignment has no user_id, only the company channel is published."""
    from app.services.assignment_engine import _handle_assignment_auth_failure

    assignment = MagicMock()
    assignment.id = uuid.uuid4()
    assignment.name = "Company Assignment"
    assignment.user_id = None  # company-managed, no owner
    assignment.is_company_managed = True

    agent = MagicMock()
    agent.id = uuid.uuid4()
    agent.name = "Company Agent"

    db = _make_mock_db()

    fake_redis = AsyncMock()
    fake_redis.publish = AsyncMock(return_value=1)

    async def _fake_redis_client():
        return fake_redis

    with patch("app.core.redis.redis_client", new=_fake_redis_client):
        await _handle_assignment_auth_failure(
            db,
            assignment=assignment,
            agent=agent,
            run_id="run-company",
            consecutive=5,
        )

    publish_channels = [str(c) for c in fake_redis.publish.call_args_list]
    assert any("activity:company" in ch for ch in publish_channels)
    # No personal channel (user_id is None).
    assert not any(
        "activity:" in ch and "company" not in ch for ch in publish_channels
    )
    # No Activity row was added (no owner to notify personally).
    db.add.assert_not_called()


# ---------------------------------------------------------------------------
# _track_assignment_auth_failures — post-run quarantine gate
# ---------------------------------------------------------------------------


_AUTH_FAIL_MSGS = [
    _tool_msg(
        "Error: Notion authentication failed — the token may be expired "
        "or revoked. The user should re-authorize this integration."
    ),
]

_CLEAN_MSGS = [
    _tool_msg("Page content retrieved successfully."),
    _assistant_msg("Done."),
]


def _make_gate_fixtures(
    *,
    trigger_type: str | None = "schedule",
    enabled: bool = True,
    is_builtin: bool = False,
    run_as_all_users: bool = False,
    is_company_managed: bool = False,
) -> tuple[MagicMock, MagicMock, MagicMock, AsyncMock]:
    """Build (run, assignment, agent, db) stubs for the gate tests."""
    run = MagicMock()
    run.trigger_context = {"type": trigger_type} if trigger_type else None

    assignment = MagicMock()
    assignment.id = uuid.uuid4()
    assignment.name = "Gate Test Assignment"
    assignment.user_id = uuid.uuid4()
    assignment.is_company_managed = is_company_managed
    assignment.enabled = enabled
    assignment.run_as_all_users = run_as_all_users

    agent = MagicMock()
    agent.id = uuid.uuid4()
    agent.name = "Gate Test Agent"
    agent.is_builtin = is_builtin

    return run, assignment, agent, _make_mock_db()


@pytest.mark.asyncio
async def test_gate_threshold_zero_disables_feature(monkeypatch):
    """threshold == 0 -> tracking is fully bypassed."""
    monkeypatch.setattr(settings, "assignment_auth_fail_quarantine_threshold", 0)
    run, assignment, agent, db = _make_gate_fixtures()

    with (
        patch(
            "app.services.assignment_engine._record_assignment_auth_failure",
            new_callable=AsyncMock,
        ) as record,
        patch(
            "app.services.assignment_engine._reset_assignment_auth_failures",
            new_callable=AsyncMock,
        ) as reset,
    ):
        await _track_assignment_auth_failures(
            db,
            run=run,
            assignment=assignment,
            agent=agent,
            run_id="run-1",
            messages=list(_AUTH_FAIL_MSGS),
        )

    record.assert_not_awaited()
    reset.assert_not_awaited()
    assert assignment.enabled is True


@pytest.mark.asyncio
async def test_gate_clean_run_resets_counter_any_trigger(monkeypatch):
    """A clean run resets the counter even for a manual trigger."""
    monkeypatch.setattr(settings, "assignment_auth_fail_quarantine_threshold", 3)
    run, assignment, agent, db = _make_gate_fixtures(trigger_type="manual")

    with (
        patch(
            "app.services.assignment_engine._record_assignment_auth_failure",
            new_callable=AsyncMock,
        ) as record,
        patch(
            "app.services.assignment_engine._reset_assignment_auth_failures",
            new_callable=AsyncMock,
        ) as reset,
    ):
        await _track_assignment_auth_failures(
            db,
            run=run,
            assignment=assignment,
            agent=agent,
            run_id="run-2",
            messages=list(_CLEAN_MSGS),
        )

    record.assert_not_awaited()
    reset.assert_awaited_once_with(str(assignment.id), report_user_id=None)
    assert assignment.enabled is True


@pytest.mark.asyncio
async def test_gate_non_scheduler_auth_failure_not_counted(monkeypatch):
    """A manual run with auth failures neither increments nor resets."""
    monkeypatch.setattr(settings, "assignment_auth_fail_quarantine_threshold", 3)
    run, assignment, agent, db = _make_gate_fixtures(trigger_type="manual")

    with (
        patch(
            "app.services.assignment_engine._record_assignment_auth_failure",
            new_callable=AsyncMock,
        ) as record,
        patch(
            "app.services.assignment_engine._reset_assignment_auth_failures",
            new_callable=AsyncMock,
        ) as reset,
    ):
        await _track_assignment_auth_failures(
            db,
            run=run,
            assignment=assignment,
            agent=agent,
            run_id="run-3",
            messages=list(_AUTH_FAIL_MSGS),
        )

    record.assert_not_awaited()
    reset.assert_not_awaited()
    assert assignment.enabled is True


# ---------------------------------------------------------------------------
# Report-only notice for assignments the quarantine will never pause
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notice_claimed_once_per_window():
    """SET NX EX — the first caller in the window notifies, the rest do not."""
    store: dict[str, str] = {}

    async def _set(key, _value, nx=False, ex=None):
        if nx and key in store:
            return None
        store[key] = "1"
        return True

    fake_redis = AsyncMock()
    fake_redis.set = AsyncMock(side_effect=_set)

    async def _fake_redis_client():
        return fake_redis

    aid = str(uuid.uuid4())
    with patch("app.core.redis.redis_client", new=_fake_redis_client):
        assert await _claim_auth_failure_notice(aid, "report") is True
        assert await _claim_auth_failure_notice(aid, "report") is False
        # A different KIND of notice is a different event and must not be
        # swallowed by the first one's cooldown.
        assert await _claim_auth_failure_notice(aid, "quarantine") is True
    assert _auth_notice_key(aid, "report") in store
    assert _auth_notice_key(aid, "quarantine") in store


@pytest.mark.asyncio
async def test_notice_is_dropped_when_redis_is_down():
    """Fail QUIET, not open.

    Every other Redis helper here fails open because losing the counter costs a
    missed quarantine. Failing open on the notice would instead put one
    notification per webhook delivery in front of the owner during the very
    incident it is reporting.
    """

    async def _boom():
        raise RuntimeError("redis down")

    with patch("app.core.redis.redis_client", new=_boom):
        # None, not False: "Redis is down" is not "already notified".
        assert await _claim_auth_failure_notice(str(uuid.uuid4()), "report") is None


@pytest.mark.asyncio
async def test_report_unquarantined_writes_a_distinct_activity():
    """The notice says what happened AND that nothing was paused."""
    assignment = MagicMock()
    assignment.id = uuid.uuid4()
    assignment.name = "SOAR Orchestrator"
    assignment.user_id = uuid.uuid4()
    assignment.is_company_managed = False

    agent = MagicMock()
    agent.id = uuid.uuid4()
    agent.name = "SecOps Orchestrator"

    db = _make_mock_db()
    fake_redis = AsyncMock()
    fake_redis.set = AsyncMock(return_value=True)
    fake_redis.publish = AsyncMock(return_value=1)

    async def _fake_redis_client():
        return fake_redis

    with patch("app.core.redis.redis_client", new=_fake_redis_client):
        await _report_unquarantined_auth_failure(
            db,
            assignment=assignment,
            agent=agent,
            run_id="run-webhook-1",
            reason=(
                "only scheduled runs are auto-paused; this one was triggered "
                "by 'webhook'"
            ),
        )

    db.add.assert_called_once()
    added = db.add.call_args[0][0]
    assert added.activity_type == "assignment_auth_failure"
    assert added.extra_data["quarantined"] is False
    assert "NOT been paused" in added.summary
    assert "re-authorize" in added.summary.lower()
    # Private assignment: the SSE toast never reaches the company channel.
    assert [c.args[0] for c in fake_redis.publish.call_args_list] == [
        f"activity:{assignment.user_id}"
    ]


@pytest.mark.asyncio
async def test_report_unquarantined_stays_silent_when_already_notified():
    assignment = MagicMock()
    assignment.id = uuid.uuid4()
    assignment.name = "SOAR Orchestrator"
    assignment.user_id = uuid.uuid4()
    assignment.is_company_managed = False
    agent = MagicMock()
    agent.id = uuid.uuid4()
    agent.name = "SecOps Orchestrator"
    db = _make_mock_db()

    fake_redis = AsyncMock()
    fake_redis.set = AsyncMock(return_value=None)  # key already present

    async def _fake_redis_client():
        return fake_redis

    with patch("app.core.redis.redis_client", new=_fake_redis_client):
        await _report_unquarantined_auth_failure(
            db,
            assignment=assignment,
            agent=agent,
            run_id="run-webhook-2",
            reason="only scheduled runs are auto-paused",
        )

    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_a_second_episode_is_reported_after_recovery():
    """notify → recover → break again must produce TWO notices, not one.

    The cooldown is episode-scoped, not wall-clock-scoped: an OAuth token that
    flaps (fixed at +1h, revoked again at +2h) would otherwise go unreported
    until the 24h window expired.
    """
    store: dict[str, str] = {}

    async def _set(key, _value, nx=False, ex=None):
        if nx and key in store:
            return None
        store[key] = "1"
        return True

    async def _delete(*keys):
        for key in keys:
            store.pop(key, None)
        return len(keys)

    fake_redis = AsyncMock()
    fake_redis.set = AsyncMock(side_effect=_set)
    fake_redis.delete = AsyncMock(side_effect=_delete)
    fake_redis.publish = AsyncMock(return_value=1)

    async def _fake_redis_client():
        return fake_redis

    assignment = MagicMock()
    assignment.id = uuid.uuid4()
    assignment.name = "SOAR Orchestrator"
    assignment.user_id = uuid.uuid4()
    assignment.is_company_managed = False
    agent = MagicMock()
    agent.id = uuid.uuid4()
    agent.name = "SecOps Orchestrator"
    db = _make_mock_db()

    with patch("app.core.redis.redis_client", new=_fake_redis_client):
        await _report_unquarantined_auth_failure(
            db, assignment=assignment, agent=agent, run_id="r1", reason="webhook"
        )
        await _reset_assignment_auth_failures(str(assignment.id))
        await _report_unquarantined_auth_failure(
            db, assignment=assignment, agent=agent, run_id="r2", reason="webhook"
        )

    assert db.add.call_count == 2


@pytest.mark.asyncio
async def test_an_undelivered_notice_gives_its_claim_back():
    """A notice that landed nowhere must not buy 24h of silence.

    Owner row gone: the Activity insert fails on the foreign key and the SSE
    publish fails too, so nothing was delivered and the next failing run has to
    be able to try again.
    """
    store: dict[str, str] = {}

    async def _set(key, _value, nx=False, ex=None):
        if nx and key in store:
            return None
        store[key] = "1"
        return True

    async def _delete(*keys):
        for key in keys:
            store.pop(key, None)
        return len(keys)

    fake_redis = AsyncMock()
    fake_redis.set = AsyncMock(side_effect=_set)
    fake_redis.delete = AsyncMock(side_effect=_delete)
    fake_redis.publish = AsyncMock(side_effect=RuntimeError("no redis pubsub"))

    async def _fake_redis_client():
        return fake_redis

    assignment = MagicMock()
    assignment.id = uuid.uuid4()
    assignment.name = "Orphaned Assignment"
    assignment.user_id = uuid.uuid4()
    assignment.is_company_managed = False
    agent = MagicMock()
    agent.id = uuid.uuid4()
    agent.name = "Some Agent"

    db = _make_mock_db()
    db.flush = AsyncMock(side_effect=RuntimeError("FK violation: user is gone"))

    with patch("app.core.redis.redis_client", new=_fake_redis_client):
        await _report_unquarantined_auth_failure(
            db, assignment=assignment, agent=agent, run_id="r1", reason="webhook"
        )

    # Claim taken, then handed back — nothing was stored and nothing published.
    assert _auth_notice_key(str(assignment.id), "report") not in store


@pytest.mark.asyncio
async def test_company_managed_notice_reaches_the_company_feed():
    """The report-only path shares the quarantine path's SSE scoping."""
    assignment = MagicMock()
    assignment.id = uuid.uuid4()
    assignment.name = "Shared Automation"
    assignment.user_id = uuid.uuid4()
    assignment.is_company_managed = True
    agent = MagicMock()
    agent.id = uuid.uuid4()
    agent.name = "Shared Agent"
    db = _make_mock_db()

    fake_redis = AsyncMock()
    fake_redis.set = AsyncMock(return_value=True)
    fake_redis.publish = AsyncMock(return_value=1)

    async def _fake_redis_client():
        return fake_redis

    with patch("app.core.redis.redis_client", new=_fake_redis_client):
        await _report_unquarantined_auth_failure(
            db,
            assignment=assignment,
            agent=agent,
            run_id="r1",
            reason="per-user fan-out assignments are never auto-disabled",
        )

    assert [c.args[0] for c in fake_redis.publish.call_args_list] == [
        "activity:company",
        f"activity:{assignment.user_id}",
    ]


@pytest.mark.asyncio
async def test_gate_webhook_run_notifies_without_pausing(monkeypatch):
    """The reported case: a SOAR-driven assignment on a dead credential.

    It must keep running — an alert-driven agent is not auto-disabled — and its
    owner must nevertheless hear about it. This branch must not just log a
    warning and tell nobody.
    """
    monkeypatch.setattr(settings, "assignment_auth_fail_quarantine_threshold", 3)
    run, assignment, agent, db = _make_gate_fixtures(trigger_type="webhook")

    with (
        patch(
            "app.services.assignment_engine._record_assignment_auth_failure",
            new_callable=AsyncMock,
        ) as record,
        patch(
            "app.services.assignment_engine._record_assignment_auth_report_failure",
            new_callable=AsyncMock,
            return_value=3,
        ) as record_report,
        patch(
            "app.services.assignment_engine._report_unquarantined_auth_failure",
            new_callable=AsyncMock,
        ) as report,
    ):
        await _track_assignment_auth_failures(
            db,
            run=run,
            assignment=assignment,
            agent=agent,
            run_id="run-4",
            messages=list(_AUTH_FAIL_MSGS),
        )

    record.assert_not_awaited()
    assert assignment.enabled is True
    record_report.assert_awaited_once_with(str(assignment.id), None)
    assert report.await_args.kwargs["consecutive"] == 3
    report.assert_awaited_once()
    assert "webhook" in report.await_args.kwargs["reason"]


@pytest.mark.asyncio
async def test_gate_quarantine_exempt_assignment_is_still_reported(monkeypatch):
    """Exempt from being PAUSED never meant exempt from being reported."""
    monkeypatch.setattr(settings, "assignment_auth_fail_quarantine_threshold", 3)
    run, assignment, agent, db = _make_gate_fixtures(
        trigger_type="schedule", is_builtin=True
    )

    with (
        patch(
            "app.services.assignment_engine._record_assignment_auth_failure",
            new_callable=AsyncMock,
        ) as record,
        patch(
            "app.services.assignment_engine._record_assignment_auth_report_failure",
            new_callable=AsyncMock,
            return_value=3,
        ) as record_report,
        patch(
            "app.services.assignment_engine._report_unquarantined_auth_failure",
            new_callable=AsyncMock,
        ) as report,
    ):
        await _track_assignment_auth_failures(
            db,
            run=run,
            assignment=assignment,
            agent=agent,
            run_id="run-5",
            messages=list(_AUTH_FAIL_MSGS),
        )

    record.assert_not_awaited()
    assert assignment.enabled is True
    record_report.assert_awaited_once_with(str(assignment.id), None)
    assert report.await_args.kwargs["consecutive"] == 3
    report.assert_awaited_once()
    assert "built-in" in report.await_args.kwargs["reason"]


@pytest.mark.asyncio
async def test_gate_scheduled_auth_failure_below_threshold_stays_enabled(
    monkeypatch,
):
    """Below the threshold the counter advances but nothing is disabled."""
    monkeypatch.setattr(settings, "assignment_auth_fail_quarantine_threshold", 3)
    run, assignment, agent, db = _make_gate_fixtures(trigger_type="schedule")

    with (
        patch(
            "app.services.assignment_engine._record_assignment_auth_failure",
            new_callable=AsyncMock,
            return_value=2,
        ) as record,
        patch(
            "app.services.assignment_engine._handle_assignment_auth_failure",
            new_callable=AsyncMock,
        ) as handle,
    ):
        await _track_assignment_auth_failures(
            db,
            run=run,
            assignment=assignment,
            agent=agent,
            run_id="run-4",
            messages=list(_AUTH_FAIL_MSGS),
        )

    record.assert_awaited_once_with(str(assignment.id))
    handle.assert_not_awaited()
    assert assignment.enabled is True


@pytest.mark.asyncio
async def test_gate_scheduled_auth_failure_at_threshold_quarantines(monkeypatch):
    """At the threshold the assignment is disabled and the handler runs."""
    monkeypatch.setattr(settings, "assignment_auth_fail_quarantine_threshold", 3)
    run, assignment, agent, db = _make_gate_fixtures(trigger_type="schedule")

    with (
        patch(
            "app.services.assignment_engine._record_assignment_auth_failure",
            new_callable=AsyncMock,
            return_value=3,
        ),
        patch(
            "app.services.assignment_engine._handle_assignment_auth_failure",
            new_callable=AsyncMock,
        ) as handle,
    ):
        await _track_assignment_auth_failures(
            db,
            run=run,
            assignment=assignment,
            agent=agent,
            run_id="run-5",
            messages=list(_AUTH_FAIL_MSGS),
        )

    assert assignment.enabled is False
    handle.assert_awaited_once()
    assert handle.await_args.kwargs["consecutive"] == 3


@pytest.mark.asyncio
async def test_gate_schedule_fanout_also_counts(monkeypatch):
    """A schedule_fanout trigger type advances the counter (non-exempt case).

    Note: a REAL fan-out assignment (company-managed + run_as_all_users) is
    quarantine-exempt — this covers the trigger-type gate alone.
    """
    monkeypatch.setattr(settings, "assignment_auth_fail_quarantine_threshold", 3)
    run, assignment, agent, db = _make_gate_fixtures(trigger_type="schedule_fanout")

    with patch(
        "app.services.assignment_engine._record_assignment_auth_failure",
        new_callable=AsyncMock,
        return_value=1,
    ) as record:
        await _track_assignment_auth_failures(
            db,
            run=run,
            assignment=assignment,
            agent=agent,
            run_id="run-6",
            messages=list(_AUTH_FAIL_MSGS),
        )

    record.assert_awaited_once_with(str(assignment.id))
    assert assignment.enabled is True


@pytest.mark.asyncio
async def test_gate_disabled_assignment_untouched(monkeypatch):
    """An already-disabled assignment is never tracked."""
    monkeypatch.setattr(settings, "assignment_auth_fail_quarantine_threshold", 3)
    run, assignment, agent, db = _make_gate_fixtures(
        trigger_type="schedule", enabled=False
    )

    with (
        patch(
            "app.services.assignment_engine._record_assignment_auth_failure",
            new_callable=AsyncMock,
        ) as record,
        patch(
            "app.services.assignment_engine._reset_assignment_auth_failures",
            new_callable=AsyncMock,
        ) as reset,
    ):
        await _track_assignment_auth_failures(
            db,
            run=run,
            assignment=assignment,
            agent=agent,
            run_id="run-7",
            messages=list(_AUTH_FAIL_MSGS),
        )

    record.assert_not_awaited()
    reset.assert_not_awaited()
    assert assignment.enabled is False


@pytest.mark.asyncio
async def test_gate_empty_messages_untouched(monkeypatch):
    """No messages (failure before the loop) -> no tracking at all."""
    monkeypatch.setattr(settings, "assignment_auth_fail_quarantine_threshold", 3)
    run, assignment, agent, db = _make_gate_fixtures(trigger_type="schedule")

    with (
        patch(
            "app.services.assignment_engine._record_assignment_auth_failure",
            new_callable=AsyncMock,
        ) as record,
        patch(
            "app.services.assignment_engine._reset_assignment_auth_failures",
            new_callable=AsyncMock,
        ) as reset,
    ):
        await _track_assignment_auth_failures(
            db,
            run=run,
            assignment=assignment,
            agent=agent,
            run_id="run-8",
            messages=[],
        )

    record.assert_not_awaited()
    reset.assert_not_awaited()
    assert assignment.enabled is True


@pytest.mark.asyncio
async def test_gate_builtin_assignment_never_quarantined(monkeypatch):
    """Built-in (seeded) assignments are exempt from auto-quarantine.

    Seeding force-re-enables them on every boot, so quarantining only creates
    a disable/resurrect loop while queued fan-out runs fail with
    "Assignment is disabled".
    """
    monkeypatch.setattr(settings, "assignment_auth_fail_quarantine_threshold", 3)
    run, assignment, agent, db = _make_gate_fixtures(
        trigger_type="schedule_fanout", is_builtin=True
    )

    with (
        patch(
            "app.services.assignment_engine._record_assignment_auth_failure",
            new_callable=AsyncMock,
        ) as record,
        patch(
            "app.services.assignment_engine._handle_assignment_auth_failure",
            new_callable=AsyncMock,
        ) as handle,
    ):
        await _track_assignment_auth_failures(
            db,
            run=run,
            assignment=assignment,
            agent=agent,
            run_id="run-builtin",
            messages=list(_AUTH_FAIL_MSGS),
        )

    record.assert_not_awaited()
    handle.assert_not_awaited()
    assert assignment.enabled is True


@pytest.mark.asyncio
async def test_gate_company_fanout_assignment_never_quarantined(monkeypatch):
    """Company-managed per-user fan-out assignments are exempt.

    The failure counter is per ASSIGNMENT, not per user — one user's expired
    credential must never disable the shared automation for everyone.
    """
    monkeypatch.setattr(settings, "assignment_auth_fail_quarantine_threshold", 3)
    run, assignment, agent, db = _make_gate_fixtures(
        trigger_type="schedule_fanout",
        run_as_all_users=True,
        is_company_managed=True,
    )

    with (
        patch(
            "app.services.assignment_engine._record_assignment_auth_failure",
            new_callable=AsyncMock,
        ) as record,
        patch(
            "app.services.assignment_engine._handle_assignment_auth_failure",
            new_callable=AsyncMock,
        ) as handle,
    ):
        await _track_assignment_auth_failures(
            db,
            run=run,
            assignment=assignment,
            agent=agent,
            run_id="run-fanout",
            messages=list(_AUTH_FAIL_MSGS),
        )

    record.assert_not_awaited()
    handle.assert_not_awaited()
    assert assignment.enabled is True


@pytest.mark.asyncio
async def test_gate_user_owned_fanout_flag_still_quarantined(monkeypatch):
    """run_as_all_users WITHOUT is_company_managed is not exempt.

    The scheduler never fans out user-owned assignments (security gate in
    autonomous.py) — such an assignment runs as its owner only, so the
    per-assignment counter is accurate and quarantine stays correct.
    """
    monkeypatch.setattr(settings, "assignment_auth_fail_quarantine_threshold", 3)
    run, assignment, agent, db = _make_gate_fixtures(
        trigger_type="schedule",
        run_as_all_users=True,
        is_company_managed=False,
    )

    with (
        patch(
            "app.services.assignment_engine._record_assignment_auth_failure",
            new_callable=AsyncMock,
            return_value=3,
        ),
        patch(
            "app.services.assignment_engine._handle_assignment_auth_failure",
            new_callable=AsyncMock,
        ) as handle,
    ):
        await _track_assignment_auth_failures(
            db,
            run=run,
            assignment=assignment,
            agent=agent,
            run_id="run-user-fanout",
            messages=list(_AUTH_FAIL_MSGS),
        )

    assert assignment.enabled is False
    handle.assert_awaited_once()


@pytest.mark.asyncio
async def test_gate_clean_run_on_exempt_assignment_still_resets(monkeypatch):
    """A clean run resets the counter even on an exempt assignment."""
    monkeypatch.setattr(settings, "assignment_auth_fail_quarantine_threshold", 3)
    run, assignment, agent, db = _make_gate_fixtures(
        trigger_type="schedule_fanout", is_builtin=True
    )

    with patch(
        "app.services.assignment_engine._reset_assignment_auth_failures",
        new_callable=AsyncMock,
    ) as reset:
        await _track_assignment_auth_failures(
            db,
            run=run,
            assignment=assignment,
            agent=agent,
            run_id="run-clean-builtin",
            messages=list(_CLEAN_MSGS),
        )

    reset.assert_awaited_once_with(str(assignment.id), report_user_id=None)
    assert assignment.enabled is True


@pytest.mark.asyncio
async def test_gate_never_raises(monkeypatch):
    """An unexpected error inside tracking is swallowed (best-effort)."""
    monkeypatch.setattr(settings, "assignment_auth_fail_quarantine_threshold", 3)
    run, assignment, agent, db = _make_gate_fixtures(trigger_type="schedule")

    with patch(
        "app.services.assignment_engine._record_assignment_auth_failure",
        new_callable=AsyncMock,
        side_effect=RuntimeError("boom"),
    ):
        # Must not raise.
        await _track_assignment_auth_failures(
            db,
            run=run,
            assignment=assignment,
            agent=agent,
            run_id="run-9",
            messages=list(_AUTH_FAIL_MSGS),
        )

    assert assignment.enabled is True


# ---------------------------------------------------------------------------
# The report-only notice waits for N consecutive failing runs, on a
# counter of its own, and words the remedy by failure kind
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_report_failure_uses_its_own_key():
    assignment_id = str(uuid.uuid4())
    fake_redis = AsyncMock()
    fake_redis.eval = AsyncMock(return_value=1)

    async def _fake_redis_client():
        return fake_redis

    with patch("app.core.redis.redis_client", new=_fake_redis_client):
        assert await _record_assignment_auth_report_failure(assignment_id) == 1

    keys = str(fake_redis.eval.call_args)
    assert f"{_ASSIGNMENT_AUTH_REPORT_FAIL_KEY_PREFIX}{assignment_id}" in keys
    assert f"{_ASSIGNMENT_AUTH_FAIL_KEY_PREFIX}{assignment_id}'" not in keys


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "trigger_type,is_builtin", [("webhook", False), ("manual", False), ("schedule", True)]
)
async def test_gate_single_report_only_failure_notifies_nobody(
    monkeypatch, trigger_type, is_builtin
):
    """One auth-shaped failure on a run that is never paused tells nobody."""
    monkeypatch.setattr(settings, "assignment_auth_fail_quarantine_threshold", 3)
    run, assignment, agent, db = _make_gate_fixtures(
        trigger_type=trigger_type, is_builtin=is_builtin
    )

    with (
        patch(
            "app.services.assignment_engine._record_assignment_auth_failure",
            new_callable=AsyncMock,
        ) as record,
        patch(
            "app.services.assignment_engine._record_assignment_auth_report_failure",
            new_callable=AsyncMock,
            return_value=1,
        ) as record_report,
        # The identity runs often: the next runs will say whether this one
        # failure was a dead credential.
        patch(
            "app.services.assignment_engine._identity_ran_recently",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "app.services.assignment_engine._report_unquarantined_auth_failure",
            new_callable=AsyncMock,
        ) as report,
    ):
        await _track_assignment_auth_failures(
            db,
            run=run,
            assignment=assignment,
            agent=agent,
            run_id="run-single",
            messages=list(_AUTH_FAIL_MSGS),
        )

    # The pause counter is never touched by a report-only run: a manual retry
    # must not bring the next scheduled failure closer to a pause.
    record.assert_not_awaited()
    record_report.assert_awaited_once_with(str(assignment.id), None)
    report.assert_not_awaited()
    assert assignment.enabled is True


@pytest.mark.asyncio
async def test_gate_report_counter_below_threshold_after_redis_outage(monkeypatch):
    """Redis down -> the counter reads 0 -> the notice is dropped, quietly."""
    monkeypatch.setattr(settings, "assignment_auth_fail_quarantine_threshold", 3)
    run, assignment, agent, db = _make_gate_fixtures(trigger_type="webhook")

    async def _boom():
        raise RuntimeError("redis down")

    with (
        patch("app.core.redis.redis_client", new=_boom),
        patch(
            "app.services.assignment_engine._report_unquarantined_auth_failure",
            new_callable=AsyncMock,
        ) as report,
    ):
        await _track_assignment_auth_failures(
            db,
            run=run,
            assignment=assignment,
            agent=agent,
            run_id="run-outage",
            messages=list(_AUTH_FAIL_MSGS),
        )

    report.assert_not_awaited()


def test_a_success_mentioning_auth_is_not_an_auth_failure_run():
    """A healthy CTI digest that reads a report about unauthorized access must
    not count toward a pause."""
    messages = [
        _tool_msg(
            "Report: APT29 gained unauthorized access to the domain controller "
            "via a phishing lure."
        ),
        _tool_msg("Created issue #401: Fix login redirect"),
        _tool_msg("Found 2 issues:\n- #12 Permission denied when saving\n- #13 Dark mode"),
    ]
    assert _messages_contain_auth_failure(messages) is False


@pytest.mark.parametrize(
    "content,kind",
    [
        (
            "Error: Notion authentication failed - the token may be expired "
            "or revoked. The user should re-authorize this integration.",
            "credential",
        ),
        ("401 Unauthorized", "credential"),
        ("Bad credentials", "credential"),
        (
            "Error: HubSpot access denied while trying to list campaigns - "
            "insufficient scopes. Required scopes: content.",
            "permission",
        ),
        ("Permission denied (publickey).", "permission"),
    ],
)
def test_auth_failure_kind(content, kind):
    assert _auth_failure_kind([content]) == kind


def test_a_run_with_both_failure_kinds_names_both_remedies():
    """Re-authorizing fixes the rejected credential, not the missing
    permission: a run with both is ``mixed`` and its notice says both."""
    from app.services.assignment_engine import _auth_failure_remedy

    both = ["401 Unauthorized", "Permission denied (publickey)."]
    assert _auth_failure_kind(both) == "mixed"
    assert _auth_failure_kind(["401 Unauthorized", "Bad credentials"]) == "credential"
    assert _auth_failure_kind(["Permission denied (publickey)."]) == "permission"
    assert _auth_failure_kind([]) == "permission"
    remedy = _auth_failure_remedy("mixed")
    assert "re-authorize" in remedy
    assert "grant the missing permission" in remedy


def test_an_opencti_missing_capability_is_a_permission_failure():
    from app.integrations.providers.opencti._common import _describe_graphql_failure

    message = _describe_graphql_failure(
        [{"message": "You are not allowed to do this.", "extensions": {"code": "FORBIDDEN_ACCESS"}}],
        "CreateLabel",
        "labelAdd",
        "mutation CreateLabel { labelAdd(input: {}) { id } }",
    )
    assert _messages_contain_auth_failure([_tool_msg(message)]) is True
    assert _auth_failure_kind([message]) == "permission"


def _notice_fixtures():
    assignment = MagicMock()
    assignment.id = uuid.uuid4()
    assignment.name = "SOAR Orchestrator"
    assignment.user_id = uuid.uuid4()
    assignment.is_company_managed = False
    agent = MagicMock()
    agent.id = uuid.uuid4()
    agent.name = "SecOps Orchestrator"
    return assignment, agent


@pytest.mark.asyncio
async def test_a_permission_failure_is_not_worded_as_a_dead_credential():
    assignment, agent = _notice_fixtures()
    db = _make_mock_db()
    fake_redis = AsyncMock()
    fake_redis.set = AsyncMock(return_value=True)
    fake_redis.publish = AsyncMock(return_value=1)

    async def _fake_redis_client():
        return fake_redis

    with patch("app.core.redis.redis_client", new=_fake_redis_client):
        await _report_unquarantined_auth_failure(
            db,
            assignment=assignment,
            agent=agent,
            run_id="run-perm",
            reason="only scheduled runs are auto-paused",
            consecutive=3,
            kind="permission",
        )

    added = db.add.call_args[0][0]
    assert "permission or capability" in added.summary
    assert "expired or revoked" not in added.summary
    assert "3 time(s) in a row" in added.summary
    assert added.extra_data["auth_failure_kind"] == "permission"


# ---------------------------------------------------------------------------
# A notice claim Redis could not check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quarantine_notice_is_sent_when_the_claim_cannot_be_checked(caplog):
    """The assignment is paused whatever happens: a duplicate beats silence."""
    assignment, agent = _notice_fixtures()
    with (
        patch(
            "app.services.assignment_engine._claim_auth_failure_notice",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "app.services.assignment_engine._emit_assignment_notice",
            new_callable=AsyncMock,
            return_value=False,
        ) as emit,
        patch(
            "app.services.assignment_engine._release_auth_failure_notice",
            new_callable=AsyncMock,
        ) as release,
    ):
        await _handle_assignment_auth_failure(
            _make_mock_db(),
            assignment=assignment,
            agent=agent,
            run_id="run-q",
            consecutive=3,
        )

    emit.assert_awaited_once()
    # Nothing was claimed, so nothing is given back.
    release.assert_not_awaited()
    assert "without deduplication" in caplog.text


@pytest.mark.asyncio
async def test_quarantine_notice_stays_suppressed_when_already_claimed():
    assignment, agent = _notice_fixtures()
    with (
        patch(
            "app.services.assignment_engine._claim_auth_failure_notice",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "app.services.assignment_engine._emit_assignment_notice",
            new_callable=AsyncMock,
        ) as emit,
    ):
        await _handle_assignment_auth_failure(
            _make_mock_db(),
            assignment=assignment,
            agent=agent,
            run_id="run-q2",
            consecutive=3,
        )
    emit.assert_not_awaited()


@pytest.mark.asyncio
async def test_report_notice_is_dropped_when_the_claim_cannot_be_checked():
    """The assignment keeps running: no alert storm, the next run retries."""
    assignment, agent = _notice_fixtures()
    with (
        patch(
            "app.services.assignment_engine._claim_auth_failure_notice",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "app.services.assignment_engine._emit_assignment_notice",
            new_callable=AsyncMock,
        ) as emit,
    ):
        await _report_unquarantined_auth_failure(
            _make_mock_db(),
            assignment=assignment,
            agent=agent,
            run_id="run-r",
            reason="only scheduled runs are auto-paused",
            consecutive=3,
        )
    emit.assert_not_awaited()


# ---------------------------------------------------------------------------
# Result shapes: what main's detector (the raw ``is_auth_or_permission_error``
# on every tool message) counted, and what counts now. A real failure main
# counted must still count; only a success that mentions one may stop.
# ---------------------------------------------------------------------------


def _formatted_http_error(status: int, body: dict, provider: str, action: str) -> str:
    import httpx

    from app.integrations.http import format_http_error

    response = httpx.Response(
        status, request=httpx.Request("GET", "https://api.example.test/x"), json=body
    )
    return format_http_error(response, provider, action)


def _opencti_forbidden() -> str:
    from app.integrations.providers.opencti._common import _describe_graphql_failure

    return _describe_graphql_failure(
        [{"message": "You are not allowed to do this.", "extensions": {"code": "FORBIDDEN_ACCESS"}}],
        "CreateLabel",
        "labelAdd",
        "mutation CreateLabel { labelAdd(input: {}) { id } }",
    )


_REAL_AUTH_FAILURES = {
    "error_prefixed_text": (
        "Error: Notion authentication failed - the token may be expired or revoked."
    ),
    "format_http_error_401": _formatted_http_error(
        401, {"message": "Bad credentials"}, "GitHub", "list repos"
    ),
    "format_http_error_403": _formatted_http_error(
        403, {"message": "Resource not accessible by integration"}, "GitHub", "list repos"
    ),
    "integration_executor_raised": (
        "Error executing tool 'get_issue': Client error '401 Unauthorized' for url "
        "'https://api.example.test/x'"
    ),
    "agent_loop_raised": "Error executing tool: Unauthorized: token revoked",
    "mcp_raised": (
        "Tool 'search' failed with error: 401 Unauthorized\n\n"
        "Detected an authentication/permission failure."
    ),
    "mcp_is_error_axios": "Request failed with status code 401",
    "mcp_is_error_status_line": (
        "Jira API returned 401 Unauthorized for GET /rest/api/3/search"
    ),
    "mcp_is_error_github": (
        "failed to get issue: GET https://api.github.com/repos/o/r/issues/1: "
        "401 Bad credentials []"
    ),
    "json_github": (
        '{"message": "Bad credentials", "documentation_url": "https://docs.github.com/rest"}'
    ),
    "json_notion": (
        '{"object": "error", "status": 401, "code": "unauthorized", '
        '"message": "API token is invalid."}'
    ),
    "json_jira": (
        '{"message": "Client must be authenticated to access this resource.", '
        '"status-code": 401}'
    ),
    "json_google": (
        '{"error": {"code": 401, "message": "Request had invalid authentication '
        'credentials.", "status": "UNAUTHENTICATED"}}'
    ),
    "graphql_errors": (
        '{"errors": [{"message": "Unauthorized", "extensions": '
        '{"code": "UNAUTHENTICATED"}}], "data": null}'
    ),
    "opencti_forbidden_access": _opencti_forbidden(),
    "http_status_line": "HTTP 401 Unauthorized",
    "status_then_detail": "401 Unauthorized: your API key is invalid.",
}

_SUCCESSES_MENTIONING_AUTH = {
    "threat_report": (
        "Report: APT29 gained unauthorized access to the domain controller "
        "via a phishing lure."
    ),
    "issue_list": "Found 2 issues:\n- #12 Permission denied when saving\n- #13 Dark mode",
    "echoed_title": "Issue created: SEC-12 \u2014 Login failed (401) for SSO users",
    "json_success_detail": '{"detail": "Alert \'Unauthorized sign-in\' acknowledged"}',
    "json_array": '[{"title": "Unauthorized sign-in attempt", "severity": "high"}]',
    "issue_id": "Created issue #401: Fix login redirect",
    "json_success_id": '{"status": "ok", "message": "Comment added to PROJ-401"}',
}


@pytest.mark.parametrize(
    "content", list(_REAL_AUTH_FAILURES.values()), ids=list(_REAL_AUTH_FAILURES)
)
def test_every_real_failure_main_counted_still_counts(content):
    from app.tools.base import is_auth_or_permission_error

    assert is_auth_or_permission_error(content) is True  # main's verdict
    assert _messages_contain_auth_failure([_tool_msg(content)]) is True


@pytest.mark.parametrize(
    "content", list(_SUCCESSES_MENTIONING_AUTH.values()), ids=list(_SUCCESSES_MENTIONING_AUTH)
)
def test_a_success_that_mentions_auth_no_longer_counts(content):
    assert _messages_contain_auth_failure([_tool_msg(content)]) is False


# ---------------------------------------------------------------------------
# The report-only evidence gate: per execution identity, and only while the
# identity runs often enough to bring the evidence within a day
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_rare_identity_is_reported_at_its_first_failure(monkeypatch):
    """A weekly or monthly identity cannot show N failures in a row within the
    counter's life: its first failure is reported, as it always was."""
    monkeypatch.setattr(settings, "assignment_auth_fail_quarantine_threshold", 3)
    run, assignment, agent, db = _make_gate_fixtures(trigger_type="webhook")

    with (
        patch(
            "app.services.assignment_engine._record_assignment_auth_report_failure",
            new_callable=AsyncMock,
            return_value=1,
        ),
        patch(
            "app.services.assignment_engine._identity_ran_recently",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "app.services.assignment_engine._report_unquarantined_auth_failure",
            new_callable=AsyncMock,
        ) as report,
    ):
        await _track_assignment_auth_failures(
            db,
            run=run,
            assignment=assignment,
            agent=agent,
            run_id="run-rare",
            messages=list(_AUTH_FAIL_MSGS),
        )

    report.assert_awaited_once()
    assert report.await_args.kwargs["consecutive"] == 1
    assert assignment.enabled is True


@pytest.mark.asyncio
async def test_gate_fanout_counts_and_resets_each_user_apart(monkeypatch):
    """One user's clean run is not the end of another user's streak."""
    monkeypatch.setattr(settings, "assignment_auth_fail_quarantine_threshold", 3)
    run, assignment, agent, db = _make_gate_fixtures(
        trigger_type="schedule_fanout", run_as_all_users=True, is_company_managed=True
    )
    run.user_id = uuid.uuid4()

    with (
        patch(
            "app.services.assignment_engine._record_assignment_auth_report_failure",
            new_callable=AsyncMock,
            return_value=1,
        ) as record_report,
        patch(
            "app.services.assignment_engine._identity_ran_recently",
            new_callable=AsyncMock,
            return_value=True,
        ) as ran_recently,
        patch(
            "app.services.assignment_engine._report_unquarantined_auth_failure",
            new_callable=AsyncMock,
        ) as report,
    ):
        await _track_assignment_auth_failures(
            db,
            run=run,
            assignment=assignment,
            agent=agent,
            run_id="run-fanout-fail",
            messages=list(_AUTH_FAIL_MSGS),
        )

    record_report.assert_awaited_once_with(str(assignment.id), run.user_id)
    assert ran_recently.await_args.kwargs["per_user"] is True
    report.assert_not_awaited()

    with patch(
        "app.services.assignment_engine._reset_assignment_auth_failures",
        new_callable=AsyncMock,
    ) as reset:
        await _track_assignment_auth_failures(
            db,
            run=run,
            assignment=assignment,
            agent=agent,
            run_id="run-fanout-clean",
            messages=list(_CLEAN_MSGS),
        )
    reset.assert_awaited_once_with(str(assignment.id), report_user_id=run.user_id)


def test_report_keys_are_per_user_only_when_given_one():
    assignment_id = str(uuid.uuid4())
    user_a, user_b = uuid.uuid4(), uuid.uuid4()
    base = f"{_ASSIGNMENT_AUTH_REPORT_FAIL_KEY_PREFIX}{assignment_id}"
    assert _auth_report_key(assignment_id) == base
    assert _auth_report_key(assignment_id, user_a) == f"{base}:{user_a}"
    assert _auth_report_key(assignment_id, user_a) != _auth_report_key(assignment_id, user_b)


@pytest.mark.asyncio
async def test_report_counter_refreshes_its_expiry_on_every_failure():
    """A streak whose runs come a day apart must outlive a high threshold."""
    fake_redis = AsyncMock()
    fake_redis.eval = AsyncMock(return_value=2)

    async def _fake_redis_client():
        return fake_redis

    with patch("app.core.redis.redis_client", new=_fake_redis_client):
        await _record_assignment_auth_report_failure(str(uuid.uuid4()))
        await _record_assignment_auth_failure(str(uuid.uuid4()))

    report_script = fake_redis.eval.await_args_list[0].args[0]
    pause_script = fake_redis.eval.await_args_list[1].args[0]
    assert report_script == _ASSIGNMENT_AUTH_REPORT_INCR_LUA
    assert "if" not in report_script
    # The pause counter keeps main's expiry, set once per streak.
    assert pause_script != report_script


def _lookup_run(created_at):
    run = MagicMock()
    run.id = uuid.uuid4()
    run.assignment_id = uuid.uuid4()
    run.user_id = uuid.uuid4()
    run.created_at = created_at
    return run


@pytest.mark.asyncio
@pytest.mark.parametrize("row,expected", [((uuid.uuid4(),), True), (None, False)])
async def test_identity_ran_recently_reads_the_previous_run(row, expected):
    from datetime import UTC, datetime

    db = _make_mock_db()
    db.execute = AsyncMock(return_value=MagicMock(first=MagicMock(return_value=row)))
    run = _lookup_run(datetime.now(UTC))

    assert await _identity_ran_recently(db, run, per_user=True) is expected
    db.execute.assert_awaited_once()
    db.begin_nested.assert_called_once()


@pytest.mark.asyncio
async def test_identity_ran_recently_is_false_when_it_cannot_tell():
    """No previous run to point to is the case main always reported."""
    from datetime import UTC, datetime

    db = _make_mock_db()
    db.execute = AsyncMock(side_effect=RuntimeError("db down"))
    assert await _identity_ran_recently(db, _lookup_run(datetime.now(UTC)), per_user=False) is False

    db.execute = AsyncMock()
    assert await _identity_ran_recently(db, _lookup_run(None), per_user=False) is False
    db.execute.assert_not_awaited()
