"""A value longer than its assignment column is refused before any write.

The unit half of ``test_assignment_text_length_postgres.py``. One rule, read off
the model's ``String(n)`` columns, is shared by the REST schemas, agent import,
the agent-builder tools and agent templates, and each refusal happens before
the database is touched. Every value a column holds keeps its answer.
"""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.v1.schemas.assignment import CreateAssignmentRequest, UpdateAssignmentRequest
from app.core import database
from app.models.assignment import Assignment
from app.services.builtin_tools import build_agent_builder_tool
from app.tools.base import ToolCall

FIELDS = ("name", "trigger_type", "delay")


def _limit(field: str) -> int:
    return Assignment.__table__.c[field].type.length


def _too_long(field: str) -> str:
    return "x" * (_limit(field) + 1)


def _refusal(field: str) -> str:
    return f"{field} must be at most {_limit(field)} characters (got {_limit(field) + 1})."


def _payload(**overrides: str) -> dict[str, str]:
    return {
        "agent_id": "00000000-0000-0000-0000-000000000001",
        "name": "Digest",
        "trigger_type": "manual",
        "prompt": "Go.",
        **overrides,
    }


#: Values the columns hold, each accepted on ``main``. A trigger type is not an
#: allow-list: an empty or unknown one is saved as sent.
_FITTING = [
    ("trigger_type", "t" * _limit("trigger_type")),
    ("trigger_type", ""),
    ("trigger_type", "custom_trigger"),
    ("trigger_type", "work_item"),
    ("name", "n" * _limit("name")),
    ("name", ""),
    ("delay", "1" * (_limit("delay") - 1) + "m"),
    ("delay", ""),
]


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def test_the_guarded_fields_are_every_bounded_column_a_request_writes():
    """A ``String(n)`` column added to the request schemas is a length to check."""
    from app.models.assignment import ASSIGNMENT_BOUNDED_TEXT_FIELDS

    bounded = {c.name for c in Assignment.__table__.c if getattr(c.type, "length", None)}
    for schema in (CreateAssignmentRequest, UpdateAssignmentRequest):
        assert bounded & set(schema.model_fields) == set(ASSIGNMENT_BOUNDED_TEXT_FIELDS)


@pytest.mark.parametrize("field", FIELDS)
def test_the_rule_reads_its_limit_off_the_column(field):
    from app.models.assignment import assignment_text_length_error

    assert assignment_text_length_error(field, _too_long(field)) == _refusal(field)
    assert assignment_text_length_error(field, "x" * _limit(field)) is None
    assert assignment_text_length_error(field, "") is None
    assert assignment_text_length_error(field, None) is None
    # Not a string: left to the caller's own type check.
    assert assignment_text_length_error(field, 10 ** (_limit(field) + 1)) is None


def test_every_write_path_applies_the_same_rule():
    """The agent-builder tools import it when they run: see
    ``test_the_builder_tools_ask_the_same_rule``."""
    from app.api.v1 import agent_import
    from app.api.v1.schemas import assignment as schemas
    from app.models import agent_template
    from app.models import assignment as model

    for module in (schemas, agent_import, agent_template):
        assert module.assignment_text_length_error is model.assignment_text_length_error


def test_every_builtin_assignment_fits_its_columns():
    """Built-ins never lean on the template fallback: an entry over a limit fails
    here instead of quietly losing its value at agent creation."""
    from app.agents.builtin_agents import BUILTIN_ASSIGNMENTS
    from app.agents.templates import BUILTIN_TEMPLATES
    from app.models.assignment import assignment_text_length_error

    entries = [
        *BUILTIN_ASSIGNMENTS,
        *(entry for t in BUILTIN_TEMPLATES for entry in t.get("default_assignments") or []),
    ]
    assert entries
    problems = [
        (entry.get("name"), error)
        for entry in entries
        for field in FIELDS
        if (error := assignment_text_length_error(field, entry.get(field)))
    ]
    assert problems == []


# ---------------------------------------------------------------------------
# REST schemas
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize(
    "build",
    [
        lambda field: CreateAssignmentRequest(**_payload(**{field: _too_long(field)})),
        lambda field: UpdateAssignmentRequest(**{field: _too_long(field)}),
    ],
    ids=["create", "update"],
)
def test_the_rest_schemas_refuse_a_value_longer_than_its_column(build, field):
    with pytest.raises(ValidationError) as exc_info:
        build(field)

    [error] = exc_info.value.errors()
    assert (error["type"], error["loc"]) == ("value_error", (field,))
    assert error["msg"] == f"Value error, {_refusal(field)}"


@pytest.mark.parametrize(("field", "value"), _FITTING)
def test_the_rest_schemas_keep_every_value_the_column_holds(field, value):
    assert getattr(CreateAssignmentRequest(**_payload(**{field: value})), field) == value
    assert getattr(UpdateAssignmentRequest(**{field: value}), field) == value


@pytest.mark.parametrize("field", FIELDS)
def test_an_update_may_still_omit_or_null_every_field(field):
    assert UpdateAssignmentRequest().model_fields_set == set()
    request = UpdateAssignmentRequest(**{field: None})
    assert getattr(request, field) is None
    assert request.model_fields_set == {field}


# ---------------------------------------------------------------------------
# Agent-builder tools
# ---------------------------------------------------------------------------


def _no_session(monkeypatch) -> list[str]:
    """Record, and refuse, any database session a tool opens."""
    opened: list[str] = []

    def _factory():
        opened.append("session")
        raise AssertionError("the tool opened a database session")

    monkeypatch.setattr(database, "async_session", _factory)
    return opened


async def _tool(tool: str, arguments: dict[str, str]) -> str:
    _, executor = build_agent_builder_tool(None, str(uuid.uuid4()))
    result = await executor.execute_tool(
        ToolCall(id=str(uuid.uuid4()), name=tool, arguments=arguments)
    )
    return result.content


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["name", "delay"])
async def test_the_builder_tools_refuse_before_opening_a_session(monkeypatch, field):
    opened = _no_session(monkeypatch)

    created = await _tool(
        "create_assignment",
        {"agent_id": str(uuid.uuid4()), "name": "Digest", "prompt": "Go.", field: _too_long(field)},
    )
    updated = await _tool(
        "update_assignment", {"assignment_id": str(uuid.uuid4()), field: _too_long(field)}
    )

    assert (created, updated) == (_refusal(field), _refusal(field))
    assert opened == []


@pytest.mark.asyncio
async def test_the_builder_tools_ask_the_same_rule(monkeypatch):
    from app.models import assignment as model

    monkeypatch.setattr(
        model, "assignment_text_length_error", lambda field, value: f"{field}: refused"
    )
    opened = _no_session(monkeypatch)

    created = await _tool(
        "create_assignment", {"agent_id": str(uuid.uuid4()), "name": "Digest", "prompt": "Go."}
    )
    updated = await _tool(
        "update_assignment", {"assignment_id": str(uuid.uuid4()), "name": "Digest"}
    )

    assert (created, updated) == ("name: refused", "name: refused")
    assert opened == []


# ---------------------------------------------------------------------------
# Agent import
# ---------------------------------------------------------------------------


class _Untouchable:
    """A session that fails the test on any use."""

    def __getattr__(self, name: str):
        raise AssertionError(f"the import touched the database ({name})")


@pytest.mark.asyncio
@pytest.mark.parametrize("field", FIELDS)
async def test_import_refuses_before_the_database_is_touched(field):
    """Variables are the first rows an import writes; the refusal comes before."""
    from app.api.v1.agent_import import create_agent_from_bundle

    bundle = {
        "version": 3,
        "agent": {"name": "Imported agent"},
        "variables": [{"id": str(uuid.uuid4()), "name": "REGION", "value": "eu"}],
        "assignments": [
            {"name": "First", "trigger_type": "manual", "prompt": "Go."},
            {"name": "Second", "trigger_type": "manual", "prompt": "Go.", field: _too_long(field)},
        ],
    }

    with pytest.raises(HTTPException) as exc_info:
        await create_agent_from_bundle(
            _Untouchable(), user=SimpleNamespace(id=uuid.uuid4()), bundle=bundle
        )

    # Named by its position when the name is what does not fit.
    where = "#2" if field == "name" else "'Second'"
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == f"Imported assignment {where}: {_refusal(field)}"


# ---------------------------------------------------------------------------
# Agent templates
# ---------------------------------------------------------------------------


def test_a_template_value_longer_than_its_column_is_read_as_absent(caplog):
    """Admin-saved JSON, where one broken entry must not fail agent creation for
    every user: like a malformed ``trigger_config``, a value its column cannot
    hold is replaced by what an absent key gets, and logged."""
    from app.models.agent_template import default_assignment_text

    entry = {
        "name": _too_long("name"),
        "trigger_type": _too_long("trigger_type"),
        "delay": _too_long("delay"),
    }
    with caplog.at_level(logging.WARNING, logger="app.models.agent_template"):
        assert default_assignment_text("digest", entry, "name", "Untitled Assignment") == (
            "Untitled Assignment"
        )
        assert default_assignment_text("digest", entry, "trigger_type", "manual") == "manual"
        assert default_assignment_text("digest", entry, "delay", None) is None

    assert len(caplog.records) == 3
    assert "'digest'" in caplog.records[0].getMessage()
    assert _refusal("name") in caplog.records[0].getMessage()


@pytest.mark.parametrize(("field", "value"), _FITTING)
def test_a_template_keeps_every_value_the_column_holds(field, value):
    from app.models.agent_template import default_assignment_text

    assert default_assignment_text("digest", {field: value}, field, "fallback") == value
    assert default_assignment_text("digest", {}, field, "fallback") == "fallback"
