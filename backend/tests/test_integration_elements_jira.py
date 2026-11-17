"""Jira project scope listing: compound cloud_id||token assembly + guards.

The Jira OAuth callback stores the plain access token in ``access_token_enc``
and the Atlassian ``cloud_id`` separately in ``extra_config``. The element
listing endpoint must assemble the compound ``cloud_id||access_token`` form
(mirroring the runtime path in ``services/integration_tools.py``) so the bearer
token is non-empty, and must fail fast / degrade gracefully when the cloud_id
or token is missing instead of building an illegal ``Bearer `` header (which
httpx rejects with ``LocalProtocolError`` -> HTTP 500). Regression coverage.
"""

import pytest
from fastapi import HTTPException

from app.api.v1 import integration_elements
from app.api.v1.integration_elements import _list_provider_elements


def _make_user():
    return type("User", (), {"id": "user-1", "is_admin": False})()


def _make_jira_integration():
    return type(
        "Integration",
        (),
        {
            "provider": "jira",
            "name": "My Jira",
            "access_token_enc": "enc-access",
            "api_key_enc": None,
            "extra_config_enc": "enc-extra",
        },
    )()


@pytest.mark.asyncio
async def test_list_integration_elements_jira_missing_cloud_id_returns_403(monkeypatch):
    """A Jira integration whose extra_config has no cloud_id must raise a clean
    403 (re-authorize) before any provider call - never a 500, and never a 401,
    which the app reads as its own session ending (#11106)."""
    integration = _make_jira_integration()

    async def _fake_get_user_integration(*_args, **_kwargs):
        return integration

    monkeypatch.setattr(
        integration_elements, "get_user_integration", _fake_get_user_integration
    )
    monkeypatch.setattr(integration_elements, "decrypt_value", lambda _v: "access-tok")

    async def _no_extra(_entity, _column):
        return {}

    monkeypatch.setattr(integration_elements, "reveal_json_field", _no_extra)

    provider_called = False

    async def _fake_list_provider_elements(*_args, **_kwargs):
        nonlocal provider_called
        provider_called = True
        return []

    monkeypatch.setattr(
        integration_elements, "_list_provider_elements", _fake_list_provider_elements
    )

    with pytest.raises(HTTPException) as exc:
        await integration_elements.list_integration_elements(
            integration_id="int-1",
            element_type="jira_project",
            user=_make_user(),
            db=None,
        )

    assert exc.value.status_code == 403
    assert "cloud_id" in str(exc.value.detail)
    assert provider_called is False


@pytest.mark.asyncio
async def test_list_integration_elements_jira_assembles_compound_token(monkeypatch):
    """With a cloud_id present, the endpoint must hand the provider listing the
    compound ``cloud_id||access_token`` token (not the bare access token)."""
    integration = _make_jira_integration()

    async def _fake_get_user_integration(*_args, **_kwargs):
        return integration

    monkeypatch.setattr(
        integration_elements, "get_user_integration", _fake_get_user_integration
    )
    monkeypatch.setattr(integration_elements, "decrypt_value", lambda _v: "access-tok")

    async def _extra(_entity, _column):
        return {"cloud_id": "cloud-123"}

    monkeypatch.setattr(integration_elements, "reveal_json_field", _extra)

    captured = {}

    async def _fake_list_provider_elements(provider, element_type, token):
        captured["provider"] = provider
        captured["element_type"] = element_type
        captured["token"] = token
        return [{"id": "PROJ"}]

    monkeypatch.setattr(
        integration_elements, "_list_provider_elements", _fake_list_provider_elements
    )

    result = await integration_elements.list_integration_elements(
        integration_id="int-1",
        element_type="jira_project",
        user=_make_user(),
        db=None,
    )

    assert captured["token"] == "cloud-123||access-tok"
    assert captured["provider"] == "jira"
    assert result["elements"] == [{"id": "PROJ"}]


@pytest.mark.asyncio
async def test_list_provider_elements_jira_plain_token_returns_empty():
    """A plain token without the ``||`` delimiter (cloud_id parses to the whole
    token, jira_token empty) must degrade to [] - never build an empty Bearer
    header / crash."""
    elements = await _list_provider_elements(
        "jira", "jira_project", "plain-token-no-delimiter"
    )
    assert elements == []


class _FakeJiraResponse:
    status_code = 200

    def json(self):
        return {"values": [], "isLast": True}


class _FakeJiraClient:
    captured: dict = {}

    def __init__(self, *_args, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, url, headers=None, params=None):
        _FakeJiraClient.captured = {"url": url, "headers": headers, "params": params}
        return _FakeJiraResponse()


@pytest.mark.asyncio
async def test_list_provider_elements_jira_normalizes_whitespace_in_header(monkeypatch):
    """A compound token whose halves carry leading/trailing whitespace must
    still build a clean ``Authorization: Bearer <token>`` header and base URL
    (no whitespace/control chars), so httpx never raises LocalProtocolError."""
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _FakeJiraClient)
    _FakeJiraClient.captured = {}

    elements = await _list_provider_elements(
        "jira", "jira_project", "  cloud-123  ||  access-tok\n"
    )

    # No projects in the fake response, but importantly no crash and a clean
    # header / URL built from the stripped values.
    assert elements == []
    assert _FakeJiraClient.captured["headers"]["Authorization"] == "Bearer access-tok"
    assert _FakeJiraClient.captured["url"].startswith(
        "https://api.atlassian.com/ex/jira/cloud-123/"
    )
