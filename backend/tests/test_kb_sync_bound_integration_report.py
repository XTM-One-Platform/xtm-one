"""A knowledge base sync source uses the integration it is bound to, and says so.

Who may use a credential is decided where a person binds it (maintainer
decision of 2026-09-27): the sync-source routes refuse to bind an
integration the person cannot see, and every sync of a bound source - on its
schedule or through "Sync now", whoever clicks it - reads through that
integration as bound. When the user the source syncs as (its configuring user,
else the knowledge base's owner) cannot see it, a WARNING names the ids once an
hour and the security audit lists the source, the way trigger
integrations are reported. PostgreSQL runs the same rules in
``test_integration_access_restricted_sites_postgres``.
"""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.api.v1 import security_audit
from app.services import kb_sync_scheduler
from app.services import trigger_integration_access as access


@pytest.fixture(autouse=True)
def _fresh_limiter():
    access._last_unseen_log.clear()
    yield
    access._last_unseen_log.clear()


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == access.__name__ and r.levelno == logging.WARNING]


# -- the warning ---------------------------------------------------------------


def test_a_source_whose_user_cannot_see_its_integration_is_named_by_id_once_an_hour(monkeypatch, caplog):
    clock = [1000.0]
    monkeypatch.setattr(access.time, "monotonic", lambda: clock[0])
    source_id, integration_id, user_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    def log() -> bool:
        return access.log_unseen_sync_source_integration(
            source_id, integration_id=integration_id, acting_user_id=user_id
        )

    with caplog.at_level(logging.WARNING, logger=access.__name__):
        first, again = log(), log()
        clock[0] += access.UNSEEN_LOG_INTERVAL_SECONDS
        an_hour_later = log()

    assert (first, again, an_hour_later) == (True, False, True)
    lines = _warnings(caplog)
    assert len(lines) == 2
    assert all(str(value) in lines[0] for value in (source_id, integration_id, user_id))


def test_a_sync_source_and_a_trigger_sharing_an_id_are_logged_apart(caplog):
    target, integration_id, user_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    with caplog.at_level(logging.WARNING, logger=access.__name__):
        assert access.log_unseen_trigger_integration(
            "assignment", target, integration_id=integration_id, acting_user_id=user_id
        )
        assert access.log_unseen_sync_source_integration(
            target, integration_id=integration_id, acting_user_id=user_id
        )

    assert len(_warnings(caplog)) == 2


@pytest.mark.parametrize("spelling", [str, lambda uid: uid], ids=["text", "uuid"])
@pytest.mark.parametrize(
    ("source_user", "kb_user", "expected"),
    [("source", "kb", "source"), (None, "kb", "kb"), (None, None, None)],
)
def test_a_source_syncs_as_its_configuring_user_else_the_knowledge_base_owner(
    source_user, kb_user, expected, spelling
):
    ids = {"source": uuid.uuid4(), "kb": uuid.uuid4()}

    def given(key):
        return spelling(ids[key]) if key else None

    assert access.sync_source_acting_user_id(given(source_user), given(kb_user)) == ids.get(expected)


# -- every sync of a bound source ------------------------------------------------


class _Nested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _session() -> SimpleNamespace:
    return SimpleNamespace(
        flush=AsyncMock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
        refresh=AsyncMock(),
        begin_nested=_Nested,
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None)),
        add=Mock(),
    )


def _source(**overrides) -> SimpleNamespace:
    fields = dict(
        id=uuid.uuid4(),
        knowledge_base_id=uuid.uuid4(),
        source_type="github",
        config={},
        integration_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        enabled=True,
        last_sync_status=None,
        last_synced_at=None,
        last_sync_error=None,
        last_sync_documents=0,
        sync_failure_count=0,
        sync_perm_fail_count=0,
        sync_partial_fail_count=0,
        sync_stale_count=0,
        sync_next_retry_at=None,
        sync_heartbeat_at=None,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _knowledge_base(source, owner=None) -> SimpleNamespace:
    return SimpleNamespace(id=source.knowledge_base_id, name="Docs", user_id=owner)


@pytest.fixture
def sync(monkeypatch):
    """One sync with its reads recorded: every run of the isolated sync, and
    every (acting user, integration) the visibility check was asked about."""
    record = SimpleNamespace(runs=[], checks=[], hidden=True)

    async def _run(**kwargs):
        record.runs.append(kwargs)
        return ["doc-1"], 0

    async def _hides(_self, _db, acting_user_id, integration_id):
        record.checks.append((acting_user_id, integration_id))
        if isinstance(record.hidden, Exception):
            raise record.hidden
        return record.hidden

    async def _log(*_args, **_kwargs):
        return None

    monkeypatch.setattr(kb_sync_scheduler, "_run_sync_in_isolated_session", _run)
    monkeypatch.setattr(access.TriggerIntegrationVisibility, "hides", _hides)
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", _log)
    monkeypatch.setattr(
        "app.core.redis.redis_client", AsyncMock(return_value=SimpleNamespace(publish=AsyncMock()))
    )
    return record


@pytest.mark.parametrize("run_by", ["schedule", "sync_now_by_someone_else"])
async def test_every_sync_reads_through_the_bound_integration_and_names_a_user_who_cannot_see_it(
    sync, caplog, run_by
):
    source = _source()
    kb = _knowledge_base(source, owner=uuid.uuid4())
    running_user = str(source.user_id) if run_by == "schedule" else str(uuid.uuid4())

    with caplog.at_level(logging.WARNING, logger=access.__name__):
        await kb_sync_scheduler.execute_sync_source(_session(), source, kb, running_user)

    assert [run["bound_integration_id"] for run in sync.runs] == [str(source.integration_id)]
    assert source.last_sync_status == "success"
    # The source's own user, whoever runs the sync: both runs say the same.
    assert sync.checks == [(source.user_id, str(source.integration_id))]
    [line] = _warnings(caplog)
    assert all(str(value) in line for value in (source.id, source.integration_id, source.user_id))


async def test_a_source_with_no_user_is_checked_for_the_knowledge_base_owner(sync, caplog):
    source = _source(user_id=None)
    kb = _knowledge_base(source, owner=uuid.uuid4())

    with caplog.at_level(logging.WARNING, logger=access.__name__):
        await kb_sync_scheduler.execute_sync_source(_session(), source, kb, str(kb.user_id))

    assert sync.checks == [(kb.user_id, str(source.integration_id))]
    assert len(_warnings(caplog)) == 1


async def test_a_user_who_can_see_the_bound_integration_is_not_reported(sync, caplog):
    sync.hidden = False
    source = _source()

    with caplog.at_level(logging.WARNING, logger=access.__name__):
        await kb_sync_scheduler.execute_sync_source(_session(), source, _knowledge_base(source), "system")

    assert [run["bound_integration_id"] for run in sync.runs] == [str(source.integration_id)]
    assert _warnings(caplog) == []


@pytest.mark.parametrize(
    "source_fields",
    [{"integration_id": None}, {"user_id": None}],
    ids=["unbound", "nobody_to_check_for"],
)
async def test_there_is_nothing_to_check_without_a_binding_or_a_user(sync, source_fields):
    source = _source(**source_fields)

    await kb_sync_scheduler.execute_sync_source(_session(), source, _knowledge_base(source), "system")

    assert len(sync.runs) == 1
    assert sync.checks == []


async def test_a_check_that_cannot_run_costs_the_warning_never_the_sync(sync, caplog):
    sync.hidden = RuntimeError("database unavailable")
    source = _source()

    with caplog.at_level(logging.WARNING, logger=access.__name__):
        await kb_sync_scheduler.execute_sync_source(_session(), source, _knowledge_base(source), "system")

    assert [run["bound_integration_id"] for run in sync.runs] == [str(source.integration_id)]
    assert source.last_sync_status == "success"
    assert _warnings(caplog) == []


# -- the security audit ------------------------------------------------------------


def _visibility(monkeypatch, *, checked: int, unseen: list) -> None:
    monkeypatch.setattr(
        access,
        "sync_source_integration_visibility",
        AsyncMock(return_value=access.TriggerVisibilityReport(checked=checked, unseen=unseen)),
    )


async def test_the_audit_lists_each_source_whose_user_cannot_see_its_integration(monkeypatch):
    source_id = uuid.uuid4()
    _visibility(monkeypatch, checked=3, unseen=[(source_id, "Docs (github)")])
    findings: list[security_audit.AuditFinding] = []

    await security_audit._check_kb_sync_source_integrations(object(), findings)

    assert [(f.id, f.category, f.severity, f.control) for f in findings] == [
        ("access.kb_sync_source_integration_not_visible", "access", "warning", None)
    ]
    assert f"Docs (github) ({source_id})" in findings[0].description


@pytest.mark.parametrize(
    ("checked", "expected"),
    [(2, [("access.kb_sync_source_integrations_visible", "access", "pass")]), (0, [])],
    ids=["bound_sources", "none_bound"],
)
async def test_the_audit_passes_when_every_bound_source_syncs_as_a_user_who_can_see_it(
    monkeypatch, checked, expected
):
    _visibility(monkeypatch, checked=checked, unseen=[])
    findings: list[security_audit.AuditFinding] = []

    await security_audit._check_kb_sync_source_integrations(object(), findings)

    assert [(f.id, f.category, f.severity) for f in findings] == expected
