"""What a knowledge base sync error says, and to whom.

A caller who can manage the knowledge base is served the full error text,
exactly as before; every other reader is served a fixed sentence, chosen by the
platform's own wording of the failure and never by the error text. The server
log keeps the full error. The routes are covered in
``test_kb_sync_error_routes.py``, the SQL search on a real PostgreSQL in
``test_kb_sync_error_readers_postgres.py``.
"""

from __future__ import annotations

import ast
import json
import logging
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.api.v1 import knowledge_bases as kb_mod
from app.models.activity import Activity
from app.services import kb_sync_scheduler
from app.services.kb_document_errors import (
    EMBEDDING_FAILED_EVENT,
    INTERNAL_ERROR_MESSAGE,
    embedding_failed_details,
    public_activity_details,
)
from app.services.kb_sync import KBSyncConfigError
from app.services.kb_sync_errors import (
    ACTIVITY_READER_RULES,
    CONFIG_ERROR_PREFIX,
    LAST_SYNC_ERROR,
    LAST_SYNC_ERROR_RULE,
    PERM_FAIL_ERROR_PREFIX,
    QUARANTINE_ERROR_PREFIX,
    READER_SENTENCES,
    READER_VISIBLE_SYNC_EVENTS,
    SyncErrorCode,
    served_sync_text,
)
from tests import app_sources

# What a failed sync stores: the HTTP client's message, with the request URL.
_HIDDEN = ("payroll-repo", "git.internal.example", "hidden-marker")
_RAW = (
    "Client error '401 Unauthorized' for url 'https://git.internal.example/acme/payroll-repo/contents' (hidden-marker)"
)
# The same, for a resource that is gone: a permanent error.
_RAW_404 = (
    "Client error '404 Not Found' for url 'https://git.internal.example/acme/payroll-repo/contents' (hidden-marker)"
)


def _hides_everything(text: str | None) -> bool:
    return not any(marker in (text or "") for marker in _HIDDEN)


def _as_reader(where: str, stored: str | None) -> str | None:
    return served_sync_text(where, stored, can_manage=False)


def _as_manager(where: str, stored: str | None) -> str | None:
    return served_sync_text(where, stored, can_manage=True)


# -- the rule --------------------------------------------------------------------

_STORED = (
    (LAST_SYNC_ERROR, _RAW),
    (LAST_SYNC_ERROR, f"{QUARANTINE_ERROR_PREFIX} 10 consecutive failures. Last error: {_RAW}"),
    ("sync_failed", f"Sync failed: {_RAW}"),
    ("sync_alert", f"Sync has failed 5 consecutive times (github). Last error: {_RAW}"),
    ("sync_quarantined", f"Source disabled after 10 consecutive failures. Last error: {_RAW}"),
    ("sync_config_error", f"Sync source disabled due to a configuration error: {_RAW}"),
    ("sync_warning", _RAW),
    ("sync_started", "Sync started for github"),
    ("sync_completed", "Synced 3 document(s) from github"),
    ("force_resync", "Force re-sync started for github (purged 3 documents)"),
    ("document_added", "Uploaded file 'handbook.md'"),
    (EMBEDDING_FAILED_EVENT, f"Failed to embed 'report.pdf': {_RAW}"),
    (EMBEDDING_FAILED_EVENT, embedding_failed_details("notes.md", INTERNAL_ERROR_MESSAGE)),
)


@pytest.mark.parametrize(("where", "stored"), _STORED)
def test_a_manager_is_served_what_they_were_always_served(where, stored):
    before = stored if where == LAST_SYNC_ERROR else public_activity_details(where, stored)
    assert _as_manager(where, stored) == before


@pytest.mark.parametrize(("where", "stored"), [(w, s) for w, s in _STORED if _RAW in s])
def test_a_reader_is_never_served_the_error_text(where, stored):
    served = _as_reader(where, stored)
    assert served
    assert _hides_everything(served)
    assert served in READER_SENTENCES.values() or where == EMBEDDING_FAILED_EVENT


@pytest.mark.parametrize("where", [LAST_SYNC_ERROR, *ACTIVITY_READER_RULES, "sync_warning"])
def test_what_a_reader_is_served_never_depends_on_the_error_text(where):
    # Only the platform's own opening picks the sentence: two errors with the
    # same opening are served the same sentence, whatever follows it.
    assert _as_reader(where, _RAW) == _as_reader(where, "something else entirely")
    assert _as_reader(where, f"{QUARANTINE_ERROR_PREFIX} {_RAW}") == _as_reader(
        where, f"{QUARANTINE_ERROR_PREFIX} something else"
    )


@pytest.mark.parametrize(("where", "stored"), [(w, s) for w, s in _STORED if _RAW not in s])
def test_what_never_carries_an_error_is_served_to_readers_as_before(where, stored):
    assert where == LAST_SYNC_ERROR or where not in ACTIVITY_READER_RULES
    assert _as_reader(where, stored) == _as_manager(where, stored)


def test_only_the_platform_worded_sync_events_are_visible_to_readers():
    assert sorted(READER_VISIBLE_SYNC_EVENTS) == ["sync_completed", "sync_started"]
    assert not READER_VISIBLE_SYNC_EVENTS & set(ACTIVITY_READER_RULES)


@pytest.mark.parametrize("where", [LAST_SYNC_ERROR, *ACTIVITY_READER_RULES, "sync_warning"])
@pytest.mark.parametrize("stored", [None, ""])
def test_nothing_is_served_where_nothing_is_stored(where, stored):
    assert _as_reader(where, stored) == stored
    assert _as_manager(where, stored) == stored


def test_a_new_sync_event_is_hidden_from_readers_until_it_is_given_a_rule():
    assert _as_reader("sync_retried", _RAW) == READER_SENTENCES[SyncErrorCode.SYNC_EVENT]
    assert _as_manager("sync_retried", _RAW) == _RAW


def test_every_reader_sentence_is_fixed_and_says_who_can_see_the_details():
    assert set(READER_SENTENCES) == set(SyncErrorCode)
    for sentence in READER_SENTENCES.values():
        assert sentence.isascii()
        assert "{" not in sentence
        assert "manage this knowledge base" in sentence
    assert len(set(READER_SENTENCES.values())) == len(READER_SENTENCES)


# -- every message the scheduler writes -------------------------------------------


class _Nested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Recorder:
    """What the scheduler wrote: activity entries, notifications, broadcasts."""

    def __init__(self, monkeypatch) -> None:
        self.entries: list[tuple[str, str | None]] = []
        self.published: list[tuple[str, str]] = []
        owner_id = uuid.uuid4()
        self.db = SimpleNamespace(
            flush=AsyncMock(),
            commit=AsyncMock(),
            rollback=AsyncMock(),
            begin_nested=_Nested,
            execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: owner_id)),
            add=Mock(),
        )
        recorder = self

        async def _log(_db, _kb_id, event, details=None, **_kwargs):
            recorder.entries.append((event, details))

        class _Redis:
            async def publish(self, channel, payload):
                recorder.published.append((channel, payload))

        monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", _log)
        monkeypatch.setattr("app.core.redis.redis_client", AsyncMock(return_value=_Redis()))

    @property
    def notifications(self) -> list[Activity]:
        return [call.args[0] for call in self.db.add.call_args_list if isinstance(call.args[0], Activity)]

    def assert_no_notification_carries_the_error(self) -> None:
        # Notifications reach people who cannot manage the knowledge base (the
        # company-wide broadcast): they only ever carry the platform's summary.
        assert self.published
        for _channel, payload in self.published:
            assert _hides_everything(payload), payload
        for activity in self.notifications:
            assert _hides_everything(json.dumps([activity.title, activity.summary, activity.extra_data]))


def _sync_source(**overrides) -> SimpleNamespace:
    fields = dict(
        id=uuid.uuid4(),
        knowledge_base_id=uuid.uuid4(),
        source_type="github",
        config={},
        integration_id=None,
        user_id=None,
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


def _assert_served(where: str, stored: str, code: SyncErrorCode) -> None:
    assert _as_manager(where, stored) == stored
    assert _as_reader(where, stored) == READER_SENTENCES[code]


@pytest.mark.asyncio
async def test_a_failed_sync_is_served_per_caller_and_logged_in_full(monkeypatch, caplog):
    recorder = _Recorder(monkeypatch)
    source = _sync_source()
    kb = SimpleNamespace(id=uuid.uuid4(), name="Docs")

    async def _fail(**_kwargs):
        raise RuntimeError(_RAW)

    monkeypatch.setattr(kb_sync_scheduler, "_run_sync_in_isolated_session", _fail)

    with (
        caplog.at_level(logging.WARNING, logger=kb_sync_scheduler.logger.name),
        pytest.raises(RuntimeError),
    ):
        await kb_sync_scheduler.execute_sync_source(recorder.db, source, kb, "system")

    # Stored in full, for the people who fix the source.
    assert source.last_sync_error == _RAW
    _assert_served(LAST_SYNC_ERROR, source.last_sync_error, SyncErrorCode.SYNC_FAILED)
    [(event, details)] = [entry for entry in recorder.entries if entry[0] == "sync_failed"]
    assert details == f"Sync failed: {_RAW}"
    _assert_served(event, details, SyncErrorCode.SYNC_FAILED)
    # And in the server log, with the knowledge base and source ids.
    assert any(
        _RAW in record.getMessage() and str(source.id) in record.getMessage() and str(kb.id) in record.getMessage()
        for record in caplog.records
    ), [record.getMessage() for record in caplog.records]


@pytest.mark.asyncio
async def test_a_configuration_error_is_served_per_caller(monkeypatch, caplog):
    recorder = _Recorder(monkeypatch)
    source = _sync_source(source_type="microsoft-drive")
    kb = SimpleNamespace(id=uuid.uuid4(), name="Docs")

    async def _misconfigured(**_kwargs):
        raise KBSyncConfigError(_RAW)

    monkeypatch.setattr(kb_sync_scheduler, "_run_sync_in_isolated_session", _misconfigured)

    with caplog.at_level(logging.ERROR, logger=kb_sync_scheduler.logger.name):
        await kb_sync_scheduler.execute_sync_source(recorder.db, source, kb, "system")

    # The only server-log record of a configuration error keeps it in full,
    # with the knowledge base and source ids.
    assert any(
        _RAW in record.getMessage() and str(source.id) in record.getMessage() and str(kb.id) in record.getMessage()
        for record in caplog.records
    ), [record.getMessage() for record in caplog.records]
    assert source.last_sync_error == f"{CONFIG_ERROR_PREFIX} {_RAW}"
    _assert_served(LAST_SYNC_ERROR, source.last_sync_error, SyncErrorCode.CONFIGURATION_ERROR)
    [(event, details)] = [entry for entry in recorder.entries if entry[0] == "sync_config_error"]
    _assert_served(event, details, SyncErrorCode.CONFIGURATION_ERROR)
    recorder.assert_no_notification_carries_the_error()


@pytest.mark.asyncio
async def test_the_failure_alerts_and_quarantines_are_served_per_caller(monkeypatch):
    # Every threshold crossed by the same failure: the persistent-failure
    # alert, the error-count quarantine and the permanent-error
    # quarantine, whose messages all quote the last error.
    for name in (
        "kb_sync_retry_alert_threshold",
        "kb_sync_failure_quarantine_threshold",
        "kb_sync_perm_fail_quarantine_threshold",
    ):
        monkeypatch.setattr(kb_sync_scheduler.settings, name, 1)
    recorder = _Recorder(monkeypatch)
    source = _sync_source()

    await kb_sync_scheduler._handle_sync_failure(
        recorder.db,
        source,
        kb_id=uuid.uuid4(),
        prior_failure_count=0,
        prior_perm_fail_count=0,
        error_message=_RAW_404,
        activity_detail=f"Sync failed: {_RAW_404}",
        source_id=str(source.id),
        source_type="github",
    )

    assert source.enabled is False
    assert source.last_sync_error.startswith(PERM_FAIL_ERROR_PREFIX)
    assert _RAW_404[:200] in source.last_sync_error
    _assert_served(LAST_SYNC_ERROR, source.last_sync_error, SyncErrorCode.NEEDS_RECONFIGURATION)
    expected = {
        "sync_failed": SyncErrorCode.SYNC_FAILED,
        "sync_alert": SyncErrorCode.REPEATED_FAILURES,
    }
    quarantines = [details for event, details in recorder.entries if event == "sync_quarantined"]
    assert len(quarantines) == 2
    for event, details in recorder.entries:
        if event == "sync_quarantined":
            code = (
                SyncErrorCode.NEEDS_RECONFIGURATION
                if details.startswith(PERM_FAIL_ERROR_PREFIX)
                else SyncErrorCode.QUARANTINED
            )
        else:
            code = expected[event]
        assert "hidden-marker" in details
        _assert_served(event, details, code)
    recorder.assert_no_notification_carries_the_error()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("quarantine_threshold", "code"),
    [(0, SyncErrorCode.STALLED), (1, SyncErrorCode.QUARANTINED)],
)
async def test_a_stalled_sync_is_served_per_caller(monkeypatch, quarantine_threshold, code):
    monkeypatch.setattr(kb_sync_scheduler.settings, "kb_sync_stale_quarantine_threshold", quarantine_threshold)
    monkeypatch.setattr(kb_sync_scheduler.settings, "kb_sync_retry_alert_threshold", 1)
    recorder = _Recorder(monkeypatch)
    source = _sync_source(last_sync_status="syncing")
    recorder.db.execute = AsyncMock(
        side_effect=[
            SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [source])),
            *[SimpleNamespace(scalar_one_or_none=lambda: None)] * 4,
        ]
    )

    assert await kb_sync_scheduler.reset_stale_syncs(recorder.db) == 1

    _assert_served(LAST_SYNC_ERROR, source.last_sync_error, code)
    for event, details in recorder.entries:
        _assert_served(
            event,
            details,
            SyncErrorCode.REPEATED_FAILURES if event == "sync_alert" else SyncErrorCode.QUARANTINED,
        )


def test_a_timed_out_sync_is_served_per_caller():
    # The scheduler's wording, pinned by test_kb_sync_scheduler.py.
    _assert_served(LAST_SYNC_ERROR, "Sync timed out after 1200s", SyncErrorCode.TIMEOUT)
    _assert_served("sync_failed", "Sync timed out after 1200s", SyncErrorCode.TIMEOUT)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("emit", "kwargs", "code"),
    [
        (
            kb_sync_scheduler._emit_stale_page_quarantine_alert,
            {"page_ids": ["1a2b3c", "4d5e6f"]},
            SyncErrorCode.STALE_PAGES,
        ),
        (
            kb_sync_scheduler._emit_partial_fail_alert,
            {"partial_fail_count": 3},
            SyncErrorCode.PARTIAL_FAILURES,
        ),
    ],
)
async def test_the_page_alerts_are_served_per_caller(monkeypatch, emit, kwargs, code):
    recorder = _Recorder(monkeypatch)

    await emit(recorder.db, kb_id=uuid.uuid4(), source_type="notion", **kwargs)

    [(event, details)] = recorder.entries
    assert event == "sync_alert"
    _assert_served(event, details, code)


def test_the_scheduler_and_the_rules_share_one_spelling_of_each_prefix():
    # The recovery paths (force re-sync, re-enabling) still import them from
    # the scheduler, which builds its messages from the rules' own constants.
    assert kb_sync_scheduler.QUARANTINE_ERROR_PREFIX is QUARANTINE_ERROR_PREFIX
    assert kb_sync_scheduler.CONFIG_ERROR_PREFIX is CONFIG_ERROR_PREFIX
    assert kb_sync_scheduler.PERM_FAIL_ERROR_PREFIX is PERM_FAIL_ERROR_PREFIX
    assert [prefix for prefix, _code in LAST_SYNC_ERROR_RULE.prefixes] == [
        "Sync timed out after ",
        "Sync stalled (no progress for >",
        QUARANTINE_ERROR_PREFIX,
        PERM_FAIL_ERROR_PREFIX,
        CONFIG_ERROR_PREFIX,
    ]


# -- no surface serves them another way --------------------------------------------

_APP = Path(kb_mod.__file__).resolve().parents[2]

#: Every module that touches a sync source or the activity feed, and why. A new
#: one - an agent tool, an MCP server, an admin view - serves them through
#: ``served_sync_text``: an agent acts as its user, so the same rule applies.
_SYNC_MODULES = {
    "agents/seed.py": "seeds the built-in knowledge bases' sources",
    "api/v1/knowledge_bases.py": "serves them, through served_sync_text",
    "services/kb_helpers.py": "log_kb_activity, which writes the activity feed",
    "services/kb_sync.py": "runs a sync, reading the source's config",
    "services/kb_sync_scheduler.py": "writes the sync errors",
    "services/resource_exposure.py": (
        "lists which knowledge bases bind an integration (ids and names), never a sync error"
    ),
    "services/trigger_integration_access.py": (
        "lists which sync sources' users cannot see their bound integration (ids and names), "
        "never a sync error"
    ),
}
_SERVED_FIELDS = {"SyncSourceResponse": "last_sync_error", "KBActivityLogResponse": "details"}


def _modules_naming(names: set[str]) -> dict[str, ast.Module]:
    found = {}
    for rel, tree in app_sources.modules(_APP):
        for node in app_sources.walk(tree):
            if (
                (isinstance(node, ast.Name) and node.id in names)
                or (isinstance(node, ast.alias) and node.name in names)
                or (isinstance(node, ast.Attribute) and node.attr in names)
            ):
                found[rel] = tree
                break
    return found


def test_no_other_module_reads_the_sync_sources_or_the_activity_feed():
    modules = _modules_naming({"KBSyncSource", "KBActivityLog", *_SERVED_FIELDS})
    assert set(modules) == set(_SYNC_MODULES), (
        "A module reads knowledge base sync sources or activity entries: serve "
        "their error text through app.services.kb_sync_errors.served_sync_text "
        "and list it in _SYNC_MODULES."
    )


def test_every_response_serves_the_error_through_the_helper():
    constructed = []
    for module, tree in _modules_naming(set(_SERVED_FIELDS)).items():
        for node in app_sources.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _SERVED_FIELDS):
                continue
            field = _SERVED_FIELDS[node.func.id]
            [value] = [kw.value for kw in node.keywords if kw.arg == field]
            assert (
                isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "served_sync_text"
            ), f"{module}:{node.lineno} serves {field} without served_sync_text"
            constructed.append(node.func.id)
    assert set(constructed) == set(_SERVED_FIELDS)
