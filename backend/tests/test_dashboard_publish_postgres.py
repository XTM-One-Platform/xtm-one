"""Exporting and publishing a custom dashboard, through the real routes, against PostgreSQL.

Export writes the file each platform imports from the stored dashboard - only
what the format holds, the rest counted (``X-Left-Out``). Publish sends that
file through an integration the caller can use, with their own access there
(OpenCTI ``workspaceConfigurationImport`` as a GraphQL multipart upload,
OpenAEV ``/custom-dashboards/import``, tenant-scoped when the integration
names a tenant), records what the platform answered, and says nothing of what
a platform refused beyond a fixed sentence. OpenCRQ is never a target.

The platforms are an ``httpx.MockTransport``; ``_get_token`` is stubbed per
integration. ``integration``-marked; the database is ``tests/scratch_database.py``.
"""

from __future__ import annotations

import base64
import io
import json
import re
import uuid
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import custom_dashboards
from app.core.database import get_db
from app.core.security import encrypt_value
from app.models.custom_dashboard import CustomDashboard
from app.models.integration import Integration
from app.models.user import User
from app.services import dashboard_publish
from app.services.custom_dashboard_spec import validate_spec
from app.services.dashboard_live import _http
from tests.scratch_database import ALL_TABLES, scratch_database

# Every dashboard route is behind DASHBOARD_STUDIO: on for the module.
pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("dashboard_studio_enabled")]

_CTI_NATIVE = {"type": "number", "perspective": "entities", "dataSelection": [{}]}
_AEV_NATIVE = {
    "widget_type": "number",
    "widget_config": {"widget_configuration_type": "flat", "series": [{"name": "Injects", "filter": {}}]},
}


@asynccontextmanager
async def _client(session_factory, user_id: uuid.UUID) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(custom_dashboards.router, prefix="/api/v1/custom-dashboards")

    async def _get_db():
        async with session_factory() as db:
            yield db
            await db.commit()

    async def _signed_in(db: AsyncSession = Depends(get_db)) -> User:
        return await db.get(User, user_id)

    for dependency in (deps.get_current_user, deps.get_current_user_ungated):
        app.dependency_overrides[dependency] = _signed_in
    app.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.fixture
def platform(monkeypatch):
    seen: list[httpx.Request] = []
    answers: dict[str, httpx.Response] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answers.get(request.url.host, httpx.Response(500, json={}))

    async def token(db, integration_id, provider="", *, user_id=None, integ=None, **_kw):
        return f"https://{integ.name}.example||key-{integ.name}"

    async def no_limit(_user):
        return None

    monkeypatch.setattr(_http, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr("app.services.integration_tools._get_token", token)
    monkeypatch.setattr(dashboard_publish, "_enforce_rate", no_limit)
    return seen, answers


@asynccontextmanager
async def _world():
    async with scratch_database(
        "dashboard_publish", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            owner = User(email="owner@example.com", hashed_password="x", full_name="Owner", role="user")
            reader = User(email="reader@example.com", hashed_password="x", full_name="Reader", role="user")
            db.add_all([owner, reader])
            await db.flush()
            cti = Integration(name="cti", provider="opencti", auth_type="api_key", user_id=owner.id)
            aev = Integration(
                name="aev", provider="openaev", auth_type="api_key", user_id=owner.id,
                extra_config_enc=encrypt_value(json.dumps({"tenant_id": "t-1"})),
            )  # fmt: skip
            reader_cti = Integration(name="readercti", provider="opencti", auth_type="api_key", user_id=reader.id)
            db.add_all([cti, aev, reader_cti])
            await db.flush()
            spec = validate_spec(
                {
                    "title": "Threat board",
                    "subtitle": "Weekly",
                    "widgets": [
                        {"kind": "text", "id": "note", "body": "Read me"},
                        {"kind": "platform", "id": "reports", "source": "opencti", "integration_id": str(cti.id), "native": _CTI_NATIVE},
                        {"kind": "platform", "id": "injects", "source": "openaev", "integration_id": str(aev.id), "native": _AEV_NATIVE},
                        {"kind": "score", "id": "score", "label": "Exposure", "value": 58},
                    ],
                }
            )
            dashboard = CustomDashboard(
                user_id=owner.id, title="Threat board", widget_count=4, revision=3, spec=spec,
                shared_user_ids=[str(reader.id)], shared_user_permissions={str(reader.id): "read_only"},
            )  # fmt: skip
            db.add(dashboard)
            await db.commit()
            ids = {
                "owner": owner.id, "reader": reader.id, "cti": cti.id, "aev": aev.id,
                "reader_cti": reader_cti.id, "dashboard": dashboard.id,
            }  # fmt: skip
        yield scratch, ids


async def test_each_export_holds_what_its_platform_draws(dashboard_studio_enabled, platform) -> None:
    async with _world() as (scratch, ids), _client(scratch.sessions, ids["owner"]) as client:
        url = f"/api/v1/custom-dashboards/{ids['dashboard']}/export"
        cti = await client.get(url, params={"format": "opencti"})
        aev = await client.get(url, params={"format": "openaev"})
        pack = await client.get(url, params={"format": "xtm"})
        unknown = await client.get(url, params={"format": "opencrq"})
    assert cti.status_code == 200
    assert (cti.headers["x-left-out"], cti.headers["content-disposition"]) == ("2", 'attachment; filename="Threat board.json"')
    manifest = json.loads(base64.b64decode(cti.json()["configuration"]["manifest"]))
    assert sorted(w["type"] for w in manifest["widgets"].values()) == ["number", "text"]
    assert aev.headers["x-left-out"] == "3"
    with zipfile.ZipFile(io.BytesIO(aev.content)) as archive:
        document = json.loads(archive.read("custom_dashboards.json"))
    assert [r["attributes"]["widget_type"] for r in document["included"]] == ["number"]
    assert all("integration_id" not in w for w in pack.json()["widgets"])
    assert unknown.status_code == 422
    assert platform[0] == []


async def test_publishing_to_opencti_uploads_the_export_with_the_callers_access(dashboard_studio_enabled, platform) -> None:
    seen, answers = platform
    answers["cti.example"] = httpx.Response(200, json={"data": {"workspaceConfigurationImport": "ws-42"}})
    async with _world() as (scratch, ids), _client(scratch.sessions, ids["owner"]) as client:
        published = await client.post(
            f"/api/v1/custom-dashboards/{ids['dashboard']}/publish",
            json={"target": "opencti", "integration_id": str(ids["cti"]), "name": "Weekly threats"},
        )
        listed = await client.get(f"/api/v1/custom-dashboards/{ids['dashboard']}/publications")
    assert published.status_code == 200, published.text
    body = published.json()
    assert body["publication"]["remote_url"] == "https://cti.example/dashboard/workspaces/dashboards/ws-42"
    assert (body["publication"]["revision"], len(body["notes"])) == (3, 2)
    [request] = seen
    assert (request.url.path, request.headers["authorization"]) == ("/graphql", "Bearer key-cti")
    assert request.headers["content-type"].startswith("multipart/form-data")
    raw = request.content.decode("utf-8", "replace")
    assert "workspaceConfigurationImport" in raw and '{"0": ["variables.file"]}' in raw
    assert '"name": "Weekly threats"' in raw
    assert [p["remote_id"] for p in listed.json()] == ["ws-42"]


async def test_publishing_to_openaev_goes_to_the_integrations_tenant(dashboard_studio_enabled, platform) -> None:
    seen, answers = platform
    answers["aev.example"] = httpx.Response(200, json={"data": {"id": "cd-7", "type": "custom_dashboards"}})
    async with _world() as (scratch, ids), _client(scratch.sessions, ids["owner"]) as client:
        published = await client.post(
            f"/api/v1/custom-dashboards/{ids['dashboard']}/publish",
            json={"target": "openaev", "integration_id": str(ids["aev"])},
        )
    assert published.status_code == 200, published.text
    assert published.json()["publication"]["remote_url"] == "https://aev.example/admin/workspaces/custom_dashboards/cd-7"
    assert seen[0].url.path == "/api/tenants/t-1/custom-dashboards/import"


async def _custom_view_board(scratch, ids, *, with_input: bool = True) -> uuid.UUID:
    """A dashboard whose OpenCTI number reads the $entity$ an entity input of Country type sets."""
    from app.services.dashboard_definition import upgrade, validate_definition

    native = {
        "type": "number",
        "perspective": "entities",
        "dataSelection": [{"filters": {"mode": "and", "filters": [{"key": ["objects"], "values": ["$entity$"], "operator": "eq", "mode": "or"}], "filterGroups": []}}],
    }
    spec = validate_spec(
        {
            "title": "Country view",
            "widgets": [
                {"kind": "platform", "id": "about", "source": "opencti", "integration_id": str(ids["cti"]), "native": native, "range": "3m"},
                {"kind": "platform", "id": "all", "source": "opencti", "integration_id": str(ids["cti"]), "native": _CTI_NATIVE},
            ],
        }
    )
    definition = upgrade(spec)
    if with_input:
        definition["inputs"]["input_entity"] = {
            "type": "input.entity", "title": "Country", "token": "entity",
            "options": {"source": "opencti", "integration_id": str(ids["cti"]), "entityTypes": ["Country"]},
        }  # fmt: skip
    async with scratch.sessions() as db:
        board = CustomDashboard(user_id=ids["owner"], title="Country view", widget_count=2, revision=1, spec=validate_definition(definition))
        db.add(board)
        await db.commit()
        return board.id


async def test_a_dashboard_reading_an_entity_input_goes_to_opencti_as_a_custom_view(dashboard_studio_enabled, platform) -> None:
    seen, answers = platform
    answers["cti.example"] = httpx.Response(200, json={"data": {"customViewConfigurationImport": {"id": "cv-9"}}})
    async with _world() as (scratch, ids):
        board = await _custom_view_board(scratch, ids)
        bare = await _custom_view_board(scratch, ids, with_input=False)
        async with _client(scratch.sessions, ids["owner"]) as client:
            exported = await client.get(f"/api/v1/custom-dashboards/{board}/export", params={"format": "opencti-custom-view"})
            as_dashboard = await client.get(f"/api/v1/custom-dashboards/{board}/export", params={"format": "opencti"})
            no_input = await client.get(f"/api/v1/custom-dashboards/{bare}/export", params={"format": "opencti-custom-view"})
            published = await client.post(
                f"/api/v1/custom-dashboards/{board}/publish", json={"target": "opencti-custom-view", "integration_id": str(ids["cti"])}
            )
    assert exported.status_code == 200, exported.text
    document = exported.json()
    assert (document["type"], document["configuration"]["target_entity_type"]) == ("custom-view", "Country")
    manifest = json.loads(base64.b64decode(document["configuration"]["manifest"]))
    assert manifest["config"] == {"relativeDate": "months-3", "startDate": None, "endDate": None}
    filters = [w["dataSelection"][0]["filters"]["filters"] for w in manifest["widgets"].values()]
    assert [{"key": ["objects"], "values": ["SELF_ID"], "operator": "eq", "mode": "or"}] in filters
    assert "$entity$" not in exported.text
    # As a dashboard, the entity input is one of its variables: the widget reading it is kept, its filter
    # naming the variable as OpenCTI does ($var:<id>), never with a $entity$ filter.
    dashboard_manifest = json.loads(base64.b64decode(as_dashboard.json()["configuration"]["manifest"]))
    assert (len(dashboard_manifest["widgets"]), "$entity$" in as_dashboard.text) == (2, False)
    [variable] = dashboard_manifest["variables"]
    assert (variable["type"], variable["entityTypes"]) == ("entity", ["Country"])
    assert f"$var:{variable['id']}" in json.dumps(dashboard_manifest["widgets"])
    assert no_input.status_code == 422 and "no OpenCTI entity input" in no_input.json()["detail"]
    assert published.status_code == 200, published.text
    assert published.json()["publication"]["remote_url"] == "https://cti.example/dashboard/settings/customization/entity_types/Country/custom-views/cv-9"
    raw = seen[0].content.decode("utf-8", "replace")
    assert "customViewConfigurationImport" in raw and '"targetEntityType": "Country"' in raw


async def test_a_saved_filter_leaves_written_in_place_read_with_the_callers_access(dashboard_studio_enabled, platform) -> None:
    from app.services.dashboard_widget_packs import pack_from_dashboard

    seen, answers = platform
    saved = {"mode": "and", "filters": [{"key": ["entity_type"], "values": ["Report"], "operator": "eq", "mode": "or"}], "filterGroups": []}
    answers["cti.example"] = httpx.Response(200, json={"data": {"savedFilter": {"filters": json.dumps(saved)}}})
    native = {"type": "number", "perspective": "entities", "dataSelection": [{"filters_id": "sf-1"}]}
    async with _world() as (scratch, ids):
        spec = validate_spec(
            {"title": "Saved", "widgets": [{"kind": "platform", "id": "saved", "title": "Reports", "source": "opencti", "integration_id": str(ids["cti"]), "native": native}]}
        )
        async with scratch.sessions() as db:
            board = CustomDashboard(
                user_id=ids["owner"], title="Saved", widget_count=1, revision=1, spec=spec,
                shared_user_ids=[str(ids["reader"])], shared_user_permissions={str(ids["reader"]): "read_only"},
            )  # fmt: skip
            db.add(board)
            await db.commit()
        async with _client(scratch.sessions, ids["owner"]) as client:
            owned = await client.get(f"/api/v1/custom-dashboards/{board.id}/export", params={"format": "opencti"})
        async with _client(scratch.sessions, ids["reader"]) as client:
            # The reader cannot use the owner's integration: the filter keeps its id, and the file says so.
            read = await client.get(f"/api/v1/custom-dashboards/{board.id}/export", params={"format": "xtm"})
        async with scratch.sessions() as db:
            pack = await pack_from_dashboard(db, user_id=ids["owner"], dashboard=await db.get(CustomDashboard, board.id), name="Saved")
            packed = pack.widgets[0]["native"]["dataSelection"][0]
    manifest = json.loads(base64.b64decode(owned.json()["configuration"]["manifest"]))
    [selection] = next(iter(manifest["widgets"].values()))["dataSelection"]
    assert ("filters_id" in selection, selection["filters"], owned.headers["x-left-out"]) == (False, saved, "0")
    assert ("filters_id" in packed, packed["filters"]) == (False, saved)
    assert [(r.url.path, r.headers["authorization"], json.loads(r.content)["variables"]) for r in seen] == [
        ("/graphql", "Bearer key-cti", {"id": "sf-1"}),
        ("/graphql", "Bearer key-cti", {"id": "sf-1"}),
    ]
    kept = read.json()["widgets"][0]["native"]["dataSelection"][0]
    assert (kept["filters_id"], kept["filters"]["filters"], read.headers["x-left-out"]) == ("sf-1", [], "1")


async def _saved_board(scratch, ids) -> uuid.UUID:
    native = {"type": "number", "perspective": "entities", "dataSelection": [{"filters_id": "sf-1"}]}
    spec = validate_spec(
        {"title": "Saved", "widgets": [{"kind": "platform", "id": "saved", "title": "Reports", "source": "opencti", "integration_id": str(ids["cti"]), "native": native}]}
    )
    async with scratch.sessions() as db:
        board = CustomDashboard(user_id=ids["owner"], title="Saved", widget_count=1, revision=1, spec=spec)
        db.add(board)
        await db.commit()
        return board.id


async def test_past_the_read_budget_a_saved_filter_keeps_its_id_and_nothing_is_read(dashboard_studio_enabled, platform, monkeypatch) -> None:
    seen, _answers = platform

    async def _spent(_user_id):
        return False

    monkeypatch.setattr(dashboard_publish, "_saved_filter_budget", _spent)
    async with _world() as (scratch, ids):
        board = await _saved_board(scratch, ids)
        async with _client(scratch.sessions, ids["owner"]) as client:
            exported = await client.get(f"/api/v1/custom-dashboards/{board}/export", params={"format": "xtm"})
    kept = exported.json()["widgets"][0]["native"]["dataSelection"][0]
    assert (exported.status_code, kept["filters_id"], exported.headers["x-left-out"], seen) == (200, "sf-1", "1", [])


async def test_a_publication_past_its_budget_reads_nothing(dashboard_studio_enabled, platform, monkeypatch) -> None:
    seen, _answers = platform

    async def _refused(_user_id):
        raise dashboard_publish.PublishError("Too many publications in a minute: try again shortly.")

    monkeypatch.setattr(dashboard_publish, "_enforce_rate", _refused)
    async with _world() as (scratch, ids):
        board = await _saved_board(scratch, ids)
        async with _client(scratch.sessions, ids["owner"]) as client:
            refused = await client.post(f"/api/v1/custom-dashboards/{board}/publish", json={"target": "opencti", "integration_id": str(ids["cti"])})
    # Not even the saved filter is read: the budget comes first.
    assert (refused.status_code, refused.json()["detail"], seen) == (422, "Too many publications in a minute: try again shortly.", [])


@pytest.mark.parametrize(
    ("answer", "detail"),
    [
        (httpx.Response(403, json={}), "OpenCTI refused: your account may not create dashboards there."),
        (httpx.Response(200, json={"errors": [{"message": "secret detail"}], "data": None}), "OpenCTI could not import the dashboard."),
        (httpx.Response(200, json={"data": {"workspaceConfigurationImport": "<script>"}}), "OpenCTI did not say which dashboard it created."),
    ],
    ids=["forbidden", "graphql-error", "bad-id"],
)
async def test_a_refusal_is_a_fixed_sentence_and_records_nothing(dashboard_studio_enabled, platform, answer, detail) -> None:
    _seen, answers = platform
    answers["cti.example"] = answer
    async with _world() as (scratch, ids), _client(scratch.sessions, ids["owner"]) as client:
        refused = await client.post(
            f"/api/v1/custom-dashboards/{ids['dashboard']}/publish",
            json={"target": "opencti", "integration_id": str(ids["cti"])},
        )
        listed = await client.get(f"/api/v1/custom-dashboards/{ids['dashboard']}/publications")
    assert (refused.status_code, refused.json()["detail"]) == (422, detail)
    assert "secret detail" not in refused.text
    assert listed.json() == []


async def test_a_reader_publishes_with_their_own_integration_never_the_owners(dashboard_studio_enabled, platform) -> None:
    seen, answers = platform
    answers["readercti.example"] = httpx.Response(200, json={"data": {"workspaceConfigurationImport": "ws-9"}})
    async with _world() as (scratch, ids), _client(scratch.sessions, ids["reader"]) as client:
        borrowed = await client.post(
            f"/api/v1/custom-dashboards/{ids['dashboard']}/publish",
            json={"target": "opencti", "integration_id": str(ids["cti"])},
        )
        own = await client.post(
            f"/api/v1/custom-dashboards/{ids['dashboard']}/publish",
            json={"target": "opencti", "integration_id": str(ids["reader_cti"])},
        )
    assert (borrowed.status_code, borrowed.json()["detail"]) == (422, "The OpenCTI integration is not one you can use.")
    assert own.status_code == 200
    assert [r.url.host for r in seen] == ["readercti.example"]


async def test_no_target_but_opencti_and_openaev_and_nothing_while_the_studio_is_off(platform, monkeypatch) -> None:
    from app.core.config import settings

    async with _world() as (scratch, ids), _client(scratch.sessions, ids["owner"]) as client:
        monkeypatch.setattr(settings, "enabled_features", "DASHBOARD_STUDIO")
        crq = await client.post(
            f"/api/v1/custom-dashboards/{ids['dashboard']}/publish",
            json={"target": "opencrq", "integration_id": str(ids["cti"])},
        )
        monkeypatch.setattr(settings, "enabled_features", "")
        off = await client.post(
            f"/api/v1/custom-dashboards/{ids['dashboard']}/publish",
            json={"target": "opencti", "integration_id": str(ids["cti"])},
        )
    assert crq.status_code == 422
    assert off.status_code == 404


# ── Share on the XTM Hub ─────────────────────────────────────────────────────

_HUB_TOKEN = "xhp_" + "a" * 43
_LIBRARY = "11111111-2222-4333-8444-555555555555"
_DOCUMENT = "66666666-7777-4888-8999-000000000000"
_UPLOADER = "99999999-8888-4777-8666-555555555555"


class _FakeHub:
    """The XTM Hub's GraphQL: the queries the share asks, in order."""

    def __init__(self, *, upload: bool = True) -> None:
        self.calls: list[tuple[str, dict, str | None, tuple | None]] = []
        self.upload = upload

    async def __call__(self, base_url, query, variables, action, *, api_token=None, upload=None):
        self.calls.append((re.search(r"(?:query|mutation)\s+(\w+)", query)[1], variables, api_token, upload))
        capabilities = ["UPLOAD"] if self.upload else []
        library = {"id": _LIBRARY, "capabilities": capabilities, "service_definition": {"identifier": "opencti_custom_dashboards"}}
        if "XtmOneHubLibraries" in query:
            return {"serviceInstances": {"edges": [{"node": library}]}}, None
        if "XtmOneHubLibrary(" in query:
            return {"serviceInstanceById": library}, None
        if "XtmOneHubUploader" in query:
            return {"me": {"id": _UPLOADER}}, None
        if "XtmOneHubPublish" in query:
            return {"createDocument": {"id": _DOCUMENT, "name": variables["input"]["name"], "status": "draft"}}, None
        return None, "Error: unexpected query"


@pytest.fixture
def hub(monkeypatch):
    import app.integrations.providers.xtm_hub as hub_module

    fake = _FakeHub()

    async def token(db, integration_id, provider="", *, user_id=None, integ=None, **_kw):
        return f"http://localhost:3002||{_HUB_TOKEN}"

    async def tokens_accepted(_base_url):
        return None

    async def no_limit(_user):
        return None

    monkeypatch.setattr(hub_module, "xtm_hub_graphql", fake)
    monkeypatch.setattr(hub_module, "_token_support_error", tokens_accepted)
    monkeypatch.setattr("app.services.integration_tools._get_token", token)
    monkeypatch.setattr(dashboard_publish, "_enforce_rate", no_limit)
    return fake


async def _add_hub(scratch, owner: uuid.UUID) -> uuid.UUID:
    async with scratch.sessions() as db:
        row = Integration(name="Hub", provider="xtm_hub", auth_type="api_key", user_id=owner)
        db.add(row)
        await db.commit()
        return row.id


async def test_sharing_on_the_hub_publishes_the_opencti_export_as_an_inactive_resource(dashboard_studio_enabled, hub) -> None:
    async with _world() as (scratch, ids):
        hub_id = await _add_hub(scratch, ids["owner"])
        async with _client(scratch.sessions, ids["owner"]) as client:
            shared = await client.post(
                f"/api/v1/custom-dashboards/{ids['dashboard']}/share-on-hub",
                json={"integration_id": str(hub_id), "short_description": "Weekly threat figures"},
            )
            listed = await client.get(f"/api/v1/custom-dashboards/{ids['dashboard']}/publications")
    assert shared.status_code == 200, shared.text
    body = shared.json()
    assert body["publication"]["target"] == "xtm_hub"
    assert body["publication"]["remote_url"] == (
        f"http://localhost:3002/app/service/opencti_custom_dashboards/{_LIBRARY}/{_DOCUMENT}"
    )
    # Only the OpenCTI widget and the text travel; the rest is listed.
    assert len(body["notes"]) == 2
    assert [name for name, *_ in hub.calls] == [
        "XtmOneHubLibraries", "XtmOneHubLibrary", "XtmOneHubUploader", "XtmOneHubPublish",
    ]  # fmt: skip
    _name, variables, token, upload = hub.calls[-1]
    assert token == _HUB_TOKEN
    document = variables["input"]
    assert (document["name"], document["short_description"], document["status"], document["uploader_id"]) == (
        "Threat board", "Weekly threat figures", "draft", _UPLOADER,
    )  # fmt: skip
    assert document["meta_description"] == "Weekly threat figures"
    assert "active" not in document
    assert document["slug"].startswith("threat-board-")
    assert variables["metadata"] == [{"key": "product_version", "value": "6.0.0"}]
    sent = json.loads(upload[2])
    assert (sent["type"], sent["configuration"]["name"]) == ("dashboard", "Threat board")
    assert [p["target"] for p in listed.json()] == ["xtm_hub"]


async def test_a_hub_account_without_the_upload_right_is_told_so(dashboard_studio_enabled, hub) -> None:
    hub.upload = False
    async with _world() as (scratch, ids):
        hub_id = await _add_hub(scratch, ids["owner"])
        async with _client(scratch.sessions, ids["owner"]) as client:
            refused = await client.post(
                f"/api/v1/custom-dashboards/{ids['dashboard']}/share-on-hub", json={"integration_id": str(hub_id)}
            )
    assert refused.status_code == 422
    assert refused.json()["detail"].startswith("Your XTM Hub account may not publish to an OpenCTI dashboards library")
    assert "XtmOneHubPublish" not in [name for name, *_ in hub.calls]


async def test_someone_elses_hub_integration_is_not_used(dashboard_studio_enabled, hub) -> None:
    async with _world() as (scratch, ids):
        hub_id = await _add_hub(scratch, ids["owner"])
        async with _client(scratch.sessions, ids["reader"]) as client:
            refused = await client.post(
                f"/api/v1/custom-dashboards/{ids['dashboard']}/share-on-hub", json={"integration_id": str(hub_id)}
            )
    assert (refused.status_code, refused.json()["detail"]) == (422, "The XTM Hub integration is not one you can use.")
    assert hub.calls == []
