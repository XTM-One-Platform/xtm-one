"""The platform source editors' questions to OpenCTI and OpenAEV: fixed queries, closed answers.

Each question is a query written here, what the person typed travelling only
as a GraphQL variable or a URL parameter; each answer is cut to closed lists
of short strings; a refusal is a fixed code. The transport is replaced by a
recorder: these tests pin what is sent and what comes back.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import dashboard_platform_schema as schema
from app.services.dashboard_live import LiveError

CTI = SimpleNamespace(base_url="https://cti.example", headers={"Authorization": "Bearer t"}, tenant_id="", route=None)
AEV = SimpleNamespace(base_url="https://aev.example", headers={"Authorization": "Bearer t"}, tenant_id="", route=None)

FILTER_KEYS = {
    "filterKeysSchema": [
        {
            "entity_type": "Stix-Core-Object",
            "filters_schema": [
                {"filterKey": "objectLabel", "filterDefinition": {"filterKey": "objectLabel", "label": "Label", "type": "id", "multiple": True, "elementsForFilterValuesSearch": ["Label"]}},
                {"filterKey": "createdBy", "filterDefinition": {"filterKey": "createdBy", "label": "Author", "type": "id", "multiple": True, "elementsForFilterValuesSearch": ["Organization", "Individual"]}},
                {"filterKey": "objects", "filterDefinition": {"filterKey": "objects", "label": "Contains", "type": "id", "multiple": True, "elementsForFilterValuesSearch": ["Stix-Core-Object"]}},
                {"filterKey": "report_types", "filterDefinition": {"filterKey": "report_types", "label": "Report type", "type": "vocabulary", "multiple": True, "elementsForFilterValuesSearch": ["report_types_ov"]}},
                {"filterKey": "x_opencti_reliability", "filterDefinition": {"filterKey": "x_opencti_reliability", "label": "Reliability", "type": "enum", "multiple": True, "elementsForFilterValuesSearch": ["A - Completely reliable", "B - Usually reliable"]}},
                {"filterKey": "revoked", "filterDefinition": {"filterKey": "revoked", "label": "Revoked", "type": "boolean", "multiple": False}},
                {"filterKey": "created", "filterDefinition": {"filterKey": "created", "label": "Created", "type": "date", "multiple": False}},
            ],
        },
        {"entity_type": "History", "filters_schema": [{"filterKey": "event_type", "filterDefinition": {"filterKey": "event_type", "label": "Event", "type": "enum", "multiple": True, "elementsForFilterValuesSearch": ["create"]}}]},
        {"entity_type": "stix-core-relationship", "filters_schema": [{"filterKey": "fromId", "filterDefinition": {"filterKey": "fromId", "label": "Source", "type": "id", "multiple": True, "elementsForFilterValuesSearch": ["Stix-Core-Object"]}}]},
    ]
}  # fmt: skip


class Recorder:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    async def __call__(self, method, url, headers, *, json_body=None, params=None, what="platform", **_):
        self.calls.append({"method": method, "url": url, "json": json_body, "params": params})
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def transport(monkeypatch):
    def install(*answers):
        recorder = Recorder(answers)
        monkeypatch.setattr(schema, "request_json", recorder)
        return recorder

    return install


async def test_entity_types_are_the_platform_types_of_the_perspective(transport):
    sent = transport({"data": {
        "sdos": {"edges": [{"node": {"id": "Report", "label": "Report"}}, {"node": {"id": "Malware", "label": "Malware"}}]},
        "scos": {"edges": [{"node": {"id": "IPv4-Addr", "label": "IPv4-Addr"}}]},
        "scrs": {"edges": [{"node": {"id": "uses", "label": "uses"}}]},
    }})  # fmt: skip
    items = await schema._opencti(CTI, "entity_types", {"perspective": "entities"})
    assert [(i["value"], i["group"]) for i in items] == [("Report", "sdos"), ("Malware", "sdos"), ("IPv4-Addr", "scos")]
    assert sent.calls[0]["json"] == {"query": schema._CTI_TYPES, "variables": {}}


@pytest.mark.parametrize(
    ("perspective", "names"),
    [("entities", ["Reports", "Mine"]), ("relationships", ["Links"]), ("audits", ["Logins"])],
)
async def test_saved_filters_are_those_of_the_widget_s_perspective(transport, perspective, names):
    transport({"data": {"savedFilters": {"edges": [
        {"node": {"id": "s1", "name": "Reports", "scope": "Stix-Core-Object"}},
        {"node": {"id": "s2", "name": "Links", "scope": "relationships"}},
        {"node": {"id": "s3", "name": "Logins", "scope": "audits"}},
        {"node": {"id": "s4", "name": "Mine", "scope": "view-reports"}},
    ]}}})  # fmt: skip
    items = await schema._opencti(CTI, "saved_filters", {"perspective": perspective})
    assert [i["label"] for i in items] == names


async def test_filter_keys_follow_the_widget_editor_rules(transport):
    transport({"data": FILTER_KEYS}, {"data": FILTER_KEYS})
    entities = await schema._opencti(CTI, "filter_keys", {"perspective": "entities"})
    relationships = await schema._opencti(CTI, "filter_keys", {"perspective": "relationships"})
    assert [k["key"] for k in entities] == [
        "created", "createdBy", "entity_type", "objectLabel", "objects", "report_types", "revoked", "x_opencti_reliability",
    ]  # fmt: skip
    assert next(k for k in entities if k["key"] == "createdBy")["search"] == ["Organization", "Individual"]
    assert [k["key"] for k in relationships] == ["fromId"]


async def test_a_value_search_sends_what_was_typed_as_a_variable_only(transport):
    typed = '") { __schema { types { name } } } #'
    sent = transport({"data": FILTER_KEYS}, {"data": {"stixCoreObjects": {"edges": [
        {"node": {"id": "r1", "entity_type": "Report", "representative": {"main": "Q3 report"}}},
    ]}}})  # fmt: skip
    items = await schema._opencti(CTI, "values", {"perspective": "entities", "key": "objects", "search": typed})
    assert items == [{"value": "r1", "label": "Q3 report", "group": "Report"}]
    assert sent.calls[1]["json"] == {"query": schema._CTI_SEARCH["core"], "variables": {"search": typed, "types": ["Stix-Core-Object"]}}


@pytest.mark.parametrize(
    ("key", "answer", "query", "variables", "expected"),
    [
        (
            "objectLabel",
            {"labels": {"edges": [{"node": {"id": "l1", "value": "ransomware"}}]}},
            "labels",
            {"search": "ran"},
            [{"value": "l1", "label": "ransomware"}],
        ),
        (
            "report_types",
            {"vocabularies": {"edges": [{"node": {"id": "v1", "name": "threat-report"}}]}},
            "vocabulary",
            {"search": "ran", "filters": {"mode": "or", "filters": [{"key": "category", "values": ["report_types_ov"], "operator": "eq", "mode": "or"}], "filterGroups": []}},
            [{"value": "threat-report", "label": "threat-report"}],
        ),
        (
            "createdBy",
            {"identities": {"edges": [{"node": {"id": "o1", "name": "Ransom Watch", "entity_type": "Organization"}}]}},
            "identities",
            {"search": "ran", "types": ["Organization", "Individual"]},
            [{"value": "o1", "label": "Ransom Watch", "group": "Organization"}],
        ),
    ],
)
async def test_each_kind_of_key_is_searched_the_way_the_platform_does(transport, key, answer, query, variables, expected):
    sent = transport({"data": FILTER_KEYS}, {"data": answer})
    assert await schema._opencti(CTI, "values", {"perspective": "entities", "key": key, "search": "ran"}) == expected
    assert sent.calls[1]["json"] == {"query": schema._CTI_SEARCH[query], "variables": variables}


async def test_enum_and_boolean_values_come_from_the_schema(transport):
    transport({"data": FILTER_KEYS}, {"data": FILTER_KEYS})
    assert await schema._opencti(CTI, "values", {"perspective": "entities", "key": "x_opencti_reliability", "search": "usually"}) == [
        {"value": "B - Usually reliable", "label": "B - Usually reliable"}
    ]
    assert [v["value"] for v in await schema._opencti(CTI, "values", {"perspective": "entities", "key": "revoked", "search": ""})] == ["true", "false"]


async def test_a_key_the_schema_does_not_hold_or_cannot_search_is_unsupported(transport):
    transport({"data": FILTER_KEYS}, {"data": FILTER_KEYS})
    with pytest.raises(LiveError, match="unsupported"):
        await schema._opencti(CTI, "values", {"perspective": "entities", "key": "nope", "search": ""})
    with pytest.raises(LiveError, match="unsupported"):
        await schema._opencti(CTI, "values", {"perspective": "entities", "key": "created", "search": ""})


async def test_attributes_drop_internal_ones_expand_hashes_and_add_the_common_ones(transport):
    sent = transport({"data": {"schemaAttributeNames": {"edges": [{"node": {"value": v}} for v in ("name", "i_aliases_ids", "hashes", "report_types")]}}})
    items = [i["value"] for i in await schema._opencti(CTI, "attributes", {"perspective": "entities", "types": ["Report", "File"]})]
    assert items[:7] == ["entity_type", "hashes.MD5", "hashes.SHA-1", "hashes.SHA-256", "hashes.SHA-512", "name", "report_types"]
    assert items[7:] == list(schema._CTI_COMMON_ATTRIBUTES)
    assert sent.calls[0]["json"]["variables"] == {"elementType": ["Report", "File"]}


async def test_stored_values_are_named_through_the_representatives(transport):
    sent = transport({"data": {"filtersRepresentatives": [{"id": "l1", "value": "ransomware", "entity_type": "Label"}, {"id": "gone", "value": None}]}})
    items = await schema._opencti(CTI, "labels", {"key": "objectLabel", "ids": ["l1", "gone"]})
    assert items == [{"value": "l1", "label": "ransomware", "group": "Label"}, {"value": "gone", "label": "gone"}]
    assert sent.calls[0]["json"]["variables"]["filters"]["filters"] == [{"key": ["objectLabel"], "values": ["l1", "gone"], "operator": "eq", "mode": "or"}]


async def test_a_regarding_of_filter_searches_its_entity_and_its_relationship_types_apart(transport):
    sent = transport(
        {"data": {"stixCoreObjects": {"edges": [{"node": {"id": "m1", "entity_type": "Malware", "representative": {"main": "Emotet"}}}]}}},
        {"data": {"sdos": {"edges": []}, "scos": {"edges": []}, "scrs": {"edges": [{"node": {"id": "targets", "label": "targets"}}]}}},
        {"data": {"filtersRepresentatives": [{"id": "m1", "value": "Emotet", "entity_type": "Malware"}]}},
    )
    found = await schema._opencti(CTI, "values", {"perspective": "entities", "key": "regardingOf.id", "search": "emo"})
    assert found == [{"value": "m1", "label": "Emotet", "group": "Malware"}]
    assert sent.calls[0]["json"] == {"query": schema._CTI_SEARCH["core"], "variables": {"search": "emo", "types": ["Stix-Core-Object"]}}
    kinds = await schema._opencti(CTI, "values", {"perspective": "entities", "key": "regardingOf.relationship_type", "search": ""})
    assert sent.calls[1]["json"]["query"] == schema._CTI_TYPES
    assert [(k["value"], k["label"]) for k in kinds] == [("targets", "targets"), ("stix-sighting-relationship", "Sighting")]
    await schema._opencti(CTI, "labels", {"perspective": "entities", "key": "regardingOf.id", "ids": ["m1"]})
    assert sent.calls[2]["json"]["variables"] == {
        "filters": {"mode": "and", "filters": [{"key": ["regardingOf"], "values": [{"key": "id", "values": ["m1"]}], "operator": "eq", "mode": "or"}], "filterGroups": []}
    }


async def test_an_entity_input_searches_entities_of_its_types_and_names_its_value(transport):
    node = {"id": "c1", "entity_type": "Country", "representative": {"main": "France"}}
    sent = transport({"data": {"stixCoreObjects": {"edges": [{"node": node}]}}}, {"data": {"stixCoreObjects": {"edges": [{"node": node}]}}})
    found = await schema._opencti(CTI, "entities", {"types": ["Country"], "search": "fra"})
    assert found == [{"value": "c1", "label": "France", "group": "Country"}]
    assert sent.calls[0]["json"] == {"query": schema._CTI_ENTITIES, "variables": {"types": ["Country"], "search": "fra"}}
    named = await schema._opencti(CTI, "entities", {"types": ["Country"], "ids": ["c1"], "search": "ignored"})
    assert named == found
    assert sent.calls[1]["json"]["variables"] == {
        "types": ["Country"], "search": None,
        "filters": {"mode": "and", "filters": [{"key": ["id"], "values": ["c1"], "operator": "eq", "mode": "or"}], "filterGroups": []},
    }  # fmt: skip
    anyone = transport({"data": {"stixCoreObjects": {"edges": []}}})
    assert await schema._opencti(CTI, "entities", {"search": "x"}) == []
    # Any entity when the input names no type.
    assert anyone.calls[0]["json"]["variables"]["types"] == ["Stix-Core-Object"]
    with pytest.raises(LiveError) as refused:
        await schema._openaev(AEV, "entities", {"types": ["asset"]})
    assert refused.value.code == "unsupported"


async def test_an_openaev_entity_input_searches_simulations_or_scenarios(transport):
    # The simulation or scenario an OpenAEV dashboard parameter picks, searched and named as its own picker does.
    sent = transport([{"id": "s1", "label": "Q4 phishing drill"}], [{"id": "s1", "label": "Q4 phishing drill"}], [{"id": "sc", "label": "Ransomware"}])
    assert await schema._openaev(AEV, "entities", {"types": ["simulation"], "search": "drill"}) == [{"value": "s1", "label": "Q4 phishing drill"}]
    assert (sent.calls[0]["url"], sent.calls[0]["params"]) == ("https://aev.example/api/simulations/options", {"searchText": "drill"})
    await schema._openaev(AEV, "entities", {"types": ["simulation"], "ids": ["s1"]})
    assert (sent.calls[1]["method"], sent.calls[1]["json"]) == ("POST", ["s1"])
    await schema._openaev(AEV, "entities", {"types": ["scenario"], "search": ""})
    assert sent.calls[2]["url"] == "https://aev.example/api/scenarios/options"
    for types in (["simulation", "scenario"], []):
        with pytest.raises(LiveError, match="unsupported"):
            await schema._openaev(AEV, "entities", {"types": types})


async def test_an_opencti_refusal_is_a_code(transport):
    transport({"errors": [{"message": "You are not allowed to do this.", "extensions": {"code": "FORBIDDEN_ACCESS"}}]}, {"errors": [{"message": "boom"}]}, ["not", "a", "dict"])
    for code in ("access_denied", "upstream_error", "upstream_error"):
        with pytest.raises(LiveError, match=code):
            await schema._opencti(CTI, "entity_types", {"perspective": "entities"})


async def test_openaev_properties_values_and_names(transport):
    tenant = SimpleNamespace(**{**vars(AEV), "tenant_id": "t/1"})
    sent = transport(
        [{"schema_property_name": "base_created_at", "schema_property_label": "Created", "schema_property_type": "instant", "schema_property_entity": "finding"},
         {"schema_property_name": "base_asset_side", "schema_property_type": "string", "schema_property_entity": "inject", "schema_property_has_dynamic_value": True,
          "schema_property_override_operators": ["empty", "not_empty"]},
         {"schema_property_name": "finding_type", "schema_property_type": "string", "schema_property_entity": "finding", "schema_property_values": ["cve", "port"], "schema_property_type_array": False},
         "junk"],
        [{"id": "s1", "label": "Q4 phishing drill"}],
        [{"id": "s1", "label": "Q4 phishing drill"}],
    )  # fmt: skip
    properties = await schema._openaev(tenant, "filter_keys", {"types": ["finding", "not-an-entity"]})
    assert [(p["key"], p["type"], p["values"], p["dynamic"], p["operators"]) for p in properties] == [
        ("base_created_at", "instant", [], False, []),
        ("finding_type", "string", ["cve", "port"], False, []),
        ("base_asset_side", "string", [], True, ["empty", "not_empty"]),
    ]
    assert sent.calls[0]["url"] == "https://aev.example/api/tenants/t%2F1/engine/schemas"
    assert sent.calls[0]["params"] == [("classNames", "finding")]
    found = await schema._openaev(tenant, "values", {"key": "base_simulation_side", "search": "drill"})
    assert found == [{"value": "s1", "label": "Q4 phishing drill"}]
    assert (sent.calls[1]["url"], sent.calls[1]["params"]) == ("https://aev.example/api/tenants/t%2F1/simulations/options", {"searchText": "drill"})
    await schema._openaev(tenant, "labels", {"key": "base_simulation_side", "ids": ["s1"]})
    assert (sent.calls[2]["method"], sent.calls[2]["json"]) == ("POST", ["s1"])
    with pytest.raises(LiveError, match="unsupported"):
        await schema._openaev(tenant, "values", {"key": "base_user_side", "search": ""})


async def test_answers_are_cut_to_closed_short_lists(transport):
    labels = {"labels": {"edges": [{"node": {"id": f"l{i}", "value": "x" * 5000}} for i in range(800)]}}
    transport({"data": FILTER_KEYS}, {"data": labels})
    items = await schema._opencti(CTI, "values", {"perspective": "entities", "key": "objectLabel", "search": ""})
    assert len(items) == schema.MAX_ITEMS
    assert {len(i["label"]) for i in items} == {200}


async def test_read_schema_refuses_an_integration_the_person_cannot_use(monkeypatch):
    from app.services import platform_access

    async def refuse(db, user_id, integration_id, source):
        raise platform_access.PlatformAccessError("integration_unavailable")

    monkeypatch.setattr(platform_access, "resolve_platform_client", refuse)
    answer = await schema.read_schema(None, uuid.uuid4(), uuid.uuid4(), "opencti", "entity_types", {"perspective": "entities"})
    assert answer == {"status": "error", "code": "integration_unavailable"}


async def test_a_search_narrows_the_lists_a_platform_answers_whole(monkeypatch, transport):
    # filterKeysSchema has no search: the agent looking for ``regardingOf`` asked with one and got every key.
    from app.services import dashboard_live, platform_access

    async def client(db, user_id, integration_id, source):
        return CTI

    async def nothing(*_a, **_k):
        return None

    monkeypatch.setattr(platform_access, "resolve_platform_client", client)
    monkeypatch.setattr(dashboard_live, "_cache_get", nothing)
    monkeypatch.setattr(dashboard_live, "_cache_set", nothing)
    monkeypatch.setattr(dashboard_live, "_enforce_rate", nothing)
    transport({"data": FILTER_KEYS}, {"data": FILTER_KEYS})
    narrowed = await schema.read_schema(None, uuid.uuid4(), uuid.uuid4(), "opencti", "filter_keys", {"perspective": "entities", "search": "AUTH"})
    whole = await schema.read_schema(None, uuid.uuid4(), uuid.uuid4(), "opencti", "filter_keys", {"perspective": "entities"})
    assert [k["key"] for k in narrowed["items"]] == ["createdBy"]
    assert len(whole["items"]) > 1


async def test_opencrq_answers_its_metric_catalog_without_asking_it():
    from app.services.dashboard_platform.opencrq import METRICS

    items = await schema._opencrq(None, "entity_types", {})
    assert [i["value"] for i in items] == list(METRICS)
    assert items[0] == {"value": "exposure_trend_ale", "label": "Annual loss expectancy over time", "group": "series", "window": True}
    with pytest.raises(LiveError, match="unsupported"):
        await schema._opencrq(None, "values", {"key": "x"})


# The widgets the editors write (``frontend/src/components/custom-dashboard/platform-native.test.ts``
# pins the same objects): the server keeps them as written.
EDITOR_CTI = {
    "type": "donut",
    "perspective": "entities",
    "dataSelection": [{
        "label": "", "number": 8, "attribute": "created-by.internal_id", "date_attribute": "published", "isTo": True,
        "perspective": "entities",
        "filters": {"mode": "and", "filters": [
            {"key": ["entity_type"], "operator": "eq", "values": ["Report"], "mode": "or"},
            {"key": ["objectLabel"], "operator": "eq", "values": ["l1", "l2"], "mode": "or"},
            {"key": ["confidence"], "operator": "gte", "values": ["75"], "mode": "or"},
        ], "filterGroups": []},
    }],
    "parameters": {"title": "Reports by author"},
}  # fmt: skip
EDITOR_AEV = {
    "widget_type": "donut",
    "widget_config": {
        "widget_configuration_type": "structural-histogram", "time_range": "DEFAULT", "date_attribute": "base_created_at",
        "title": "Findings by type",
        "series": [{"name": "Findings by type", "filter": {"mode": "and", "filters": [
            {"id": "base_entity", "key": "base_entity", "operator": "eq", "mode": "or", "values": ["finding"]},
            {"id": "base_simulation_side-0", "key": "base_simulation_side", "operator": "eq", "mode": "or", "values": ["s1"]},
        ]}}],
        "mode": "structural", "field": "finding_type", "limit": 10,
    },
}  # fmt: skip


@pytest.mark.parametrize(("source", "native", "shape"), [("opencti", EDITOR_CTI, "distribution"), ("openaev", EDITOR_AEV, "distribution")])
def test_the_editors_widgets_are_valid_native_widgets(source, native, shape):
    from app.services.dashboard_platform import check_native

    checked = check_native(source, native)
    assert checked.shape == shape
    if source == "opencti":
        assert checked.native["dataSelection"][0]["filters"] == native["dataSelection"][0]["filters"]
        assert checked.native["dataSelection"][0]["attribute"] == "created-by.internal_id"
    else:
        assert checked.native["widget_config"]["series"][0]["filter"]["filters"][0]["key"] == "base_entity"
        assert checked.native["widget_config"]["field"] == "finding_type"


EDITOR_WIDGETS = json.loads((Path(__file__).parent / "fixtures/dashboards/editor_widgets.json").read_text())


def _without_defaults(value):
    """A configuration without the empty values and false switches the server writes or drops."""
    if isinstance(value, dict):
        kept = {k: _without_defaults(v) for k, v in value.items()}
        return {k: v for k, v in kept.items() if v not in ("", [], False, None)}
    if isinstance(value, list):
        return [_without_defaults(v) for v in value]
    return value


@pytest.mark.parametrize(
    ("source", "native"),
    [("opencti", w) for w in EDITOR_WIDGETS["opencti"]] + [("openaev", w) for w in EDITOR_WIDGETS["openaev"]],
    ids=lambda v: v if isinstance(v, str) else f"{v.get('type') or v.get('widget_type')}-{v.get('perspective', '')}",
)
def test_every_new_editor_widget_is_kept_as_written(source, native):
    # ``platform-native.test.ts`` pins that this file is what the editors write.
    from app.services.dashboard_platform import check_native

    checked = check_native(source, native).native
    if source == "opencti":
        assert checked == native
    else:
        expected = _without_defaults(native)
        # A breakdown or a list with no limit reads 100 rows, as OpenAEV's own default.
        if native["widget_config"]["widget_configuration_type"] in ("structural-histogram", "list"):
            expected["widget_config"].setdefault("limit", 100)
        assert _without_defaults(checked) == expected


async def test_an_export_page_gets_its_entity_inputs_named(monkeypatch):
    """An export's page has no session to search a platform: its entity input showed the entity's id."""
    cti = str(uuid.uuid4())
    asked: list[tuple] = []

    async def _read(db, user_id, integration_id, source, kind, params):
        asked.append((str(integration_id), source, kind, params))
        return {"status": "ok", "items": [{"value": "is-1", "label": "Lazarus Group"}, {"value": "other", "label": "Not asked"}]}

    monkeypatch.setattr(schema, "read_schema", _read)
    definition = {
        "inputs": {
            "pick": {"type": "input.entity", "token": "actor", "defaultValue": "is-1",
                     "options": {"source": "opencti", "integration_id": cti, "entityTypes": ["Intrusion-Set"]}},
            "none": {"type": "input.entity", "token": "unset", "options": {"source": "opencti", "integration_id": cti, "entityTypes": ["Country"]}},
            "text": {"type": "input.text", "token": "q", "defaultValue": "is-1"},
        }
    }  # fmt: skip
    assert await schema.entity_input_names(None, uuid.uuid4(), definition) == {"is-1": "Lazarus Group"}
    assert asked == [(cti, "opencti", "entities", {"types": ["Intrusion-Set"], "ids": ["is-1"]})]

    async def _refused(*_a):
        return {"status": "error", "code": "access_denied"}

    monkeypatch.setattr(schema, "read_schema", _refused)
    assert await schema.entity_input_names(None, uuid.uuid4(), definition) == {}
