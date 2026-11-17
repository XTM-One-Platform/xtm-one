"""An entity the caller cannot see answers like one that does not exist.

``get_manageable_entity()`` loaded the row by id alone and then asked
``can_manage()``, so a non-admin naming another user's private entity got the
403 "Only the creator or an admin can ..." - a confirmation that the id exists
- where an unknown id gets 404. The same held for another user's external AI
key, and for a runner command whose runner the caller cannot see (answered
"Runner not found" instead of the command's own 404).

"Cannot see it" now answers exactly like "does not exist" (same status, same
body); the 403 stays for an entity the caller CAN see but may not manage (a
company-managed entity for a non-admin, a read-only share), which the UI relies
on for its read-only banners. Admins are unchanged.

The fake database answers a lookup the way PostgreSQL would: it returns the row
unless the query carries the visibility (or ownership) predicate and the row
is not visible to the caller - so the code without the predicate still gets the
row, answers 403, and these tests fail on it.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.api import deps
from app.api.v1 import custom_tools as custom_tools_api
from app.api.v1 import helpers
from app.api.v1 import prompts as prompts_api
from app.api.v1 import runners as runners_api
from app.core.database import get_db
from app.models.custom_tool import CustomTool

UNKNOWN_ID = "00000000-0000-0000-0000-000000000000"
ENTITY_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")

_VISIBILITY_MARKERS = ("shared_user_ids",)


def _sql(statement) -> str:
    """The statement's WHERE clause as SQL (the column list names every column)."""
    where = getattr(statement, "whereclause", None)
    if where is None:
        return ""
    try:
        return str(where.compile(dialect=postgresql.dialect()))
    except Exception:
        return ""


def _tables(statement) -> set[str]:
    froms = getattr(statement, "get_final_froms", None)
    return {getattr(f, "name", "") for f in froms()} if froms else set()


def _is_row_lock(statement) -> bool:
    """A ``FOR UPDATE`` selecting one column: a lock whose result the route ignores."""
    if getattr(statement, "_for_update_arg", None) is None:
        return False
    descriptions = getattr(statement, "column_descriptions", [])
    return len(descriptions) == 1 and not hasattr(descriptions[0].get("expr"), "__mapper__")


class _Rows:
    def __init__(self, row=None):
        self._row = row

    def scalar_one_or_none(self):
        return self._row

    def scalars(self):
        rows = [] if self._row is None else list(self._row) if isinstance(self._row, list) else [self._row]
        return SimpleNamespace(all=lambda: rows, first=lambda: rows[0] if rows else None)


class _FakeDB:
    """Holds ``rows`` in lookup order; ``visible`` says whether the caller may see them.

    A lookup filtered by the visibility / ownership predicate gets ``None`` for
    a row the caller cannot see - what the SQL predicate does in PostgreSQL.
    """

    def __init__(self, *rows, visible: bool = True):
        self._rows = list(rows)
        self._visible = visible
        self.statements: list = []

    async def scalar(self, statement, *_args, **_kwargs):
        """A column read (``select(Model.col)``): that column of the next row, which stays queued."""
        self.statements.append(statement)
        if not self._rows:
            return None
        return getattr(self._rows[0], statement.selected_columns[0].key, None)

    async def execute(self, statement, *_args, **_kwargs):
        self.statements.append(statement)
        if "group_members" in _tables(statement):
            return _Rows([])
        if _is_row_lock(statement):
            return _Rows(None)
        row = self._rows.pop(0) if self._rows else None
        if row is not None and not self._visible and any(m in _sql(statement) for m in _VISIBILITY_MARKERS):
            return _Rows(None)
        return _Rows(row)

    async def delete(self, _obj):
        return None

    async def flush(self):
        return None

    async def commit(self):
        return None

    async def rollback(self):
        return None


def _user(*, admin: bool):
    return SimpleNamespace(
        id=uuid.uuid4(), email="u@example.com", role="admin" if admin else "user", is_admin=admin, is_active=True
    )


def _entity(*, owner=None, company_managed=False, shares=None):
    """A custom tool: private to *owner*, company-managed, or shared with users at a tier."""
    shares = shares or {}
    return SimpleNamespace(
        id=ENTITY_ID,
        user_id=owner,
        is_company_managed=company_managed,
        access_restricted=False,
        shared_group_ids=None,
        shared_group_permissions=None,
        shared_user_ids=[str(u) for u in shares] or None,
        shared_user_permissions={str(u): tier for u, tier in shares.items()} or None,
    )


async def _manageable(db, user, entity_id=str(ENTITY_ID)):
    return await helpers.get_manageable_entity(db, CustomTool, user, entity_id, action="delete", entity_label="Custom tool")


async def _answer(db, user, entity_id=str(ENTITY_ID)):
    try:
        await _manageable(db, user, entity_id)
    except HTTPException as exc:
        return exc.status_code, exc.detail
    return 200, None


# ---------------------------------------------------------------------------
# get_manageable_entity
# ---------------------------------------------------------------------------


async def test_an_invisible_entity_is_answered_like_an_unknown_one():
    user = _user(admin=False)
    someone_else = uuid.uuid4()

    unknown = await _answer(_FakeDB(), user, UNKNOWN_ID)
    invisible = await _answer(_FakeDB(_entity(owner=someone_else), visible=False), user)

    assert unknown == (404, "Custom tool not found")
    assert invisible == unknown


async def test_a_visible_entity_the_caller_may_not_manage_keeps_its_403():
    user = _user(admin=False)
    company = await _answer(_FakeDB(_entity(company_managed=True)), user)
    read_only = await _answer(_FakeDB(_entity(owner=uuid.uuid4(), shares={user.id: "read_only"})), user)

    assert company == (403, "Only admins can delete company-managed custom tools")
    assert read_only == (403, "Only the creator or an admin can delete this custom tool")


@pytest.mark.parametrize("how", ["owner", "config-share"])
async def test_a_manageable_entity_still_resolves(how):
    user = _user(admin=False)
    entity = _entity(owner=user.id) if how == "owner" else _entity(owner=uuid.uuid4(), shares={user.id: "config"})

    assert await _manageable(_FakeDB(entity), user) is entity


@pytest.mark.parametrize("kind", ["private", "company"])
async def test_an_admin_is_unchanged(kind):
    admin = _user(admin=True)
    entity = _entity(owner=uuid.uuid4()) if kind == "private" else _entity(company_managed=True)
    db = _FakeDB(entity, visible=False)

    assert await _manageable(db, admin) is entity
    assert not any("shared_user_ids" in _sql(s) for s in db.statements), "an admin's lookup is not narrowed"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _client(router, db: _FakeDB, user) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/x")

    async def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[deps.get_current_user] = lambda: user
    for gate in ("require_admin", "require_ai_enabled", "require_agentic_ai_licensed", "require_agentic_ai", "require_chat_enabled"):
        app.dependency_overrides[getattr(deps, gate)] = (lambda: user) if gate == "require_admin" else (lambda: None)
    return TestClient(app, raise_server_exceptions=False)


def _pair(router, method, template, row, user, *, expected):
    unknown = _client(router, _FakeDB(), user).request(method, "/api/v1/x" + template.format(id=UNKNOWN_ID))
    invisible = _client(router, _FakeDB(row, visible=False), user).request(
        method, "/api/v1/x" + template.format(id=ENTITY_ID)
    )
    assert (unknown.status_code, unknown.json()) == expected
    assert (invisible.status_code, invisible.json()) == expected


@pytest.mark.parametrize(
    ("router", "template", "detail"),
    [
        (custom_tools_api.router, "/{id}", "Custom tool not found"),
        (prompts_api.router, "/{id}", "Prompt not found"),
    ],
    ids=["custom-tool", "prompt"],
)
def test_deleting_an_invisible_entity_gets_the_unknown_id_404(router, template, detail):
    _pair(router, "DELETE", template, _entity(owner=uuid.uuid4()), _user(admin=False), expected=(404, {"detail": detail}))


@pytest.mark.parametrize("verb", ["approve", "reject"])
def test_a_command_on_an_invisible_runner_is_answered_like_an_unknown_command(monkeypatch, verb):
    monkeypatch.setattr(runners_api, "_require_enabled", lambda: None)
    command = SimpleNamespace(id=ENTITY_ID, runner_id=uuid.uuid4(), status="pending_approval")
    user = _user(admin=False)
    expected = (404, {"detail": f"No pending command to {verb}"})

    unknown = _client(runners_api.router, _FakeDB(), user).post(f"/api/v1/x/commands/{UNKNOWN_ID}/{verb}", json={})
    # The command row exists; the runner lookup (visibility-filtered) finds nothing.
    invisible = _client(runners_api.router, _FakeDB(command), user).post(f"/api/v1/x/commands/{ENTITY_ID}/{verb}", json={})

    assert (unknown.status_code, unknown.json()) == expected
    assert (invisible.status_code, invisible.json()) == expected
