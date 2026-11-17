"""HubSpot CRM entity-type ("crm_object_type") element listing.

The trigger filter UI lists selectable HubSpot CRM object types so an
``integration_event`` assignment can be scoped to specific entity types
(Contact, Company, Deal, ...). Standard objects are a fixed set; custom
objects are fetched best-effort from ``GET /crm/v3/schemas`` (requires the
``crm.schemas.custom.read`` scope). The listing must:

  * always return the standard objects, keyed on the ``subscriptionType``
    prefix HubSpot sends (e.g. ``contact``) so the webhook matcher can
    compare against the value directly;
  * add custom objects keyed on their ``objectTypeId`` (e.g. ``2-12345``),
    which is the prefix HubSpot uses for custom-object webhooks;
  * degrade gracefully to the standard-only list when the schema call is
    unauthorized / errors out or raises, so the picker still renders.

"""

import logging

import pytest

from app.api.v1.integration_elements import (
    HUBSPOT_STANDARD_OBJECT_TYPES,
    _list_provider_elements,
)

_STANDARD_IDS = [obj_id for obj_id, _ in HUBSPOT_STANDARD_OBJECT_TYPES]


class _FakeResponse:
    def __init__(self, *, is_success: bool, payload: dict, status_code: int = 200):
        self.is_success = is_success
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload


class _FakeSchemasClient:
    """Configurable fake ``httpx.AsyncClient`` for the /crm/v3/schemas call.

    Scenario is driven by class attributes set per test (mirrors the
    class-attribute style used by ``test_integration_elements_teams.py``).
    """

    response: _FakeResponse | None = None
    raise_exc: Exception | None = None
    captured: dict = {}

    def __init__(self, *_args, **_kwargs):
        type(self).captured = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, url, headers=None, **_kwargs):
        type(self).captured = {"url": url, "headers": headers}
        if type(self).raise_exc is not None:
            raise type(self).raise_exc
        return type(self).response


@pytest.fixture(autouse=True)
def _reset_fake_client():
    _FakeSchemasClient.response = None
    _FakeSchemasClient.raise_exc = None
    _FakeSchemasClient.captured = {}
    yield


def _patch_client(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _FakeSchemasClient)


@pytest.mark.asyncio
async def test_hubspot_standard_objects_always_returned(monkeypatch):
    """When the schema call returns no custom objects, the picker must still
    list every standard object, keyed on the webhook ``subscriptionType``
    prefix with ``metadata.object_type == id``."""
    _patch_client(monkeypatch)
    _FakeSchemasClient.response = _FakeResponse(is_success=True, payload={"results": []})

    elements = await _list_provider_elements("hubspot", "crm_object_type", "token")

    assert [el["id"] for el in elements] == _STANDARD_IDS
    for el in elements:
        assert el["group"] == "Standard objects"
        # The id must equal the metadata object_type so the webhook matcher
        # (which compares the subscriptionType prefix) lines up exactly.
        assert el["metadata"]["object_type"] == el["id"]
    # Sanity: the canonical CRM objects are present.
    assert {"contact", "company", "deal", "ticket"} <= set(_STANDARD_IDS)


@pytest.mark.asyncio
async def test_hubspot_custom_objects_keyed_on_object_type_id(monkeypatch):
    """A successful schema call adds custom objects keyed on ``objectTypeId``
    (the custom-object webhook prefix), resolves the display name from
    ``labels.singular`` then ``name``, and skips entries with no id."""
    _patch_client(monkeypatch)
    _FakeSchemasClient.response = _FakeResponse(
        is_success=True,
        payload={
            "results": [
                {
                    "objectTypeId": "2-12345",
                    "labels": {"singular": "Project", "plural": "Projects"},
                    "name": "project",
                    "fullyQualifiedName": "p_99_project",
                },
                {
                    # No labels.singular -> falls back to the raw schema name.
                    "objectTypeId": "2-9999",
                    "name": "Subscription",
                },
                {
                    # Empty objectTypeId must be skipped (no usable webhook key).
                    "objectTypeId": "",
                    "name": "ignored",
                },
            ]
        },
    )

    elements = await _list_provider_elements("hubspot", "crm_object_type", "token")

    by_id = {el["id"]: el for el in elements}
    # Standard objects still present (first), custom objects appended.
    assert [el["id"] for el in elements][: len(_STANDARD_IDS)] == _STANDARD_IDS
    assert "2-12345" in by_id
    assert "2-9999" in by_id
    # Entry with a blank objectTypeId is dropped entirely.
    assert "" not in by_id
    assert all(el["name"] != "ignored" for el in elements)

    project = by_id["2-12345"]
    assert project["group"] == "Custom objects"
    assert project["name"] == "Project"  # labels.singular wins
    assert project["metadata"]["object_type"] == "2-12345"
    assert project["metadata"]["fully_qualified_name"] == "p_99_project"

    # No labels -> the raw schema name is used.
    assert by_id["2-9999"]["name"] == "Subscription"

    # The schema fetch reuses the integration bearer token.
    assert _FakeSchemasClient.captured["url"] == ("https://api.hubapi.com/crm/v3/schemas")
    assert _FakeSchemasClient.captured["headers"]["Authorization"] == "Bearer token"


@pytest.mark.asyncio
async def test_hubspot_degrades_to_standard_on_unauthorized(monkeypatch, caplog):
    """An unauthorized / failed schema call (missing ``crm.schemas.custom.read``)
    must degrade to the standard-only list and log the HTTP status — never
    raise so the picker keeps rendering."""
    _patch_client(monkeypatch)
    _FakeSchemasClient.response = _FakeResponse(is_success=False, payload={}, status_code=403)

    with caplog.at_level(logging.INFO, logger="app.api.v1.integration_elements"):
        elements = await _list_provider_elements("hubspot", "crm_object_type", "token")

    assert [el["id"] for el in elements] == _STANDARD_IDS
    assert any("403" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_hubspot_degrades_to_standard_on_exception(monkeypatch, caplog):
    """A network error / exception during the schema call must be swallowed
    (logged at WARNING) and the standard objects returned."""
    _patch_client(monkeypatch)
    _FakeSchemasClient.raise_exc = RuntimeError("boom")

    with caplog.at_level(logging.WARNING, logger="app.api.v1.integration_elements"):
        elements = await _list_provider_elements("hubspot", "crm_object_type", "token")

    assert [el["id"] for el in elements] == _STANDARD_IDS
    assert any("custom object schemas" in r.getMessage() for r in caplog.records)
