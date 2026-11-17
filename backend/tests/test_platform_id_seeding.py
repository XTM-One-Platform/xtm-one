"""Tests for forcing the platform instance ID via ``PLATFORM_ID``.

The ``platform_settings`` singleton's primary key doubles as the anonymous
telemetry ``instance_id``.  ``PLATFORM_ID`` forces it for identity continuity
across clones/restores (parity with OpenCTI's ``PLATFORM_ID`` and OpenAEV's
``OPENAEV_INSTANCEID``).  These tests pin:

1. ``Settings.platform_id`` validation (valid UUID or empty, fail closed).
2. ``configured_platform_id()`` parsing.
3. ``get_platform_settings`` creating the singleton with the forced ID.
4. ``reconcile_platform_id`` re-keying an existing row / no-op behavior.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from app.api.v1.platform_settings_helpers import (
    configured_platform_id,
    get_platform_settings,
    reconcile_platform_id,
)
from app.core import config
from app.core.config import settings as app_settings
from app.models.platform_settings import PlatformSettings

FORCED = "1f6e86b2-33fc-48b3-9a66-f2b93166739d"


# ---------------------------------------------------------------------------
# 1. Settings validation
# ---------------------------------------------------------------------------


def test_platform_id_accepts_valid_uuid_and_normalizes():
    assert config.Settings(platform_id=f"  {FORCED.upper()} ").platform_id == FORCED


def test_platform_id_empty_is_allowed():
    assert config.Settings(platform_id="").platform_id == ""


def test_platform_id_rejects_non_uuid():
    with pytest.raises(ValidationError) as exc:
        config.Settings(platform_id="not-a-uuid")
    assert "PLATFORM_ID must be a valid UUID" in str(exc.value)


# ---------------------------------------------------------------------------
# 2. configured_platform_id()
# ---------------------------------------------------------------------------


def test_configured_platform_id_unset(monkeypatch):
    monkeypatch.setattr(app_settings, "platform_id", "")
    assert configured_platform_id() is None


def test_configured_platform_id_set(monkeypatch):
    monkeypatch.setattr(app_settings, "platform_id", FORCED)
    assert configured_platform_id() == uuid.UUID(FORCED)


# ---------------------------------------------------------------------------
# 3/4. get_platform_settings + reconcile_platform_id
# ---------------------------------------------------------------------------


def _fake_db(existing: PlatformSettings | None) -> AsyncMock:
    """Async-session double: SELECT returns ``existing``; add/flush recorded."""
    db = AsyncMock()
    select_result = MagicMock()
    select_result.scalar_one_or_none.return_value = existing
    db.execute = AsyncMock(return_value=select_result)
    db.add = MagicMock()
    db.expunge = MagicMock()
    # ``session.begin_nested()`` is a sync call returning an async context
    # manager (MagicMock auto-configures ``__aenter__``/``__aexit__``).
    db.begin_nested = MagicMock()
    return db


@pytest.mark.asyncio
async def test_create_uses_forced_id(monkeypatch):
    monkeypatch.setattr(app_settings, "platform_id", FORCED)
    db = _fake_db(existing=None)

    ps = await get_platform_settings(db)

    db.add.assert_called_once()
    assert ps.id == uuid.UUID(FORCED)


@pytest.mark.asyncio
async def test_create_without_forced_id_generates_random(monkeypatch):
    monkeypatch.setattr(app_settings, "platform_id", "")
    db = _fake_db(existing=None)

    ps = await get_platform_settings(db)

    db.add.assert_called_once()
    # No forced ID: the column default (uuid4) applies at flush time — the
    # mocked flush leaves it unset here, proving no explicit ID was injected.
    assert ps.id is None


@pytest.mark.asyncio
async def test_reconcile_noop_when_unset(monkeypatch):
    monkeypatch.setattr(app_settings, "platform_id", "")
    db = _fake_db(existing=None)

    await reconcile_platform_id(db)

    db.execute.assert_not_awaited()
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_reconcile_noop_when_id_matches(monkeypatch):
    monkeypatch.setattr(app_settings, "platform_id", FORCED)
    existing = PlatformSettings(id=uuid.UUID(FORCED))
    db = _fake_db(existing=existing)

    await reconcile_platform_id(db)

    # Single SELECT (the load); no UPDATE issued.
    assert db.execute.await_count == 1
    db.expunge.assert_not_called()


@pytest.mark.asyncio
async def test_reconcile_rekeys_existing_row(monkeypatch):
    monkeypatch.setattr(app_settings, "platform_id", FORCED)
    old_id = uuid.uuid4()
    existing = PlatformSettings(id=old_id)
    db = _fake_db(existing=existing)

    await reconcile_platform_id(db)

    # SELECT (load) + UPDATE (re-key).
    assert db.execute.await_count == 2
    update_stmt = db.execute.await_args_list[1].args[0]
    # Assert on the compiled statement's values rather than on SQLAlchemy's
    # auto-generated bindparam names (id / id_1), which are not a stable
    # public API across versions/dialects.
    compiled = update_stmt.compile()
    bound_values = set(compiled.params.values())
    assert uuid.UUID(FORCED) in bound_values  # SET id = <forced>
    assert old_id in bound_values  # WHERE id = <old>
    # ORM session synchronization must be disabled: it cannot remap a changed
    # primary key (the expunge below handles the stale identity instead).
    assert update_stmt.get_execution_options().get("synchronize_session") is False
    # The stale instance (old PK identity) must leave the session.
    db.expunge.assert_called_once_with(existing)
