"""A HubSpot CRM object-type Scope(s) is a hard boundary on the HubSpot tools.

Same contract as the GitHub repository scope (``test_github_repo_scope``):
the tools of an unselected object type are withdrawn, a generic call naming an
object type outside the selection is refused before any request, an empty
selection allows nothing, and the selection follows the work handed to other
agents.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from app.integrations.providers.hubspot import build_tools
from app.integrations.providers.hubspot_scope import (
    DROPPED_WHEN_SCOPED,
    HUBSPOT_OBJECT_TYPE_SCOPE_KEY,
    TOOL_OBJECT_TYPE,
    canonical_object_type,
    object_type_scope_from_bindings,
)
from app.services.element_scope import agent_run_scope, assignment_element_scope
from app.services.integration_tools import _SafeIntegrationExecutor
from app.tools.base import ToolCall

CONTACT = {"id": "contact", "name": "Contact", "metadata": {"object_type": "contact"}}
DEAL = {"id": "deal", "name": "Deal", "metadata": {"object_type": "deal"}}
CUSTOM = {"id": "2-12345", "name": "Vendor", "metadata": {"object_type": "2-12345"}}
SCOPE = frozenset({"contact"})


def _tools(scope=SCOPE):
    executor = _SafeIntegrationExecutor()
    defs = build_tools("pat-token", executor, object_type_scope=scope)
    return {d.name: d for d in defs}, executor


async def _call(executor, tool, **arguments):
    return await executor.execute_tool(ToolCall(id="c1", name=tool, arguments=arguments))


_RealAsyncClient = httpx.AsyncClient


@contextlib.contextmanager
def _hubspot_api(responder: Callable[[httpx.Request], httpx.Response]) -> Iterator[list[httpx.Request]]:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return responder(request)

    class _Client(_RealAsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    with patch("httpx.AsyncClient", _Client):
        yield sent


def _contact(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"id": "1", "properties": {"firstname": "Ada"}, "results": []})


# ---------------------------------------------------------------------------
# What an object-type scope is
# ---------------------------------------------------------------------------


def test_no_object_type_binding_means_no_scope():
    assert object_type_scope_from_bindings(None) is None
    assert object_type_scope_from_bindings({}) is None
    assert object_type_scope_from_bindings({"github:repository": [{"id": "a/b"}]}) is None


def test_a_selection_is_read_by_type_id_and_an_unusable_one_allows_nothing():
    assert object_type_scope_from_bindings({HUBSPOT_OBJECT_TYPE_SCOPE_KEY: [CONTACT, CUSTOM]}) == {"contact", "2-12345"}
    assert object_type_scope_from_bindings({HUBSPOT_OBJECT_TYPE_SCOPE_KEY: []}) == frozenset()
    assert object_type_scope_from_bindings({HUBSPOT_OBJECT_TYPE_SCOPE_KEY: ["contact", {"id": " "}]}) == frozenset()
    assert object_type_scope_from_bindings({HUBSPOT_OBJECT_TYPE_SCOPE_KEY: "contact"}) == frozenset()


@pytest.mark.parametrize(("value", "canonical"), [("contacts", "contact"), ("Contact", "contact"), ("0-1", "contact"), ("companies", "company"), ("0-3", "deal"), ("tickets", "ticket"), ("2-12345", "2-12345"), (None, "")])
def test_every_api_spelling_of_a_standard_type_is_the_same_type(value, canonical):
    assert canonical_object_type(value) == canonical


def test_assignments_and_handovers_narrow_the_selection():
    agent = {HUBSPOT_OBJECT_TYPE_SCOPE_KEY: [CONTACT, DEAL]}
    assert object_type_scope_from_bindings(assignment_element_scope(agent, {HUBSPOT_OBJECT_TYPE_SCOPE_KEY: [DEAL]})) == {"deal"}
    handed = {"element_scope": {HUBSPOT_OBJECT_TYPE_SCOPE_KEY: [CONTACT]}}
    assert object_type_scope_from_bindings(agent_run_scope({HUBSPOT_OBJECT_TYPE_SCOPE_KEY: [DEAL]}, None, handed).enforced) == frozenset()
    assert object_type_scope_from_bindings(agent_run_scope(None, None, handed).enforced) == SCOPE


# ---------------------------------------------------------------------------
# The toolbox under a scope
# ---------------------------------------------------------------------------


def test_the_tools_of_unselected_types_and_campaigns_are_withdrawn():
    defs, executor = _tools()
    for name, object_type in TOOL_OBJECT_TYPE.items():
        assert (name in defs) is (object_type == "contact"), name
        assert (name in executor.tool_names) is (object_type == "contact"), name
    for name in DROPPED_WHEN_SCOPED:
        assert name not in defs
    # Account-level metadata and the generic tools stay offered.
    for name in ("list_hubspot_owners", "list_hubspot_pipelines", "get_hubspot_associations", "delete_hubspot_record"):
        assert name in defs


def test_without_a_scope_the_toolbox_is_unchanged():
    executor = _SafeIntegrationExecutor()
    names = {d.name for d in build_tools("pat-token", executor)}
    assert names >= set(TOOL_OBJECT_TYPE) | DROPPED_WHEN_SCOPED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments", "named"),
    [
        ("delete_hubspot_record", {"object_type": "deals", "record_id": "9"}, "deals"),
        ("get_hubspot_associations", {"object_type": "contacts", "object_id": "1", "to_object_type": "companies"}, "companies"),
        ("create_hubspot_association", {"from_type": "0-3", "from_id": "9", "to_type": "contacts", "to_id": "1"}, "0-3"),
        ("get_hubspot_contact", {"contact_id": "1", "associations": "companies,deals"}, "companies"),
    ],
)
async def test_an_object_type_outside_the_scope_is_refused_before_any_request(tool, arguments, named):
    _, executor = _tools()
    with _hubspot_api(_contact) as sent:
        result = await _call(executor, tool, **arguments)
    assert result.is_error is True
    assert f"HubSpot object type '{named}' is outside the Scope(s)" in result.content
    assert "Allowed object types: contact" in result.content
    assert sent == []


@pytest.mark.asyncio
async def test_a_selected_type_runs_whatever_spelling_names_it():
    _, executor = _tools(frozenset({"contact", "company"}))
    with _hubspot_api(_contact) as sent:
        read = await _call(executor, "get_hubspot_contact", contact_id="1", associations="companies")
        associations = await _call(
            executor, "get_hubspot_associations", object_type="0-1", object_id="1", to_object_type="company"
        )
    assert read.is_error is False
    assert associations.is_error is False
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_a_custom_object_is_named_by_its_type_id():
    _, executor = _tools(frozenset({"2-12345"}))
    with _hubspot_api(lambda r: httpx.Response(204)) as sent:
        ok = await _call(executor, "delete_hubspot_record", object_type="2-12345", record_id="7")
        other = await _call(executor, "delete_hubspot_record", object_type="2-99999", record_id="7")
    assert ok.is_error is False
    assert other.is_error is True
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_an_empty_selection_allows_nothing():
    defs, executor = _tools(frozenset())
    assert not set(TOOL_OBJECT_TYPE) & set(defs)
    with _hubspot_api(_contact) as sent:
        result = await _call(executor, "delete_hubspot_record", object_type="contacts", record_id="1")
    assert result.is_error is True
    assert "none (no valid object type is selected)" in result.content
    assert sent == []


def test_every_run_builds_the_hubspot_tools_under_its_scope():
    source = (Path(__file__).resolve().parents[1] / "app/services/integration_tools.py").read_text(encoding="utf-8")
    assert 'if provider == "hubspot":' in source
    assert '_builder_kwargs["object_type_scope"] = _object_type_scope' in source
