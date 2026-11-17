"""``integration_allowed_tools`` names are checked against the provider's tool catalog at write."""

from __future__ import annotations

import uuid

import pytest

from app.services import tool_scope_names as tsn
from app.services.tool_scope_names import (
    IntegrationAllowedToolsError,
    provider_tool_names,
    unknown_integration_tool_names,
    validate_integration_allowed_tools,
)


def test_the_opencti_catalog_holds_its_opt_in_tools():
    """``list_opencti_labels`` is offered by the builder; ``create_opencti_label``
    only with live writes on, and a scope may name it before the setting is on."""
    names = provider_tool_names("opencti")
    assert names is not None
    assert "list_opencti_labels" in names
    assert "create_opencti_label" in names


def test_aliases_are_valid_names():
    names = provider_tool_names("github")
    assert names is not None
    assert "search_github_issues" in names
    assert "list_github_issues" in names  # alias of search_github_issues


@pytest.mark.asyncio
async def test_an_unknown_name_is_refused_with_the_closest_valid_ones():
    with pytest.raises(IntegrationAllowedToolsError) as exc:
        await validate_integration_allowed_tools(None, {"opencti": ["list_opencti_labelz"]})
    message = str(exc.value)
    assert "integration_allowed_tools['opencti']" in message
    assert "list_opencti_labelz" in message
    assert "closest: list_opencti_labels" in message
    assert "Nothing was saved" in message


@pytest.mark.asyncio
async def test_every_unknown_name_is_named_in_one_refusal():
    with pytest.raises(IntegrationAllowedToolsError) as exc:
        await validate_integration_allowed_tools(
            None,
            {"github": ["search_issues", "search_github_issues"], "opencti": ["nope_tool"]},
        )
    message = str(exc.value)
    assert "search_issues" in message
    assert "nope_tool" in message
    assert "unknown tool(s) search_github_issues" not in message


@pytest.mark.asyncio
async def test_valid_names_save():
    await validate_integration_allowed_tools(
        None, {"opencti": ["list_opencti_labels"], "github": ["search_github_issues", "create_github_issue"]}
    )


@pytest.mark.asyncio
async def test_a_multi_instance_suffix_is_accepted():
    await validate_integration_allowed_tools(None, {"github": ["search_github_issues__work"]})
    with pytest.raises(IntegrationAllowedToolsError):
        await validate_integration_allowed_tools(None, {"github": ["search_github_issues__"]})
    with pytest.raises(IntegrationAllowedToolsError):
        await validate_integration_allowed_tools(None, {"github": ["no_such_tool__work"]})


@pytest.mark.asyncio
async def test_empty_null_and_non_dict_values_are_not_checked():
    for value in (None, {}, {"github": None}, {"github": []}, "not a map"):
        await validate_integration_allowed_tools(None, value)


@pytest.mark.asyncio
async def test_an_unreadable_catalog_is_never_refused():
    """A provider with no builder (or one whose build fails) is not checked."""
    await validate_integration_allowed_tools(None, {"no_such_provider": ["anything"]})


@pytest.mark.asyncio
async def test_the_provider_wide_openapi_list_keeps_its_per_integration_meaning(monkeypatch):
    async def _boom(*_args, **_kwargs):  # pragma: no cover - must not be read
        raise AssertionError("the provider-wide openapi list is read per integration elsewhere")

    monkeypatch.setattr(tsn, "_openapi_integration_tool_names", _boom)
    await validate_integration_allowed_tools(None, {"openapi": ["anything_at_all"]})


@pytest.mark.asyncio
async def test_an_openapi_instance_key_is_checked_against_that_integration(monkeypatch):
    integration_id = str(uuid.uuid4())
    seen: list[str] = []

    async def _tools(_db, wanted):
        seen.append(wanted)
        return frozenset({"get_pets", "create_pet"})

    monkeypatch.setattr(tsn, "_openapi_integration_tool_names", _tools)
    key = f"openapi@{integration_id}"
    await validate_integration_allowed_tools(None, {key: ["get_pets"]})
    with pytest.raises(IntegrationAllowedToolsError) as exc:
        await validate_integration_allowed_tools(None, {key: ["get_petz"]})
    assert "closest: get_pets" in str(exc.value)
    assert seen and set(seen) == {integration_id}


def _bound_openapi(monkeypatch, tools_by_id: dict[str, frozenset[str]]) -> None:
    async def _tools(_db, wanted):
        return tools_by_id.get(wanted)

    monkeypatch.setattr(tsn, "_openapi_integration_tool_names", _tools)


@pytest.mark.asyncio
async def test_the_provider_wide_openapi_list_is_checked_against_the_bound_integrations(monkeypatch):
    """A name no bound OpenAPI integration exposes used to be dropped by the
    per-integration split, leaving the integration with every tool."""
    pets, stock = str(uuid.uuid4()), str(uuid.uuid4())
    _bound_openapi(monkeypatch, {pets: frozenset({"get_pets"}), stock: frozenset({"get_stock"})})
    # Valid on either integration, a multi-instance suffix included.
    await validate_integration_allowed_tools(None, {"openapi": ["get_pets", "get_stock__eu"]}, [pets, stock])
    # Unknown alone, then mixed with a valid one: refused, every unknown named.
    for listed in (["get_petz"], ["get_pets", "get_petz"]):
        with pytest.raises(IntegrationAllowedToolsError) as exc:
            await validate_integration_allowed_tools(None, {"openapi": listed}, [pets, stock])
        message = str(exc.value)
        assert "integration_allowed_tools['openapi']: unknown tool(s) get_petz" in message
        assert "closest: get_pets" in message
        assert "Nothing was saved" in message


@pytest.mark.asyncio
async def test_an_explicit_clear_or_empty_openapi_list_is_not_refused(monkeypatch):
    pets = str(uuid.uuid4())
    _bound_openapi(monkeypatch, {pets: frozenset({"get_pets"})})
    for value in (None, {"openapi": None}, {"openapi": []}):
        await validate_integration_allowed_tools(None, value, [pets])


@pytest.mark.asyncio
async def test_an_openapi_list_without_a_readable_bound_integration_is_not_checked(monkeypatch):
    _bound_openapi(monkeypatch, {})
    await validate_integration_allowed_tools(None, {"openapi": ["anything"]}, [str(uuid.uuid4())])


@pytest.mark.asyncio
async def test_stale_names_are_listed_for_the_read_flag():
    assert await unknown_integration_tool_names(None, {"github": ["get_repo", "search_github_issues"]}) == {
        "github": ["get_repo"]
    }
    assert await unknown_integration_tool_names(None, {"github": ["search_github_issues"]}) == {}
