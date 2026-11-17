"""Regression coverage for the ``sync_sources_customized`` contract on built-in KBs.

Two halves of the contract are pinned here:

1. **API** (`backend/app/api/v1/knowledge_bases.py`): an admin create / update /
   delete of a sync source on a *built-in* KB flips ``sync_sources_customized``
   to ``True`` (and leaves a non-built-in KB untouched).
2. **Seeder** (`backend/app/agents/seed.py`): startup seeding reconciles a
   built-in KB's sync sources only while ``sync_sources_customized`` is ``False``;
   once the flag is set, source reconciliation is skipped so operator-customized
   (e.g. airgapped mirror) sources survive restarts.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

import app.api.v1.knowledge_bases as kb_mod
from app.agents import seed
from app.api.v1.schemas.knowledge_base import (
    CreateSyncSourceRequest,
    UpdateSyncSourceRequest,
)
from app.models.knowledge_base import KnowledgeBase


class _ScalarsList:
    def __init__(self, items):
        self._items = items

    def all(self):
        return self._items


class _ExecScalars:
    """Mimics ``result.scalars().all()`` used by the seeder."""

    def __init__(self, items):
        self._items = items

    def scalars(self):
        return _ScalarsList(self._items)


class _ExecScalarOne:
    """Mimics ``result.scalar_one_or_none()`` used by the sync-source endpoints."""

    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


def _patch_kb_endpoint_deps(monkeypatch, kb):
    """Stub the shared helper lookups so the endpoint body runs in isolation."""
    monkeypatch.setattr(kb_mod, "get_user_knowledge_base", AsyncMock(return_value=kb))
    monkeypatch.setattr(kb_mod, "require_admin_for_company_managed", MagicMock())
    # The mutation routes now run the full write gate (``_assert_kb_manageable``
    # → ``get_user_group_ids`` + ``can_manage``); these tests use an admin and
    # cover the ``sync_sources_customized`` contract, not the permission
    # boundary, so isolate the gate to a no-op pass.
    monkeypatch.setattr(kb_mod, "get_user_group_ids", AsyncMock(return_value=[]))
    monkeypatch.setattr(kb_mod, "can_manage", MagicMock(return_value=True))
    monkeypatch.setattr(kb_mod, "_sync_source_to_response", MagicMock(return_value=None))
    monkeypatch.setattr(
        kb_mod,
        "assert_knowledge_base_not_package_origin",
        AsyncMock(return_value=None),
    )
    # The EE-tier sync-source licensing gate is orthogonal to the
    # ``sync_sources_customized`` contract under test and makes its own DB-backed
    # deployment-tier lookup; stub it to a no-op so these tests stay isolated
    # (the gate itself is covered directly below).
    monkeypatch.setattr(kb_mod, "_assert_sync_sources_licensed", AsyncMock())


# ───────────────────────────── API contract ──────────────────────────────


@pytest.mark.asyncio
async def test_create_sync_source_marks_builtin_customized(monkeypatch):
    kb = SimpleNamespace(id=uuid.uuid4(), is_builtin=True, sync_sources_customized=False)
    _patch_kb_endpoint_deps(monkeypatch, kb)

    user = SimpleNamespace(id=uuid.uuid4(), is_admin=True, role="admin")
    db = AsyncMock()
    db.add = MagicMock()

    body = CreateSyncSourceRequest(
        source_type="website",
        config={"url": "https://internal-mirror.example/docs"},
        sync_interval="manual",
        integration_id=None,
    )

    await kb_mod.create_sync_source(kb_id=str(kb.id), body=body, user=user, db=db)

    assert kb.sync_sources_customized is True
    db.add.assert_called_once()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_sync_source_marks_builtin_customized(monkeypatch):
    kb = SimpleNamespace(id=uuid.uuid4(), is_builtin=True, sync_sources_customized=False)
    _patch_kb_endpoint_deps(monkeypatch, kb)

    source = SimpleNamespace(integration_id=None)
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=True, role="admin")
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ExecScalarOne(source))

    body = UpdateSyncSourceRequest(enabled=False)

    await kb_mod.update_sync_source(
        kb_id=str(kb.id), source_id=str(uuid.uuid4()), body=body, user=user, db=db
    )

    assert kb.sync_sources_customized is True
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_sync_source_marks_builtin_customized(monkeypatch):
    kb = SimpleNamespace(id=uuid.uuid4(), is_builtin=True, sync_sources_customized=False)
    _patch_kb_endpoint_deps(monkeypatch, kb)

    source = SimpleNamespace(id=uuid.uuid4())
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=True, role="admin")
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ExecScalarOne(source))
    db.delete = AsyncMock()

    await kb_mod.delete_sync_source(
        kb_id=str(kb.id), source_id=str(source.id), user=user, db=db
    )

    assert kb.sync_sources_customized is True
    db.delete.assert_awaited_once_with(source)


@pytest.mark.asyncio
async def test_delete_sync_source_does_not_mark_non_builtin(monkeypatch):
    """A regular (non-built-in) KB must never gain the customized flag."""
    kb = SimpleNamespace(id=uuid.uuid4(), is_builtin=False, sync_sources_customized=False)
    _patch_kb_endpoint_deps(monkeypatch, kb)

    source = SimpleNamespace(id=uuid.uuid4())
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=True, role="admin")
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ExecScalarOne(source))
    db.delete = AsyncMock()

    await kb_mod.delete_sync_source(
        kb_id=str(kb.id), source_id=str(source.id), user=user, db=db
    )

    assert kb.sync_sources_customized is False


# ──────────────────── re-enable quarantine recovery ───────────────────────


@pytest.mark.asyncio
async def test_update_sync_source_reenable_resets_watchdog_budgets(monkeypatch):
    """Re-enabling a disabled source resets ALL watchdog counters (stale
    resets, consecutive failures, permanent failures),
    drops the scheduled backoff retry, and clears a watchdog-written
    quarantine banner, so a recovered source gets a full run of attempts,
    retries promptly, and the UI stops showing a stale "Source
    quarantined ..." message."""
    from datetime import UTC, datetime, timedelta

    from app.services.kb_sync_scheduler import QUARANTINE_ERROR_PREFIX

    kb = SimpleNamespace(id=uuid.uuid4(), is_builtin=False, sync_sources_customized=False)
    _patch_kb_endpoint_deps(monkeypatch, kb)

    source = SimpleNamespace(
        integration_id=None,
        enabled=False,
        source_type="github",
        sync_stale_count=5,
        sync_failure_count=10,
        sync_perm_fail_count=5,
        sync_next_retry_at=datetime.now(UTC) + timedelta(hours=20),
        last_sync_error=f"{QUARANTINE_ERROR_PREFIX} 10 consecutive failures ...",
    )
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=True, role="admin")
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ExecScalarOne(source))
    db.flush = AsyncMock()

    body = UpdateSyncSourceRequest(enabled=True)

    await kb_mod.update_sync_source(
        kb_id=str(kb.id), source_id=str(uuid.uuid4()), body=body, user=user, db=db
    )

    assert source.enabled is True
    assert source.sync_stale_count == 0
    assert source.sync_failure_count == 0
    assert source.sync_perm_fail_count == 0
    assert source.sync_next_retry_at is None
    assert source.last_sync_error is None, "stale quarantine banner must be cleared"


@pytest.mark.asyncio
async def test_update_sync_source_reenable_clears_perm_fail_banner(monkeypatch):
    """Re-enabling a source quarantined by the permanent-failure watchdog
    clears its "needs reconfiguration" banner too — both watchdog
    prefixes are recognised by the recovery path."""
    from app.services.kb_sync_scheduler import PERM_FAIL_ERROR_PREFIX

    kb = SimpleNamespace(id=uuid.uuid4(), is_builtin=False, sync_sources_customized=False)
    _patch_kb_endpoint_deps(monkeypatch, kb)

    source = SimpleNamespace(
        integration_id=None,
        enabled=False,
        source_type="github",
        sync_stale_count=0,
        sync_failure_count=5,
        sync_perm_fail_count=5,
        sync_next_retry_at=None,
        last_sync_error=f"{PERM_FAIL_ERROR_PREFIX} 5 consecutive 'not found' errors ...",
    )
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=True, role="admin")
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ExecScalarOne(source))
    db.flush = AsyncMock()

    await kb_mod.update_sync_source(
        kb_id=str(kb.id),
        source_id=str(uuid.uuid4()),
        body=UpdateSyncSourceRequest(enabled=True),
        user=user,
        db=db,
    )

    assert source.enabled is True
    assert source.sync_perm_fail_count == 0
    assert source.last_sync_error is None, "perm-fail quarantine banner must be cleared"


@pytest.mark.asyncio
async def test_update_sync_source_reenable_clears_config_error_banner(monkeypatch):
    """Re-enabling a source auto-disabled by a permanent configuration error
    clears its "Source disabled after configuration error" banner —
    all three scheduler-written auto-disable prefixes are recognised by the
    recovery path."""
    from app.services.kb_sync_scheduler import CONFIG_ERROR_PREFIX

    kb = SimpleNamespace(id=uuid.uuid4(), is_builtin=False, sync_sources_customized=False)
    _patch_kb_endpoint_deps(monkeypatch, kb)

    source = SimpleNamespace(
        integration_id=None,
        enabled=False,
        source_type="github",
        sync_stale_count=0,
        sync_failure_count=0,
        sync_perm_fail_count=0,
        sync_next_retry_at=None,
        last_sync_error=f"{CONFIG_ERROR_PREFIX} sharing link used as site_id",
    )
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=True, role="admin")
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ExecScalarOne(source))
    db.flush = AsyncMock()

    await kb_mod.update_sync_source(
        kb_id=str(kb.id),
        source_id=str(uuid.uuid4()),
        body=UpdateSyncSourceRequest(enabled=True),
        user=user,
        db=db,
    )

    assert source.enabled is True
    assert source.last_sync_error is None, "config-error banner must be cleared"


@pytest.mark.asyncio
async def test_update_sync_source_reenable_keeps_plain_error(monkeypatch):
    """Re-enabling a source an operator disabled manually keeps a genuine
    (non-quarantine) last error visible for diagnosis — only the
    watchdog-written quarantine banner is cleared."""
    kb = SimpleNamespace(id=uuid.uuid4(), is_builtin=False, sync_sources_customized=False)
    _patch_kb_endpoint_deps(monkeypatch, kb)

    source = SimpleNamespace(
        integration_id=None,
        enabled=False,
        source_type="github",
        sync_stale_count=0,
        sync_failure_count=2,
        sync_next_retry_at=None,
        last_sync_error="404 Not Found",
    )
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=True, role="admin")
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ExecScalarOne(source))
    db.flush = AsyncMock()

    await kb_mod.update_sync_source(
        kb_id=str(kb.id),
        source_id=str(uuid.uuid4()),
        body=UpdateSyncSourceRequest(enabled=True),
        user=user,
        db=db,
    )

    assert source.enabled is True
    assert source.last_sync_error == "404 Not Found"


@pytest.mark.asyncio
async def test_update_sync_source_disable_keeps_counters(monkeypatch):
    """Disabling a source must NOT touch the watchdog counters — only the
    disabled->enabled transition is a recovery action."""
    kb = SimpleNamespace(id=uuid.uuid4(), is_builtin=False, sync_sources_customized=False)
    _patch_kb_endpoint_deps(monkeypatch, kb)

    source = SimpleNamespace(
        integration_id=None,
        enabled=True,
        sync_stale_count=3,
        sync_failure_count=7,
        sync_perm_fail_count=2,
        sync_next_retry_at=None,
    )
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=True, role="admin")
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ExecScalarOne(source))
    db.flush = AsyncMock()

    await kb_mod.update_sync_source(
        kb_id=str(kb.id),
        source_id=str(uuid.uuid4()),
        body=UpdateSyncSourceRequest(enabled=False),
        user=user,
        db=db,
    )

    assert source.enabled is False
    assert source.sync_stale_count == 3
    assert source.sync_failure_count == 7
    assert source.sync_perm_fail_count == 2


# ─────────────────────── sync-source licensing gate ───────────────────────


@pytest.mark.asyncio
async def test_assert_sync_sources_licensed_blocks_when_not_licensed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate must 403 whenever AI is not licensed - Community Edition in
    XTM One mode AND an unlicensed Copilot deployment alike. It reads the ONE
    licence predicate, not the edition (which is ``""`` in Copilot mode and
    would wave an unlicensed Copilot through)."""
    import app.api.v1.platform as platform_mod

    monkeypatch.setattr(
        platform_mod, "is_agentic_ai_licensed", AsyncMock(return_value=False)
    )

    with pytest.raises(HTTPException) as exc_info:
        await kb_mod._assert_sync_sources_licensed(AsyncMock())

    assert exc_info.value.status_code == 403
    # The one refusal sentence every licence gate carries.
    from app.api.deps import ENTERPRISE_EDITION_REQUIRED_DETAIL

    assert exc_info.value.detail == ENTERPRISE_EDITION_REQUIRED_DETAIL


@pytest.mark.asyncio
@pytest.mark.parametrize("validated", [True, False])
async def test_assert_sync_sources_licensed_follows_the_licence_in_copilot_mode(
    monkeypatch: pytest.MonkeyPatch, validated: bool
) -> None:
    """Through the REAL predicate, not a double: in Copilot mode the answer is
    the license's validity and nothing else. The retired edition comparison
    read Copilot's ``""`` as "not Community Edition" and let an unlicensed
    Copilot deployment write sync sources."""
    import app.api.v1.platform as platform_mod

    monkeypatch.setattr(platform_mod.settings, "platform_mode", "copilot")

    async def _ps(_db: object) -> None:
        return None

    monkeypatch.setattr(platform_mod, "_load_ps", _ps)
    monkeypatch.setattr(
        platform_mod,
        "resolve_xtm_license",
        lambda _ps: SimpleNamespace(license_validated=validated),
    )

    if validated:
        await kb_mod._assert_sync_sources_licensed(AsyncMock())
    else:
        with pytest.raises(HTTPException) as exc_info:
            await kb_mod._assert_sync_sources_licensed(AsyncMock())
        assert exc_info.value.status_code == 403


def test_assert_sync_sources_licensed_reads_the_one_predicate() -> None:
    """Pinned on the source: the gate must not re-derive the licence from the
    edition or any other reading."""
    import inspect

    source = inspect.getsource(kb_mod._assert_sync_sources_licensed)
    assert "is_agentic_ai_licensed" in source
    assert "get_deployment_edition" not in source
    assert "EDITION_COMMUNITY" not in source


@pytest.mark.asyncio
async def test_assert_sync_sources_licensed_allows_licensed_deployments(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enterprise Edition / a valid Copilot license must pass the gate."""
    import app.api.v1.platform as platform_mod

    monkeypatch.setattr(
        platform_mod, "is_agentic_ai_licensed", AsyncMock(return_value=True)
    )
    # Must not raise.
    await kb_mod._assert_sync_sources_licensed(AsyncMock())


# ──────────────────────────── seeder contract ────────────────────────────


def _builtin_kb_seed_def():
    return {
        "slug": "test-kb",
        "name": "Test KB",
        "tags": ["builtin"],
        "sync_sources": [
            {
                "source_type": "website",
                "config": {"url": "https://default-source.example"},
                "sync_interval": "daily",
            }
        ],
    }


async def _run_seeder_with_flag(monkeypatch, *, customized: bool) -> AsyncMock:
    existing_kb = KnowledgeBase(
        id=uuid.uuid4(),
        name="Test KB",
        is_builtin=True,
        tags=["builtin", "slug:test-kb"],
    )
    existing_kb.sync_sources_customized = customized

    monkeypatch.setattr(seed, "BUILTIN_KNOWLEDGE_BASES", [_builtin_kb_seed_def()])
    monkeypatch.setattr(seed, "_sync_kb_fields", MagicMock())
    sync_mock = AsyncMock()
    monkeypatch.setattr(seed, "_sync_kb_sources", sync_mock)

    # The seeder first resolves the company-managed owner (a ``users`` query),
    # then loads built-in KBs; later queries (e.g. built-in agents in the
    # binding-sync phase) must return empty scalar sets.
    owner_user_id = uuid.uuid4()
    calls = {"n": 0}

    async def _execute(stmt=None, *_args, **_kwargs):
        if stmt is not None and "from users" in str(stmt).lower():
            return _ExecScalarOne(owner_user_id)
        calls["n"] += 1
        return _ExecScalars([existing_kb] if calls["n"] == 1 else [])

    db = AsyncMock()
    db.execute = _execute
    db.add = MagicMock()
    db.delete = AsyncMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()

    await seed.seed_builtin_knowledge_bases(db, platform_mode="xtm_one")
    return sync_mock


@pytest.mark.asyncio
async def test_seeder_reconciles_sources_when_not_customized(monkeypatch):
    sync_mock = await _run_seeder_with_flag(monkeypatch, customized=False)
    sync_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_seeder_skips_sources_when_customized(monkeypatch):
    sync_mock = await _run_seeder_with_flag(monkeypatch, customized=True)
    sync_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_seeder_sets_chunking_on_a_new_builtin_kb_before_seeding(monkeypatch):
    """A new built-in KB gets its chunk settings before its documents are seeded.

    The documents are processed with the KB's chunk size; a row created with the
    model default and corrected only on a later boot keeps the wrong chunks,
    since unchanged documents are never re-processed.
    """
    kb_def = {
        "slug": "chunked-kb",
        "name": "Chunked KB",
        "tags": ["builtin"],
        "chunk_size": 1024,
        "chunk_overlap": 128,
        "knowledge_base_seed": "chunked-kb",
    }
    monkeypatch.setattr(seed, "BUILTIN_KNOWLEDGE_BASES", [kb_def])
    seen: list[tuple[int, int, str]] = []

    async def _seed_docs(_db, kb, folder):
        seen.append((kb.chunk_size, kb.chunk_overlap, folder))
        return 0

    monkeypatch.setattr(seed, "_seed_documents_from_folder", _seed_docs)
    owner_user_id = uuid.uuid4()

    async def _execute(stmt=None, *_args, **_kwargs):
        if stmt is not None and "from users" in str(stmt).lower():
            return _ExecScalarOne(owner_user_id)
        return _ExecScalars([])

    db = AsyncMock()
    db.execute = _execute
    db.add = MagicMock()
    db.delete = AsyncMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()

    await seed.seed_builtin_knowledge_bases(db, platform_mode="xtm_one")

    assert seen == [(1024, 128, "chunked-kb")]
    added = [c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], KnowledgeBase)]
    assert len(added) == 1
    assert added[0].tags == ["builtin", "slug:chunked-kb"]
