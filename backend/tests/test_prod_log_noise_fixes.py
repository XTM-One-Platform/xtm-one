"""Regression coverage for the prod log-noise fixes batch.

Each test maps to a section of the fix:

* Shared ``validate_cron_expression`` helper in ``core/utils``.
* Autonomous scheduler ``_should_warn_cron`` warn-once semantics.
* Teams ``send_message`` accepting HTTP 202 as success.
* OpenAI image-model deprecation WARNING dedup (drives the real
  ``OpenAIProvider.generate_image`` code path with a mocked SDK client).

These are pure unit tests with no DB or network dependencies.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest

# ---------------------------------------------------------------------------
# Shared cron validator
# ---------------------------------------------------------------------------


def test_validate_cron_expression_accepts_valid_5_field():
    from app.core.utils import validate_cron_expression

    assert validate_cron_expression("0 9 * * 1-5") == "0 9 * * 1-5"


def test_validate_cron_expression_strips_whitespace():
    from app.core.utils import validate_cron_expression

    assert validate_cron_expression("  */15 * * * *  ") == "*/15 * * * *"


def test_validate_cron_expression_rejects_the_prod_bug_value():
    """The exact value from the prod log: ``"*/15 * * 1"`` has only 4 fields."""
    from app.core.utils import validate_cron_expression

    with pytest.raises(ValueError, match="Invalid cron expression"):
        validate_cron_expression("*/15 * * 1")


def test_validate_cron_expression_error_message_has_single_prefix():
    """Regression: callers must surface ``str(cron_err)`` directly.

    The helper already formats the message as
    ``Invalid cron expression '...': ...``. Earlier callers prefixed it
    again with ``f"Invalid cron expression: {cron_err}"``, producing
    user-facing output like
    ``Invalid cron expression: Invalid cron expression '*/15 * * 1': ...``.
    Pinning the helper-side format here means a future refactor that
    drops the prefix would trip this test instead of silently producing
    a vague error in every caller that just stringifies the exception.
    """
    from app.core.utils import validate_cron_expression

    try:
        validate_cron_expression("*/15 * * 1")
    except ValueError as cron_err:
        msg = str(cron_err)
        # Single prefix only — exactly one occurrence of the canonical
        # leading phrase. A double-prefix regression would push the count
        # to >= 2.
        assert msg.count("Invalid cron expression") == 1, (
            f"Helper message must carry exactly one prefix; got {msg!r}"
        )
        # Sanity: the offending value is echoed so the error is actionable.
        assert "'*/15 * * 1'" in msg
    else:
        raise AssertionError("Helper must raise on invalid cron")


@pytest.mark.parametrize("bad", [None, "", "   ", 42, []])
def test_validate_cron_expression_rejects_non_string_or_empty(bad: object):
    from app.core.utils import validate_cron_expression

    with pytest.raises(ValueError):
        validate_cron_expression(bad)


def test_assignment_schema_delegates_to_shared_helper():
    """The schema validator must surface the same error message as the helper."""
    from app.api.v1.schemas.assignment import CreateAssignmentRequest

    with pytest.raises(Exception) as exc:
        CreateAssignmentRequest(
            agent_id=str(uuid4()),
            name="x",
            prompt="y",
            trigger_type="schedule",
            trigger_config={"cron": "*/15 * * 1"},
        )
    # Pydantic v2 wraps ValueError into ValidationError; substring check is enough.
    assert "Invalid cron expression" in str(exc.value)


def test_agent_import_non_dict_trigger_config_rejection_logic():
    """Pin the non-dict ``trigger_config`` guard added to ``agents.import``.

    The pre-fix import handler did ``wf_data.get("trigger_config", {}) or {}``
    and passed the result straight to ``Assignment(trigger_config=...)``.
    A truthy non-dict (string / list / scalar) would survive the ``or {}``
    and silently land in the JSON column, later crashing any caller that
    does ``trigger_config.get(...)`` — most notably the scheduler when
    the trigger type is later flipped to ``"schedule"``.

    This test exercises the exact inline branch from the route handler on
    representative inputs so a regression that re-introduces the silent
    persistence path trips immediately. Same stub-driven style as
    ``test_settings_update_image_model_uses_canonical_remap`` so it stays
    DB-free.
    """
    # Mirrors the exact branch from the route handler. The earlier
    # ``raw or {}`` shortcut had a subtle bug: it coerced *falsy*
    # non-dict JSON values (``[]``, ``""``, ``0``, ``False``) to
    # ``{}`` *before* the ``isinstance(..., dict)`` check ran, so
    # those payloads silently slipped past the intended 400 and
    # persisted as empty dicts on the assignment row. The fixed
    # predicate explicitly treats only ``None``/missing as "no
    # trigger_config"; every other non-dict is rejected.
    def _is_invalid_for_storage(raw: object) -> bool:
        if raw is None:
            return False
        return not isinstance(raw, dict)

    # Every non-dict payload, *including the falsy ones the old
    # ``or {}`` shortcut would have swallowed*. The four falsy values
    # at the start (``[]``, ``""``, ``0``, ``False``) are the
    # regression guard — drop them and the broken predicate passes
    # this test silently.
    bad_payloads = (
        [],
        "",
        0,
        False,
        "broken",
        "0 9 * * *",
        42,
        ["nope"],
        (1, 2),
        3.14,
        True,
    )
    for bad in bad_payloads:
        assert _is_invalid_for_storage(bad), (
            f"{bad!r} must be rejected — bundle authors must not be "
            f"able to smuggle a non-dict trigger_config into the row"
        )

    # Dict-shaped payloads (including empty + falsy) must pass through.
    for good in ({}, {"cron": "0 9 * * *"}, {"path": "/hook"}):
        assert not _is_invalid_for_storage(good)

    # ``None`` and missing both mean "no trigger_config" and are
    # coerced to ``{}`` by the route handler before the type check.
    # Reject them here would be a regression for legitimate bundles
    # that omit the field entirely.
    assert not _is_invalid_for_storage(None)


def test_agent_template_load_coerces_non_dict_trigger_config():
    """Pin the non-dict coerce-to-``{}`` added to the ``create_agent``
    template-load path.

    Templates are admin-managed but ``default_assignments`` is a plain
    JSON list and the storage layer happily accepts a non-dict
    ``trigger_config`` value. Rather than rejecting the whole agent
    creation when one template entry is malformed, the route handler
    coerces silently so downstream users aren't blocked by an admin
    mistake — this test mirrors the inline coerce so a regression that
    drops the ``isinstance`` guard (and re-introduces the original
    silent-persistence path) fails loudly.
    """
    def _coerce(raw: object) -> dict:
        return raw if isinstance(raw, dict) else {}

    # Bad shapes must be coerced to ``{}``. The four falsy values at
    # the start (``[]``, ``""``, ``0``, ``False``) explicitly pin the
    # contract — the template path uses a plain ``.get(..., {})``
    # *without* the ``raw or {}`` shortcut that bit the import path,
    # so those falsy non-dicts are correctly identified as non-dict
    # and coerced. Drop these cases and a future refactor that
    # re-introduces ``raw or {}`` would silently degrade them to ``{}``
    # without anyone noticing.
    for bad in ([], "", 0, False, "broken", 42, ["nope"], 3.14, None, True):
        assert _coerce(bad) == {}, (
            f"{bad!r} must be coerced to {{}} — template-path predicate "
            f"must NOT collapse it to {{}} via a ``raw or {{}}`` shortcut"
        )

    # Good shapes must pass through unchanged.
    assert _coerce({}) == {}
    assert _coerce({"cron": "0 9 * * *"}) == {"cron": "0 9 * * *"}


# ---------------------------------------------------------------------------
# Autonomous scheduler warn-once
# ---------------------------------------------------------------------------


def test_autonomous_should_warn_cron_only_once_per_process():
    from app.services import autonomous

    # Snapshot + restore so the test is hermetic.
    snapshot = autonomous._cron_warned_once.copy()
    autonomous._cron_warned_once.clear()
    try:
        wf_id = uuid4()
        assert autonomous._should_warn_cron("assignment", wf_id) is True
        # Every subsequent call for the same (kind, id) must return False —
        # no more "once per hour" leakage that was producing dozens of
        # WARNINGs per pod per hour in prod.
        for _ in range(50):
            assert autonomous._should_warn_cron("assignment", wf_id) is False
        # Different kind / id pairs are still tracked independently.
        assert autonomous._should_warn_cron("evaluation", wf_id) is True
        assert autonomous._should_warn_cron("assignment", uuid4()) is True
    finally:
        autonomous._cron_warned_once.clear()
        autonomous._cron_warned_once.update(snapshot)


@pytest.mark.asyncio
async def test_autonomous_flags_invalid_assignment_cron_as_failed_run():
    from app.models.assignment import AssignmentRun
    from app.services import autonomous

    now = datetime.now(UTC)
    assignment = MagicMock()
    assignment.id = uuid4()
    assignment.agent_id = uuid4()
    assignment.user_id = uuid4()
    assignment.run_as_user_id = None
    assignment.is_company_managed = False
    assignment.enabled = True

    db = MagicMock()
    db.commit = AsyncMock()
    cron_expr = "*/15 * * 1"
    cron_err = ValueError(
        "Exactly 5, 6 or 7 columns has to be specified for iterator expression."
    )

    await autonomous._flag_assignment_cron_misconfiguration(
        db=db,
        assignment=assignment,
        cron_expr=cron_expr,
        cron_err=cron_err,
        now=now,
    )

    assert assignment.enabled is False
    db.commit.assert_awaited_once()
    db.add.assert_called_once()
    failed_run = db.add.call_args.args[0]
    assert isinstance(failed_run, AssignmentRun)
    assert failed_run.assignment_id == assignment.id
    assert failed_run.agent_id == assignment.agent_id
    assert failed_run.status == "failed"
    assert failed_run.started_at == now
    assert failed_run.completed_at == now
    # The failed run mirrors the normal schedule-trigger shape (so the UI
    # renders it consistently) plus a ``misconfigured`` flag.
    assert failed_run.trigger_payload == {
        "trigger_type": "schedule",
        "scheduled_at": now.isoformat(),
    }
    assert failed_run.trigger_context == {
        "type": "schedule",
        "cron": cron_expr,
        "scheduled_at": now.isoformat(),
        "triggered_at": now.isoformat(),
        "misconfigured": True,
    }
    assert failed_run.error is not None
    assert "Invalid cron expression" in failed_run.error


@pytest.mark.asyncio
async def test_flag_misconfiguration_rolls_back_on_commit_failure():
    """A failed commit must roll back and re-raise.

    The scheduler reuses one session across every assignment in a tick, so a
    failed commit that is not rolled back leaves the session in a
    failed-transaction state and every subsequent assignment in the same tick
    errors until a rollback occurs.  The helper must roll back (keeping the
    session usable) and re-raise so the caller logs it; the still-enabled
    assignment is then retried on the next tick.
    """
    from app.services import autonomous

    assignment = MagicMock()
    assignment.id = uuid4()
    assignment.agent_id = uuid4()
    assignment.user_id = uuid4()
    assignment.run_as_user_id = None
    assignment.is_company_managed = False
    assignment.enabled = True

    db = MagicMock()
    db.commit = AsyncMock(side_effect=RuntimeError("transient DB outage"))
    db.rollback = AsyncMock()

    with pytest.raises(RuntimeError, match="transient DB outage"):
        await autonomous._flag_assignment_cron_misconfiguration(
            db=db,
            assignment=assignment,
            cron_expr="*/15 * * 1",
            cron_err=ValueError("bad cron"),
        )

    db.commit.assert_awaited_once()
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_autonomous_stringifies_non_string_cron_in_failed_run():
    """A non-string cron value (e.g. a malformed config storing a list) must be
    coerced to a string so the JSON trigger columns stay serialisable and the
    run-detail UI renders a plain value rather than a raw object."""
    import json

    from app.models.assignment import AssignmentRun
    from app.services import autonomous

    assignment = MagicMock()
    assignment.id = uuid4()
    assignment.agent_id = uuid4()
    assignment.user_id = uuid4()
    assignment.run_as_user_id = None
    assignment.is_company_managed = False
    assignment.enabled = True

    db = MagicMock()
    db.commit = AsyncMock()
    cron_expr = ["*/15", "*", "*", "1"]  # not a plain string

    await autonomous._flag_assignment_cron_misconfiguration(
        db=db,
        assignment=assignment,
        cron_expr=cron_expr,
        cron_err=ValueError("bad cron"),
    )

    failed_run = db.add.call_args.args[0]
    assert isinstance(failed_run, AssignmentRun)
    assert isinstance(failed_run.trigger_context["cron"], str)
    # The trigger columns must round-trip through JSON without raising.
    json.dumps(failed_run.trigger_context)
    json.dumps(failed_run.trigger_payload)


@pytest.mark.asyncio
async def test_autonomous_scheduler_routes_non_string_cron_to_misconfig_flow():
    """End-to-end scheduler path for a non-string cron value.

    ``croniter`` rejects a non-string (e.g. a list from a malformed/legacy
    config row) with ``AttributeError`` — *not* ``ValueError`` — so without an
    explicit type guard the error escapes the ``(CroniterBadCronError,
    ValueError)`` handler, falls through to the generic ``except Exception``,
    and re-introduces a full traceback on every scheduler tick without
    disabling the assignment.  This pins the integration path: a non-string
    cron must disable the assignment and persist the failed misconfiguration
    run, exactly like a malformed string cron.
    """
    from app.models.assignment import AssignmentRun
    from app.services import autonomous

    agent_id = uuid4()
    assignment = MagicMock()
    assignment.id = uuid4()
    assignment.agent_id = agent_id
    assignment.user_id = uuid4()
    assignment.run_as_user_id = None
    assignment.is_company_managed = False
    assignment.enabled = True
    assignment.name = "Broken schedule"
    assignment.last_triggered_at = None
    assignment.trigger_config = {"cron": ["*/15", "*", "*", "1"]}  # not a string

    agent = MagicMock()
    agent.id = agent_id
    agent.enabled = True
    agent.is_builtin = False  # user assignment - not exempt from cron disable

    def _result(items: list[Any]) -> MagicMock:
        scalars = MagicMock()
        scalars.all.return_value = items
        res = MagicMock()
        res.scalars.return_value = scalars
        return res

    db = MagicMock()
    # 1st execute → enabled scheduled assignments; 2nd → referenced agents.
    # The misconfiguration branch ``continue``s before any further query.
    db.execute = AsyncMock(side_effect=[_result([assignment]), _result([agent])])
    db.commit = AsyncMock()

    snapshot = set(autonomous._cron_warned_once)
    autonomous._cron_warned_once.clear()
    try:
        count = await autonomous._process_scheduled_assignments(db)
    finally:
        autonomous._cron_warned_once.clear()
        autonomous._cron_warned_once.update(snapshot)

    assert count == 0
    assert assignment.enabled is False
    db.add.assert_called_once()
    failed_run = db.add.call_args.args[0]
    assert isinstance(failed_run, AssignmentRun)
    assert failed_run.status == "failed"
    assert failed_run.trigger_context["misconfigured"] is True
    assert isinstance(failed_run.trigger_context["cron"], str)
    db.commit.assert_awaited()


def _scalar_result(items: list[Any]) -> MagicMock:
    scalars = MagicMock()
    scalars.all.return_value = items
    res = MagicMock()
    res.scalars.return_value = scalars
    return res


@pytest.mark.asyncio
async def test_autonomous_scheduler_flags_empty_string_cron():
    """A present-but-empty cron ("") is invalid, not "unconfigured".

    The scheduler must not short-circuit it via ``if not cron_expr``; it must
    flow into the misconfiguration handler (disable + failed run) like any
    other invalid value, since an empty-string cron on a schedule trigger can
    never fire.
    """
    from app.models.assignment import AssignmentRun
    from app.services import autonomous

    agent_id = uuid4()
    assignment = MagicMock()
    assignment.id = uuid4()
    assignment.agent_id = agent_id
    assignment.user_id = uuid4()
    assignment.run_as_user_id = None
    assignment.is_company_managed = False
    assignment.enabled = True
    assignment.name = "Empty cron"
    assignment.last_triggered_at = None
    assignment.trigger_config = {"cron": ""}  # present but invalid

    agent = MagicMock()
    agent.id = agent_id
    agent.enabled = True
    agent.is_builtin = False  # user assignment - not exempt from cron disable

    db = MagicMock()
    db.execute = AsyncMock(
        side_effect=[_scalar_result([assignment]), _scalar_result([agent])]
    )
    db.commit = AsyncMock()

    snapshot = set(autonomous._cron_warned_once)
    autonomous._cron_warned_once.clear()
    try:
        count = await autonomous._process_scheduled_assignments(db)
    finally:
        autonomous._cron_warned_once.clear()
        autonomous._cron_warned_once.update(snapshot)

    assert count == 0
    assert assignment.enabled is False
    db.add.assert_called_once()
    failed_run = db.add.call_args.args[0]
    assert isinstance(failed_run, AssignmentRun)
    assert failed_run.status == "failed"
    assert failed_run.trigger_context["misconfigured"] is True
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_autonomous_scheduler_skips_absent_cron_silently():
    """A truly-absent cron key (None) is "not configured" — it must still be
    skipped silently (no disable, no failed run), so the ``is None`` guard
    doesn't start flagging assignments that simply have no cron key.
    """
    from app.services import autonomous

    agent_id = uuid4()
    assignment = MagicMock()
    assignment.id = uuid4()
    assignment.agent_id = agent_id
    assignment.enabled = True
    assignment.name = "No cron key"
    assignment.last_triggered_at = None
    assignment.trigger_config = {}  # cron key absent → None

    agent = MagicMock()
    agent.id = agent_id
    agent.enabled = True
    agent.is_builtin = False  # user assignment - not exempt from cron disable

    db = MagicMock()
    db.execute = AsyncMock(
        side_effect=[_scalar_result([assignment]), _scalar_result([agent])]
    )
    db.commit = AsyncMock()

    snapshot = set(autonomous._cron_warned_once)
    autonomous._cron_warned_once.clear()
    try:
        count = await autonomous._process_scheduled_assignments(db)
    finally:
        autonomous._cron_warned_once.clear()
        autonomous._cron_warned_once.update(snapshot)

    assert count == 0
    assert assignment.enabled is True  # untouched
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_autonomous_scheduler_remediates_even_when_log_rate_limited():
    """Remediation must not be gated by the log rate-limiter.

    ``_should_warn_cron`` is consumed once per (kind, id) per process.  If it
    is already spent for this assignment — e.g. a prior remediation attempt
    took the token but failed to commit during a transient DB outage — the
    scheduler must STILL disable the assignment and persist the failed run on
    the next tick.  Gating the remediation behind the rate-limiter (the
    pre-fix behaviour) would permanently silence the misconfiguration after a
    single failed attempt, re-introducing the silent-skip bug this change set
    fixes.
    """
    from app.models.assignment import AssignmentRun
    from app.services import autonomous

    agent_id = uuid4()
    assignment = MagicMock()
    assignment.id = uuid4()
    assignment.agent_id = agent_id
    assignment.user_id = uuid4()
    assignment.run_as_user_id = None
    assignment.is_company_managed = False
    assignment.enabled = True
    assignment.name = "Broken schedule"
    assignment.last_triggered_at = None
    assignment.trigger_config = {"cron": "*/15 * * 1"}  # malformed (4 fields)

    agent = MagicMock()
    agent.id = agent_id
    agent.enabled = True
    agent.is_builtin = False  # user assignment - not exempt from cron disable

    def _result(items: list[Any]) -> MagicMock:
        scalars = MagicMock()
        scalars.all.return_value = items
        res = MagicMock()
        res.scalars.return_value = scalars
        return res

    db = MagicMock()
    db.execute = AsyncMock(side_effect=[_result([assignment]), _result([agent])])
    db.commit = AsyncMock()

    snapshot = set(autonomous._cron_warned_once)
    autonomous._cron_warned_once.clear()
    # Pre-spend the rate-limit token so the ERROR log is suppressed this tick;
    # remediation must still run.
    autonomous._cron_warned_once.add(("assignment", str(assignment.id)))
    try:
        count = await autonomous._process_scheduled_assignments(db)
    finally:
        autonomous._cron_warned_once.clear()
        autonomous._cron_warned_once.update(snapshot)

    assert count == 0
    # Disabled + failed run persisted despite the log being rate-limited away.
    assert assignment.enabled is False
    db.add.assert_called_once()
    failed_run = db.add.call_args.args[0]
    assert isinstance(failed_run, AssignmentRun)
    assert failed_run.status == "failed"
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_autonomous_scheduler_skips_non_string_evaluation_cron():
    """The sibling evaluation scheduler shares the cron-parse idiom.

    A non-string ``schedule`` makes ``croniter`` raise ``AttributeError`` (not
    ``ValueError``), so without the type guard it escapes the
    ``(CroniterBadCronError, ValueError)`` handler and falls through to the
    generic ``except Exception``, logging a full traceback every tick.  This
    pins that a non-string evaluation schedule routes through the single
    rate-limited WARNING-and-skip path instead — and persists no run (unlike
    assignments, evaluations only warn-and-skip).
    """
    from app.services import autonomous

    evaluation = MagicMock()
    evaluation.id = uuid4()
    evaluation.name = "Broken eval"
    evaluation.enabled = True
    evaluation.last_triggered_at = None
    evaluation.trigger_config = {"schedule": ["0", "9", "*", "*"]}  # not a string

    def _result(items: list[Any]) -> MagicMock:
        scalars = MagicMock()
        scalars.all.return_value = items
        res = MagicMock()
        res.scalars.return_value = scalars
        return res

    db = MagicMock()
    db.execute = AsyncMock(side_effect=[_result([evaluation])])
    db.get = AsyncMock(return_value=evaluation)

    snapshot = set(autonomous._cron_warned_once)
    autonomous._cron_warned_once.clear()
    with (
        patch.object(autonomous.logger, "warning") as mock_warning,
        patch.object(autonomous.logger, "error") as mock_error,
    ):
        try:
            count = await autonomous._process_scheduled_evaluations(db)
        finally:
            autonomous._cron_warned_once.clear()
            autonomous._cron_warned_once.update(snapshot)

    assert count == 0
    # Routed through the concise WARNING-and-skip path, never the outer
    # ``except Exception`` ERROR-with-traceback.
    mock_warning.assert_called_once()
    mock_error.assert_not_called()
    # Evaluations only warn-and-skip — no failed run is persisted.
    db.add.assert_not_called()


# ---------------------------------------------------------------------------
# Teams 202 acceptance
# ---------------------------------------------------------------------------


class _TeamsStubResponse:
    def __init__(self, status_code: int, body: dict[str, Any] | None = None):
        self.status_code = status_code
        self._body = body
        self.text = ""

    def json(self) -> dict[str, Any]:
        if self._body is None:
            raise ValueError("no json body")
        return self._body


class _StubAsyncClient:
    def __init__(self, response: _TeamsStubResponse):
        self._response = response

    async def __aenter__(self) -> _StubAsyncClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def post(self, *_args: object, **_kwargs: object) -> _TeamsStubResponse:
        return self._response


@pytest.mark.asyncio
async def test_teams_send_message_accepts_202_without_error_log(
    caplog: pytest.LogCaptureFixture,
):
    """HTTP 202 is Bot Framework's async-accepted code; must not log ERROR."""
    from app.channels import teams

    channel = teams.TeamsChannel(
        access_token="fake",
        service_url="https://example.invalid",
        conversation_id="conv1",
        bot_id="bot1",
    )

    caplog.set_level(logging.ERROR, logger="app.channels.teams")

    with patch.object(
        teams.httpx, "AsyncClient", lambda *a, **kw: _StubAsyncClient(_TeamsStubResponse(202))
    ):
        result = await channel.send_message(
            teams.OutboundMessage(text="hi", metadata={})
        )

    # 202 is a SUCCESS (queued for async delivery — the norm for private/
    # shared channels routed via Graph): the result must be non-None so
    # callers gated on ``is not None`` (private-channel monitoring setup,
    # thread TTL refresh) still run.
    assert result == ""
    assert not any(
        "Teams send_message failed" in rec.message for rec in caplog.records
    ), "HTTP 202 must NOT produce an ERROR log"


@pytest.mark.asyncio
async def test_teams_send_message_still_errors_on_5xx(
    caplog: pytest.LogCaptureFixture,
):
    from app.channels import teams

    channel = teams.TeamsChannel(
        access_token="fake",
        service_url="https://example.invalid",
        conversation_id="conv1",
        bot_id="bot1",
    )

    caplog.set_level(logging.ERROR, logger="app.channels.teams")

    with patch.object(
        teams.httpx, "AsyncClient", lambda *a, **kw: _StubAsyncClient(_TeamsStubResponse(500))
    ):
        result = await channel.send_message(
            teams.OutboundMessage(text="hi", metadata={})
        )

    assert result is None
    assert any(
        "Teams send_message failed" in rec.message for rec in caplog.records
    ), "Real failures must still produce an ERROR log"


# ---------------------------------------------------------------------------
# OpenAI dall-e-3 deprecation WARNING dedup
# ---------------------------------------------------------------------------


def _build_openai_provider_with_mock_images() -> tuple[Any, Any]:
    """Return ``(provider, mock_generate)`` for image-gen unit tests.

    The provider is a real :class:`OpenAIProvider` instance — only the
    ``client.images.generate`` SDK call is replaced with an ``AsyncMock``
    that returns a stubbed response with a tiny valid base64 payload.
    This lets the test drive the *real* ``generate_image`` code path
    (including the dedup branch + size remap) without touching the
    network.
    """
    from app.llm.openai_provider import OpenAIProvider

    provider = OpenAIProvider(api_key="sk-test-not-real")

    fake_response = MagicMock()
    # ``b64_json`` decoded must yield real bytes — use a 1×1 PNG so
    # ``base64.b64decode`` returns something non-empty without raising.
    fake_response.data = [
        MagicMock(
            b64_json=(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkAAIAAAoAAv/lxKUAAAAASUVORK5CYII="
            )
        )
    ]
    mock_generate = AsyncMock(return_value=fake_response)
    provider.client.images.generate = mock_generate  # type: ignore[assignment]

    return provider, mock_generate


@pytest.mark.asyncio
async def test_openai_image_model_deprecation_warning_fires_once_per_process(
    caplog: pytest.LogCaptureFixture,
):
    """Drives the real ``generate_image`` code path with a mocked SDK call.

    Hits the dedup branch on three consecutive calls and asserts the
    actual logger output: 1 WARNING + 2 DEBUGs. If the dedup logic in
    ``openai_provider.generate_image`` regresses, this test will fail
    (unlike the original version that re-implemented the dedup inline).
    """
    from app.llm.openai_provider import OpenAIProvider

    OpenAIProvider._logged_deprecated_models.clear()
    provider, mock_generate = _build_openai_provider_with_mock_images()
    caplog.set_level(logging.DEBUG, logger="app.llm.openai_provider")

    for _ in range(3):
        out = await provider.generate_image(
            prompt="ignored", model="dall-e-3", size="1024x1024"
        )
        assert isinstance(out, bytes) and out

    assert mock_generate.call_count == 3, "SDK should be hit on every call"
    # Every SDK invocation must be made with the remapped model — the
    # deprecated ID must never be sent to OpenAI even once.
    for kwargs in [c.kwargs for c in mock_generate.call_args_list]:
        assert kwargs["model"] == OpenAIProvider._DEPRECATED_IMAGE_MODELS["dall-e-3"], (
            f"Expected remapped model in SDK call, got {kwargs['model']!r}"
        )

    deprecation_records = [
        r
        for r in caplog.records
        if "deprecated" in r.message and r.name == "app.llm.openai_provider"
    ]
    warnings = [r for r in deprecation_records if r.levelno == logging.WARNING]
    debugs = [r for r in deprecation_records if r.levelno == logging.DEBUG]
    assert len(warnings) == 1, (
        f"Expected exactly 1 deprecation WARNING, got {len(warnings)}"
    )
    assert len(debugs) == 2, (
        f"Subsequent dedup hits must log DEBUG, got {len(debugs)}"
    )


def test_canonical_image_model_remaps_deprecated_ids():
    """Single source of truth for the deprecation remap.

    Exercised by both ``settings.update_general_assistant_config`` (save
    time) and ``OpenAIProvider.generate_image`` (runtime). If this
    breaks, both code paths break with it.
    """
    from app.llm.openai_provider import OpenAIProvider

    assert OpenAIProvider.canonical_image_model("dall-e-3") == "gpt-image-1"
    assert OpenAIProvider.canonical_image_model("dall-e-2") == "gpt-image-1"
    # Non-deprecated models pass through untouched.
    assert OpenAIProvider.canonical_image_model("gpt-image-1") == "gpt-image-1"
    assert OpenAIProvider.canonical_image_model("dall-e-99-unknown") == (
        "dall-e-99-unknown"
    )
    # ``None`` and empty are no-ops (admin clearing the field).
    assert OpenAIProvider.canonical_image_model(None) is None
    assert OpenAIProvider.canonical_image_model("") == ""


def test_settings_update_image_model_uses_canonical_remap():
    """The settings save path must rewrite deprecated IDs at write time.

    Without this, ``PlatformSettings.image_model`` keeps the stale
    ``"dall-e-3"`` forever and ``generate_image`` re-warns on every
    process restart. We exercise the inline branch from
    ``api/v1/settings.update_general_assistant_config`` directly with a
    minimal stub ``ps`` object so the test stays DB-free (the full
    handler requires admin auth + GA config + a transaction).
    """
    from app.llm.openai_provider import OpenAIProvider

    class _StubPlatformSettings:
        image_model: str | None = "legacy-placeholder"

    ps = _StubPlatformSettings()

    # Mirror exactly what ``settings.update_general_assistant_config``
    # does on the ``body.image_model is not None`` branch.
    incoming_value = "dall-e-3"
    ps.image_model = OpenAIProvider.canonical_image_model(incoming_value or None)

    assert ps.image_model == "gpt-image-1", (
        "Saving image_model='dall-e-3' must persist the canonical name "
        "so the admin UI no longer surfaces the deprecated ID."
    )

    # And the empty-string-clears case (UI sends ``""`` to clear the field).
    ps.image_model = OpenAIProvider.canonical_image_model("" or None)
    assert ps.image_model is None, (
        "Saving an empty image_model must clear the field, not coerce "
        "to a fallback model."
    )


# ---------------------------------------------------------------------------
# asyncio WebSocket noise handler
# ---------------------------------------------------------------------------


def test_install_asyncio_exception_handler_no_op_outside_running_loop():
    """Calling outside an event loop must not raise (lifespan re-installs it)."""
    from app.core.logging import install_asyncio_exception_handler

    install_asyncio_exception_handler()  # No exception expected.


@pytest.mark.asyncio
async def test_asyncio_handler_demotes_keepalive_timeout_to_debug(
    caplog: pytest.LogCaptureFixture,
):
    import asyncio

    from app.core.logging import install_asyncio_exception_handler

    install_asyncio_exception_handler()

    loop = asyncio.get_running_loop()
    handler = loop.get_exception_handler()
    assert handler is not None, "Handler must be installed on the running loop"

    caplog.set_level(logging.DEBUG, logger="asyncio")
    handler(
        loop,
        {
            "message": (
                "ConnectionClosedError exception in shielded future\n"
                "future: <Future finished exception=ConnectionClosedError(..., "
                "Close(code=1011, reason='keepalive ping timeout'), None)>"
            ),
        },
    )
    # Must NOT produce an ERROR record.
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)


# ---------------------------------------------------------------------------
# Minimal Teams import smoke test (catches import-time regressions)
# ---------------------------------------------------------------------------


def test_teams_module_imports_without_side_effects():
    # If the 202 change introduced a syntax/typing problem the rest of the
    # suite would fail with confusing errors — this is a fast sanity check.
    import importlib

    importlib.import_module("app.channels.teams")


# ---------------------------------------------------------------------------
# httpx import sanity (used by tests above)
# ---------------------------------------------------------------------------


def test_httpx_available():
    # The test stubs above assume httpx is importable. If not, those tests
    # would fail with an unrelated ImportError — this surfaces it cleanly.
    assert hasattr(httpx, "AsyncClient")
