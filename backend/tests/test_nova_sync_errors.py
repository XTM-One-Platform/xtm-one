"""What a Nova rule sync error says, and to whom.

Whoever can manage the Nova integration is served the stored error exactly as
before; anyone else a fixed sentence. The server log keeps the full error with
the source and integration ids. The routes are covered in
``test_nova_sync_error_routes.py``, the real database in
``test_nova_sync_error_readers_postgres.py``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.v1 import integration_testing, nova_rules
from app.integrations import connectivity
from app.integrations.connectivity import ProbeContext, ProbeResult, register_connectivity
from app.services import nova_rule_sync
from app.services.nova_rule_sync import READER_SYNC_ERROR_MESSAGE, served_sync_error

_RAW = (
    "Client error '401 Unauthorized' for url 'https://git.internal.example/acme/payroll-repo/contents' (hidden-marker)"
)


@pytest.fixture(autouse=True)
def _isolate_registry():
    snapshot = dict(connectivity._PROBES)
    yield
    connectivity._PROBES.clear()
    connectivity._PROBES.update(snapshot)


def test_a_manager_is_served_the_stored_error():
    assert served_sync_error(_RAW, can_manage=True) == _RAW


def test_anyone_else_is_served_a_fixed_sentence():
    assert served_sync_error(_RAW, can_manage=False) == READER_SYNC_ERROR_MESSAGE
    assert served_sync_error("something else entirely", can_manage=False) == READER_SYNC_ERROR_MESSAGE


@pytest.mark.parametrize("stored", [None, ""])
def test_nothing_is_served_where_nothing_is_stored(stored):
    assert served_sync_error(stored, can_manage=False) == stored
    assert served_sync_error(stored, can_manage=True) == stored


def test_the_sentence_is_fixed_and_says_who_can_see_the_details():
    assert READER_SYNC_ERROR_MESSAGE.isascii()
    assert "{" not in READER_SYNC_ERROR_MESSAGE
    assert "manage this integration" in READER_SYNC_ERROR_MESSAGE


def test_a_probe_does_not_know_the_caller_manages_the_integration_unless_told():
    assert ProbeContext(provider="nova", name="Nova", token=None, extra={}).caller_can_manage is False


@pytest.mark.asyncio
@pytest.mark.parametrize("manages", [True, False])
async def test_the_connectivity_test_hands_the_caller_standing_to_the_probe(manages):
    seen: list[ProbeContext] = []

    async def probe(ctx: ProbeContext) -> ProbeResult:
        seen.append(ctx)
        return ProbeResult.ok("fine")

    register_connectivity("acme", probe)
    await integration_testing.test_provider_connectivity("acme", None, {}, "Acme", caller_can_manage=manages)

    [ctx] = seen
    assert ctx.caller_can_manage is manages


def _source(error: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        integration_id=uuid.uuid4(),
        repo="Nova-Hunting/nova-rules",
        branch="main",
        path="",
        sync_interval="daily",
        enabled=True,
        last_synced_at=datetime.now(UTC),
        last_sync_status="error",
        last_sync_error=error,
        last_sync_rules=0,
    )


def test_the_rule_source_routes_serve_the_error_per_caller():
    source = _source(_RAW)
    assert nova_rules._source_to_dict(source, can_manage=True)["last_sync_error"] == _RAW
    assert nova_rules._source_to_dict(source, can_manage=False)["last_sync_error"] == READER_SYNC_ERROR_MESSAGE


class _SyncSession:
    """Enough of a session for ``sync_nova_rule_source``: the owned rules, adds, deletes."""

    def __init__(self, rows) -> None:
        self.rows = list(rows)
        self.added: list = []
        self.deleted: list = []

    async def execute(self, _statement):
        rows = self.rows
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    def add(self, row) -> None:
        self.added.append(row)

    async def delete(self, row) -> None:
        self.deleted.append(row)


def _block(name: str, keyword: str) -> str:
    return f'rule {name} {{ meta: category = "Jailbreak"\n  keywords: $k = "{keyword}"\n  condition: keywords.$k }}'


def _owned(source, name: str, action: str = "block") -> SimpleNamespace:
    return SimpleNamespace(
        name=name, rule_text=_block(name, "old"), category="Jailbreak", action=action, enabled=True,
        tags=[nova_rule_sync._sync_tag(source.id)],
    )


def _github(monkeypatch, files: dict[str, str | None]) -> None:
    async def _list(_client, _repo, _branch, _path):
        return list(files)

    async def _fetch(_client, _repo, _branch, fpath):
        return files[fpath]

    monkeypatch.setattr(nova_rule_sync, "_list_nov_files", _list)
    monkeypatch.setattr(nova_rule_sync, "_fetch_file", _fetch)


def _sync_source() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), repo="Nova-Hunting/nova-rules", branch="main", path="", last_sync_error=None)


@pytest.mark.asyncio
async def test_an_unreadable_file_prunes_nothing_and_says_so(monkeypatch):
    source = _sync_source()
    guard = _owned(source, "Guard")
    db = _SyncSession([guard])
    _github(monkeypatch, {"a.nov": _block("Other", "x"), "b.nov": None})

    # Guard (kept, its file unread) and Other (new) are what the source holds now.
    assert await nova_rule_sync.sync_nova_rule_source(db, source) == 2

    assert db.deleted == []
    assert source.last_sync_error == "1 rule file(s) could not be read, so no rule was removed."


@pytest.mark.asyncio
async def test_a_refused_update_is_reported_on_the_source(monkeypatch):
    source = _sync_source()
    guard = _owned(source, "Guard")
    db = _SyncSession([guard, _owned(source, "Gone")])
    _github(monkeypatch, {"a.nov": 'rule Guard { keywords: $a = "z" }'})

    assert await nova_rule_sync.sync_nova_rule_source(db, source) == 1

    assert [r.name for r in db.deleted] == ["Gone"] and "old" in guard.rule_text
    assert source.last_sync_error.startswith("Kept the last accepted version of 1 rule(s)")
    assert source.last_sync_error.endswith(": Guard.")


@pytest.mark.asyncio
async def test_a_refused_block_replaced_later_in_the_repo_is_applied_not_kept(monkeypatch):
    source = _sync_source()
    guard = _owned(source, "Guard")
    db = _SyncSession([guard])
    _github(monkeypatch, {"a.nov": 'rule Guard { keywords: $a = "z" }', "b.nov": _block("Guard", "new")})

    assert await nova_rule_sync.sync_nova_rule_source(db, source) == 1

    assert "new" in guard.rule_text and source.last_sync_error is None


def test_a_long_report_is_cut_at_a_word_boundary():
    names = [f"Rule_{'x' * 60}_{i}" for i in range(10)]
    notice = nova_rule_sync._sync_notice(names, 2)
    assert len(notice) <= nova_rule_sync._SYNC_NOTICE_MAX_CHARS
    assert notice.endswith("...") and notice[:-3].split(" ")[-1] in {*names, *(f"{n}," for n in names)}


@pytest.mark.asyncio
async def test_a_complete_sync_clears_the_previous_report(monkeypatch):
    source = _sync_source()
    source.last_sync_error = "Kept the last accepted version of 1 rule(s) ..."
    db = _SyncSession([_owned(source, "Guard")])
    _github(monkeypatch, {"a.nov": _block("Guard", "fresh")})

    await nova_rule_sync.sync_nova_rule_source(db, source)

    assert source.last_sync_error is None


@pytest.mark.asyncio
async def test_a_scheduled_run_keeps_what_the_sync_reported(monkeypatch):
    source = SimpleNamespace(
        id=uuid.uuid4(), integration_id=uuid.uuid4(), repo="r", sync_failure_count=2,
        sync_next_retry_at=None, last_sync_status="error", last_sync_error="old failure",
    )

    async def _sync(_db, src):
        src.last_sync_error = "Kept the last accepted version of 1 rule(s)"
        return 3

    monkeypatch.setattr(nova_rule_sync, "sync_nova_rule_source", _sync)
    await nova_rule_sync._run_one(SimpleNamespace(commit=AsyncMock()), source, datetime.now(UTC))

    assert (source.last_sync_status, source.last_sync_rules) == ("ok", 3)
    assert source.last_sync_error == "Kept the last accepted version of 1 rule(s)"


@pytest.mark.asyncio
async def test_a_manual_sync_keeps_what_the_sync_reported(monkeypatch):
    from app.models.nova_rule_source import NovaRuleSource

    source = SimpleNamespace(last_sync_error="old failure", last_sync_status="error", last_synced_at=None)

    async def _sync(_db, src):
        src.last_sync_error = "Kept the last accepted version of 1 rule(s)"
        return 3

    async def _get(model, _id):
        assert model is NovaRuleSource
        return source

    monkeypatch.setattr(nova_rule_sync, "sync_nova_rule_source", _sync)
    db = SimpleNamespace(get=_get, commit=AsyncMock(), rollback=AsyncMock())
    user = SimpleNamespace(is_admin=True)

    result = await nova_rules.sync_nova_source_now(str(uuid.uuid4()), user=user, db=db, _agentic_gate=None)

    assert result == {"synced_rules": 3}
    assert (source.last_sync_status, source.last_sync_error) == ("ok", "Kept the last accepted version of 1 rule(s)")


@pytest.mark.asyncio
async def test_a_failed_sync_is_stored_as_before_and_logged_in_full(monkeypatch, caplog):
    source = SimpleNamespace(
        id=uuid.uuid4(),
        integration_id=uuid.uuid4(),
        repo="Nova-Hunting/nova-rules",
        sync_failure_count=0,
        sync_next_retry_at=None,
        last_sync_status=None,
        last_sync_error=None,
    )
    monkeypatch.setattr(nova_rule_sync, "sync_nova_rule_source", AsyncMock(side_effect=RuntimeError(_RAW)))
    db = SimpleNamespace(commit=AsyncMock())

    with caplog.at_level(logging.WARNING, logger=nova_rule_sync.logger.name):
        await nova_rule_sync._run_one(db, source, datetime.now(UTC))

    assert source.last_sync_status == "error"
    assert source.last_sync_error == _RAW
    assert any(
        _RAW in record.getMessage()
        and str(source.id) in record.getMessage()
        and str(source.integration_id) in record.getMessage()
        for record in caplog.records
    ), [record.getMessage() for record in caplog.records]
