"""Presets of fleet member settings: the rules that need no database (#9162 R4).

* A preset's values follow the rules of a fleet default: the same fields, the
  same checks, canonical shapes; a reset (``None``) is not a preset value.
* The three built-ins of section 10.6 are valid, distinct and read only.
* The write routes are administrator-only and licence-gated; the list is open
  to every signed-in user.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.api import deps
from app.api.v1 import fleet_member_presets as api
from app.services import fleet_member_presets as presets
from app.services.fleets import FleetActionRefusedError


def test_values_are_checked_like_a_fleet_default():
    assert presets.normalize_preset_values(
        {"context_inclusion": " Summary ", "organization_scope": {"mode": "MONITORED"}}
    ) == {"context_inclusion": "summary", "organization_scope": {"mode": "monitored"}}
    org = uuid.uuid4()
    assert presets.normalize_preset_values(
        {"organization_scope": {"mode": "list", "organization_ids": [str(org), str(org)]}}
    ) == {"organization_scope": {"mode": "list", "organization_ids": [str(org)]}}


@pytest.mark.parametrize(
    ("values", "status"),
    [
        ({}, 422),
        ("summary", 422),
        ({"role_label": "x"}, 422),
        ({"context_inclusion": "everything"}, 422),
        ({"context_inclusion": None}, 422),
        ({"collaboration_instruction": "x" * 4001}, 422),
        ({"organization_scope": {"mode": "list", "organization_ids": []}}, 422),
    ],
    ids=["empty", "not-an-object", "unknown-field", "bad-inclusion", "reset", "too-long", "empty-list"],
)
def test_a_value_the_bulk_endpoint_refuses_is_refused(values, status):
    with pytest.raises(FleetActionRefusedError) as exc:
        presets.normalize_preset_values(values)
    assert exc.value.status_code == status


def test_the_three_builtins_are_valid_distinct_and_marked():
    assert len(presets.BUILTIN_PRESETS) == 3
    assert len({p.id for p in presets.BUILTIN_PRESETS}) == 3
    assert len({p.name.casefold() for p in presets.BUILTIN_PRESETS}) == 3
    for p in presets.BUILTIN_PRESETS:
        assert p.builtin and p.id.startswith(presets.BUILTIN_ID_PREFIX)
        assert presets.normalize_preset_values(p.values) == p.values


def test_a_builtin_name_cannot_be_taken():
    name = presets.BUILTIN_PRESETS[0].name.upper()
    with pytest.raises(FleetActionRefusedError) as exc:
        presets._normalize_name(name)
    assert exc.value.status_code == 409


def test_a_name_is_trimmed_and_bounded():
    assert presets._normalize_name("  Two   words ") == "Two words"
    for bad in ("", "   ", "x" * (presets.PRESET_NAME_MAX_LENGTH + 1)):
        with pytest.raises(FleetActionRefusedError):
            presets._normalize_name(bad)


def test_a_builtin_cannot_be_changed_or_deleted():
    builtin = presets.BUILTIN_PRESETS[0].id
    with pytest.raises(FleetActionRefusedError) as exc:
        asyncio.run(presets.delete_preset(None, builtin))  # type: ignore[arg-type]
    assert exc.value.status_code == 403
    with pytest.raises(FleetActionRefusedError) as exc:
        asyncio.run(presets.update_preset(None, builtin, name="Mine"))  # type: ignore[arg-type]
    assert exc.value.status_code == 403


def _dependencies(endpoint) -> set:
    import inspect

    return {
        p.default.dependency
        for p in inspect.signature(endpoint).parameters.values()
        if hasattr(p.default, "dependency")
    }


@pytest.mark.parametrize(
    "endpoint",
    [api.create_fleet_member_preset, api.update_fleet_member_preset, api.delete_fleet_member_preset],
    ids=["create", "update", "delete"],
)
def test_every_write_is_admin_only_and_licence_gated(endpoint):
    found = _dependencies(endpoint)
    assert deps.require_admin in found
    assert deps.require_agentic_ai_licensed in found


def test_the_list_is_open_to_every_user():
    found = _dependencies(api.list_fleet_member_presets)
    assert deps.require_admin not in found
    assert deps.get_current_user in found
