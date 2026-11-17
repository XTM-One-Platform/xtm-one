# SPDX-License-Identifier: Apache-2.0
"""OpenCTI Live writes per agent binding (services/integration_live_writes, #11958)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services.integration_live_writes import (
    LiveWritesSettingError,
    binding_override,
    checked_integration_live_writes,
    effective_live_writes,
    live_writes_overrides,
    merge_live_writes,
    newly_live_keys,
    parse_integration_live_writes,
    pruned_live_writes,
    stored_live_writes,
)

A = str(uuid.UUID(int=1))
B = str(uuid.UUID(int=2))
C = str(uuid.UUID(int=3))


# -- Effective value ----------------------------------------------------------


@pytest.mark.parametrize(
    ("integration", "override", "read_only", "expected"),
    [
        ("on", None, False, True),  # inherit on
        ("off", None, False, False),  # inherit off
        (None, None, False, False),  # unset integration: off
        ("off", "on", False, True),  # Live override on an Off integration
        ("on", "off", False, False),  # Draft only override on an On integration
        ("on", "on", True, False),  # read-only wins
        ("on", None, True, False),  # read-only wins over the integration too
        (" ON ", None, False, True),
    ],
)
def test_effective_live_writes(integration, override, read_only, expected):
    assert effective_live_writes(integration, override, read_only=read_only) is expected


# -- Parsing ------------------------------------------------------------------


def test_parse_blank_leaves_unchanged():
    assert parse_integration_live_writes("") == (None, None, False)
    assert parse_integration_live_writes(None) == (None, None, False)


def test_parse_clear():
    assert parse_integration_live_writes("clear") == (None, None, True)
    assert parse_integration_live_writes("none") == (None, None, True)


def test_parse_object_json_and_pairs():
    value, error, provided = parse_integration_live_writes({A.upper(): "live", "OpenCTI": "draft", B: "inherit"})
    assert error is None and provided
    assert value == {A: "on", "opencti": "off", B: "inherit"}
    assert parse_integration_live_writes(f'{{"{A}": "on"}}')[0] == {A: "on"}
    assert parse_integration_live_writes(f"opencti:{A}:off, {B}:on")[0] == {A: "off", B: "on"}
    assert parse_integration_live_writes({A: True, B: False})[0] == {A: "on", B: "off"}


@pytest.mark.parametrize("raw", ["{bad json", {"github": "on"}, {A: "maybe"}, "noseparator", 5])
def test_parse_refuses_malformed(raw):
    value, error, provided = parse_integration_live_writes(raw)
    assert value is None and error and provided


def test_merge_and_stored():
    assert merge_live_writes({A: "on"}, {B: "off"}) == {A: "on", B: "off"}
    assert merge_live_writes({A: "on"}, {A: "inherit"}) is None
    assert stored_live_writes({A: "on", "x": "on", B: "inherit"}) == {A: "on"}
    assert stored_live_writes(None) == {}


# -- Bindings -----------------------------------------------------------------


def test_prune_keeps_bound_opencti_and_template_keys():
    bindings = {"opencti": [A], "opencti#2": [B], "github": [C]}
    assert pruned_live_writes({A: "on", B: "off", C: "on"}, bindings, []) == {A: "on", B: "off"}
    assert pruned_live_writes({"opencti": "on"}, {}, ["opencti"]) == {"opencti": "on"}
    assert pruned_live_writes({"opencti": "on"}, {}, []) is None


def test_overrides_multi_instance_and_template():
    agent = SimpleNamespace(
        integration_live_writes={A: "off", B: "on", "opencti": "on"},
        integration_bindings={"opencti": [A], "opencti#2": [B]},
        integration_template_bindings=["opencti"],
    )
    # The run resolved the template binding to C, a third instance.
    overrides = live_writes_overrides(agent, {"opencti": A, "opencti#2": B, "opencti#3": C})
    assert overrides == {A: "off", B: "on", C: "on"}
    assert binding_override(overrides, A) == "off"
    assert binding_override(overrides, uuid.UUID(B)) == "on"
    assert binding_override(overrides, str(uuid.UUID(int=9))) is None
    assert binding_override({}, A) is None


def test_overrides_template_key_never_reaches_own_binding():
    agent = SimpleNamespace(
        integration_live_writes={"opencti": "on"},
        integration_bindings={"opencti": [A]},
        integration_template_bindings=["opencti"],
    )
    # A is the agent's own id binding: it inherits, the template key is not its override.
    assert live_writes_overrides(agent, {"opencti": A}) == {}


def test_newly_live_keys():
    assert newly_live_keys({A: "on", B: "off"}, {A: "on"}) == []
    assert newly_live_keys({A: "on", B: "on"}, {A: "off"}) == [A, B]


# -- Write checks -------------------------------------------------------------


@pytest.mark.asyncio
async def test_checked_refuses_unbound_key():
    with pytest.raises(LiveWritesSettingError) as exc:
        await checked_integration_live_writes(
            None, uuid.UUID(int=7), True, {C: "off"}, bindings={"opencti": [A]}, template_bindings=[], baseline=None
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_checked_admin_may_set_live_and_draft_needs_nothing():
    stored = await checked_integration_live_writes(
        None, uuid.UUID(int=7), True, {A: "on"}, bindings={"opencti": [A]}, template_bindings=[], baseline=None
    )
    assert stored == {A: "on"}
    # Draft only by a non-admin: no integration authority read at all (db None).
    stored = await checked_integration_live_writes(
        None, uuid.UUID(int=7), False, {A: "off"}, bindings={"opencti": [A]}, template_bindings=[], baseline=None
    )
    assert stored == {A: "off"}
    # A Live value already stored is not re-judged.
    stored = await checked_integration_live_writes(
        None, uuid.UUID(int=7), False, {A: "on"}, bindings={"opencti": [A]}, template_bindings=[], baseline={A: "on"}
    )
    assert stored == {A: "on"}


@pytest.mark.asyncio
async def test_checked_template_live_needs_admin():
    with pytest.raises(LiveWritesSettingError) as exc:
        await checked_integration_live_writes(
            None,
            uuid.UUID(int=7),
            False,
            {"opencti": "on"},
            bindings={},
            template_bindings=["opencti"],
            baseline=None,
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_checked_live_refused_when_not_integration_manager(monkeypatch):
    owner = uuid.UUID(int=8)
    caller = uuid.UUID(int=7)
    row = SimpleNamespace(
        id=uuid.UUID(A),
        user_id=owner,
        is_company_managed=False,
        shared_group_ids=None,
        shared_group_permissions=None,
        shared_user_ids=None,
        shared_user_permissions=None,
    )

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return [row]

    class _Db:
        async def execute(self, _stmt):
            return _Result()

    async def _groups(_db, _uid):
        return []

    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", _groups)
    with pytest.raises(LiveWritesSettingError) as exc:
        await checked_integration_live_writes(
            _Db(), caller, False, {A: "on"}, bindings={"opencti": [A]}, template_bindings=[], baseline=None
        )
    assert exc.value.status_code == 403
    # The integration's owner may.
    row.user_id = caller
    stored = await checked_integration_live_writes(
        _Db(), caller, False, {A: "on"}, bindings={"opencti": [A]}, template_bindings=[], baseline=None
    )
    assert stored == {A: "on"}


def test_agent_prunes_overrides_with_bindings():
    from app.models.agent import Agent

    agent = Agent(
        integration_bindings={"opencti": [A]},
        integration_template_bindings=[],
        integration_live_writes={A: "on", B: "off"},
    )
    agent.prune_orphaned_integration_live_writes()
    assert agent.integration_live_writes == {A: "on"}
    agent.integration_bindings = {}
    agent.prune_orphaned_integration_live_writes()
    assert agent.integration_live_writes is None


def test_copy_carries_overrides():
    from app.models.agent_copy import AGENT_BUNDLE_SKIP_FIELDS, AGENT_CATALOG_CLONE_SKIP_FIELDS, AGENT_COPY_FIELDS

    assert "integration_live_writes" in AGENT_COPY_FIELDS
    assert "integration_live_writes" in AGENT_BUNDLE_SKIP_FIELDS
    assert "integration_live_writes" in AGENT_CATALOG_CLONE_SKIP_FIELDS
