"""The LM Studio console routes without a database: their gates, their route, their answers to failures.

Who may open the console, the platform provider it feeds and the provider's
own resolution run on a real PostgreSQL in ``test_lmstudio_console_postgres.py``;
the client in ``test_lmstudio_client.py``.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from pydantic import ValidationError

from app.api.deps import require_admin, require_ai_enabled
from app.api.v1 import lmstudio as console
from app.api.v1.schemas.lmstudio import DownloadRequest, LinkProviderRequest, LoadRequest, UnloadRequest
from app.core.egress import current_route
from app.integrations.providers import lmstudio as client

_ADMIN = SimpleNamespace(id=uuid.uuid4(), role="admin", is_admin=True)
_MEMBER = SimpleNamespace(id=uuid.uuid4(), role="user", is_admin=False)
_ROUTE = SimpleNamespace(group_id=uuid.uuid4(), name="the relay route")


def _dependency_calls(route: APIRoute) -> set:
    calls, stack = set(), list(route.dependant.dependencies)
    while stack:
        dependency = stack.pop()
        calls.add(dependency.call)
        stack.extend(dependency.dependencies)
    return calls


def _routes() -> list[APIRoute]:
    return [route for route in console.router.routes if isinstance(route, APIRoute)]


def test_every_console_route_reaches_the_server_only_with_ai_on():
    routes = _routes()
    assert len(routes) == 9
    for route in routes:
        assert require_ai_enabled in _dependency_calls(route), route.path


def test_feeding_the_platform_provider_is_for_admins():
    admin_only = {
        (method, route.path)
        for route in _routes()
        for method in route.methods
        if require_admin in _dependency_calls(route)
    }
    assert admin_only == {
        ("POST", "/{integration_id}/lmstudio/llm-provider"),
        ("PUT", "/{integration_id}/lmstudio/llm-provider/{config_id}/models"),
        ("DELETE", "/{integration_id}/lmstudio/llm-provider/{config_id}"),
    }


def _integration(*, routed: bool = True, company: bool = True, owner=None) -> SimpleNamespace:
    from app.core.security import encrypt_value

    return SimpleNamespace(
        id=uuid.uuid4(),
        provider="lmstudio",
        name="GPU box",
        user_id=None if company else (owner.id if owner else uuid.uuid4()),
        is_company_managed=company,
        relay_group_id=uuid.uuid4() if routed else None,
        api_key_enc=None,
        extra_config_enc=encrypt_value(json.dumps({"base_url": "http://gpu-host:1234", "api_token": "lm-token"})),
    )


class _Db:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


@pytest.fixture
def console_of(monkeypatch):
    """The console on ``integration``: ``_load`` answers it, the route is a marker, audits are recorded."""
    audits: list[dict] = []

    def _install(integration):
        async def _load(db, user, integration_id):
            return integration

        async def _route(db, row):
            assert row is integration
            return _ROUTE if row.relay_group_id is not None else None

        async def _audit(db, **kwargs):
            audits.append(kwargs)

        async def _no_configs(db, integration_id):
            return []

        monkeypatch.setattr(console, "_load", _load)
        monkeypatch.setattr("app.services.relays.integration_egress_route", _route)
        monkeypatch.setattr(console, "_audit", _audit)
        monkeypatch.setattr("app.services.integration_llm_provider.linked_configs", _no_configs)
        return audits

    return _install


@pytest.mark.parametrize(
    ("kind", "status"),
    [
        ("auth", 424),
        ("unsupported", 424),
        ("invalid_request", 424),
        ("unavailable", 424),
        ("invalid_response", 424),
        ("relay_no_relay_online", 424),
        ("not_found", 404),
    ],
)
async def test_a_failure_is_answered_with_its_fixed_sentence(monkeypatch, console_of, kind, status):
    console_of(_integration())

    async def _refused(connection):
        raise client.LMStudioError(kind, "The fixed sentence.", detail="upstream: tenant acme, host internal.corp")

    monkeypatch.setattr(client, "list_models", _refused)
    with pytest.raises(HTTPException) as raised:
        await console.get_models("x", user=_ADMIN, db=_Db(), _ai_gate=None)
    # Never 401 (the web client would sign the user out) nor 502 (it would
    # show the backend as down), and never the upstream's own words.
    assert raised.value.status_code == status
    assert raised.value.detail == {"code": f"lmstudio_{kind}", "message": "The fixed sentence."}


async def test_every_server_call_runs_on_the_integration_s_relay_route(monkeypatch, console_of):
    console_of(_integration())
    routes: list = []

    async def _list(connection):
        routes.append(("list", current_route(), connection))
        return []

    async def _load_model(connection, model, **options):
        routes.append(("load", current_route(), options))
        return {"instance_id": model, "type": "llm", "load_time_seconds": 1.0}

    async def _unload(connection, instance_id):
        routes.append(("unload", current_route()))
        return instance_id

    async def _download(connection, model, *, quantization=None):
        routes.append(("download", current_route(), quantization))
        return {"job_id": "job_1", "status": "downloading"}

    async def _status(connection, job_id):
        routes.append(("status", current_route()))
        return {"job_id": job_id, "status": "completed"}

    for name, fake in (("list_models", _list), ("load_model", _load_model), ("unload_model", _unload),
                       ("download_model", _download), ("download_status", _status)):
        monkeypatch.setattr(client, name, fake)

    db = _Db()
    await console.get_models("x", user=_ADMIN, db=db, _ai_gate=None)
    await console.load_model("x", LoadRequest(model="openai/gpt-oss-20b", context_length=8192), None, user=_ADMIN, db=db, _ai_gate=None)
    await console.unload_model("x", UnloadRequest(instance_id="openai/gpt-oss-20b"), None, user=_ADMIN, db=db, _ai_gate=None)
    await console.start_download("x", DownloadRequest(model="openai/gpt-oss-20b"), None, user=_ADMIN, db=db, _ai_gate=None)
    await console.get_download("x", "job_1", user=_ADMIN, db=db, _ai_gate=None)

    assert [entry[0] for entry in routes] == ["list", "load", "unload", "download", "status"]
    assert all(entry[1] is _ROUTE for entry in routes)
    connection = routes[0][2]
    assert (connection.base_url, connection.token) == ("http://gpu-host:1234", "lm-token")
    assert routes[1][2] == {"context_length": 8192}
    assert routes[3][2] is None
    # Outside the calls, nothing stays routed.
    assert current_route() is None


async def test_an_unrouted_integration_calls_its_server_directly(monkeypatch, console_of):
    console_of(_integration(routed=False))
    sent: list = []

    async def _list(connection):
        sent.append((current_route(), connection.base_url))
        return []

    monkeypatch.setattr(client, "list_models", _list)
    await console.get_models("x", user=_ADMIN, db=_Db(), _ai_gate=None)
    assert sent == [(None, "http://gpu-host:1234")]


async def test_changing_the_server_is_for_its_holder_and_is_audited(monkeypatch, console_of):
    owner = SimpleNamespace(id=uuid.uuid4(), role="user", is_admin=False)
    loads: list = []

    async def _load_model(connection, model, **options):
        loads.append(model)
        return {"instance_id": f"{model}:2", "type": "llm", "load_time_seconds": 2.5}

    monkeypatch.setattr(client, "load_model", _load_model)
    body = LoadRequest(model="qwen/qwen3-4b", flash_attention=True)

    # A company-managed server is an admin's; a member who manages the
    # integration (a config grantee) reads it, and changes nothing.
    audits = console_of(_integration(company=True))
    with pytest.raises(HTTPException) as raised:
        await console.load_model("x", body, None, user=_MEMBER, db=_Db(), _ai_gate=None)
    assert raised.value.status_code == 403
    # A personal server is its owner's (and an admin's), not a grantee's.
    console_of(_integration(company=False, owner=owner))
    with pytest.raises(HTTPException):
        await console.load_model("x", body, None, user=_MEMBER, db=_Db(), _ai_gate=None)
    assert loads == []

    db = _Db()
    loaded = await console.load_model("x", body, None, user=owner, db=db, _ai_gate=None)
    assert (loaded.instance_id, loads, db.commits) == ("qwen/qwen3-4b:2", ["qwen/qwen3-4b"], 1)
    [audit] = audits
    assert audit["user"] is owner
    assert audit["summary"] == "Loaded model 'qwen/qwen3-4b' on LM Studio integration 'GPU box'"
    assert audit["details"] == {"model": "qwen/qwen3-4b", "instance_id": "qwen/qwen3-4b:2", "flash_attention": True}


async def test_a_job_id_that_is_not_one_is_an_unknown_download(monkeypatch, console_of):
    console_of(_integration())

    async def _status(connection, job_id):
        raise AssertionError("never sent")

    monkeypatch.setattr(client, "download_status", _status)
    with pytest.raises(HTTPException) as raised:
        await console.get_download("x", "..%2Fmodels", user=_ADMIN, db=_Db(), _ai_gate=None)
    assert raised.value.status_code == 404


# ---------------------------------------------------------------------------
# Bodies: an invalid one is a 422, never a 500
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (LoadRequest, {"model": "../x"}),
        (LoadRequest, {"model": "openai/gpt-oss-20b", "context_length": 0}),
        (LoadRequest, {"model": "openai/gpt-oss-20b", "eval_batch_size": 10**9}),
        (LoadRequest, {"model": "openai/gpt-oss-20b", "ttl": 5}),
        (LoadRequest, {"model": ""}),
        (LoadRequest, {"model": None}),
        (UnloadRequest, {"instance_id": "a b"}),
        (DownloadRequest, {"model": "https://evil.example/m"}),
        (DownloadRequest, {"model": "openai/gpt-oss-20b", "quantization": "Q4_K_M"}),
        (DownloadRequest, {"model": "https://huggingface.co/a/b", "quantization": "Q4 K"}),
        (LinkProviderRequest, {"available_models": ["a", "a"]}),
        (LinkProviderRequest, {"available_models": ["a"], "default_model": "b"}),
        (LinkProviderRequest, {"provider_config_id": str(uuid.uuid4()), "available_models": ["a"]}),
    ],
)
def test_a_body_that_is_not_valid_is_refused_by_its_model(model, payload):
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_the_server_url_is_shown_and_the_token_is_a_redacted_extra_field():
    from app.api.v1.integration_catalog import PROVIDER_CATALOG
    from app.api.v1.integrations import _is_sensitive_setting_name

    entry = PROVIDER_CATALOG["lmstudio"]
    # No credential field: the token is optional, so the integration is
    # connected once its URL is set (``auth_type`` ``none``), directly or
    # through a relay group.
    assert (entry["auth_type"], entry["fields"], entry.get("requires_relay", False)) == ("none", [], False)
    assert list(entry["extra_fields"]) == [client.BASE_URL_FIELD, client.TOKEN_FIELD]
    assert not _is_sensitive_setting_name(client.BASE_URL_FIELD)
    assert _is_sensitive_setting_name(client.TOKEN_FIELD)
