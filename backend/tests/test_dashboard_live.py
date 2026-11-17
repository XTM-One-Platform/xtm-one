"""Live data of platform widgets: queries, answers and the bounded request.

Pure: the platforms are ``httpx.MockTransport`` handlers. The OpenCTI queries
are the strings of ``dashboard_live.opencti.QUERIES`` (validated against
OpenCTI's schema when they were written); here, the variables each widget sends
and what each answer becomes. The route tests, against PostgreSQL, are in
``test_dashboard_live_postgres.py``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime

import httpx
import pytest

from app.services import dashboard_live
from app.services.dashboard_live import LiveError, LiveRequest, _http, read_widget_data, request_for
from app.services.dashboard_live import openaev as live_openaev
from app.services.dashboard_live import opencrq as live_opencrq
from app.services.dashboard_live import opencti as live_opencti
from app.services.dashboard_platform import check_native
from app.services.platform_access import PlatformAccessError, PlatformClient

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
INTEGRATION = uuid.UUID("11111111-1111-4111-8111-111111111111")

FILTERS = {
    "mode": "and",
    "filters": [{"id": "f1", "key": ["entity_type"], "values": ["Report"], "operator": "eq", "mode": "or"}],
    "filterGroups": [],
}
NO_IDS = {"mode": "and", "filters": [{"key": ["entity_type"], "values": ["Report"], "operator": "eq", "mode": "or"}], "filterGroups": []}
EMPTY = {"mode": "and", "filters": [], "filterGroups": []}


def _cti(widget_type: str, perspective: str = "entities", selections: int = 1, **parameters) -> dict:
    selection = {"label": "Reports", "attribute": "created-by.internal_id", "date_attribute": "created_at", "filters": FILTERS}
    return check_native(
        "opencti",
        {"type": widget_type, "perspective": perspective, "dataSelection": [selection] * selections, "parameters": parameters},
    ).native


def _client(**kw) -> PlatformClient:
    return PlatformClient(
        integration_id=INTEGRATION, provider=kw.pop("provider", "opencti"), name="Lab", base_url="https://cti.example",
        headers={"Authorization": "Bearer key"}, **kw,
    )  # fmt: skip


# ── OpenCTI ──────────────────────────────────────────────────────────────────


def test_an_opencti_number_counts_with_the_filters_and_no_filter_ids():
    query, variables = live_opencti.build_query(_cti("number"), "number", range_days=30, now=NOW)
    assert "stixCoreObjectsNumber" in query
    # ``count`` runs up to a day ago, as OpenCTI's ``dayAgo()``: the difference is the last 24 hours. The period starts
    # at the start of the day (``daysAgo``) and is also put as filters on the date.
    assert variables == {
        "filters": {
            "mode": "and",
            "filters": [
                {"key": ["created_at"], "values": ["2026-09-23T00:00:00Z"], "operator": "gt", "mode": "or"},
                {"key": ["created_at"], "values": ["2026-10-23T12:00:00Z"], "operator": "lt", "mode": "or"},
            ],
            "filterGroups": [NO_IDS],
        },
        "dateAttribute": "created_at",
        "endDate": "2026-10-22T12:00:00Z",
        "startDate": "2026-09-23T00:00:00Z",
    }


def test_an_opencti_widget_reads_the_dashboard_period_as_opencti_does():
    # computeStartEndDates: a relative period from the start of today back in calendar months, up to now.
    assert live_opencti.period(91, now=NOW, range_key="3m") == (datetime(2026, 7, 23, tzinfo=UTC), NOW)
    assert live_opencti.period(365, now=NOW, range_key="1y") == (datetime(2025, 10, 23, tzinfo=UTC), NOW)
    end_of_month = datetime(2026, 5, 31, 9, 0, tzinfo=UTC)
    assert live_opencti.period(91, now=end_of_month, range_key="3m")[0] == datetime(2026, 2, 28, tzinfo=UTC)
    assert live_opencti.period(7, now=NOW, range_key="7d") == (datetime(2026, 10, 16, tzinfo=UTC), NOW)
    assert live_opencti.period(None, now=NOW) == (None, None)
    # A fixed period as it is: an OpenCTI dashboard's start and end dates.
    start, end = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 3, 31, tzinfo=UTC)
    assert live_opencti.period(89, now=NOW, start=start, end=end) == (start, end)
    # A list reads the period too, as filters on its date (StixCoreObjectsList), which it did not before.
    _query, rows = live_opencti.build_query(_cti("list"), "rows", range_days=91, range_key="3m", now=NOW)
    assert rows["filters"]["filters"] == [
        {"key": ["created_at"], "values": ["2026-07-23T00:00:00Z"], "operator": "gt", "mode": "or"},
        {"key": ["created_at"], "values": ["2026-10-23T12:00:00Z"], "operator": "lt", "mode": "or"},
    ]
    assert rows["filters"]["filterGroups"] == [NO_IDS]
    _query, series = live_opencti.build_query(
        _cti("line", interval="month"), "series", range_days=89, range_start=start, range_end=end, now=NOW
    )
    assert (series["startDate"], series["endDate"]) == ("2026-01-01T00:00:00Z", "2026-03-31T00:00:00Z")
    assert series["timeSeriesParameters"][0]["filters"]["filters"][1]["values"] == ["2026-03-31T00:00:00Z"]
    # Without a period nothing is added: a list reads all of its items.
    assert live_opencti.build_query(_cti("list"), "rows", range_days=None, now=NOW)[1]["filters"] == NO_IDS


def test_an_opencti_widget_does_not_send_the_filters_left_blank():
    # As OpenCTI's widgets (pruneEmptyFiltersAndGroups): a filter with no value is not sent - the API refuses a
    # bare regardingOf - but one whose operator needs no value is, and so are the groups left with something.
    native = _cti("number")
    native["dataSelection"][0]["filters"] = {
        "mode": "and",
        "filters": [
            {"id": "a", "key": ["entity_type"], "values": ["Software"], "operator": "eq", "mode": "or"},
            {"id": "b", "key": ["regardingOf"], "values": [], "operator": "eq", "mode": "or"},
            {"id": "c", "key": ["confidence"], "values": [], "operator": "nil", "mode": "or"},
            {
                "id": "d",
                "key": ["regardingOf"],
                "values": [
                    {"key": "relationship_type", "values": ["targets"]},
                    {"key": "dynamic", "values": [{"mode": "and", "filters": [{"key": ["name"], "values": []}], "filterGroups": []}]},
                ],
                "operator": "eq",
                "mode": "or",
            },
        ],
        "filterGroups": [
            {"mode": "or", "filters": [{"key": ["name"], "values": [], "operator": "eq"}], "filterGroups": []},
            {"mode": "or", "filters": [{"key": ["name"], "values": ["Emotet"], "operator": "eq"}], "filterGroups": []},
        ],
    }
    _query, variables = live_opencti.build_query(native, "number", range_days=None, now=NOW)
    assert variables["filters"] == {
        "mode": "and",
        "filters": [
            {"key": ["entity_type"], "values": ["Software"], "operator": "eq", "mode": "or"},
            {"key": ["confidence"], "values": [], "operator": "nil", "mode": "or"},
            {
                "key": ["regardingOf"],
                "values": [
                    {"key": "relationship_type", "values": ["targets"]},
                    {"key": "dynamic", "values": [{"mode": "and", "filters": [], "filterGroups": []}]},
                ],
                "operator": "eq",
                "mode": "or",
            },
        ],
        "filterGroups": [{"mode": "or", "filters": [{"key": ["name"], "values": ["Emotet"], "operator": "eq"}], "filterGroups": []}],
    }


def test_an_opencti_relationship_number_sends_its_dynamic_filters_and_no_start_date():
    query, variables = live_opencti.build_query(_cti("number", "relationships"), "number", range_days=None, now=NOW)
    assert "stixRelationshipsNumber" in query
    assert variables == {
        "filters": NO_IDS,
        "dynamicFrom": EMPTY,
        "dynamicTo": EMPTY,
        "dateAttribute": "created_at",
        "endDate": "2026-10-22T12:00:00Z",
    }


def test_an_opencti_distribution_groups_by_the_attribute_with_its_limit():
    _query, variables = live_opencti.build_query(_cti("donut"), "distribution", range_days=None, now=NOW)
    assert variables == {
        "filters": NO_IDS,
        "field": "created-by.internal_id",
        "limit": 10,
        "dateAttribute": "created_at",
        "startDate": None,
        "endDate": "2026-10-23T12:00:00Z",
    }
    _query, rel = live_opencti.build_query(_cti("donut", "relationships"), "distribution", range_days=None, now=NOW)
    assert rel["isTo"] is True and rel["dynamicFrom"] == EMPTY


def test_an_opencti_series_sends_one_parameter_per_selection_over_a_year_by_default():
    _query, variables = live_opencti.build_query(
        _cti("line", selections=2, interval="month"), "series", range_days=None, now=NOW
    )
    assert variables == {
        "startDate": "2025-10-23T12:00:00Z",
        "endDate": "2026-10-23T12:00:00Z",
        "interval": "month",
        "timeSeriesParameters": [{"field": "created_at", "filters": NO_IDS}] * 2,
    }


def test_an_opencti_radar_polar_area_and_word_cloud_read_a_donut_s_distribution():
    donut = live_opencti.build_query(_cti("donut"), "distribution", range_days=30, now=NOW)
    for widget_type in ("radar", "polar-area", "wordcloud"):
        assert live_opencti.build_query(_cti(widget_type), "distribution", range_days=30, now=NOW) == donut
    heatmap = live_opencti.build_query(_cti("heatmap", selections=2, interval="week"), "series", range_days=30, now=NOW)
    assert heatmap == live_opencti.build_query(_cti("line", selections=2, interval="week"), "series", range_days=30, now=NOW)


def test_an_opencti_bookmark_widget_lists_the_reader_s_bookmarks():
    native = _cti("bookmark")
    native["dataSelection"][0].update(number=5, sort_by="modified", sort_mode="desc")
    query, variables = live_opencti.build_query(native, "rows", range_days=30, now=NOW)
    assert query == live_opencti.BOOKMARKS
    assert variables == {"first": 5, "orderBy": "modified", "orderMode": "desc", "filters": NO_IDS}
    native["dataSelection"][0].update(sort_by="x_opencti_score", sort_mode="desc")
    assert live_opencti.build_query(native, "rows", range_days=None, now=NOW)[1]["orderBy"] == "created_at"
    answer = {"edges": [{"node": {"id": "m1", "entity_type": "Malware", "created_at": "2026-01-01T00:00:00Z", "modified": "2026-10-20T08:00:00Z", "representative": {"main": "Emotet"}}}], "pageInfo": {"globalCount": 3}}
    rows = live_opencti._rows("https://cti.example", native, answer)
    assert rows == {
        "columns": [
            {"key": "name", "label": "Name"},
            {"key": "entity_type", "label": "Type", "kind": "chip"},
            {"key": "modified", "label": "Updated", "kind": "date"},
        ],
        "rows": [
            {
                "name": "Emotet",
                "entity_type": {"label": "Malware", "color": "#F0B60A", "icon": "biohazard"},
                "modified": "2026-10-20T08:00:00Z",
                "_url": "https://cti.example/dashboard/id/m1",
                "_entityType": "Malware",
                "_icon": "biohazard",
            }
        ],
        "total": 3,
    }


def test_an_opencti_list_names_the_status_and_the_types_as_opencti_does():
    # ItemStatus: the status' template, else "Disabled" when the type has no workflow, else "Unknown".
    assert live_opencti._cell({"status": {"template": {"name": "Analyzed", "color": "#4caf50"}}}, "x_opencti_workflow_id") == {
        "label": "Analyzed",
        "color": "#4caf50",
    }
    assert live_opencti._cell({"status": None, "workflowEnabled": False}, "x_opencti_workflow_id") == {"label": "Disabled"}
    assert live_opencti._cell({}, "x_opencti_workflow_id") == {"label": "Disabled"}
    assert live_opencti._cell({"status": None, "workflowEnabled": True}, "x_opencti_workflow_id") == {"label": "Unknown"}
    assert "workflowEnabled" in live_opencti._ENTITY_NODE
    # ItemEntityType: a relationship by its name (relationship_<type>), first letter up and no icon; an entity type by its own.
    assert live_opencti._cell({"entity_type": "targets"}, "entity_type") == {"label": "Targets", "color": live_opencti.entity_color("targets")}
    assert live_opencti._cell({"entity_type": "stix-sighting-relationship"}, "entity_type")["label"] == "Sighted in/at"
    assert live_opencti._cell({"entity_type": "Threat-Actor-Individual"}, "entity_type") == {
        "label": "Threat Actor Individual",
        "color": live_opencti.entity_color("Threat-Actor-Individual"),
        "icon": live_opencti.entity_icon("Threat-Actor-Individual"),
    }


def test_an_opencti_chart_by_entity_type_names_the_types():
    # useDistributionGraphData: grouped by entity_type, a slice is the type's name, not its key.
    native = _cti("donut")
    native["dataSelection"][0]["attribute"] = "entity_type"
    items = live_opencti._distribution_items(native, [{"label": "Threat-Actor-Individual", "value": 3}, {"label": "IPv4-Addr", "value": 1}], "")
    assert [label for label, _value, _extras in items] == ["Threat Actor Individual", "IPv4 address"]


def test_an_opencti_list_sorts_on_a_known_order_only():
    # As OpenCTI's entities list: its sort field, else created_at, ascending.
    _query, variables = live_opencti.build_query(_cti("list"), "rows", range_days=None, now=NOW)
    assert variables == {"filters": NO_IDS, "first": 10, "orderBy": "created_at", "orderMode": "asc"}
    native = _cti("list")
    native["dataSelection"][0]["sort_by"] = "confidence"
    assert live_opencti.build_query(native, "rows", range_days=None, now=NOW)[1]["orderBy"] == "created_at"


def test_an_opencti_audit_widget_is_not_queried():
    native = {**_cti("number"), "perspective": "audits"}
    with pytest.raises(LiveError) as refused:
        live_opencti.build_query(native, "number", range_days=None)
    assert refused.value.code == "unsupported"


def test_opencti_answers_become_the_closed_shapes():
    native = _cti("donut")
    # ``total`` now, ``count`` a day ago: the difference is ``total - count`` over the last 24 hours.
    assert live_opencti.normalize(native, "number", {"data": {"stixCoreObjectsNumber": {"total": 42, "count": 39}}}) == {
        "value": 42.0,
        "change": 3.0,
        "previous": 39.0,
        "period": "day",
    }
    distribution = {
        "data": {
            "stixCoreObjectsDistribution": [
                {"label": "id-1", "value": 5, "entity": {"id": "id-1", "representative": {"main": "ACME CERT"}}},
                {"label": "id-2", "value": 2, "entity": {"name": "Jane Analyst"}},
                {"label": "Report\x07", "value": "1", "entity": None},
            ]
        }
    }
    assert live_opencti.normalize(native, "distribution", distribution) == {
        "items": [
            {"label": "ACME CERT", "value": 5.0},
            {"label": "Jane Analyst", "value": 2.0},
            {"label": "Report", "value": 1.0},
        ]
    }
    series = {"data": {"x": [{"data": [{"date": "2026-10-01T00:00:00.000Z", "value": 4}]}, {"data": []}]}}
    assert live_opencti.normalize(_cti("line", selections=2), "series", series) == {
        "series": [
            {"name": "Reports", "points": [{"x": "2026-10-01", "y": 4.0}]},
            {"name": "Reports", "points": []},
        ]
    }


@pytest.mark.parametrize(
    ("errors", "code"),
    [
        ([{"message": "You are not allowed to do this.", "extensions": {"code": "FORBIDDEN_ACCESS"}}], "access_denied"),
        ([{"message": "You must be logged in to do this."}], "access_denied"),
        ([{"message": "Cannot query field"}], "upstream_error"),
    ],
)
def test_an_opencti_graphql_error_is_a_fixed_code(errors, code):
    with pytest.raises(LiveError) as failed:
        live_opencti.normalize(_cti("number"), "number", {"errors": errors, "data": None})
    assert failed.value.code == code


def test_an_opencti_list_links_each_row_by_quoted_id():
    value = {
        "pageInfo": {"globalCount": 120},
        "edges": [{"node": {"id": "a/b?c", "entity_type": "Report", "created_at": "2026-10-01T10:00:00Z", "representative": {"main": "Q3"}}}],
    }
    native = _cti("list")
    native["dataSelection"][0]["columns"] = [{"attribute": "name"}, {"attribute": "created_at", "label": "Seen"}]
    assert live_opencti._rows("https://cti.example", native, value) == {
        "columns": [{"key": "name", "label": "Name"}, {"key": "created_at", "label": "Seen", "kind": "date"}],
        "rows": [
            {"name": "Q3", "created_at": "2026-10-01T10:00:00Z", "_url": "https://cti.example/dashboard/id/a%2Fb%3Fc", "_entityType": "Report"}
        ],
        "total": 120,
    }


async def test_an_opencti_map_is_read_as_regions_by_alpha_2_code_and_points_where_located(monkeypatch):
    answer = {
        "data": {
            "stixRelationshipsDistribution": [
                {"label": "c1", "value": 9, "entity": {"id": "c1", "entity_type": "Country", "name": "France",
                                                        "x_opencti_aliases": ["FRA", "fr"], "latitude": 46.6, "longitude": 2.2}},
                {"label": "c2", "value": 4, "entity": {"id": "c2", "entity_type": "Country", "name": "Atlantis",
                                                        "x_opencti_aliases": ["ATL", "ZZ"], "latitude": None, "longitude": None}},
                {"label": "c3", "value": 2, "entity": {"id": "c3", "entity_type": "City", "name": "Lyon", "latitude": 45.76, "longitude": 4.84}},
                {"label": "r1", "value": 7, "entity": {"id": "r1", "entity_type": "Region", "name": "Western Europe"}},
            ]
        }
    }  # fmt: skip
    seen, record = _recording(lambda _r: httpx.Response(200, json=answer))
    _use_transport(monkeypatch, record)
    native = check_native(
        "opencti",
        {"type": "map", "perspective": "relationships", "dataSelection": [{"attribute": "internal_id", "number": 400, "isTo": True}]},
    ).native
    request = LiveRequest(source="opencti", integration_id=INTEGRATION, native=native, shape="geo")
    # As ``LocationMiniMapTargets``: countries as regions ranked by level, cities as points (a country is not one).
    # Levels run, as OpenCTI computes them, from the answer's last value (7) to its first plus one (10):
    # France is trunc(9 * (9 - 7) / 3) = 6.
    assert await live_opencti.fetch(_client(), request) == {
        "points": [{"label": "Lyon", "lat": 45.76, "lon": 4.84, "value": 2.0, "group": "City"}],
        "regions": [{"code": "FR", "label": "France", "value": 9.0, "level": 6}],
    }
    (sent,) = seen
    body = json.loads(sent.content)
    assert "XtmOneWidgetMap" in body["query"]
    assert {k: body["variables"][k] for k in ("field", "limit", "isTo")} == {"field": "internal_id", "limit": 300, "isTo": True}


async def test_an_opencti_relationship_list_carries_each_relationships_ends(monkeypatch):
    answer = {
        "data": {
            "stixRelationships": {
                "pageInfo": {"globalCount": 1},
                "edges": [{"node": {"id": "rel", "entity_type": "uses", "created_at": "2026-10-01T10:00:00Z",
                                    "representative": {"main": "APT28 uses X-Agent"},
                                    "from": {"id": "a", "entity_type": "Intrusion-Set", "representative": {"main": "APT28"}},
                                    "to": {"id": "m"}}}],
            }
        }
    }  # fmt: skip
    seen, record = _recording(lambda _r: httpx.Response(200, json=answer))
    _use_transport(monkeypatch, record)
    request = LiveRequest(source="opencti", integration_id=INTEGRATION, native=_cti("list", "relationships"), shape="rows")
    data = await live_opencti.fetch(_client(), request)
    assert [c["key"] for c in data["columns"]] == [
        "entity_type", "from_entity_type", "from_relationship_type", "to_entity_type", "to_relationship_type", "created",
        "created_at", "createdBy", "objectMarking",
    ]  # fmt: skip
    row = data["rows"][0]
    assert {k: row[k] for k in ("from_relationship_type", "to_relationship_type")} == {"from_relationship_type": "APT28", "to_relationship_type": "Restricted"}
    assert row["from_entity_type"] == {"label": "Intrusion Set", "color": "#FF9800", "icon": "gem"}
    # The row opens its source, and leads with its type's icon.
    assert (row["_url"], row["_icon"]) == ("https://cti.example/dashboard/id/a", "spline")
    assert "from {" in json.loads(seen[0].content)["query"]


async def test_an_opencti_entity_list_has_opencti_s_default_columns(monkeypatch):
    answer = {"data": {"stixCoreObjects": {"pageInfo": {"globalCount": 0}, "edges": []}}}
    _seen, record = _recording(lambda _r: httpx.Response(200, json=answer))
    _use_transport(monkeypatch, record)
    request = LiveRequest(source="opencti", integration_id=INTEGRATION, native=_cti("list"), shape="rows")
    data = await live_opencti.fetch(_client(), request)
    assert [(c["key"], c["label"], c.get("kind", "text")) for c in data["columns"]] == [
        ("entity_type", "Type", "chip"), ("name", "Name", "text"), ("created", "Original creation date", "date"),
        ("created_at", "Platform creation date", "date"), ("modified", "Modification date", "date"), ("createdBy", "Author", "text"),
        ("creators", "Creators", "text"), ("x_opencti_workflow_id", "Processing status", "chip"), ("objectLabel", "Labels", "chips"),
        ("objectMarking", "Marking", "chips"),
    ]  # fmt: skip


# ── OpenAEV ──────────────────────────────────────────────────────────────────


def _aev(widget_type: str, config: dict) -> dict:
    return check_native("openaev", {"widget_type": widget_type, "widget_config": config}).native


SERIES = [{"name": "Injects", "filter": {"mode": "and", "filters": [{"key": "base_entity", "operator": "eq", "values": ["inject"], "mode": "or"}]}}]


def test_an_openaev_widget_posts_its_configuration_to_the_matching_ad_hoc_endpoint():
    native = _aev("number", {"widget_configuration_type": "flat", "series": SERIES, "time_range": "CUSTOM", "start": "2026-01-01", "end": "2026-02-01"})
    path, body = live_openaev.build_request(native, range_days=30, tenant_id="t/1")
    assert path == "/api/tenants/t%2F1/dashboards/adhoc/count"
    assert body == {
        "widget_config": {
            "widget_configuration_type": "flat",
            "time_range": "LAST_MONTH",
            "date_attribute": "base_created_at",
            "series": SERIES,
        }
    }
    list_native = _aev("list", {"widget_configuration_type": "list", "perspective": {"name": "x", "filter": {}}})
    assert live_openaev.build_request(list_native, range_days=None)[0] == "/api/dashboards/adhoc/entities"
    # OpenAEV's own ranges by key (TimeRangeUtils), a fixed period as its custom range.
    for key, named in (("1d", "LAST_DAY"), ("3m", "LAST_QUARTER"), ("6m", "LAST_SEMESTER"), ("1y", "LAST_YEAR")):
        config = live_openaev.build_request(native, range_days=dashboard_live.RANGES[key], range_key=key)[1]["widget_config"]
        assert (config["time_range"], "start" in config) == (named, False)
    start, end = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 3, 31, tzinfo=UTC)
    config = live_openaev.build_request(native, range_days=89, range_start=start, range_end=end)[1]["widget_config"]
    assert (config["time_range"], config["start"], config["end"]) == ("CUSTOM", "2026-01-01T00:00:00Z", "2026-03-31T00:00:00Z")


def test_the_page_ranges_the_api_takes_are_the_live_layer_s():
    from typing import get_args

    from app.api.v1.schemas.custom_dashboard import WidgetDataRange

    assert list(get_args(WidgetDataRange)) == list(dashboard_live.RANGES)


def test_a_period_is_a_range_key_or_fixed_dates():
    assert dashboard_live.fixed_period("2026-01-01/2026-03-31") == (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 3, 31, tzinfo=UTC))
    assert dashboard_live.fixed_period("2026-01-01/") == (datetime(2026, 1, 1, tzinfo=UTC), None)
    for wrong in ("/", "2026-03-31/2026-01-01", "2026-02-30/", "last week", None, "7d"):
        assert dashboard_live.fixed_period(wrong) is None
    assert dashboard_live.is_range("6m") and dashboard_live.is_range("/2026-03-31") and not dashboard_live.is_range("2w")
    request = request_for(
        {"source": "openaev", "integration_id": str(INTEGRATION), "native": {"widget_type": "number", "widget_config": {"widget_configuration_type": "flat", "series": SERIES}}},
        "2026-01-01/2026-03-31",
    )
    assert (request.range_key, request.range_start, request.range_end, request.range_days) == (
        None, datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 3, 31, tzinfo=UTC), 89,
    )  # fmt: skip
    request = request_for({**{"source": "openaev", "integration_id": str(INTEGRATION)}, "native": request.native}, "3m")
    assert (request.range_key, request.range_days, request.range_start) == ("3m", 91, None)


def test_all_time_sets_the_widget_s_own_period_aside():
    assert dashboard_live.is_range("all") and not dashboard_live.is_range("ALL")
    native = {"widget_type": "number", "widget_config": {"widget_configuration_type": "flat", "time_range": "LAST_MONTH", "series": SERIES}}
    request = request_for({"source": "openaev", "integration_id": str(INTEGRATION), "native": native}, "all")
    assert (request.range_key, request.range_days, request.range_start, request.range_end) == ("all", None, None, None)
    # OpenAEV reads every date (its ALL_TIME); OpenCTI reads without a period.
    config = live_openaev.build_request(request.native, range_days=None, range_key="all")[1]["widget_config"]
    assert config["time_range"] == "ALL_TIME"
    assert live_opencti.period(None, now=datetime(2026, 10, 26, tzinfo=UTC), range_key="all") == (None, None)


def test_openaev_answers_become_the_closed_shapes():
    flat = _aev("number", {"widget_configuration_type": "flat", "series": SERIES})
    assert live_openaev.normalize(flat, {"interval_count": 12, "previous_interval_count": 10, "difference_count": 2}) == {
        "value": 12.0,
        "change": 2.0,
        "previous": 10.0,
        "period": "interval",
        # An inject count's tile: NumberWidget's lightning bolt, in the warning colour.
        "icon": {"name": "zap", "color": "warning"},
        "subject": "inject",
    }
    temporal = _aev("line", {"widget_configuration_type": "temporal-histogram", "series": SERIES})
    answer = [{"label": "Injects", "data": [{"key": "2026-10-01T00:00:00Z", "label": "Oct", "value": 3}]}]
    assert live_openaev.normalize(temporal, answer) == {
        "series": [{"name": "Injects", "points": [{"x": "2026-10-01", "y": 3.0, "key": "2026-10-01T00:00:00Z"}]}]
    }
    structural = _aev("donut", {"widget_configuration_type": "structural-histogram", "series": SERIES, "field": "inject_status"})
    answer = [{"label": "s", "data": [{"key": "SUCCESS", "label": "Success", "value": 7}, {"key": "ERROR", "value": 1}]}]
    assert live_openaev.normalize(structural, answer) == {
        "items": [
            {"label": "Success", "value": 7.0, "key": "SUCCESS", "color": "success"},
            {"label": "ERROR", "value": 1.0, "key": "ERROR", "color": "#f14337"},
        ]
    }
    entities = _aev("list", {"widget_configuration_type": "list", "perspective": {"name": "x", "filter": {}}})
    answer = {"es_datas": [{"base_id": "e1", "base_representative": "Phishing run", "base_entity": "simulation", "base_created_at": "2026-10-02T09:00:00Z"}], "total": 9}
    # No columns configured: the representative alone. A simulation row has no leading icon.
    assert live_openaev.normalize(entities, answer, "https://aev.example")["rows"] == [
        {"base_representative": "Phishing run", "_url": "https://aev.example/admin/simulations/e1", "_entityType": "simulation"}
    ]


def _posture(widget_type: str, *series: dict, config_type: str = "structural-histogram", field: str = "inject_expectation_type") -> dict:
    config = {"widget_configuration_type": config_type, "series": list(series) or SERIES}
    if config_type == "structural-histogram":
        config["field"] = field
    return _aev(widget_type, config)


def _status(name: str, values: dict[str, int], key: bool = False) -> dict:
    """An ad hoc series answer: buckets labelled (and, for a technique, keyed by its id)."""
    return {"label": name, "data": [{"key": f"id-{k}" if key else k, "label": k, "value": v} for k, v in values.items()]}


def test_a_posture_radar_keeps_its_series_one_axis_per_security_platform_coloured_by_meaning():
    radar = live_openaev.normalize(_posture("posture-radar", field="base_security_platforms_side"), [_status("SUCCESS", {"EDR": 4, "SIEM": 2}), _status("FAILED", {"SIEM": 1})])
    assert radar == {
        "series": [
            {"name": "Success", "color": "success", "points": [{"x": "EDR", "y": 4.0, "key": "EDR"}, {"x": "SIEM", "y": 2.0, "key": "SIEM"}]},
            {"name": "Failed", "color": "#f14337", "points": [{"x": "SIEM", "y": 1.0, "key": "SIEM"}]},
        ]
    }



def test_the_technique_widgets_read_every_technique():
    for widget_type in ("security-coverage", "attack-path"):
        native = _posture(widget_type, field="base_attack_patterns_side")
        path, body = live_openaev.build_request(native, range_days=None)
        assert (path, body["widget_config"]["limit"]) == ("/api/dashboards/adhoc/series", 10_000)
    donut = _posture("donut", field="inject_status")
    # Another breakdown keeps its own limit.
    assert live_openaev.build_request(donut, range_days=None)[1]["widget_config"]["limit"] == 100


# OpenAEV's kill chains and techniques (STAGED): ATT&CK's tactics and an ICS one.
PHASES = [
    {"phase_id": "ta-exec", "phase_name": "Execution", "phase_order": 4, "phase_kill_chain_name": "mitre-attack"},
    {"phase_id": "ta-init", "phase_name": "Initial Access", "phase_order": 3, "phase_kill_chain_name": "mitre-attack"},
    {"phase_id": "ics-impact", "phase_name": "Impact", "phase_order": 12, "phase_kill_chain_name": "mitre-ics-attack"},
]
PATTERNS = [
    {"attack_pattern_id": "ap-phish", "attack_pattern_external_id": "T1566", "attack_pattern_name": "Phishing", "attack_pattern_kill_chain_phases": ["ta-init"]},
    {"attack_pattern_id": "ap-phish-link", "attack_pattern_external_id": "T1566.002", "attack_pattern_name": "Spearphishing Link", "attack_pattern_parent": "ap-phish", "attack_pattern_kill_chain_phases": ["ta-init"]},
    {"attack_pattern_id": "ap-valid", "attack_pattern_external_id": "T1078", "attack_pattern_name": "Valid Accounts", "attack_pattern_kill_chain_phases": ["ta-init", "ta-exec"]},
    {"attack_pattern_id": "ap-cmd", "attack_pattern_external_id": "T1059", "attack_pattern_name": "Command and Scripting Interpreter", "attack_pattern_kill_chain_phases": ["ta-exec"]},
    {"attack_pattern_id": "ap-ics", "attack_pattern_external_id": "T0831", "attack_pattern_name": "Manipulation of Control", "attack_pattern_kill_chain_phases": ["ics-impact"]},
]


def _reference_of(phases=PHASES, patterns=PATTERNS) -> dict:
    return {
        "phases": [{"id": p["phase_id"], "name": p["phase_name"], "order": float(p["phase_order"]), "chain": p["phase_kill_chain_name"]} for p in phases],
        "patterns": [
            {"id": p["attack_pattern_id"], "external_id": p["attack_pattern_external_id"], "name": p["attack_pattern_name"],
             "parent": p.get("attack_pattern_parent"), "phases": p["attack_pattern_kill_chain_phases"]}
            for p in patterns
        ],
    }  # fmt: skip


def _technique_series(success: dict[str, int], failed: dict[str, int]) -> list[dict]:
    return [
        {"label": "SUCCESS", "data": [{"key": k, "label": k, "value": v} for k, v in success.items()]},
        {"label": "FAILED", "data": [{"key": k, "label": k, "value": v} for k, v in failed.items()]},
    ]


async def test_the_reference_is_paged_bounded_and_kept_per_credential(monkeypatch, no_cache_no_limit):
    pages = [PATTERNS[:3], PATTERNS[3:]]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/kill_chain_phases"):
            return httpx.Response(200, json=PHASES)
        body = json.loads(request.content)
        return httpx.Response(200, json={"content": pages[body["page"]], "last": body["page"] == 1})

    seen, record = _recording(handler)
    _use_transport(monkeypatch, record)
    monkeypatch.setattr(live_openaev, "_PATTERN_PAGE", 3)
    client = _client(provider="openaev", tenant_id="t/1")
    reference = await live_openaev.read_reference(client)
    assert reference == _reference_of()
    assert [(r.method, r.url.raw_path.decode(), json.loads(r.content) if r.content else None) for r in seen] == [
        ("GET", "/api/tenants/t%2F1/kill_chain_phases", None),
        ("POST", "/api/tenants/t%2F1/attack_patterns/search", {"page": 0, "size": 3}),
        ("POST", "/api/tenants/t%2F1/attack_patterns/search", {"page": 1, "size": 3}),
    ]
    # Read again from the cache by the same credential; another credential reads its own.
    assert await live_openaev.read_reference(client) == reference
    assert len(seen) == 3
    other = _client(provider="openaev", tenant_id="t/1")
    other.headers["Authorization"] = "Bearer another"
    await live_openaev.read_reference(other)
    assert len(seen) == 6


def _attack_path(simulation: str | None = "sim-1") -> dict:
    series = {"name": "SUCCESS", "filter": {"mode": "and", "filters": [{"key": "base_entity", "operator": "eq", "values": ["expectation-inject"], "mode": "and"}]}}
    if simulation:
        series["filter"]["filters"].append({"key": "base_simulation_side", "operator": "eq", "values": [simulation], "mode": "and"})
    return _posture("attack-path", series, {**series, "name": "FAILED"}, field="base_attack_patterns_side")


INJECTS = {
    "es_datas": [
        # Phishing leads to command execution and valid accounts; valid accounts to command execution.
        {"base_id": "i1", "base_attack_patterns_side": ["ap-phish"], "base_attack_patterns_children_side": ["ap-cmd", "ap-valid", "ap-phish", "ap-unknown"]},
        {"base_id": "i2", "base_attack_patterns_side": ["ap-valid"], "base_attack_patterns_children_side": ["ap-cmd"]},
        {"base_id": "i3", "base_attack_patterns_side": ["ap-cmd", "ap-unknown"]},
    ]
}


# ── Generic fields of the shapes ─────────────────────────────────────────────


def test_a_distribution_item_keeps_a_valid_colour_entity_type_link_and_key_only():
    from app.services.dashboard_live import distribution_data

    data = distribution_data(
        [
            ("APT28", 4, {"color": "#ff0000", "entityType": "Intrusion-Set", "link": "https://cti.example/dashboard/id/1", "key": "x"}),
            ("Named", 3, {"color": "danger", "entityType": "attack-pattern", "link": "/dashboards"}),
            ("Bad", 2, {"color": "red; background:url(x)", "entityType": "<script>", "link": "javascript:alert(1)"}),
            ("Odd", 1, {"color": "#ff00", "entityType": "a" * 61, "link": "//evil.example/x"}),
            ("Plain", 1),
        ]
    )
    assert data["items"] == [
        {"label": "APT28", "value": 4.0, "color": "#ff0000", "entityType": "Intrusion-Set", "link": "https://cti.example/dashboard/id/1", "key": "x"},
        {"label": "Named", "value": 3.0, "color": "danger", "entityType": "attack-pattern", "link": "/dashboards"},
        {"label": "Bad", "value": 2.0},
        {"label": "Odd", "value": 1.0},
        {"label": "Plain", "value": 1.0},
    ]


def test_a_series_keeps_its_colour_and_a_point_drawn_alone_its_key():
    from app.services.dashboard_live import series_data

    points = [(f"2026-01-{day:02d}", 1, f"k{day}") for day in range(1, 31)] + [(f"2026-02-{day:02d}", 1, None) for day in range(1, 29)]
    points += [(f"2026-03-{day:02d}", 1) for day in range(1, 31)]
    data = series_data([("Success", points, {"color": "success"}), ("Failed", [("2026-01-01", 2, "f" * 150)], {"color": "url(x)"})])
    success, failed = data["series"]
    assert success["color"] == "success"
    # 88 days, two per bucket: no bucket keeps a key, since a click on it would filter on one of its days only.
    assert len(success["points"]) == 44
    assert success["points"][:2] == [{"x": "2026-01-01", "y": 2.0}, {"x": "2026-01-03", "y": 2.0}]
    # Drawn one point per bucket, each keeps its own.
    short = series_data([("s", [("2026-01-01", 1, "a"), ("2026-01-02", 2, None), ("2026-01-03", 3, "c" * 150)])])["series"][0]["points"]
    assert short == [{"x": "2026-01-01", "y": 1.0, "key": "a"}, {"x": "2026-01-02", "y": 2.0}, {"x": "2026-01-03", "y": 3.0, "key": "c" * 100}]
    assert "color" not in failed
    # On the shared axis of two-day buckets, its one point is in a bucket too.
    assert failed["points"] == [{"x": "2026-01-01", "y": 2.0}]


def test_a_number_keeps_its_previous_value_period_and_icon_when_they_validate():
    from app.services.dashboard_live import number_data

    assert number_data(12, 3, previous=9, period="interval", icon_name="ShieldCheck", icon_color="warning") == {
        "value": 12.0,
        "change": 3.0,
        "previous": 9.0,
        "period": "interval",
        "icon": {"name": "shield-check", "color": "warning"},
    }
    assert number_data(12, None, previous="nan", period="fortnight", icon_name="no-such-icon", icon_color="warning") == {"value": 12.0, "change": None}
    assert number_data(1, icon_name="bolt", icon_color="#12345") == {"value": 1.0, "change": None, "icon": {"name": "bolt"}}


def test_rows_cells_are_drawn_by_their_column_kind_and_rows_keep_their_own_fields():
    from app.services.dashboard_live import MAX_CHIPS, rows_data

    columns = [
        ("type", "Type", "entityType"),
        ("name", "Name"),
        ("created", "Created", "date"),
        ("labels", "Labels", "chips"),
        ("status", "Status", "chip"),
        ("score", "Score", "number"),
        ("odd", "Odd", "html"),
    ]
    row = {
        "type": "Malware",
        "name": {"not": "text"},
        "created": "2026-10-01T10:00:00+02:00",
        "labels": [{"label": "ransomware", "color": "#ff0000"}, "plain", {"label": ""}, 7] + [f"l{i}" for i in range(20)],
        "status": {"label": "Not detected", "color": "danger", "icon": "ShieldCheck"},
        "score": "9.5",
        "odd": "<b>x</b>",
        "_url": "https://cti.example/dashboard/id/1",
        "_entityType": "Malware",
        "_icon": "bug",
    }
    bad = {"type": "no spaces allowed", "created": "yesterday", "labels": "a", "status": 3, "score": "many", "_url": "javascript:x", "_entityType": "!", "_icon": "nope-x"}
    data = rows_data(columns, [row, bad], 2)
    assert data["columns"] == [
        {"key": "type", "label": "Type", "kind": "entityType"},
        {"key": "name", "label": "Name"},
        {"key": "created", "label": "Created", "kind": "date"},
        {"key": "labels", "label": "Labels", "kind": "chips"},
        {"key": "status", "label": "Status", "kind": "chip"},
        {"key": "score", "label": "Score", "kind": "number"},
        {"key": "odd", "label": "Odd"},
    ]
    first, second = data["rows"]
    assert first["type"] == "Malware"
    assert first["name"] == "{'not': 'text'}"
    assert first["created"] == "2026-10-01T08:00:00Z"
    assert first["labels"][:2] == [{"label": "ransomware", "color": "#ff0000"}, {"label": "plain"}]
    # An empty label and a number are not chips; past MAX_CHIPS the rest is dropped.
    assert first["labels"][2:] == [{"label": f"l{i}"} for i in range(MAX_CHIPS - 2)]
    assert first["status"] == {"label": "Not detected", "color": "danger", "icon": "shield-check"}
    assert first["score"] == 9.5
    assert first["odd"] == "<b>x</b>"
    assert (first["_url"], first["_entityType"], first["_icon"]) == ("https://cti.example/dashboard/id/1", "Malware", "bug")
    assert second == {"type": None, "name": "", "created": None, "labels": [], "status": None, "score": None, "odd": ""}


def test_an_event_keeps_its_second_line_cut_and_a_region_its_level():
    from app.services.dashboard_live import MAX_SECONDARY, events_data, geo_data

    events = events_data(
        [("Report", "2026-10-01", None, None, None, {"secondary": "x" * 400, "entityType": "Report", "color": "info", "link": "https://cti.example/r/1", "key": "k"})]
    )["events"]
    assert events == [
        {"label": "Report", "start": "2026-10-01", "end": None, "lane": None, "value": None, "secondary": "x" * MAX_SECONDARY, "entityType": "Report", "color": "info", "link": "https://cti.example/r/1"}
    ]
    regions = geo_data([], [("FR", "France", 4, 9), ("DE", "Germany", 3, 2.5), ("IT", "Italy", 2, 10), ("ES", "Spain", 1)])["regions"]
    assert regions == [
        {"code": "FR", "label": "France", "value": 4.0, "level": 9},
        {"code": "DE", "label": "Germany", "value": 3.0},
        {"code": "IT", "label": "Italy", "value": 2.0},
        {"code": "ES", "label": "Spain", "value": 1.0},
    ]


@pytest.mark.parametrize(
    ("shape", "data"),
    [
        ("distribution", {"items": [{"label": "a", "value": 1, "link": "https://evil.example"}]}),
        ("events", {"events": [{"label": "a", "start": "2026-10-01", "link": "https://evil.example"}]}),
        ("rows", {"columns": [{"key": "a"}], "rows": [{"a": "x", "_url": "https://evil.example"}]}),
    ],
)
def test_a_link_written_in_a_dashboard_stays_inside_the_platform(shape, data):
    from app.services.dashboard_live import closed_static_data

    with pytest.raises(ValueError):
        closed_static_data(shape, data)
    # Data a builder already closed (a derived source's parent) keeps the links an adapter built.
    reclosed = closed_static_data(shape, data, reclosing=True)
    record = (reclosed.get("items") or reclosed.get("events") or reclosed.get("rows"))[0]
    assert record.get("link", record.get("_url")) == "https://evil.example"


def test_static_data_carries_the_generic_fields():
    from app.services.dashboard_live import closed_static_data

    assert closed_static_data(
        "distribution", {"items": [{"label": "a", "value": 1, "color": "chart2", "entityType": "Malware", "link": "/agents"}]}
    ) == {"items": [{"label": "a", "value": 1.0, "color": "chart2", "entityType": "Malware", "link": "/agents"}]}
    assert closed_static_data("number", {"value": 3, "previous": 2, "period": "day", "icon": {"name": "zap", "color": "warning"}}) == {
        "value": 3.0, "change": None, "previous": 2.0, "period": "day", "icon": {"name": "zap", "color": "warning"}
    }  # fmt: skip
    series = closed_static_data("series", {"series": [{"name": "s", "color": "success", "points": [{"x": "a", "y": 1, "key": "A"}]}]})
    assert series == {"series": [{"name": "s", "points": [{"x": "a", "y": 1.0, "key": "A"}], "color": "success"}]}
    rows = closed_static_data("rows", {"columns": [{"key": "d", "label": "D", "kind": "date"}], "rows": [{"d": "2026-10-01"}]})
    assert rows == {"columns": [{"key": "d", "label": "D", "kind": "date"}], "rows": [{"d": "2026-10-01"}], "total": None}
    regions = closed_static_data("geo", {"regions": [{"code": "FR", "value": 1, "level": 3}]})["regions"]
    assert regions == [{"code": "FR", "label": "FR", "value": 1.0, "level": 3}]


def test_a_pipeline_step_never_writes_a_row_s_own_field():
    from pydantic import TypeAdapter, ValidationError

    from app.services.dashboard_pipeline import Step, run_pipeline

    adapter = TypeAdapter(Step)
    for step in ({"op": "rename", "field": "name", "to": "_url"}, {"op": "eval", "field": "_icon", "expression": "1"}):
        with pytest.raises(ValidationError):
            adapter.validate_python(step)
    rows = {
        "columns": [{"key": "name", "label": "Name"}, {"key": "n", "label": "N"}],
        "rows": [{"name": "a", "n": "1", "_url": "https://cti.example/x", "_entityType": "Malware"}],
        "total": 1,
    }
    shape, data = run_pipeline([adapter.validate_python({"op": "fields", "keep": ["name"]})], "rows", rows)
    assert (shape, data["rows"]) == ("rows", [{"name": "a", "_url": "https://cti.example/x", "_entityType": "Malware"}])


def test_a_module_s_presentation_is_checked_against_the_options_of_its_view(monkeypatch):
    from app.services.dashboard_live import presentation_of

    monkeypatch.setattr(live_opencti, "presentation", lambda native: {"view": "xtm.pie", "options": {"showDonutHole": True, "labelDisplay": "values"}}, raising=False)
    assert presentation_of("opencti", {}) == {"view": "xtm.pie", "options": {"showDonutHole": True, "labelDisplay": "values"}}
    for wrong in ({"view": "xtm.pie", "options": {"labelDisplay": "everywhere"}}, {"view": "xtm.nothing", "options": {}}, {"view": "xtm.pie", "options": []}):
        monkeypatch.setattr(live_opencti, "presentation", lambda native, wrong=wrong: wrong, raising=False)
        assert presentation_of("opencti", {}) is None
    monkeypatch.setattr(live_opencti, "presentation", lambda native: None, raising=False)
    assert presentation_of("opencti", {}) is None
    # A preset that fails on a malformed configuration is left out, never a failed read.
    monkeypatch.setattr(live_opencti, "presentation", lambda native: {"view": "xtm.map", "options": {"zoom": float(native["zoom"])}}, raising=False)
    assert presentation_of("opencti", {"zoom": "far"}) is None
    assert presentation_of("opencti", {}) is None


async def test_the_presentation_travels_with_the_data_and_is_not_cached(monkeypatch, no_cache_no_limit):
    _resolver(monkeypatch, _client())
    _use_transport(monkeypatch, lambda _r: httpx.Response(200, json={"data": {"stixCoreObjectsNumber": {"total": 7, "count": 1}}}))
    preset = {"view": "xtm.singlevalue", "options": {}}
    monkeypatch.setattr(live_opencti, "presentation", lambda native: preset, raising=False)
    widget = {"source": "opencti", "integration_id": str(INTEGRATION), "native": _cti("number")}
    viewer = uuid.uuid4()
    first = await read_widget_data(None, viewer, request_for(widget))
    second = await read_widget_data(None, viewer, request_for(widget))
    assert first["presentation"] == second["presentation"] == preset
    assert [list(value) for value in no_cache_no_limit.values()] == [["status", "shape", "data", "fetched_at"]]


# ── OpenCRQ ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("metric", "answer", "expected"),
    [
        ("kev_findings", {"summary": {"kevCount": 4}}, {"value": 4.0, "change": None}),
        (
            "findings_by_severity",
            {"summary": {"severityDistribution": {"low": 1, "critical": 2}}},
            {"items": [{"label": "critical", "value": 2.0}, {"label": "low", "value": 1.0}]},
        ),
        (
            "findings_trend",
            [{"date": "2026-10-01", "newCount": 3, "dismissedCount": 1}],
            {
                "series": [
                    {"name": "New", "points": [{"x": "2026-10-01", "y": 3.0}]},
                    {"name": "Dismissed", "points": [{"x": "2026-10-01", "y": 1.0}]},
                ]
            },
        ),
        ("risk_by_scope", [{"name": "EU", "ale": 1200.5}], {"items": [{"label": "EU", "value": 1200.5}]}),
        (
            "assets_by_exposure",
            {"byExposure": [{"exposure": "internet_facing", "assetCount": 8}]},
            {"items": [{"label": "internet_facing", "value": 8.0}]},
        ),
    ],
)
def test_opencrq_metrics_read_their_own_fields(metric, answer, expected):
    assert live_opencrq.normalize(metric, answer) == expected


def test_an_opencrq_row_metric_sorts_and_formats_its_figures():
    answer = [
        {"name": "Ransomware", "organizationName": "EU", "latestRun": {"aleMean": 1500000}},
        {"name": "Fraud", "organizationName": None, "latestRun": {"aleMean": 2500000}},
        {"name": "Never run", "organizationName": "US", "latestRun": None},
    ]
    assert live_opencrq.normalize("scenario_ale", answer)["rows"] == [
        {"name": "Fraud", "scope": "", "ale": "2,500,000"},
        {"name": "Ransomware", "scope": "EU", "ale": "1,500,000"},
        {"name": "Never run", "scope": "US", "ale": ""},
    ]


def _recording(handler):
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return seen, record


def _use_transport(monkeypatch, handler) -> None:
    monkeypatch.setattr(_http, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_an_opencrq_metric_calls_its_procedure_for_the_only_tenant(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/trpc/tenants.getTenants":
            return httpx.Response(200, json={"result": {"data": [{"id": "tenant-1", "name": "Main"}]}})
        return httpx.Response(200, json={"result": {"data": [{"name": "EU", "ale": 10}]}})

    seen, record = _recording(handler)
    _use_transport(monkeypatch, record)
    native = check_native("opencrq", {"metric": "risk_by_scope"}).native
    request = LiveRequest(source="opencrq", integration_id=INTEGRATION, native=native, shape="distribution", range_days=30)
    data = await live_opencrq.fetch(_client(provider="opencrq"), request)
    assert data == {"items": [{"label": "EU", "value": 10.0}]}
    call = seen[1]
    assert call.url.path == "/trpc/dashboard.getRiskScopeFigures"
    assert json.loads(call.url.params["input"]) == {"windowDays": 30}
    assert call.headers["x-tenant-id"] == "tenant-1"
    assert call.headers["authorization"] == "Bearer key"


async def test_an_opencrq_metric_with_several_tenants_and_no_tenant_field_is_unavailable(monkeypatch):
    _use_transport(
        monkeypatch,
        lambda _r: httpx.Response(200, json={"result": {"data": [{"id": "a"}, {"id": "b"}]}}),
    )
    native = check_native("opencrq", {"metric": "kev_findings"}).native
    request = LiveRequest(source="opencrq", integration_id=INTEGRATION, native=native, shape="number")
    with pytest.raises(LiveError) as failed:
        await live_opencrq.fetch(_client(provider="opencrq"), request)
    assert failed.value.code == "tenant_required"


def test_a_long_series_keeps_its_whole_range_in_summed_buckets():
    points = [(f"d{i:03d}", 1) for i in range(365)]
    [series] = dashboard_live.series_data([("Reports", points)])["series"]
    assert len(series["points"]) == 53
    assert series["points"][0] == {"x": "d000", "y": 7.0}
    assert series["points"][-1] == {"x": "d364", "y": 1.0}
    assert sum(p["y"] for p in series["points"]) == 365.0


def test_a_long_level_series_is_averaged_not_summed():
    points = [(f"2026-{1 + i // 31:02d}-{1 + i % 31:02d}", 1_000_000) for i in range(366)]
    [series] = dashboard_live.series_data([("ALE", points)], aggregate="mean")["series"]
    assert len(series["points"]) == 53
    assert {p["y"] for p in series["points"]} == {1_000_000.0}


def test_series_of_different_lengths_share_one_axis():
    days = [f"2026-01-{d:02d}" for d in range(1, 32)] + [f"2026-02-{d:02d}" for d in range(1, 29)]
    days += [f"2026-03-{d:02d}" for d in range(1, 32)] + [f"2026-04-{d:02d}" for d in range(1, 31)]
    long = [(d, 1) for d in days]  # 120 days
    short = [(d, 2) for d in days[::2]]  # every other day
    a, b = dashboard_live.series_data([("A", long), ("B", short)])["series"]
    assert [p["x"] for p in a["points"]] == [p["x"] for p in b["points"]] == days[::2]
    assert {p["y"] for p in a["points"]} == {2.0} and {p["y"] for p in b["points"]} == {2.0}


# ── The bounded request ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(401, json={}), "access_denied"),
        (httpx.Response(403, json={}), "access_denied"),
        (httpx.Response(500, json={}), "upstream_error"),
        (httpx.Response(302, headers={"location": "http://169.254.169.254/latest"}), "upstream_error"),
        (httpx.Response(200, text="<html>login</html>", headers={"content-type": "text/html"}), "upstream_error"),
        (httpx.Response(200, content=b"x" * (_http.MAX_BODY_BYTES + 1), headers={"content-type": "application/json"}), "upstream_error"),
        (httpx.Response(200, content=b"\x1f\x8b", headers={"content-type": "application/json", "content-encoding": "gzip"}), "upstream_error"),
        (httpx.Response(200, content=b"{nope", headers={"content-type": "application/json"}), "upstream_error"),
    ],
    ids=["401", "403", "500", "redirect", "html", "oversize", "gzip", "bad-json"],
)
async def test_an_answer_that_is_not_a_small_json_200_is_a_fixed_code(monkeypatch, response, code):
    seen, record = _recording(lambda _r: response)
    _use_transport(monkeypatch, record)
    with pytest.raises(LiveError) as failed:
        await _http.request_json("GET", "https://cti.example/graphql", {})
    assert failed.value.code == code
    # One request: a redirect is never followed.
    assert len(seen) == 1
    assert seen[0].headers["accept-encoding"] == "identity"


async def test_a_platform_that_does_not_answer_in_time_is_a_timeout(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    _use_transport(monkeypatch, handler)
    with pytest.raises(LiveError) as failed:
        await _http.request_json("GET", "https://cti.example/graphql", {})
    assert failed.value.code == "timeout"


async def test_the_real_client_refuses_a_link_local_address():
    with pytest.raises(LiveError) as failed:
        await _http.request_json("GET", "http://169.254.169.254/latest/meta-data", {})
    assert failed.value.code == "upstream_error"


# ── OpenCTI saved filters ─────────────────────────────────────────────────────


def _saved(filters: object, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "savedFilter(" in body["query"]:
            return httpx.Response(status, json={"data": {"savedFilter": None if filters is None else {"filters": filters}}})
        return httpx.Response(200, json={"data": {"stixCoreObjectsNumber": {"total": 3, "count": 0}}})

    return handler


async def test_a_saved_filter_is_read_through_the_viewer_s_access_and_put_in_its_place(monkeypatch):
    seen, record = _recording(_saved(json.dumps({"mode": "and", "filters": [{"key": ["entity_type"], "values": ["Report"], "operator": "eq", "mode": "or"}], "filterGroups": []})))
    _use_transport(monkeypatch, record)
    native = check_native("opencti", {"type": "number", "perspective": "entities", "dataSelection": [{"filters_id": "8a5f-saved"}]}).native
    assert native["dataSelection"][0]["filters_id"] == "8a5f-saved"
    resolved = await live_opencti.resolve_saved_filters(_client(), native)
    assert "filters_id" not in resolved["dataSelection"][0]
    assert resolved["dataSelection"][0]["filters"]["filters"][0]["values"] == ["Report"]
    assert json.loads(seen[0].content)["variables"] == {"id": "8a5f-saved"}
    # The stored widget keeps its reference.
    assert native["dataSelection"][0]["filters_id"] == "8a5f-saved"


@pytest.mark.parametrize(
    ("filters", "code"),
    [(None, "access_denied"), ("not json", "upstream_error"), (json.dumps({"mode": "and", "filters": [{"key": ["x"], "values": [], "operator": "script"}]}), "upstream_error")],
    ids=["not-readable", "unreadable", "a-script"],
)
async def test_a_saved_filter_that_cannot_be_used_stops_the_widget(monkeypatch, filters, code):
    _seen, record = _recording(_saved(filters))
    _use_transport(monkeypatch, record)
    native = check_native("opencti", {"type": "number", "perspective": "entities", "dataSelection": [{"filters_id": "8a5f-saved"}]}).native
    with pytest.raises(LiveError) as failed:
        await live_opencti.resolve_saved_filters(_client(), native)
    assert failed.value.code == code


async def test_a_file_s_saved_filters_are_written_in_place_one_by_one(monkeypatch):
    readable = json.dumps({"mode": "and", "filters": [{"key": ["entity_type"], "values": ["Report"], "operator": "eq", "mode": "or"}], "filterGroups": []})

    def handler(request: httpx.Request) -> httpx.Response:
        saved_id = json.loads(request.content)["variables"]["id"]
        return httpx.Response(200, json={"data": {"savedFilter": {"filters": readable} if saved_id == "sf-ok" else None}})

    _seen, record = _recording(handler)
    _use_transport(monkeypatch, record)
    native = check_native(
        "opencti", {"type": "number", "perspective": "relationships", "dataSelection": [{"filters_id": "sf-ok", "dynamicTo_id": "sf-hidden"}]}
    ).native
    resolved = await live_opencti.resolve_saved_filters(_client(), native, keep_unreadable=True)
    selection = resolved["dataSelection"][0]
    assert ("filters_id" in selection, selection["filters"]["filters"][0]["values"], selection["dynamicTo_id"]) == (False, ["Report"], "sf-hidden")
    # Drawn, a filter the viewer cannot read still stops the widget.
    with pytest.raises(LiveError) as failed:
        await live_opencti.resolve_saved_filters(_client(), native)
    assert failed.value.code == "access_denied"


# ── A published link's marking ceiling ────────────────────────────────────────

_MARKINGS = [
    {"id": "m-clear", "definition": "TLP:CLEAR", "definition_type": "TLP", "x_opencti_order": 1},
    {"id": "m-green", "definition": "TLP:GREEN", "definition_type": "TLP", "x_opencti_order": 2},
    {"id": "m-amber", "definition": "TLP:AMBER", "definition_type": "TLP", "x_opencti_order": 3},
    {"id": "m-red", "definition": "TLP:RED", "definition_type": "TLP", "x_opencti_order": 4},
    {"id": "p-green", "definition": "PAP:GREEN", "definition_type": "PAP", "x_opencti_order": 2},
    {"id": "s-secret", "definition": "SECRET", "definition_type": "statement", "x_opencti_order": 9},
]


@pytest.mark.parametrize(
    ("ceiling", "excluded"),
    [
        (("TLP:GREEN",), ["m-amber", "m-red", "p-green", "s-secret"]),
        (("TLP:AMBER", "PAP:GREEN"), ["m-red", "s-secret"]),
        # A name the platform does not know allows nothing marked.
        (("TLP:NOPE",), ["m-amber", "m-clear", "m-green", "m-red", "p-green", "s-secret"]),
    ],
    ids=["one-type", "two-types", "unknown"],
)
def test_a_ceiling_leaves_out_what_is_marked_above_it_or_of_a_type_it_does_not_name(ceiling, excluded):
    assert live_opencti.marking_exclusions(_MARKINGS, ceiling) == excluded


async def test_an_opencti_read_under_a_ceiling_asks_for_nothing_marked_above_it(monkeypatch, no_cache_no_limit):
    _resolver(monkeypatch, _client())

    def handler(request: httpx.Request) -> httpx.Response:
        if "markingDefinitions" in json.loads(request.content)["query"]:
            return httpx.Response(200, json={"data": {"markingDefinitions": {"edges": [{"node": m} for m in _MARKINGS]}}})
        return httpx.Response(200, json={"data": {"stixCoreObjectsNumber": {"total": 7, "count": 1}}})

    seen, record = _recording(handler)
    _use_transport(monkeypatch, record)
    widget = {"source": "opencti", "integration_id": str(INTEGRATION), "native": _cti("number")}
    under = await read_widget_data(None, uuid.uuid4(), request_for(widget, None, ("TLP:AMBER", "PAP:GREEN")))
    assert under["status"] == "ok"
    filters = json.loads(seen[1].content)["variables"]["filters"]
    assert {"key": ["objectMarking"], "values": ["m-red", "s-secret"], "operator": "not_eq", "mode": "and"} in filters["filters"]
    # Read again without the ceiling: another answer, never the one kept for it.
    assert request_for(widget).marking_ceiling == ()
    assert dashboard_live.cache_key(uuid.UUID(int=1), request_for(widget)) != dashboard_live.cache_key(
        uuid.UUID(int=1), request_for(widget, None, ("TLP:AMBER",))
    )


async def test_under_a_ceiling_what_a_widget_reaches_above_it_is_restricted(monkeypatch):
    red, clear = [{"id": "m-red"}], [{"id": "m-clear"}]
    rows = {"data": {"stixRelationships": {"pageInfo": {"globalCount": 1}, "edges": [{"node": {
        "id": "rel", "entity_type": "uses", "representative": {"main": "APT28 \u27a1\ufe0f X-Agent", "secondary": "secret"},
        "objectMarking": clear, "createdBy": {"name": "Secret Org", "objectMarking": red},
        "from": {"id": "a", "entity_type": "Intrusion-Set", "representative": {"main": "APT28"}, "objectMarking": clear},
        "to": {"id": "m", "entity_type": "Malware", "representative": {"main": "X-Agent"}, "objectMarking": red},
    }}]}}}  # fmt: skip
    distribution = {"data": {"stixRelationshipsDistribution": [
        {"label": "c1", "value": 4, "entity": {"id": "c1", "entity_type": "Country", "representative": {"main": "Atlantis"}, "objectMarking": red}},
        {"label": "c2", "value": 2, "entity": {"id": "c2", "entity_type": "Country", "representative": {"main": "France"}, "objectMarking": clear}},
    ]}}  # fmt: skip

    def handler(answer):
        def respond(request: httpx.Request) -> httpx.Response:
            if "markingDefinitions" in json.loads(request.content)["query"]:
                return httpx.Response(200, json={"data": {"markingDefinitions": {"edges": [{"node": m} for m in _MARKINGS]}}})
            return httpx.Response(200, json=answer)

        return respond

    _seen, record = _recording(handler(rows))
    _use_transport(monkeypatch, record)
    listed = await live_opencti.fetch(
        _client(), LiveRequest(source="opencti", integration_id=INTEGRATION, native=_cti("list", "relationships"), shape="rows", marking_ceiling=("TLP:AMBER",))
    )
    row = listed["rows"][0]
    assert (row["from_relationship_type"], row["to_relationship_type"], row["to_entity_type"]["label"]) == ("APT28", "Restricted", "Restricted")
    # The author is an identity: marked above the ceiling, it is not named either.
    assert row["createdBy"] == "Restricted"
    assert "X-Agent" not in json.dumps(listed) and "Secret Org" not in json.dumps(listed)
    assert live_opencti.mask_restricted(rows, {"m-red"})["data"]["stixRelationships"]["edges"][0]["node"]["representative"] == {
        "main": "APT28 \u27a1\ufe0f Restricted", "secondary": None,
    }  # fmt: skip

    seen, record = _recording(handler(distribution))
    _use_transport(monkeypatch, record)
    native = _cti("distribution-list", "relationships")
    native["dataSelection"][0]["attribute"] = "internal_id"
    shown = await live_opencti.fetch(
        _client(), LiveRequest(source="opencti", integration_id=INTEGRATION, native=native, shape="distribution", marking_ceiling=("TLP:AMBER",))
    )
    assert [(item["label"], item.get("link")) for item in shown["items"]] == [
        ("Restricted", None), ("France", "https://cti.example/dashboard/id/c2"),
    ]  # fmt: skip
    assert "objectMarking { id }" in json.loads(seen[1].content)["query"]

    # A map's places: one marked above the ceiling is not drawn at all.
    places = {"data": {"stixRelationshipsDistribution": [
        {"label": "c1", "value": 3, "entity": {"id": "c1", "entity_type": "City", "name": "Area 51", "latitude": 37.2, "longitude": -115.8, "objectMarking": red}},
        {"label": "c2", "value": 2, "entity": {"id": "c2", "entity_type": "City", "name": "Lyon", "latitude": 45.76, "longitude": 4.84, "objectMarking": clear}},
    ]}}  # fmt: skip
    seen, record = _recording(handler(places))
    _use_transport(monkeypatch, record)
    native = check_native("opencti", {"type": "map", "perspective": "relationships", "dataSelection": [{"attribute": "internal_id", "number": 400, "isTo": True}]}).native
    drawn = await live_opencti.fetch(
        _client(), LiveRequest(source="opencti", integration_id=INTEGRATION, native=native, shape="geo", marking_ceiling=("TLP:AMBER",))
    )
    assert [p["label"] for p in drawn["points"]] == ["Lyon"]
    assert "objectMarking { id }" in json.loads(seen[1].content)["query"]


async def test_without_the_platform_s_markings_a_ceiling_reads_nothing(monkeypatch, no_cache_no_limit):
    _resolver(monkeypatch, _client())
    _seen, record = _recording(lambda _r: httpx.Response(200, json={"data": {"markingDefinitions": None}}))
    _use_transport(monkeypatch, record)
    widget = {"source": "opencti", "integration_id": str(INTEGRATION), "native": _cti("number")}
    result = await read_widget_data(None, uuid.uuid4(), request_for(widget, None, ("TLP:GREEN",)))
    assert result["code"] == "upstream_error"


# ── read_widget_data ─────────────────────────────────────────────────────────


@pytest.fixture
def no_cache_no_limit(monkeypatch):
    store: dict[str, dict] = {}

    async def get(key):
        return store.get(key)

    async def put(key, value, ttl=None):
        store[key] = value

    async def no_limit(_user):
        return None

    monkeypatch.setattr(dashboard_live, "_cache_get", get)
    monkeypatch.setattr(dashboard_live, "_cache_set", put)
    monkeypatch.setattr(dashboard_live, "_enforce_rate", no_limit)
    return store


def _resolver(monkeypatch, outcome):
    calls: list[tuple] = []

    async def resolve(db, user_id, integration_id, provider):
        calls.append((user_id, integration_id, provider))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr("app.services.platform_access.resolve_platform_client", resolve)
    return calls


async def test_an_unusable_integration_is_reported_before_anything_is_read(monkeypatch, no_cache_no_limit):
    _resolver(monkeypatch, PlatformAccessError())
    _use_transport(monkeypatch, lambda _r: pytest.fail("no request may be sent"))
    widget = {"source": "opencti", "integration_id": str(INTEGRATION), "native": _cti("number")}
    result = await read_widget_data(None, uuid.uuid4(), request_for(widget))
    assert result == {"status": "error", "code": "integration_unavailable", "open_url": None}


async def test_a_type_xtm_one_does_not_draw_is_a_link(monkeypatch, no_cache_no_limit):
    _resolver(monkeypatch, _client())
    # Activity and history: read by OpenCTI administrators only.
    native = check_native("opencti", {"type": "number", "perspective": "audits", "dataSelection": [{}]}).native
    widget = {"source": "opencti", "integration_id": str(INTEGRATION), "native": native}
    result = await read_widget_data(None, uuid.uuid4(), request_for(widget))
    assert result == {"status": "error", "code": "unsupported", "open_url": "https://cti.example/dashboard/workspaces/dashboards"}


async def test_data_is_cached_per_viewer_after_the_access_check(monkeypatch, no_cache_no_limit):
    calls = _resolver(monkeypatch, _client())
    seen, record = _recording(
        lambda _r: httpx.Response(200, json={"data": {"stixCoreObjectsNumber": {"total": 7, "count": 1}}})
    )
    _use_transport(monkeypatch, record)
    viewer = uuid.uuid4()
    widget = {"source": "opencti", "integration_id": str(INTEGRATION), "native": _cti("number")}
    first = await read_widget_data(None, viewer, request_for(widget, "7d"))
    second = await read_widget_data(None, viewer, request_for(widget, "7d"))
    assert first["status"] == "ok" and first["shape"] == "number" and first["data"] == {"value": 7.0, "change": 6.0, "previous": 1.0, "period": "day"}
    assert second["data"] == first["data"]
    # The platform was asked once; the access check ran both times.
    assert len(seen) == 1
    assert len(calls) == 2
    assert json.loads(seen[0].content)["variables"]["startDate"] is not None
    # Another viewer is never served the first one's answer.
    await read_widget_data(None, uuid.uuid4(), request_for(widget, "7d"))
    assert len(seen) == 2


async def test_a_refresh_reads_the_platform_again_and_keeps_its_answer_for_the_next_readers(monkeypatch, no_cache_no_limit):
    _resolver(monkeypatch, _client())
    totals = iter([7, 9])
    seen, record = _recording(
        lambda _r: httpx.Response(200, json={"data": {"stixCoreObjectsNumber": {"total": next(totals), "count": 1}}})
    )
    _use_transport(monkeypatch, record)
    viewer = uuid.uuid4()
    widget = {"source": "opencti", "integration_id": str(INTEGRATION), "native": _cti("number")}
    first = await read_widget_data(None, viewer, request_for(widget, "7d"))
    refreshed = await read_widget_data(None, viewer, request_for(widget, "7d"), fresh=True)
    after = await read_widget_data(None, viewer, request_for(widget, "7d"))
    assert [first["data"]["value"], refreshed["data"]["value"], after["data"]["value"]] == [7.0, 9.0, 9.0]
    assert len(seen) == 2


async def test_a_page_of_widgets_read_at_once_waits_its_turn_and_draws_every_widget(monkeypatch, no_cache_no_limit):
    _resolver(monkeypatch, _client())
    running, peak = 0, 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1
        return httpx.Response(200, json={"data": {"stixCoreObjectsNumber": {"total": 7, "count": 1}}})

    _use_transport(monkeypatch, handler)
    viewer = uuid.uuid4()
    widgets = [
        {"source": "opencti", "integration_id": str(INTEGRATION), "native": _cti("number", title=f"w{index}")}
        for index in range(dashboard_live.CONCURRENT_READS + 3)
    ]
    results = await asyncio.gather(*(read_widget_data(None, viewer, request_for(w)) for w in widgets))
    assert [r["status"] for r in results] == ["ok"] * len(widgets)
    assert peak == dashboard_live.CONCURRENT_READS
    assert dashboard_live._slots == {}


async def test_a_read_that_waits_too_long_is_rate_limited(monkeypatch, no_cache_no_limit):
    _resolver(monkeypatch, _client())
    monkeypatch.setattr(dashboard_live, "QUEUE_SECONDS", 0.05)
    viewer = str(uuid.uuid4())
    busy = asyncio.Semaphore(dashboard_live.CONCURRENT_READS)
    for _ in range(dashboard_live.CONCURRENT_READS):
        await busy.acquire()
    monkeypatch.setitem(dashboard_live._slots, viewer, (busy, dashboard_live.CONCURRENT_READS))
    widget = {"source": "opencti", "integration_id": str(INTEGRATION), "native": _cti("number")}
    result = await read_widget_data(None, uuid.UUID(viewer), request_for(widget))
    assert result["status"] == "error" and result["code"] == "rate_limited"
    assert dashboard_live._slots[viewer] == (busy, dashboard_live.CONCURRENT_READS)


async def test_a_full_queue_is_rate_limited_without_waiting(monkeypatch, no_cache_no_limit):
    _resolver(monkeypatch, _client())
    viewer = str(uuid.uuid4())
    busy = asyncio.Semaphore(0)
    monkeypatch.setitem(dashboard_live._slots, viewer, (busy, dashboard_live.MAX_QUEUED))
    widget = {"source": "opencti", "integration_id": str(INTEGRATION), "native": _cti("number")}
    result = await read_widget_data(None, uuid.UUID(viewer), request_for(widget))
    assert result["status"] == "error" and result["code"] == "rate_limited"
    assert dashboard_live._slots[viewer] == (busy, dashboard_live.MAX_QUEUED)


# ── STIX standard ids ────────────────────────────────────────────────────────

AUTHOR = "identity--11111111-1111-4111-8111-111111111111"
MARKING = "marking-definition--22222222-2222-4222-8222-222222222222"


def _with_stix_filters() -> dict:
    return check_native(
        "opencti",
        {
            "type": "number",
            "perspective": "entities",
            "dataSelection": [
                {
                    "filters": {
                        "mode": "and",
                        "filters": [
                            {"key": ["createdBy"], "values": [AUTHOR, "not-an-id"]},
                            {"key": ["regardingOf"], "values": [{"key": "id", "values": [MARKING]}]},
                        ],
                        "filterGroups": [{"mode": "or", "filters": [{"key": ["objectMarking"], "values": [MARKING]}], "filterGroups": []}],
                    }
                }
            ],
        },
    ).native


def test_the_standard_ids_of_every_filter_are_found_once_and_replaced():
    native = _with_stix_filters()
    assert live_opencti.standard_ids([native]) == [AUTHOR, MARKING]
    replaced = live_opencti.replace_ids(native, {AUTHOR: "internal-a", MARKING: "internal-m"})
    group = replaced["dataSelection"][0]["filters"]
    assert group["filters"][0]["values"] == ["internal-a", "not-an-id"]
    assert group["filters"][1]["values"] == [{"key": "id", "values": ["internal-m"]}]
    assert group["filterGroups"][0]["filters"][0]["values"] == ["internal-m"]
    # The stored native is untouched.
    assert native["dataSelection"][0]["filters"]["filters"][0]["values"] == [AUTHOR, "not-an-id"]


async def test_the_ids_are_resolved_in_one_query_and_unknown_ones_kept(monkeypatch):
    seen, record = _recording(
        lambda _r: httpx.Response(200, json={"data": {"i0": {"id": "internal-a"}, "i1": None}})
    )
    _use_transport(monkeypatch, record)
    mapping = await live_opencti.resolve_standard_ids(_client(), [_with_stix_filters()])
    assert mapping == {AUTHOR: "internal-a"}
    [request] = seen
    assert json.loads(request.content)["variables"] == {"i0": AUTHOR, "i1": MARKING}


async def test_a_platform_that_cannot_resolve_leaves_the_ids_as_exported(monkeypatch):
    _use_transport(monkeypatch, lambda _r: httpx.Response(403, json={}))
    assert await live_opencti.resolve_standard_ids(_client(), [_with_stix_filters()]) == {}


async def test_a_widget_is_read_with_its_standard_ids_resolved_first(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "XtmOneResolveIds" in body["query"]:
            return httpx.Response(200, json={"data": {"i0": {"id": "internal-a"}, "i1": {"id": "internal-m"}}})
        return httpx.Response(200, json={"data": {"stixCoreObjectsNumber": {"total": 3, "count": 1}}})

    seen, record = _recording(handler)
    _use_transport(monkeypatch, record)
    native = _with_stix_filters()
    request = LiveRequest(source="opencti", integration_id=INTEGRATION, native=native, shape="number")
    assert await live_opencti.fetch(_client(), request) == {"value": 3.0, "change": 2.0, "previous": 1.0, "period": "day"}
    resolution, query = seen
    assert "XtmOneResolveIds" in json.loads(resolution.content)["query"]
    sent = json.loads(query.content)["variables"]["filters"]
    assert sent["filters"][0]["values"] == ["internal-a", "not-an-id"]
    assert sent["filterGroups"][0]["filters"][0]["values"] == ["internal-m"]
    # The stored configuration keeps the portable ids.
    assert native["dataSelection"][0]["filters"]["filters"][0]["values"] == [AUTHOR, "not-an-id"]


async def test_more_ids_than_one_query_holds_are_resolved_in_several(monkeypatch):
    ids = [f"identity--{i:08x}-1111-4111-8111-111111111111" for i in range(150)]
    native = {"dataSelection": [{"filters": {"mode": "and", "filters": [{"key": ["createdBy"], "values": ids[:100]}, {"key": ["objectAssignee"], "values": ids[100:]}], "filterGroups": []}}]}

    def handler(request: httpx.Request) -> httpx.Response:
        variables = json.loads(request.content)["variables"]
        return httpx.Response(200, json={"data": {alias: {"id": f"in-{sid[10:18]}"} for alias, sid in variables.items()}})

    seen, record = _recording(handler)
    _use_transport(monkeypatch, record)
    mapping = await live_opencti.resolve_standard_ids(_client(), [native])
    assert [len(json.loads(r.content)["variables"]) for r in seen] == [100, 50]
    assert len(mapping) == 150 and mapping[ids[149]] == "in-00000095"
