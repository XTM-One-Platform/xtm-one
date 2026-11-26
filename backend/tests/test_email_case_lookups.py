"""Every lookup of a user by an externally supplied email ignores its case.

Accounts are stored lowercased (``ix_users_email_lower`` allows one per
address), so a platform or IdP that reports ``First.Last@corp.com`` must find
``first.last@corp.com``. Each test reads what the lookup binds, not its SQL
text, so a rewritten expression cannot pass by accident.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api import auth_sso
from app.api.v1.settings import UserPreferencesRequest, update_preferences
from app.channels.router import resolve_user_by_email
from app.core.config import Settings
from app.models.user import User


class _Result:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _RecordingDb:
    def __init__(self, found=None):
        self.statements: list = []
        self._found = found

    async def execute(self, stmt):
        self.statements.append(stmt)
        return _Result(self._found)


def _bound(stmt) -> list:
    return list(stmt.compile().params.values())


async def test_channel_sender_is_resolved_whatever_the_platforms_case():
    db = _RecordingDb()

    await resolve_user_by_email(db, " First.Last@Corp.com ")

    (stmt,) = db.statements
    assert "lower(users.email) =" in str(stmt)
    assert "first.last@corp.com" in _bound(stmt)


async def test_a_refused_sign_in_finds_the_account_whatever_the_idps_case():
    db = _RecordingDb()

    await auth_sso._record_refusal(db, "First.Last@Corp.com", "not entitled")

    (stmt,) = db.statements
    assert "lower(users.email) =" in str(stmt)
    assert "first.last@corp.com" in _bound(stmt)


def test_the_admin_email_is_lowercased_like_every_account():
    assert Settings(admin_email=" Admin@Filigran.io ").admin_email == "admin@filigran.io"


async def test_the_unchanged_address_in_another_case_is_accepted_on_the_profile(monkeypatch):
    me = User(email="me@corp.com", full_name="Me")

    db = await _update_profile(monkeypatch, me, email=" Me@Corp.com ", full_name="Me Again")

    assert (me.email, me.full_name) == ("me@corp.com", "Me Again")
    assert db.statements == []


class _ProfileDb(_RecordingDb):
    async def flush(self):
        return None

    async def commit(self):
        return None


async def _update_profile(monkeypatch, user, **fields):
    from app.api.v1 import settings as settings_api

    async def _no_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(settings_api, "log_audit", _no_audit)
    db = _ProfileDb()
    await update_preferences(UserPreferencesRequest(**fields), request=None, user=user, db=db)
    return db


async def test_the_profile_never_changes_the_email(monkeypatch):
    """Other products find their account by it: an administrator changes it (``test_email_change_by_admin``)."""
    me = User(email="me@corp.com", full_name="Me", role="admin")

    with pytest.raises(HTTPException) as excinfo:
        await _update_profile(monkeypatch, me, email="Me.Again@Corp.com", full_name="Renamed")

    assert excinfo.value.status_code == 422
    assert (me.email, me.full_name) == ("me@corp.com", "Me")


async def test_the_admin_email_account_still_changes_its_other_preferences(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.admin_email", "admin@corp.com")
    admin = User(email="admin@corp.com", full_name="Admin", role="admin")

    await _update_profile(monkeypatch, admin, email=" Admin@Corp.com ", full_name="Platform Admin", title="CISO")

    assert (admin.email, admin.full_name, admin.title) == ("admin@corp.com", "Platform Admin", "CISO")


def test_every_user_id_column_the_merge_moves_exists_and_has_no_foreign_key():
    """A typo or a rename would otherwise be skipped silently on a real database."""
    import importlib.util
    from pathlib import Path

    import app.models  # noqa: F401 - registers every table
    from app.core.database import Base

    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "u6299emlcase01_merge_users_differing_by_email_case.py"
    )
    spec = importlib.util.spec_from_file_location("u6299emlcase01", path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)

    for table, column in revision._UNKEYED_USER_IDS:
        if table in _MOVED_OUT_OF_THE_MODELS:
            continue
        assert table in Base.metadata.tables, table
        col = Base.metadata.tables[table].columns[column]
        assert not col.foreign_keys, f"{table}.{column} has a foreign key: the catalog covers it"


#: Tables the merge moved user ids in, since replaced in the models: their rows
#: were moved into the new table after the merge (``c3cnc8254032``).
_MOVED_OUT_OF_THE_MODELS = {"persona_improvements"}

#: No-FK user-id columns the merge handles another way (``_fold_daily_stats``),
#: or that it never has to: ``improvement_proposals``,
#: ``health_dataset_entry_sources``, ``health_cycle_runs`` (formerly ``concierge_*``) and
#: ``platform_settings.organization_setup_prompted_by_user_id`` (#9173, who answered
#: the once-per-instance setup proposal) were created after it.
_HANDLED_ELSEWHERE = {
    ("llm_daily_stats", "user_id"),
    ("improvement_proposals", "requested_by_user_id"),
    ("improvement_proposals", "decided_by_user_id"),
    ("health_dataset_entry_sources", "source_user_id"),
    ("health_cycle_runs", "requested_by_user_id"),
    ("platform_settings", "organization_setup_prompted_by_user_id"),
}


def test_every_user_id_column_without_a_foreign_key_is_moved_by_the_merge():
    """The other direction: a new column that holds a user id with no foreign key
    is skipped by the merge, and its rows would name a deleted user. Add it to
    ``_UNKEYED_USER_IDS`` in a new migration's own list, or here when it names no user."""
    import importlib.util
    from pathlib import Path

    from sqlalchemy.dialects.postgresql import UUID

    import app.models  # noqa: F401 - registers every table
    from app.core.database import Base

    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "u6299emlcase01_merge_users_differing_by_email_case.py"
    )
    spec = importlib.util.spec_from_file_location("u6299emlcase01", path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)

    listed = set(revision._UNKEYED_USER_IDS) | _HANDLED_ELSEWHERE
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if col.foreign_keys or not isinstance(col.type, UUID):
                continue
            if col.name.endswith("user_id"):
                assert (table.name, col.name) in listed, (
                    f"{table.name}.{col.name} holds a user id with no foreign key: "
                    "the user merge does not move it"
                )
