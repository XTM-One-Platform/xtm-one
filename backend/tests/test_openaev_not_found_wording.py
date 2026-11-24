"""A real 404 is recognised by its formatted wording, not by the digits it does not carry.

``format_http_error`` words a 404 as "resource not found" with no status code, so every
``"404" in err`` test missed the errors ``safe_request`` builds from real responses: the
connector instance fallback of ``manage_openaev_connector_instance`` never ran and the
OpenAEV not-found annotations were never added.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.integrations.http import format_http_error, is_not_found_error
from app.integrations.providers.openaev._common import (
    _annotate_cross_tenant_404,
    _annotate_not_found,
)
from tests.test_openaev_package_tools import _build, _FakeResp, _run


def _real_404(action: str) -> str:
    """The error text ``safe_request`` returns for a 404 of OpenAEV."""
    resp = httpx.Response(
        404,
        json={"message": "Connector not found with id: x"},
        request=httpx.Request("GET", "https://aev.example/api/x"),
    )
    return format_http_error(resp, "OpenAEV", action)


def test_the_formatted_404_carries_no_digits_and_is_still_recognised():
    err = _real_404("get asset")
    assert "404" not in err
    assert is_not_found_error(err)
    assert is_not_found_error("OpenAEV error (HTTP 404): Not Found")
    assert not is_not_found_error(None)
    assert not is_not_found_error("")
    assert not is_not_found_error("Error: OpenAEV rejected the request to get asset — bad request.")


def test_openaev_annotations_apply_to_the_formatted_404():
    err = _real_404("get asset")
    assert "three different things" in _annotate_not_found(err)
    scoped = _annotate_cross_tenant_404(err, "/api/tenants/t1/assets/a1")
    assert scoped is not None and "tenant t1" in scoped


class _RealWordingApi:
    """Stub for `_api` answering by path; an unknown path answers OpenAEV's real 404 text."""

    def __init__(self, routes):
        self._routes = routes
        self.calls: list[str] = []

    async def __call__(self, base_url, headers, method, path, action, *, json=None, params=None, **kw):
        self.calls.append(path)
        answer = self._routes.get(path)
        if answer is None:
            return None, _real_404(action)
        return _FakeResp(answer), None


_LOGS = {"content": [{"i": 1}], "totalElements": 1}


@pytest.mark.parametrize("kind", ["", "injector"])
async def test_an_instance_id_as_connector_id_reads_the_instance(monkeypatch, kind):
    from app.integrations.providers.openaev import platform

    executor, _ = _build()
    fake = _RealWordingApi(
        {
            "/api/connector-instances/ci1": {"connector_instance_id": "ci1", "catalog_connector_id": "cc1"},
            "/api/connector-instances/ci1/logs/search": _LOGS,
        }
    )
    monkeypatch.setattr(platform, "_api", fake)
    arguments = {"action": "logs", "connector_id": "ci1"}
    if kind:
        arguments["connector_kind"] = kind
    result = await _run(executor, "manage_openaev_connector_instance", arguments)
    assert not result.is_error, result.content
    assert json.loads(result.content) == {"connector_instance_id": "ci1", "logs": [{"i": 1}]}
    assert fake.calls[-1] == "/api/connector-instances/ci1/logs/search"


async def test_without_kind_a_real_404_moves_on_to_collectors(monkeypatch):
    from app.integrations.providers.openaev import platform

    executor, _ = _build()
    fake = _RealWordingApi(
        {
            "/api/collectors/c1/related-ids": {"connector_instance_id": "ci1", "catalog_connector_id": "cc1"},
            "/api/connector-instances/ci1/logs/search": _LOGS,
        }
    )
    monkeypatch.setattr(platform, "_api", fake)
    result = await _run(executor, "manage_openaev_connector_instance", {"action": "logs", "connector_id": "c1"})
    assert not result.is_error, result.content
    assert fake.calls[:2] == ["/api/injectors/c1/related-ids", "/api/collectors/c1/related-ids"]
