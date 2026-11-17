"""manage_opencti_ingestion resolves marking and author refs and reads them back.

OpenCTI stores a feed's ``object_marking_refs`` / ``markings`` /
``created_by_ref`` as given and applies only internal ids: a feed created with
the STIX id of TLP:CLEAR answered "Created" and pushed unmarked reports. The
tool now resolves each ref (internal id, STIX id or name) before writing,
refuses an unknown one with nothing written, and shows what OpenCTI stored.
"""

import json

import app.integrations.providers.opencti.ingestion as ingestion_mod
import app.integrations.providers.opencti.knowledge as knowledge_mod
import app.integrations.providers.opencti.markings as markings_mod
from app.integrations.providers.opencti import build_tools
from app.tools.base import CallableToolExecutor, ToolCall

TOKEN = "https://demo.opencti.io||test-token"

TLP_CLEAR = {
    "id": "11111111-1111-4111-8111-111111111111",
    "standard_id": "marking-definition--94868c89-4ee5-4a9a-a3ee-c2a7d5b9fbaf",
    "x_opencti_stix_ids": [],
    "definition": "TLP:CLEAR",
    "definition_type": "TLP",
    "x_opencti_order": 1,
}
PAP_AMBER = {
    "id": "22222222-2222-4222-8222-222222222222",
    "standard_id": "marking-definition--aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    "x_opencti_stix_ids": [],
    "definition": "PAP:AMBER",
    "definition_type": "PAP",
    "x_opencti_order": 3,
}
AUTHOR = {
    "id": "ec023ace-08e7-42a7-8183-3205b852210d",
    "standard_id": "identity--bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    "name": "Filigran",
    "entity_type": "Organization",
}
USER = {"id": "33333333-3333-4333-8333-333333333333", "name": "[F] Ingestion"}


class _Stub:
    """``_graphql`` stand-in for an OpenCTI holding two markings and one author."""

    def __init__(self, stored_markings=None):
        self.calls: list[tuple[str, dict | None]] = []
        self.added: dict | None = None
        self.stored_markings = stored_markings

    def mutations(self) -> list[str]:
        return [q for q, _ in self.calls if q.lstrip().startswith("mutation")]

    async def __call__(self, client, base_url, headers, query, variables=None):
        self.calls.append((query, variables))
        variables = variables or {}
        if "RefTarget" in query:
            return {
                "stixObjectOrStixRelationship": {
                    "id": "report-1",
                    "entity_type": "Report",
                    "parent_types": ["Basic-Object", "Stix-Object", "Stix-Core-Object", "Stix-Domain-Object"],
                }
            }, None
        if "markingDefinitions(" in query:
            return {"markingDefinitions": {"edges": [{"node": TLP_CLEAR}, {"node": PAP_AMBER}]}}, None
        if "markingDefinition(id" in query:
            return {"markingDefinition": None}, None
        if "identity(id" in query:
            hit = variables.get("id") in (AUTHOR["id"], AUTHOR["standard_id"])
            return {"identity": AUTHOR if hit else None}, None
        if "identities(" in query:
            return {"identities": {"edges": [{"node": AUTHOR}]}}, None
        if "ingestionRssAdd" in query:
            self.added = variables["input"]
            return {"ingestionRssAdd": {"id": "feed-1", "name": variables["input"]["name"]}}, None
        if "ingestionCsvAdd" in query:
            self.added = variables["input"]
            return {"ingestionCsvAdd": {"id": "feed-2", "name": variables["input"]["name"]}}, None
        if "ingestionRss(id" in query:
            refs = self.stored_markings
            if refs is None:
                refs = (self.added or {}).get("object_marking_refs") or []
            by_id = {m["id"]: m for m in (TLP_CLEAR, PAP_AMBER)}
            return {
                "ingestionRss": {
                    "id": "feed-1",
                    "name": "Securelist",
                    "uri": "https://securelist.com/feed/",
                    "ingestion_running": False,
                    "report_types": ["threat-report"],
                    "user": USER,
                    "defaultCreatedBy": AUTHOR if (self.added or {}).get("created_by_ref") else None,
                    "defaultMarkingDefinitions": [
                        {"id": by_id[r]["id"], "definition": by_id[r]["definition"]} for r in refs if r in by_id
                    ],
                }
            }, None
        if "ingestionCsv(id" in query:
            return {
                "ingestionCsv": {
                    "id": "feed-2",
                    "name": "CSV",
                    "markings": (self.added or {}).get("markings") or [],
                    "user": USER,
                }
            }, None
        if "ingestionRssFieldPatch" in query:
            return {"ingestionRssFieldPatch": {"id": "feed-1", "name": "Securelist"}}, None
        return {}, None


def _build(monkeypatch, stub):
    for module in (ingestion_mod, markings_mod, knowledge_mod):
        monkeypatch.setattr(module, "_graphql", stub)
    executor = CallableToolExecutor()
    tools = build_tools(TOKEN, executor, scope_headers={})
    return executor, tools


async def _run(executor, name, arguments):
    return await executor.execute_tool(ToolCall(id=f"call-{name}", name=name, arguments=arguments))


def _create(config: dict, feed_type: str = "rss") -> dict:
    return {
        "action": "create",
        "feed_type": feed_type,
        "name": "Securelist",
        "uri": "https://securelist.com/feed/",
        "user_id": USER["id"],
        "running": False,
        "config": json.dumps(config),
    }


async def test_create_resolves_a_marking_given_by_stix_id_name_or_internal_id(monkeypatch):
    for ref in (TLP_CLEAR["standard_id"], "TLP:CLEAR", "tlp:clear", TLP_CLEAR["id"]):
        stub = _Stub()
        executor, _ = _build(monkeypatch, stub)
        result = await _run(executor, "manage_opencti_ingestion", _create({"object_marking_refs": [ref]}))
        assert not result.is_error, result.content
        assert stub.added["object_marking_refs"] == [TLP_CLEAR["id"]], ref
        assert "markings: TLP:CLEAR" in result.content


async def test_create_resolves_the_author_by_name_or_stix_id(monkeypatch):
    for ref in ("Filigran", AUTHOR["standard_id"], AUTHOR["id"]):
        stub = _Stub()
        executor, _ = _build(monkeypatch, stub)
        result = await _run(executor, "manage_opencti_ingestion", _create({"created_by_ref": ref}))
        assert not result.is_error, result.content
        assert stub.added["created_by_ref"] == AUTHOR["id"], ref
        assert "author: Filigran (Organization" in result.content


async def test_unknown_marking_errors_and_creates_nothing(monkeypatch):
    stub = _Stub()
    executor, _ = _build(monkeypatch, stub)
    result = await _run(
        executor, "manage_opencti_ingestion", _create({"object_marking_refs": ["TLP:CLEAR", "TLP:PURPLE"]})
    )
    assert result.is_error
    assert "'TLP:PURPLE'" in result.content
    assert "Nothing was written" in result.content
    assert stub.mutations() == []


async def test_unknown_author_errors_and_creates_nothing(monkeypatch):
    stub = _Stub()
    executor, _ = _build(monkeypatch, stub)
    result = await _run(executor, "manage_opencti_ingestion", _create({"created_by_ref": "Nobody Inc"}))
    assert result.is_error
    assert "'Nobody Inc'" in result.content
    assert stub.mutations() == []


async def test_label_import_switch_is_refused_as_absent_from_opencti(monkeypatch):
    stub = _Stub()
    executor, _ = _build(monkeypatch, stub)
    result = await _run(executor, "manage_opencti_ingestion", _create({"label_import": False}))
    assert result.is_error
    assert "no label import setting" in result.content
    assert stub.mutations() == []


async def test_create_flags_a_marking_opencti_did_not_store(monkeypatch):
    stub = _Stub(stored_markings=[])
    executor, _ = _build(monkeypatch, stub)
    result = await _run(executor, "manage_opencti_ingestion", _create({"object_marking_refs": ["TLP:CLEAR"]}))
    assert result.is_error
    assert "did not store every marking" in result.content


async def test_get_shows_markings_author_user_and_label_import(monkeypatch):
    stub = _Stub(stored_markings=[TLP_CLEAR["id"]])
    stub.added = {"created_by_ref": AUTHOR["id"]}
    executor, _ = _build(monkeypatch, stub)
    result = await _run(
        executor, "manage_opencti_ingestion", {"action": "get", "feed_type": "rss", "feed_id": "feed-1"}
    )
    assert not result.is_error, result.content
    assert f"markings: TLP:CLEAR ({TLP_CLEAR['id']})" in result.content
    assert f"author: Filigran (Organization, {AUTHOR['id']})" in result.content
    assert f"ingestion user: [F] Ingestion ({USER['id']})" in result.content
    assert "label import: always on" in result.content


async def test_update_resolves_markings_before_patching(monkeypatch):
    stub = _Stub()
    executor, _ = _build(monkeypatch, stub)
    result = await _run(
        executor,
        "manage_opencti_ingestion",
        {
            "action": "update",
            "feed_type": "rss",
            "feed_id": "feed-1",
            "config": '{"object_marking_refs": ["PAP:AMBER"]}',
        },
    )
    assert not result.is_error, result.content
    _, variables = stub.calls[-1]
    assert {"key": "object_marking_refs", "value": [PAP_AMBER["id"]]} in variables["input"]


async def test_csv_markings_are_resolved_and_named_on_read_back(monkeypatch):
    stub = _Stub()
    executor, _ = _build(monkeypatch, stub)
    config = {"markings": ["TLP:CLEAR"], "csv_mapper_id": "m-1"}
    result = await _run(executor, "manage_opencti_ingestion", _create(config, feed_type="csv"))
    assert not result.is_error, result.content
    assert stub.added["markings"] == [TLP_CLEAR["id"]]
    assert "markings: TLP:CLEAR" in result.content


async def test_add_opencti_marking_accepts_a_definition_name(monkeypatch):
    stub = _Stub()
    _build(monkeypatch, stub)
    executor_live = CallableToolExecutor()
    build_tools(TOKEN, executor_live, scope_headers={"opencti-draft-id": "draft-1"})
    result = await _run(
        executor_live, "add_opencti_marking", {"entity_id": "report-1", "marking_definition_id": "TLP:CLEAR"}
    )
    sent = [v for q, v in stub.calls if "AddMarking" in q]
    assert sent, result.content
    assert sent[-1]["input"]["toId"] == TLP_CLEAR["id"]


async def test_add_opencti_marking_refuses_an_unknown_name(monkeypatch):
    stub = _Stub()
    _build(monkeypatch, stub)
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor, scope_headers={"opencti-draft-id": "draft-1"})
    result = await _run(
        executor, "add_opencti_marking", {"entity_id": "report-1", "marking_definition_id": "TLP:PURPLE"}
    )
    assert result.is_error
    assert not [q for q, _ in stub.calls if "AddMarking" in q]


async def test_list_opencti_markings_shows_ids(monkeypatch):
    stub = _Stub()
    executor, tools = _build(monkeypatch, stub)
    assert "list_opencti_markings" in {t.name for t in tools}
    result = await _run(executor, "list_opencti_markings", {})
    assert not result.is_error, result.content
    assert f"TLP:CLEAR [TLP] id: {TLP_CLEAR['id']}, STIX id: {TLP_CLEAR['standard_id']}" in result.content
