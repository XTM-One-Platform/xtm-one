"""Group share grants go through one writer.

``shared_group_ids`` is a JSON array with no foreign key. Every route that
writes it, and the agent tools that do, go through
``helpers.normalize_group_grants`` (``apply_group_share_fields`` for a request
body), the counterpart of ``normalize_user_grants``: canonical ids, an added
group must exist, a stored one whose group was deleted is dropped, the groups
are locked ``FOR KEY SHARE`` against ``prune_group_grants``, and a permission
entry only for a granted group, with a known tier.

The writer is pinned here on a session double, and a tripwire pins that no
module under ``app/`` assigns the columns any other way. The race with a group
deletion and a real route are in ``test_share_grant_cleanup_postgres.py``.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import BaseModel

from app.api.v1.helpers import (
    SHARE_PERMISSION_CONFIG,
    SHARE_PERMISSION_READ_ONLY,
    apply_group_share_fields,
    normalize_group_grants,
    share_grant_models,
)


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


def _db(*existing: uuid.UUID | str) -> AsyncMock:
    """A session whose group lookup finds *existing* (the ids as text, as selected)."""
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_Rows([(str(gid),) for gid in existing]))
    return db


def _statement(db: AsyncMock):
    assert db.execute.await_count == 1
    return db.execute.await_args.args[0]


# ---------------------------------------------------------------------------
# normalize_group_grants
# ---------------------------------------------------------------------------


async def test_ids_are_stored_canonical_and_once():
    gid = uuid.uuid4()
    db = _db(gid)

    ids, perms = await normalize_group_grants(db, [gid.hex, str(gid).upper(), str(gid)], None)

    assert ids == [str(gid)]
    assert perms is None


@pytest.mark.parametrize("given", [None, []])
async def test_no_group_is_stored_as_null_without_a_query(given):
    db = _db()

    assert await normalize_group_grants(db, given, {str(uuid.uuid4()): "config"}) == (None, None)
    assert db.execute.await_count == 0


@pytest.mark.parametrize("raw", ["not-a-uuid", "", "   ", "' OR 1=1 --"])
async def test_an_id_that_is_not_a_uuid_is_refused(raw):
    db = _db()

    with pytest.raises(HTTPException) as refused:
        await normalize_group_grants(db, [raw], None)

    assert refused.value.status_code == 422
    assert refused.value.detail == "Invalid group id in share grants."
    assert db.execute.await_count == 0


async def test_an_added_group_that_does_not_exist_is_refused():
    kept, unknown = uuid.uuid4(), uuid.uuid4()
    db = _db(kept)

    with pytest.raises(HTTPException) as refused:
        await normalize_group_grants(db, [str(kept), str(unknown)], None, existing_ids=[str(kept)])

    assert refused.value.status_code == 422
    assert refused.value.detail == f"Cannot share with unknown group(s): {unknown}"


async def test_a_stored_group_deleted_since_is_dropped():
    """A stale form re-sending a stored grant to a deleted group does not block the save."""
    kept, deleted = uuid.uuid4(), uuid.uuid4()
    db = _db(kept)

    ids, perms = await normalize_group_grants(
        db,
        [str(deleted), str(kept)],
        {str(deleted): SHARE_PERMISSION_CONFIG, str(kept): SHARE_PERMISSION_CONFIG},
        existing_ids=[str(deleted).upper(), str(kept)],
    )

    assert ids == [str(kept)]
    assert perms == {str(kept): SHARE_PERMISSION_CONFIG}


async def test_only_deleted_groups_left_store_null():
    deleted = uuid.uuid4()
    db = _db()

    assert await normalize_group_grants(db, [str(deleted)], None, existing_ids=[str(deleted)]) == (None, None)


async def test_permissions_only_for_granted_groups_with_canonical_keys():
    granted, other = uuid.uuid4(), uuid.uuid4()
    db = _db(granted)

    _, perms = await normalize_group_grants(
        db,
        [str(granted)],
        {str(granted).upper(): SHARE_PERMISSION_READ_ONLY, str(other): SHARE_PERMISSION_CONFIG, "x": "config"},
    )

    assert perms == {str(granted): SHARE_PERMISSION_READ_ONLY}


@pytest.mark.parametrize("tier", ["admin", "", "CONFIG", None])
async def test_an_unknown_tier_is_refused(tier):
    gid = uuid.uuid4()
    db = _db(gid)

    with pytest.raises(HTTPException) as refused:
        await normalize_group_grants(db, [str(gid)], {str(gid): tier})

    assert refused.value.status_code == 422
    assert refused.value.detail == "Invalid share permission. Expected one of: read_only, config, config_with_chats"


async def test_the_groups_are_read_for_key_share_without_flushing():
    """The lock ``prune_group_grants``' ``FOR UPDATE`` waits on, taken before the edited row is flushed."""
    gid = uuid.uuid4()
    db = _db(gid)

    await normalize_group_grants(db, [str(gid)], None)

    statement = _statement(db)
    lock = statement._for_update_arg
    assert (lock.read, lock.key_share) == (True, True)
    assert statement.get_execution_options()["autoflush"] is False
    assert [table.name for table in statement.get_final_froms()] == ["groups"]


# ---------------------------------------------------------------------------
# apply_group_share_fields
# ---------------------------------------------------------------------------


class _Body(BaseModel):
    name: str | None = None
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None


def _entity(ids=None, perms=None):
    return SimpleNamespace(shared_group_ids=ids, shared_group_permissions=perms)


async def test_an_update_not_naming_the_grants_leaves_them_alone():
    stored = [str(uuid.uuid4())]
    entity, db = _entity(stored, {stored[0]: "config"}), _db()

    await apply_group_share_fields(db, entity, _Body(name="x"), partial=True)

    assert (entity.shared_group_ids, entity.shared_group_permissions) == (stored, {stored[0]: "config"})
    assert db.execute.await_count == 0


async def test_an_update_of_the_permissions_alone_is_checked_against_the_stored_ids():
    granted = uuid.uuid4()
    entity, db = _entity([str(granted)]), _db(granted)

    await apply_group_share_fields(
        db,
        entity,
        _Body(shared_group_permissions={str(granted): "config", str(uuid.uuid4()): "config"}),
        partial=True,
    )

    assert entity.shared_group_ids == [str(granted)]
    assert entity.shared_group_permissions == {str(granted): "config"}


async def test_an_update_with_null_clears_unless_null_keeps_the_stored_grants():
    stored = uuid.uuid4()
    body = _Body(shared_group_ids=None)

    kept = _entity([str(stored)])
    await apply_group_share_fields(_db(stored), kept, body, partial=True, null_keeps_stored=True)
    assert kept.shared_group_ids == [str(stored)]

    cleared = _entity([str(stored)], {str(stored): "config"})
    await apply_group_share_fields(_db(stored), cleared, body, partial=True)
    assert (cleared.shared_group_ids, cleared.shared_group_permissions) == (None, None)


async def test_an_update_adding_an_unknown_group_is_refused_and_writes_nothing():
    stored = uuid.uuid4()
    entity = _entity([str(stored)])

    with pytest.raises(HTTPException) as refused:
        await apply_group_share_fields(
            _db(stored), entity, _Body(shared_group_ids=[str(stored), str(uuid.uuid4())]), partial=True
        )

    assert refused.value.status_code == 422
    assert entity.shared_group_ids == [str(stored)]


async def test_a_create_takes_both_fields_from_the_body():
    gid = uuid.uuid4()
    entity = _entity()

    await apply_group_share_fields(
        _db(gid), entity, _Body(shared_group_ids=[gid.hex], shared_group_permissions={gid.hex: "config"}), partial=False
    )

    assert (entity.shared_group_ids, entity.shared_group_permissions) == ([str(gid)], {str(gid): "config"})


# ---------------------------------------------------------------------------
# Tripwire: no other writer
# ---------------------------------------------------------------------------

_APP = Path(__file__).resolve().parents[1] / "app"
_COLUMNS = frozenset({"shared_group_ids", "shared_group_permissions"})
#: The writer itself, and the deletion's prune.
_WRITER_MODULE = _APP / "api" / "v1" / "helpers.py"
_WRITER = "normalize_group_grants"


def _is_none(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _is_writer_call(node: ast.AST) -> bool:
    if isinstance(node, ast.Await):
        node = node.value
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (isinstance(func, ast.Name) and func.id == _WRITER) or (
        isinstance(func, ast.Attribute) and func.attr == _WRITER
    )


def _targets(node: ast.AST) -> list[ast.AST]:
    if isinstance(node, ast.Assign):
        found: list[ast.AST] = []
        for target in node.targets:
            found.extend(target.elts if isinstance(target, ast.Tuple) else [target])
        return found
    if isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        return [node.target]
    return []


def _violations(source: str, where: str, models: set[str]) -> list[str]:
    tree = ast.parse(source)
    found: list[str] = []
    for node in ast.walk(tree):
        # ``entity.shared_group_ids = ...``: only a clear, or the writer's result.
        if any(isinstance(t, ast.Attribute) and t.attr in _COLUMNS for t in _targets(node)):
            value = getattr(node, "value", None)
            if isinstance(node, ast.AugAssign) or not (_is_none(value) or _is_writer_call(value)):
                found.append(f"{where}:{node.lineno} assigns a group grant column")
        if isinstance(node, ast.Call):
            func = node.func
            # ``Model(shared_group_ids=...)`` on a shareable model.
            if isinstance(func, ast.Name) and func.id in models:
                for keyword in node.keywords:
                    if keyword.arg in _COLUMNS and not _is_none(keyword.value):
                        found.append(f"{where}:{node.lineno} constructs {func.id} with a group grant")
            # ``setattr(entity, "shared_group_ids", ...)``.
            if isinstance(func, ast.Name) and func.id == "setattr" and len(node.args) >= 2:
                name = node.args[1]
                if isinstance(name, ast.Constant) and name.value in _COLUMNS:
                    found.append(f"{where}:{node.lineno} setattr()s a group grant column")
        # ``for field in (..., "shared_group_ids"): setattr(entity, field, ...)``.
        if isinstance(node, ast.For) and isinstance(node.iter, (ast.Tuple, ast.List, ast.Set)):
            names = {e.value for e in node.iter.elts if isinstance(e, ast.Constant)}
            calls_setattr = any(
                isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name) and inner.func.id == "setattr"
                for inner in ast.walk(node)
            )
            if names & _COLUMNS and calls_setattr:
                found.append(f"{where}:{node.lineno} copies a group grant column in a setattr loop")
    return sorted(found, key=lambda line: int(line.split(" ", 1)[0].rsplit(":", 1)[1]))


def test_the_tripwire_catches_each_form_it_forbids():
    """A tripwire that never fires proves nothing: each form, in a scratch module."""
    source = (
        "def f(entity, body, Agent):\n"
        "    entity.shared_group_ids = body.shared_group_ids or None\n"
        "    entity.shared_group_ids, entity.shared_group_permissions = body.a, body.b\n"
        "    agent = Agent(shared_group_ids=body.shared_group_ids)\n"
        "    setattr(entity, 'shared_group_permissions', body.shared_group_permissions)\n"
        "    for field in ('name', 'shared_group_ids'):\n"
        "        setattr(entity, field, getattr(body, field))\n"
        "    entity.shared_group_ids = None\n"
        "    agent = Agent(shared_group_ids=None)\n"
    )
    found = _violations(source, "scratch.py", {"Agent"})

    assert [line.split(" ", 1)[0].rsplit(":", 1)[1] for line in found] == ["2", "3", "4", "5", "6"]


def test_no_module_writes_group_grants_outside_the_writer():
    models = {model.__name__ for model in share_grant_models("shared_group_ids")}
    found = [
        violation
        for path in sorted(_APP.rglob("*.py"))
        if path != _WRITER_MODULE
        for violation in _violations(
            path.read_text(encoding="utf-8"), str(path.relative_to(_APP.parent)), models
        )
    ]

    assert found == []
