"""The OpenCTI System taxonomy probe of the organization import (OpenCTI-Platform/opencti#19943).

OpenCTI disables introspection by default since OpenCTI-Platform/opencti#19370, so the probe reads one
System with the taxonomy attributes instead of ``__type``. The HTTP layer is faked under the real
``_graphql`` so the error strings are the ones the OpenCTI client builds, never hand-written ones.
"""

from __future__ import annotations

import logging
import uuid
from unittest.mock import patch

import httpx
import pytest

from app.integrations.providers.opencti._common import _client
from app.services import organization_context_import as imp

BASE = "https://opencti.example"
HEADERS = {"Authorization": "Bearer x"}
_INTROSPECTION_OFF = {
    "errors": [
        {
            "message": "GraphQL introspection is not allowed, but the query contained __schema or __type",
            "extensions": {"code": "GRAPHQL_VALIDATION_FAILED"},
        }
    ]
}


def _unknown_field(name: str) -> dict:
    return {
        "errors": [
            {
                "message": f'Cannot query field "{name}" on type "System".',
                "extensions": {"code": "GRAPHQL_VALIDATION_FAILED"},
            }
        ]
    }


def _fake_opencti(answer, queries: list[str]):
    """An OpenCTI whose /graphql refuses introspection and answers every other query with *answer*."""

    async def _request(self, method, url, **kwargs):
        query = (kwargs.get("json") or {}).get("query", "")
        queries.append(query)
        request = httpx.Request(method, url)
        if "__type" in query or "__schema" in query:
            return httpx.Response(400, json=_INTROSPECTION_OFF, request=request)
        if isinstance(answer, Exception):
            raise answer
        body = answer(query) if callable(answer) else answer
        return httpx.Response(200, json=body, request=request)

    return patch.object(httpx.AsyncClient, "request", _request)


async def _probe(answer) -> tuple[tuple[bool, str | None], list[str]]:
    queries: list[str] = []
    with _fake_opencti(answer, queries):
        async with _client(HEADERS) as client:
            found = await imp.opencti_has_system_taxonomy(client, BASE, HEADERS)
    return found, queries


@pytest.mark.asyncio
async def test_taxonomy_is_found_with_introspection_disabled():
    """(a) An OpenCTI with the taxonomy and introspection off: found, and no introspection is sent."""
    found, queries = await _probe({"data": {"systems": {"edges": []}}})
    assert found == (True, None)
    assert queries and not any("__type" in q or "__schema" in q for q in queries)
    assert "systems(first: 1)" in queries[0]
    for attribute in imp.OPENCTI_SYSTEM_ATTRIBUTES.values():
        assert attribute in queries[0]


@pytest.mark.asyncio
async def test_an_older_opencti_refusing_a_taxonomy_field_has_none():
    """(b) An OpenCTI before #19943 refuses the attribute as unknown: no taxonomy, no error."""
    found, _ = await _probe(_unknown_field("x_opencti_system_category"))
    assert found == (False, None)


@pytest.mark.asyncio
async def test_an_auth_failure_is_an_error_never_a_missing_taxonomy(caplog):
    """(c) An authentication failure is reported as an error and logged, never read as an older OpenCTI."""
    auth = {"errors": [{"message": "You must be logged in to do this.", "extensions": {"code": "AUTH_REQUIRED"}}]}
    with caplog.at_level(logging.WARNING):
        (taxonomy, err), _ = await _probe(auth)
    assert taxonomy is False and err and "AUTH_REQUIRED" in err
    assert "System taxonomy probe failed" in caplog.text


@pytest.mark.asyncio
async def test_a_network_failure_is_an_error_never_a_missing_taxonomy():
    """(c) A network failure is reported as an error, never read as an older OpenCTI."""
    (taxonomy, err), _ = await _probe(httpx.ConnectError("connection refused"))
    assert taxonomy is False and err


def test_only_a_validation_error_on_the_probe_names_means_an_older_opencti():
    assert imp.opencti_lacks_system_taxonomy(
        "GraphQL error in 'XtmOrganizationImportSystemTaxonomy': Cannot query field \"x_opencti_platform\" "
        'on type "System". [GRAPHQL_VALIDATION_FAILED]'
    )
    assert imp.opencti_lacks_system_taxonomy('Unknown type "System". [GRAPHQL_VALIDATION_FAILED]')
    assert not imp.opencti_lacks_system_taxonomy("Error: OpenCTI authentication failed")
    assert not imp.opencti_lacks_system_taxonomy('Cannot query field "name" on type "Report".')


def _creds(monkeypatch):
    from app.services import ctem_metrics_collector

    async def creds(db, user_id, provider):
        return BASE, dict(HEADERS)

    monkeypatch.setattr(ctem_metrics_collector, "_user_creds", creds)


def _systems_answer(query: str) -> dict:
    if "XtmOrganizationImportSystemTaxonomy" in query:
        return {"data": {"systems": {"edges": []}}}
    node = {"id": "s1", "name": "web-01", "x_opencti_system_category": "application", "x_opencti_criticality": "high"}
    return {"data": {"systems": {"edges": [{"node": node}], "pageInfo": {"globalCount": 1, "hasNextPage": False}}}}


@pytest.mark.asyncio
async def test_the_systems_import_selects_the_taxonomy_when_the_probe_finds_it(monkeypatch):
    """(d) With the taxonomy found, the Systems page query selects every taxonomy attribute."""
    _creds(monkeypatch)
    queries: list[str] = []
    with _fake_opencti(_systems_answer, queries):
        read = await imp.read_source(
            None, user_id=uuid.uuid4(), section="assets", source="opencti", filters={"criticality": ["high"]}
        )
    assert read.error is None and read.total == 1
    page = next(q for q in queries if "XtmOrganizationImportSystems(" in q)
    for attribute in imp.OPENCTI_SYSTEM_ATTRIBUTES.values():
        assert attribute in page


@pytest.mark.asyncio
async def test_the_systems_import_reports_an_unreadable_probe_instead_of_dropping_the_taxonomy(monkeypatch):
    _creds(monkeypatch)
    queries: list[str] = []
    auth = {"errors": [{"message": "You must be logged in to do this.", "extensions": {"code": "AUTH_REQUIRED"}}]}
    with _fake_opencti(auth, queries):
        read = await imp.read_source(None, user_id=uuid.uuid4(), section="assets", source="opencti", filters={})
    assert read.error and read.error.startswith("OpenCTI: ") and read.rows == []
    assert not any("XtmOrganizationImportSystems(" in q for q in queries)


@pytest.mark.asyncio
async def test_the_filter_options_say_unreadable_when_the_probe_fails(monkeypatch):
    _creds(monkeypatch)
    with _fake_opencti(httpx.ConnectError("connection refused"), []):
        found = await imp._opencti_options(None, str(uuid.uuid4()))
    assert {found.unavailable[k] for k in imp.OPENCTI_SYSTEM_ATTRIBUTES} == {"unreadable"}


@pytest.mark.asyncio
async def test_the_filter_options_say_no_taxonomy_on_an_older_opencti(monkeypatch):
    _creds(monkeypatch)

    def older(query: str) -> dict:
        if "XtmOrganizationImportSystemTaxonomy" in query:
            return _unknown_field("x_opencti_system_category")
        return {"data": {}}

    with _fake_opencti(older, []):
        found = await imp._opencti_options(None, str(uuid.uuid4()))
    assert {found.unavailable[k] for k in imp.OPENCTI_SYSTEM_ATTRIBUTES} == {"no_system_taxonomy"}
