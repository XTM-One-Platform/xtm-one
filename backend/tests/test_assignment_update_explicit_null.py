"""An explicit ``null`` on a NOT NULL assignment field is "no change".

Every ``UpdateAssignmentRequest`` field is ``X | None``, so a client (or a
generated SDK) may legitimately send ``null``. The update loop used to write it
through, and the commit failed with a NOT NULL violation - or, for the JSON
``trigger_config``, stored a JSON ``null`` no read could serialize: an
unhandled 500. The database half is ``test_assignment_update_explicit_null_postgres.py``.
"""

from __future__ import annotations

from typing import get_args
from unittest.mock import AsyncMock, patch

import pytest

from app.api.v1 import assignments as assignments_api
from app.api.v1.schemas.assignment import AssignmentResponse, UpdateAssignmentRequest
from app.models.assignment import Assignment
from tests.test_assignment_pause_provenance import (
    _invoke_update,
    _rest_assignment,
    _user,
)

_NON_NULLABLE = sorted(assignments_api._NON_NULLABLE_UPDATE_FIELDS)
_NULLABLE = sorted(
    set(assignments_api._UPDATE_LOOP_FIELDS) - assignments_api._NON_NULLABLE_UPDATE_FIELDS
)


def test_every_loop_field_is_an_assignment_column():
    """The NOT NULL set is derived from the model: a renamed field must not
    silently drop out of it."""
    columns = set(Assignment.__table__.c.keys())
    assert set(assignments_api._UPDATE_LOOP_FIELDS) <= columns


def test_the_derived_set_covers_the_known_not_null_fields():
    assert {"name", "prompt", "trigger_type", "trigger_config", "dry_run", "enabled"} <= set(
        _NON_NULLABLE
    )
    assert {"description", "hide_runs", "human_in_the_loop"} <= set(_NULLABLE)


#: Request fields ``update_assignment`` applies in a block of their own below the
#: loop, each writing an explicit ``null`` through (inherit / clear).
_DEDICATED_BLOCK_FIELDS = frozenset(
    {
        "max_iterations",
        "channel_config",
        "target_knowledge_base_id",
        "run_as_user_id",
        # null or {} clears the override (inherit the agent's).
        "wait_policy",
        # null inherits the agent's, then the platform's list (#11533).
        "organization_context_categories",
        # null goes back to "inherit" (#11533): see _NULL_NORMALISED_FIELDS.
        "organization_context_mode",
    }
)

#: Dedicated-block fields whose block writes a value, never ``null``, for an
#: explicit ``null``: their column may be NOT NULL.
_NULL_NORMALISED_FIELDS = frozenset({"organization_context_mode"})


def test_every_request_field_is_applied_by_the_loop_or_a_dedicated_block():
    """A request field in neither would be accepted and silently dropped; a
    dedicated block writes ``null`` through, so its column must allow it."""
    loop = set(assignments_api._UPDATE_LOOP_FIELDS)
    assert loop.isdisjoint(_DEDICATED_BLOCK_FIELDS)
    assert set(UpdateAssignmentRequest.model_fields) == loop | _DEDICATED_BLOCK_FIELDS
    columns = Assignment.__table__.c
    assert _NULL_NORMALISED_FIELDS <= _DEDICATED_BLOCK_FIELDS
    assert all(columns[field].nullable for field in _DEDICATED_BLOCK_FIELDS - _NULL_NORMALISED_FIELDS)


@pytest.mark.asyncio
async def test_an_explicit_null_organization_context_mode_goes_back_to_inherit(monkeypatch):
    """The NOT NULL mode column never receives the ``null`` its block is sent."""
    assignment, _stored = _stored_assignment()
    assignment.organization_context_mode = "off"
    body = UpdateAssignmentRequest.model_validate({"organization_context_mode": None})

    with patch("app.services.audit.log_audit", new_callable=AsyncMock):
        await _invoke_update(monkeypatch, assignment, body, _user())

    assert assignment.organization_context_mode == "inherit"


def test_the_not_null_fields_are_the_ones_the_response_cannot_carry_as_null():
    """A loop field the model lets be NULL but ``AssignmentResponse`` cannot carry
    as null would commit and then fail to serialize on every read."""

    def _response_accepts_null(field: str) -> bool:
        return type(None) in get_args(AssignmentResponse.model_fields[field].annotation)

    assert {
        field
        for field in assignments_api._UPDATE_LOOP_FIELDS
        if not _response_accepts_null(field)
    } == assignments_api._NON_NULLABLE_UPDATE_FIELDS


def _stored_assignment():
    assignment = _rest_assignment(enabled=True)
    stored = {}
    for field in assignments_api._UPDATE_LOOP_FIELDS:
        value = f"stored-{field}"
        setattr(assignment, field, value)
        stored[field] = value
    assignment.trigger_type = stored["trigger_type"] = "manual"
    assignment.trigger_config = stored["trigger_config"] = {}
    assignment.enabled = stored["enabled"] = True
    return assignment, stored


@pytest.mark.asyncio
@pytest.mark.parametrize("field", _NON_NULLABLE)
async def test_explicit_null_on_a_not_null_field_leaves_it_unchanged(monkeypatch, field):
    assignment, stored = _stored_assignment()
    body = UpdateAssignmentRequest.model_validate({field: None})
    assert field in body.model_fields_set

    with patch("app.services.audit.log_audit", new_callable=AsyncMock):
        db = await _invoke_update(monkeypatch, assignment, body, _user())

    assert getattr(assignment, field) == stored[field]
    assert db.commits == 1


@pytest.mark.asyncio
async def test_dry_run_and_name_null_together(monkeypatch):
    assignment, stored = _stored_assignment()
    body = UpdateAssignmentRequest.model_validate({"dry_run": None, "name": None})

    with patch("app.services.audit.log_audit", new_callable=AsyncMock):
        await _invoke_update(monkeypatch, assignment, body, _user())

    assert assignment.dry_run == stored["dry_run"]
    assert assignment.name == stored["name"]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["description", "hide_runs", "human_in_the_loop"])
async def test_explicit_null_still_clears_a_nullable_field(monkeypatch, field):
    """``null`` keeps its meaning (clear / inherit) where the column allows it."""
    assignment, _stored = _stored_assignment()
    body = UpdateAssignmentRequest.model_validate({field: None})

    with patch("app.services.audit.log_audit", new_callable=AsyncMock):
        await _invoke_update(monkeypatch, assignment, body, _user())

    assert getattr(assignment, field) is None


def test_a_null_deprecated_write_flag_does_not_rewrite_the_write_scope():
    """``write_knowledge_base_enabled: null`` is no change; mapping it through the
    compat normalizer would turn an inherit-all write scope (NULL) into none ([])."""
    body = UpdateAssignmentRequest.model_validate({"write_knowledge_base_enabled": None})
    assert "write_knowledge_base_ids" not in body.model_fields_set
    # The legacy explicit False still maps to "no writes".
    legacy = UpdateAssignmentRequest.model_validate({"write_knowledge_base_enabled": False})
    assert legacy.write_knowledge_base_ids == []


@pytest.mark.asyncio
async def test_a_null_trigger_type_is_not_a_trigger_edit(monkeypatch):
    """No webhook-secret minting and no work_items smart default for a null the
    loop skipped: the trigger did not change."""
    assignment, stored = _stored_assignment()
    assignment.trigger_type = stored["trigger_type"] = "work_item"
    secret_calls: list = []
    monkeypatch.setattr(
        assignments_api,
        "ensure_webhook_secret",
        lambda *a, **k: secret_calls.append(a) or {},
    )
    body = UpdateAssignmentRequest.model_validate({"trigger_type": None, "trigger_config": None})

    with patch("app.services.audit.log_audit", new_callable=AsyncMock):
        await _invoke_update(monkeypatch, assignment, body, _user())

    assert assignment.trigger_type == "work_item"
    assert assignment.trigger_config == stored["trigger_config"]
    assert secret_calls == []


@pytest.mark.asyncio
async def test_a_real_value_is_still_written(monkeypatch):
    assignment, _stored = _stored_assignment()
    body = UpdateAssignmentRequest(dry_run=True, name="Renamed")

    with patch("app.services.audit.log_audit", new_callable=AsyncMock):
        await _invoke_update(monkeypatch, assignment, body, _user())

    assert assignment.dry_run is True
    assert assignment.name == "Renamed"
