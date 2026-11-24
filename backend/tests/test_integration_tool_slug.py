"""A stable tool-name suffix per integration instance.

The suffix multi-instance tools carry (``<tool>__<slug>``) is stored on the
integration, derived once from its display name: renaming the integration
leaves every tool name byte-identical, and an assignment gate names one
instance's tool by base name plus integration id whatever its suffix is.
"""

from __future__ import annotations

import importlib.util
import pathlib
import uuid
from types import SimpleNamespace

import pytest

from app.core.integration_tool_slug import MAX_TOOL_SLUG_LEN, derive_tool_slug, tool_slug_error
from app.services.assignment_activation_check import (
    check_tool_label,
    parse_activation_check,
    resolve_check_tool,
    run_activation_check,
)
from app.services.integration_tool_slugs import ToolSlugError, validate_tool_slug_change
from app.services.integration_tools import _instance_tool_slug, _suffix_instance_tools
from app.tools.base import CallableToolExecutor, CompositeToolExecutor, ToolDefinition

PROVIDER = "fakeprov"


def _defs() -> list[ToolDefinition]:
    return [
        ToolDefinition(name=name, description=name, source=f"integration:{PROVIDER}")
        for name in ("list_fake_events", "get_fake_event")
    ]


def _build(instances: list[tuple[str, str, str | None]]) -> list[str]:
    """The tool names a build of *instances* ``(id, display name, stored slug)`` yields."""
    used: dict[str, set[str]] = {}
    names: list[str] = []
    for integration_id, name, slug in instances:
        executor = CallableToolExecutor()

        async def _ok(**_kwargs) -> str:
            return "ok"

        for definition in _defs():
            executor.register(definition.name, _ok)
        suffixed = _suffix_instance_tools(
            _defs(),
            executor,
            provider=PROVIDER,
            integ_name=name,
            integ_desc=None,
            integration_id=integration_id,
            used_slugs=used,
            tool_slug=slug,
        )
        names.extend(d.name for d in suffixed)
    return names


# ── derivation: the slug the tool build computed from the display name ───────


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Microsoft - Samuel Hassine", "microsoft_samuel_hass"),
        ("A corp", "a_corp"),
        ("  Executive Assistant Notion  ", "executive_assistant_n"),
    ],
)
def test_derive_tool_slug_matches_the_previous_algorithm(name, expected):
    assert derive_tool_slug(name, PROVIDER, "id-1") == expected
    # The build derives the same slug with no stored value (backward compatible).
    assert _instance_tool_slug(PROVIDER, name, "id-1", {}) == expected


def test_derive_tool_slug_falls_back_to_provider_and_id():
    integration_id = uuid.uuid4()
    slug = derive_tool_slug("", "custom:my-tool", integration_id)
    assert slug.startswith("custom_my_tool_")
    assert tool_slug_error(slug) is None


# ── the stored slug wins over the display name ───────────────────────────────


def test_renaming_the_display_name_leaves_every_tool_name_identical():
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    slug_a, slug_b = derive_tool_slug("A corp", PROVIDER, a), derive_tool_slug("B corp", PROVIDER, b)
    before = _build([(a, "A corp", slug_a), (b, "B corp", slug_b)])
    after = _build([(a, "Alpha corporation", slug_a), (b, "B corp", slug_b)])
    assert before == after
    assert "list_fake_events__a_corp" in before
    assert "list_fake_events__b_corp" in before


def test_a_changed_slug_is_served_on_the_next_build():
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    names = _build([(a, "A corp", "alpha"), (b, "B corp", "b_corp")])
    assert "list_fake_events__alpha" in names


def test_an_invalid_stored_slug_falls_back_to_the_display_name():
    names = _build([(str(uuid.uuid4()), "A corp", "Not Valid!"), (str(uuid.uuid4()), "B corp", None)])
    assert "list_fake_events__a_corp" in names
    assert "list_fake_events__b_corp" in names


def test_two_instances_with_the_same_stored_slug_still_diverge():
    names = _build([(str(uuid.uuid4()), "X", "same"), (str(uuid.uuid4()), "Y", "same")])
    assert "list_fake_events__same" in names
    assert "list_fake_events__same_2" in names


# ── validation ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["", "Upper", "with-dash", "_lead", "trail_", "x" * (MAX_TOOL_SLUG_LEN + 1), None])
def test_tool_slug_error_refuses_bad_values(value):
    assert tool_slug_error(value) is not None


@pytest.mark.parametrize("value", ["alpha", "sam_corporate", "ea", "a1_b2"])
def test_tool_slug_error_accepts_good_values(value):
    assert tool_slug_error(value) is None


class _Db:
    def __init__(self, taken: int) -> None:
        self.taken = taken
        self.statements: list[object] = []

    async def scalar(self, statement):
        self.statements.append(statement)
        return self.taken


def _integration(**overrides):
    values = {"id": uuid.uuid4(), "provider": PROVIDER, "is_company_managed": False, "user_id": uuid.uuid4()}
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_validate_tool_slug_change_normalizes_and_accepts():
    db = _Db(taken=0)
    assert await validate_tool_slug_change(db, _integration(), "  Alpha ") == "alpha"
    assert len(db.statements) == 1


@pytest.mark.asyncio
async def test_validate_tool_slug_change_refuses_a_collision():
    with pytest.raises(ToolSlugError, match="already uses"):
        await validate_tool_slug_change(_Db(taken=1), _integration(), "alpha")


@pytest.mark.asyncio
async def test_validate_tool_slug_change_refuses_a_bad_pattern_without_a_query():
    db = _Db(taken=0)
    with pytest.raises(ToolSlugError):
        await validate_tool_slug_change(db, _integration(), "not valid")
    assert db.statements == []


# ── activation_check: base name plus integration id ──────────────────────────


def test_parse_activation_check_accepts_both_integration_forms():
    integration_id = str(uuid.uuid4())
    by_at, error, _ = parse_activation_check({"tool": f"list_fake_events@{integration_id}"})
    assert error is None
    by_key, error, _ = parse_activation_check({"tool": "list_fake_events", "integration_id": integration_id})
    assert error is None
    assert by_at == by_key
    assert by_at["tool"] == "list_fake_events"
    assert by_at["integration_id"] == integration_id
    assert check_tool_label(by_at) == f"list_fake_events@{integration_id}"


def test_parse_activation_check_refuses_two_different_integrations():
    _value, error, _ = parse_activation_check(
        {"tool": f"list_fake_events@{uuid.uuid4()}", "integration_id": str(uuid.uuid4())}
    )
    assert error and "two different integrations" in error


def test_parse_activation_check_refuses_a_bad_integration_id():
    _value, error, _ = parse_activation_check({"tool": "list_fake_events", "integration_id": "nope"})
    assert error and "UUID" in error


def test_parse_activation_check_keeps_the_suffixed_literal():
    value, error, _ = parse_activation_check({"tool": "list_fake_events__a_corp"})
    assert error is None
    assert value == {"tool": "list_fake_events__a_corp", "arguments": {}, "condition": "non_empty"}


def _composite(instances: list[tuple[str, str]]) -> CompositeToolExecutor:
    """An executor holding the suffixed tools of *instances* ``(id, slug)``, each answering its id."""
    composite = CompositeToolExecutor()
    used: dict[str, set[str]] = {}
    for integration_id, slug in instances:
        executor = CallableToolExecutor()

        async def _answer(_id: str = integration_id, **_kwargs) -> str:
            return f'[{{"id": "{_id}"}}]'

        for definition in _defs():
            executor.register(definition.name, _answer)
        composite.add_tools(
            _suffix_instance_tools(
                _defs(),
                executor,
                provider=PROVIDER,
                integ_name=slug,
                integ_desc=None,
                integration_id=integration_id,
                used_slugs=used,
                tool_slug=slug,
            ),
            executor,
        )
    return composite


@pytest.mark.asyncio
async def test_a_gate_by_base_name_and_integration_id_survives_a_suffix_change():
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    check, _error, _ = parse_activation_check(
        {"tool": "list_fake_events", "integration_id": a, "condition": {"path": "$.id", "in": [a]}}
    )
    for slug_a in ("a_corp", "alpha"):
        composite = _composite([(a, slug_a), (b, "b_corp")])
        assert resolve_check_tool(check, composite) == f"list_fake_events__{slug_a}"
        outcome = await run_activation_check(check, composite)
        assert outcome.passed and outcome.error is None, outcome


@pytest.mark.asyncio
async def test_a_gate_on_an_integration_the_agent_does_not_hold_skips_as_misconfigured() -> None:
    check, _error, _ = parse_activation_check({"tool": "list_fake_events", "integration_id": str(uuid.uuid4())})
    outcome = await run_activation_check(check, _composite([(str(uuid.uuid4()), "a_corp"), (str(uuid.uuid4()), "b")]))
    assert not outcome.passed and outcome.misconfigured
    assert outcome.error and "holds no tool" in outcome.error


# ── the migration backfills today's slug ─────────────────────────────────────


def test_the_migration_backfills_with_the_derivation_the_build_used():
    path = next(pathlib.Path(__file__).resolve().parents[1].glob("alembic/versions/t7319toolslug1_*.py"))
    spec = importlib.util.spec_from_file_location("t7319toolslug1", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "t7319toolslug1"
    assert module.derive_tool_slug is derive_tool_slug
