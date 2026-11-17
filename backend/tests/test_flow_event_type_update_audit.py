"""Audit coverage for ``PUT /flow-event-types/{id}``.

``create_event_type`` and ``delete_event_type`` both write an audit entry;
the update handler historically committed and returned without one, leaving
renames and ``fields``-schema rewrites invisible in the audit trail.  These
tests pin the fixed behaviour: an entry is written when something actually
changed (naming the changed attributes), and a no-op update stays silent.

They also pin the one name rule every writer shares: the REST schemas
and the flow-builder agent tool validate through ``EventTypeName``.
"""

import json
import sys
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.dialects import postgresql

from app.api.v1 import flow_event_types as fet_api
from app.api.v1.schemas import flow as flow_schemas
from app.api.v1.schemas.flow import (
    FlowEventTypeCreate,
    FlowEventTypeResponse,
    FlowEventTypeUpdate,
)
from app.services.builtin_tools.flow_builder import build_flow_builder_tool
from app.tools.base import ToolCall


class _FakeDB:
    async def commit(self):
        return None

    async def refresh(self, _row):
        return None


def _make_row(**overrides):
    now = datetime.now(UTC)
    row = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="incident-created",
        description="Fired when an incident is created",
        # Full ``FlowEventFieldSchema.model_dump()`` shape, as stored by the
        # create handler.
        fields=[
            {"name": "severity", "type": "string", "required": False, "description": ""}
        ],
        is_company_managed=False,
        created_at=now,
        updated_at=now,
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


def _setup(monkeypatch, row):
    audit_calls: list[dict] = []

    async def _get_editable(_db, _user, _event_type_id):
        return row

    async def _log_audit(_db, **kwargs):
        audit_calls.append(kwargs)

    monkeypatch.setattr(fet_api, "_get_editable", _get_editable)
    monkeypatch.setattr(fet_api, "log_audit", _log_audit)
    return audit_calls


@pytest.mark.asyncio
async def test_update_writes_audit_entry_naming_changed_attributes(monkeypatch):
    row = _make_row()
    audit_calls = _setup(monkeypatch, row)
    user = SimpleNamespace(id=row.user_id, is_admin=False)

    payload = FlowEventTypeUpdate(
        name="incident-updated",
        fields=[
            {"name": "severity", "type": "string"},
            {"name": "assignee", "type": "string"},
        ],
    )
    await fet_api.update_event_type(
        str(row.id), payload, db=_FakeDB(), user=user, _=None
    )

    assert len(audit_calls) == 1
    entry = audit_calls[0]
    assert entry["action"] == "flow_event_type.update"
    assert entry["entity_type"] == "flow_event_type"
    assert entry["entity_id"] == row.id
    assert entry["entity_name"] == "incident-updated"
    assert "name" in entry["summary"]
    assert "fields" in entry["summary"]
    assert "description" not in entry["summary"]


@pytest.mark.asyncio
async def test_noop_update_writes_no_audit_entry(monkeypatch):
    row = _make_row()
    audit_calls = _setup(monkeypatch, row)
    user = SimpleNamespace(id=row.user_id, is_admin=False)

    payload = FlowEventTypeUpdate(
        name=row.name,
        description=row.description,
        fields=list(row.fields),
    )
    await fet_api.update_event_type(
        str(row.id), payload, db=_FakeDB(), user=user, _=None
    )

    assert audit_calls == []


# -- Blank names -------------------------------------------------------
# The schemas validated the RAW value, so "   " passed `min_length=1` and the
# handlers then stored `payload.name.strip()` == "" - an unnamed declaration the
# builder cannot pick, audited as "Updated flow event type '' (name)".


@pytest.mark.parametrize("model", [FlowEventTypeCreate, FlowEventTypeUpdate])
@pytest.mark.parametrize("blank", ["", "   ", "\t\n "])
def test_a_blank_name_is_refused(model, blank):
    with pytest.raises(ValidationError):
        model(name=blank)


def test_names_are_stripped_before_the_length_check():
    assert FlowEventTypeCreate(name="  incident  ").name == "incident"
    assert FlowEventTypeUpdate(name="  incident  ").name == "incident"
    # The limit applies to what is stored, not to the padding around it.
    assert len(FlowEventTypeCreate(name="  " + "x" * 120 + "  ").name) == 120
    with pytest.raises(ValidationError):
        FlowEventTypeCreate(name="x" * 121)
    # Omitting the name on an update still means "leave it alone".
    assert FlowEventTypeUpdate().name is None


@pytest.mark.asyncio
async def test_update_with_the_same_name_padded_is_a_noop(monkeypatch):
    row = _make_row()
    audit_calls = _setup(monkeypatch, row)
    user = SimpleNamespace(id=row.user_id, is_admin=False)

    payload = FlowEventTypeUpdate(name=f"  {row.name}  ")
    await fet_api.update_event_type(
        str(row.id), payload, db=_FakeDB(), user=user, _=None
    )

    assert row.name == "incident-created"
    assert audit_calls == []


class _FakeCreateDB(_FakeDB):
    def __init__(self):
        self.added = []

    def add(self, row):
        self.added.append(row)

    async def refresh(self, row):
        now = datetime.now(UTC)
        row.id = row.id or uuid.uuid4()
        row.created_at = row.created_at or now
        row.updated_at = row.updated_at or now
        row.is_company_managed = bool(row.is_company_managed)


@pytest.mark.asyncio
async def test_create_stores_the_stripped_name(monkeypatch):
    audit_calls: list[dict] = []

    async def _log_audit(_db, **kwargs):
        audit_calls.append(kwargs)

    monkeypatch.setattr(fet_api, "log_audit", _log_audit)
    db = _FakeCreateDB()
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=False)

    response = await fet_api.create_event_type(
        FlowEventTypeCreate(name="  incident-created  "), db=db, user=user, _=None
    )

    assert db.added[0].name == "incident-created"
    assert response.name == "incident-created"
    assert audit_calls[0]["entity_name"] == "incident-created"


# -- Names with nothing visible ----------------------------------------
# Zero-width characters are not whitespace, so a whitespace-only strip left
# "\u200b" standing as a one-character name - stored, and just as unpickable
# as the empty one. Pydantic's own strip also kept the separators U+001C-U+001F
# that the agent tool's `str.strip()` removed, so the two writers disagreed.


@pytest.mark.parametrize("model", [FlowEventTypeCreate, FlowEventTypeUpdate])
@pytest.mark.parametrize(
    "invisible",
    [
        "\u200b",  # zero-width space
        "\ufeff",  # zero-width no-break space (byte order mark)
        "\u200c\u200d",  # zero-width non-joiner + joiner
        " \u2060 ",  # word joiner inside ordinary padding
        "\u00a0\u3000",  # no-break space + ideographic (full-width) space
        "\x1c\x1f",  # separators `str.isspace()` counts as whitespace
    ],
)
def test_a_name_with_nothing_visible_is_refused(model, invisible):
    with pytest.raises(ValidationError):
        model(name=invisible)


def test_invisible_padding_is_stripped_only_at_the_edges():
    assert FlowEventTypeCreate(name="\ufeff incident\u200b\u3000").name == "incident"
    assert FlowEventTypeUpdate(name="\x1cincident\x1f").name == "incident"
    # Inside a name they are content: the joiner holds an emoji sequence, and
    # the format characters closing a subdivision flag are not stripped either.
    technologist = "on-call \U0001f468\u200d\U0001f4bb"
    assert FlowEventTypeCreate(name=technologist).name == technologist
    england = "Ops \U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f"
    assert FlowEventTypeCreate(name=england).name == england


def test_interleaved_padding_is_stripped_in_one_pass():
    # A whitespace strip followed by a zero-width strip would leave " incident"
    # for " \u200b incident": the whole set is stripped together.
    assert (
        FlowEventTypeCreate(name=" \u200b \ufeff incident \u2060 \u200b").name
        == "incident"
    )
    padded = " \u200b" * 20_000 + "incident" + "\u200b " * 20_000
    assert FlowEventTypeCreate(name=padded).name == "incident"


def test_the_padding_set_covers_every_whitespace_character():
    # The set is built from the Basic Multilingual Plane, where every
    # whitespace character lives today: one added elsewhere fails here.
    whitespace = {c for c in map(chr, range(sys.maxunicode + 1)) if c.isspace()}
    assert whitespace <= set(flow_schemas._EVENT_TYPE_NAME_PADDING)


# -- The agent tool writes through the same rule -----------------------


class _FakeToolDB:
    def __init__(self):
        self.added = []

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        return None


async def _declare_with_the_agent_tool(name, fields=None):
    db = _FakeToolDB()
    _definitions, executor = build_flow_builder_tool(db, str(uuid.uuid4()))
    arguments = {"name": name}
    if fields is not None:
        arguments["fields"] = json.dumps(fields)
    result = await executor.execute_tool(
        ToolCall(id="call-1", name="create_flow_event_type", arguments=arguments)
    )
    return db, result


@pytest.mark.asyncio
@pytest.mark.parametrize("blank", ["   ", "\u200b", " \ufeff\u2060 "])
async def test_the_agent_tool_refuses_a_blank_name(blank):
    db, result = await _declare_with_the_agent_tool(blank)

    assert db.added == []
    assert result.content == "Give the event type a name."


@pytest.mark.asyncio
async def test_the_agent_tool_stores_the_name_the_api_would_store():
    db, _result = await _declare_with_the_agent_tool("\ufeff  incident-created \x1f")

    assert [row.name for row in db.added] == ["incident-created"]


@pytest.mark.asyncio
@pytest.mark.parametrize("too_long", ["x" * 119 + " y", "x" * 121])
async def test_the_agent_tool_refuses_a_name_over_the_limit(too_long):
    # It used to strip THEN cut at 120, which could leave the stored name
    # ending on a space ("x" * 119 + " y" -> "x" * 119 + " ") and silently
    # renamed anything longer. The API refuses both; so does the tool.
    db, result = await _declare_with_the_agent_tool(too_long)

    assert db.added == []
    assert "120 characters" in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    [
        {"name": "ticket title", "type": "string"},  # not an identifier
        {"name": "severity", "type": "integer"},  # not a declared type
        {"name": "x" * 61, "type": "string"},  # over the field name limit
    ],
)
async def test_the_agent_tool_refuses_a_field_the_api_would_refuse(field):
    # The tool stored any field it was handed, and the list endpoint validates
    # stored fields against the request rules: one such row answered 500 to
    # everyone who could see it.
    db, result = await _declare_with_the_agent_tool("incident-created", [field])

    assert db.added == []
    assert "field" in result.content.lower()


@pytest.mark.asyncio
async def test_the_agent_tool_stores_fields_in_the_api_shape():
    db, _result = await _declare_with_the_agent_tool(
        "incident-created",
        [
            {"name": " severity ", "type": "number", "required": True},
            {"name": "note", "description": "Free text"},
            {"type": "string"},  # nameless entries are skipped, as before
        ],
    )

    assert [row.fields for row in db.added] == [
        [
            {"name": "severity", "type": "number", "required": True, "description": ""},
            {"name": "note", "type": "string", "required": False, "description": "Free text"},
        ]
    ]


# -- Rows stored before the fix keep working ----------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_name", ["", "  incident-created  "])
async def test_an_update_without_a_name_leaves_a_legacy_name_alone(
    monkeypatch, legacy_name
):
    row = _make_row(name=legacy_name)
    audit_calls = _setup(monkeypatch, row)
    user = SimpleNamespace(id=row.user_id, is_admin=False)

    response = await fet_api.update_event_type(
        str(row.id),
        FlowEventTypeUpdate(description="Now documented"),
        db=_FakeDB(),
        user=user,
        _=None,
    )

    assert row.name == legacy_name
    assert response.name == legacy_name
    assert row.description == "Now documented"
    assert audit_calls[0]["summary"].endswith("(description)")


@pytest.mark.asyncio
async def test_resending_a_padded_legacy_name_stores_it_stripped(monkeypatch):
    row = _make_row(name="  incident-created  ")
    audit_calls = _setup(monkeypatch, row)
    user = SimpleNamespace(id=row.user_id, is_admin=False)

    await fet_api.update_event_type(
        str(row.id),
        FlowEventTypeUpdate(name="  incident-created  "),
        db=_FakeDB(),
        user=user,
        _=None,
    )

    assert row.name == "incident-created"
    assert audit_calls[0]["summary"].endswith("(name)")


# -- Non-admin callers, through the real visibility filter --------------
# The tests above stub `_get_editable`. Here it runs for real: the statement is
# compiled for PostgreSQL, so the non-admin branch of `user_visible_filter` is
# built exactly as the driver would receive it (rule 19: `flow_event_types`
# once shipped without the per-user columns and 500'd for every non-admin).


class _CompilingDB(_FakeDB):
    def __init__(self, row):
        self.row = row
        self.statements: list[str] = []

    async def execute(self, statement):
        self.statements.append(str(statement.compile(dialect=postgresql.dialect())))
        return SimpleNamespace(
            scalar_one_or_none=lambda: self.row,
            scalars=lambda: SimpleNamespace(all=lambda: [self.row]),
        )


def _setup_real_lookup(monkeypatch):
    audit_calls: list[dict] = []

    async def _log_audit(_db, **kwargs):
        audit_calls.append(kwargs)

    async def _no_groups(_db, _user_id):
        return []

    monkeypatch.setattr(fet_api, "log_audit", _log_audit)
    monkeypatch.setattr(fet_api, "get_user_group_ids", _no_groups)
    return audit_calls


@pytest.mark.asyncio
async def test_a_non_admin_owner_renames_through_the_visibility_filter(monkeypatch):
    row = _make_row()
    audit_calls = _setup_real_lookup(monkeypatch)
    db = _CompilingDB(row)
    user = SimpleNamespace(id=row.user_id, is_admin=False)

    response = await fet_api.update_event_type(
        str(row.id),
        FlowEventTypeUpdate(name="  incident-renamed  "),
        db=db,
        user=user,
        _=None,
    )

    assert response.name == "incident-renamed"
    assert "shared_user_ids" in db.statements[0]
    assert [call["entity_name"] for call in audit_calls] == ["incident-renamed"]


@pytest.mark.asyncio
async def test_a_non_admin_cannot_rename_an_event_type_they_do_not_own(monkeypatch):
    row = _make_row()
    audit_calls = _setup_real_lookup(monkeypatch)
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=False)

    with pytest.raises(HTTPException) as exc_info:
        await fet_api.update_event_type(
            str(row.id),
            FlowEventTypeUpdate(name="hijacked"),
            db=_CompilingDB(row),
            user=user,
            _=None,
        )

    assert exc_info.value.status_code == 403
    assert row.name == "incident-created"
    assert audit_calls == []


@pytest.mark.asyncio
async def test_rows_the_agent_tool_stored_before_still_list(monkeypatch):
    # Rows written by the tool before it applied the field rule are served as
    # stored rather than re-validated against the request rule, which failed
    # the whole list for everyone who could see one of them.
    stored = {"name": "ticket title", "type": "integer", "required": True, "description": ""}
    row = _make_row(fields=[stored])
    _setup_real_lookup(monkeypatch)
    user = SimpleNamespace(id=row.user_id, is_admin=False)

    listed = await fet_api.list_event_types(db=_CompilingDB(row), user=user, _=None)
    # FastAPI dumps what the handler returns, then validates it again against
    # the route's response model.
    served = TypeAdapter(list[FlowEventTypeResponse]).validate_python(
        [item.model_dump() for item in listed]
    )

    assert [field.model_dump() for field in served[0].fields] == [stored]
