"""The platforms' widget editors as descriptions: what the studio's one source form draws.

Each adapter describes its product's widget editor (``dashboard_platform.forms``);
the frontend engine (``platform-form.ts``) knows no platform. These tests pin
the descriptions: well-formed, faithful to what ``check_native`` accepts,
every label translated, served behind the Dashboard Studio, and written to
``fixtures/dashboards/platform_forms.json``, which the frontend tests run the
engine on (the new widget of every type must be ``editor_widgets.json``).

After changing a description, write the fixture again::

    uv run --no-sync python -c "from tests.test_dashboard_platform_forms import write_fixture; write_fixture()"
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.services.dashboard_platform import PLATFORM_SOURCES, openaev, opencrq, opencti
from app.services.dashboard_platform.forms import FormError, form_labels, platform_forms, validate_form

FIXTURE = Path(__file__).parent / "fixtures/dashboards/platform_forms.json"
FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


def _serialized() -> str:
    return json.dumps({"forms": platform_forms()}, indent=2, ensure_ascii=False) + "\n"


def write_fixture() -> None:
    FIXTURE.write_text(_serialized(), encoding="utf-8")


def _forms() -> dict[str, dict]:
    return {entry["source"]: entry["form"] for entry in platform_forms()}


def test_every_platform_has_a_valid_description_and_the_frontend_reads_this_one():
    assert [entry["source"] for entry in platform_forms()] == list(PLATFORM_SOURCES)
    assert [entry["name"] for entry in platform_forms()] == ["OpenCTI", "OpenAEV", "OpenCRQ"]
    for form in _forms().values():
        validate_form(form)
    assert FIXTURE.read_text(encoding="utf-8") == _serialized(), "the fixture is stale: see the module docstring"


@pytest.mark.parametrize("lang", ["en", "fr", "es", "ja"])
def test_every_label_a_description_uses_is_translated(lang):
    catalog = json.loads((FRONTEND / f"src/i18n/lang/{lang}.json").read_text(encoding="utf-8"))
    missing = {source: sorted(label for label in form_labels(form) if label not in catalog) for source, form in _forms().items()}
    assert missing == {source: [] for source in PLATFORM_SOURCES}


def test_the_labels_are_found_where_the_frontend_translates_them():
    labels = form_labels(_forms()["opencti"])
    # A type, a perspective named from a table, a list's title, a hint, a filter pair.
    assert {"Donut", "Knowledge graph", "Data selection {n}", "Count the source of the relationships instead of their target.", "Entities"} <= labels
    # A field's mode or a value shown as it is is not a label.
    assert not labels & {"remove", "keep", "null", "created_at", "entity_type"}


def test_the_opencti_editor_offers_what_check_native_accepts():
    form = _forms()["opencti"]
    types = {t["value"]: t["attrs"] for t in form["types"]}
    assert set(types) == set(opencti.WIDGET_TYPES)
    for name, attrs in types.items():
        limit, _shape, perspectives = opencti.WIDGET_TYPES[name]
        assert attrs["limit"] == limit
        assert set(attrs["perspectives"]) <= set(perspectives)
    interval = _field(form, "parameters.interval")
    # Each interval named in the reader's language, not shown as its value ("day").
    assert interval["options"] == [{"value": v, "label": v.capitalize()} for v in opencti.INTERVALS]


def test_both_module_editors_pick_their_widget_type_on_tiles_with_icons():
    for source, form in _forms().items():
        if source == "opencrq":
            continue
        picker = next(item for item in form["fields"][0]["fields"] if item.get("id") == "type") if form["fields"][0].get("kind") == "row" else None
        picker = picker or next(item for item in form["fields"] if item.get("id") == "type")
        assert picker["display"] == "tiles", source
        assert all(isinstance(option, dict) and option.get("icon") for option in picker["options"]), source


def test_the_openaev_editor_offers_what_check_native_accepts():
    form = _forms()["openaev"]
    types = {t["value"]: t["attrs"] for t in form["types"]}
    assert set(types) == set(openaev.WIDGET_TYPES)
    for name, attrs in types.items():
        configs, max_series, _drawn = openaev.WIDGET_TYPES[name]
        assert set(attrs["configs"]) <= set(configs)
        assert attrs["maxSeries"] == max_series
    time_range = _field(form, "widget_config.time_range")
    assert [o["value"] for o in time_range["options"]] == list(openaev.TIME_RANGES)


def test_the_opencrq_editor_offers_its_metric_catalog():
    form = _forms()["opencrq"]
    assert [(t["value"], t["label"], t["attrs"]) for t in form["types"]] == [
        (name, metric.label, {"window": metric.window}) for name, metric in opencrq.METRICS.items()
    ]
    assert _field(form, "window_days")["max"] == 366


def _field(form: dict, path: str) -> dict:
    def walk(fields: list):
        for item in fields:
            if item.get("path") == path:
                return item
            found = walk(item.get("fields", []))
            if found:
                return found
        return None

    found = walk(form["fields"])
    assert found is not None, path
    return found


def _first_select(form: dict) -> dict:
    """The description's first select with a list of options."""

    def walk(fields: list):
        for item in fields:
            if item.get("kind") == "select" and isinstance(item.get("options"), list):
                return item
            found = walk(item.get("fields", []))
            if found:
                return found
        return None

    return walk(form["fields"])


def _broken(change) -> dict:
    form = copy.deepcopy(opencrq.form())
    change(form)
    return form


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda f: f["fields"][0]["fields"].append({"kind": "slider", "path": "x"}), "kind"),
        (lambda f: f["fields"][0]["fields"][1].update(when={"truthy": {"$eval": "x"}}), "unknown expression"),
        (lambda f: f["fields"][0]["fields"][1].update(when={"truthy": {"$source": "nowhere"}}), "unknown source"),
        (lambda f: f["fields"][0]["fields"][1].update(default={"$template": "nowhere", "with": {}}), "unknown template"),
        (lambda f: f["fields"][0]["fields"][0]["onChange"].append({"change": "nowhere", "value": 1}), "no top-level field"),
        (lambda f: f["fields"][0]["fields"][1].update(path="window days"), "dotted path"),
        (lambda f: f["fields"][0]["fields"][1].update(label=["Days"]), "label"),
        (lambda f: f["fields"][0]["fields"][1].update(when={"matches": [1, 2]}), "unknown condition"),
        (lambda f: f["fields"][0]["fields"][1].update(colour="red"), "does not take"),
        (lambda f: f.update(default={"type": "nowhere"}), "default"),
        (lambda f: f["types"].append(copy.deepcopy(f["types"][0])), "once"),
        (lambda f: _first_select(f).update(display="grid"), "display"),
        (lambda f: _first_select(f)["options"][0].update(icon="Chart Pie"), "icon"),
    ],
)
def test_a_malformed_description_is_refused(change, message):
    with pytest.raises(FormError, match=message):
        validate_form(_broken(change))


@pytest.fixture
def client():
    from app.api import deps
    from app.api.v1 import custom_dashboards

    app = FastAPI()
    app.include_router(custom_dashboards.router, prefix="/api/v1/custom-dashboards")
    app.dependency_overrides[deps.get_current_user] = lambda: object()
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_the_route_serves_every_description_behind_the_studio(client, dashboard_studio_enabled):
    async with client:
        response = await client.get("/api/v1/custom-dashboards/platform-forms")
    assert response.status_code == 200
    assert response.json() == {"forms": platform_forms()}


async def test_the_route_is_the_studios(client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "enabled_features", "")
    async with client:
        response = await client.get("/api/v1/custom-dashboards/platform-forms")
    assert response.status_code == 404


async def test_the_route_needs_a_signed_in_person(dashboard_studio_enabled):
    from app.api.v1 import custom_dashboards

    app = FastAPI()
    app.include_router(custom_dashboards.router, prefix="/api/v1/custom-dashboards")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as anonymous:
        response = await anonymous.get("/api/v1/custom-dashboards/platform-forms")
    assert response.status_code == 401
