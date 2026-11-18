"""The grounded STIX extraction against what a provider or a deployment really hands it:
answers off their schema or cut at their budget, a run's spending budget, unnamed texts,
attachments without text, duplicate objects, a configured ATT&CK mirror (#9126)."""

from __future__ import annotations

import asyncio
import json

import pytest

from app.llm.base import ChatResponse, FileRef
from app.services.stix_extraction import Usage, provider_structured_call
from app.services.stix_extraction.bundle import ExtractedObject, build_bundle, default_report_name
from app.services.stix_extraction.iocs import find_pattern_entities
from app.services.stix_extraction.llm import conform
from app.tools.base import FileContext, ToolModelSpend, spending_for_tool

_SCHEMA = {
    "type": "object",
    "properties": {
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "mentions": {
                        "type": "array",
                        "items": {"type": "object", "properties": {"text": {"type": "string"}}},
                    },
                },
                "required": ["name", "aliases"],
            },
        }
    },
    "required": ["entities"],
}



def _payload(answer: object) -> dict:
    """The extraction tool's answer: its ``structured`` result, or its JSON text (a failure)."""
    from app.tools.base import ToolResult

    return answer.structured if isinstance(answer, ToolResult) else json.loads(answer)

class _Answers:
    """A provider answering from a list, counting its calls."""

    def __init__(self, *responses: ChatResponse) -> None:
        self.responses = list(responses)
        self.calls = 0

    async def chat(self, **_: object) -> ChatResponse:
        self.calls += 1
        return self.responses.pop(0)


def _answer(payload: object, **kwargs: object) -> ChatResponse:
    return ChatResponse(
        content=json.dumps(payload), model="m", usage={"input_tokens": 100, "output_tokens": 50}, **kwargs
    )


def _call(llm: object, usage: Usage):
    return provider_structured_call(llm, "m", usage)(step="detection", system="s", user="u", schema=_SCHEMA)


def test_a_value_of_the_wrong_type_is_left_out_not_read() -> None:
    answer = {
        "entities": [
            {"name": "APT28", "aliases": "Fancy Bear", "mentions": ["APT28", {"text": "APT28"}]},
            "APT29",
            {"name": "Sofacy"},
        ]
    }
    assert conform(answer, _SCHEMA) == {
        "entities": [{"name": "APT28", "mentions": [{"text": "APT28"}]}, {"name": "Sofacy"}]
    }
    assert conform(["not", "an", "object"], _SCHEMA) is None


def test_an_answer_off_its_schema_keeps_only_what_fits() -> None:
    llm = _Answers(_answer({"entities": [{"name": "APT28", "aliases": "Fancy Bear", "mentions": ["APT28"]}]}))
    usage = Usage()
    assert asyncio.run(_call(llm, usage)) == {"entities": [{"name": "APT28", "mentions": []}]}
    assert llm.calls == 1


def test_an_answer_cut_at_its_budget_is_not_asked_again() -> None:
    llm = _Answers(_answer({"entities": []}, stop_reason="max_tokens"), _answer({"entities": []}))
    usage = Usage()
    assert asyncio.run(_call(llm, usage)) is None
    assert llm.calls == 1
    assert usage.failed_calls == 1


def test_a_call_spends_the_run_and_is_not_made_once_the_run_cannot_pay() -> None:
    recorded: list[tuple[dict, str | None]] = []
    allowance = {"open": True}
    released: list[bool] = []

    def reserve(_i: int, _o: int):
        return (lambda: released.append(True)) if allowance["open"] else None

    spend = ToolModelSpend(record=lambda usage, served: recorded.append((usage, served)), reserve=reserve)
    # Asked of "m", served by another model (a failover): the served one is recorded.
    llm = _Answers(ChatResponse(content=json.dumps({"entities": []}), model="served-model",
                                usage={"input_tokens": 100, "output_tokens": 50}))
    usage = Usage()

    async def run() -> dict | None:
        with spending_for_tool(spend):
            return await _call(llm, usage)

    assert asyncio.run(run()) == {"entities": []}
    # Recorded with the model that served this call, which prices its tokens.
    assert recorded == [({"input_tokens": 100, "output_tokens": 50}, "served-model")]
    assert released == [True], "the reservation is handed back once the call is paid"

    allowance["open"] = False
    assert asyncio.run(run()) is None
    assert llm.calls == 1
    assert usage.refused_calls == 1


def test_a_refused_call_makes_the_tool_answer_invalid() -> None:
    from app.services.builtin_tools.stix_extraction import build_stix_extraction_tool

    _, executor = build_stix_extraction_tool(_Answers(), "m")
    handler = executor.handler("extract_stix_from_document")
    spend = ToolModelSpend(record=lambda _u, _m: None, reserve=lambda _i, _o: None)

    async def run() -> dict:
        with spending_for_tool(spend):
            return _payload(await handler(text="APT28 used X-Agent against Ukraine."))

    payload = asyncio.run(run())
    assert payload["valid"] is False
    assert any("budget could not pay" in error for error in payload["errors"])


def test_two_unnamed_texts_get_two_reports_and_one_text_keeps_its_own() -> None:
    first, second = "APT28 targeted Ukraine.", "Lazarus targeted banks."
    assert default_report_name(first) != default_report_name(second)
    assert default_report_name(first) == default_report_name(" APT28  targeted\nUkraine. ")

    def report_id(text: str) -> str:
        bundle = build_bundle(
            [ExtractedObject("E1", "intrusion-set", text.split()[0], spans=[(0, 5)])],
            [],
            document_name=default_report_name(text),
        )
        return next(o["id"] for o in bundle["objects"] if o["type"] == "report")

    assert report_id(first) != report_id(second)


def test_an_attachment_without_text_is_skipped_unless_it_is_named() -> None:
    from app.services.builtin_tools.stix_extraction import _sources

    context = FileContext()
    context.add(FileRef.from_bytes(b"APT28 targeted Ukraine.", filename="report.txt", content_type="text/plain"))
    context.add(FileRef.from_bytes(b"", filename="screenshot.txt", content_type="text/plain"))
    sources, skipped = asyncio.run(_sources(None, None, "", "", context))
    assert [name for name, _ in sources] == ["report.txt (e28bd95deb15)"]
    assert skipped == ["screenshot.txt"]

    screenshot = next(f for f in context.list_files() if f.filename == "screenshot.txt")
    named = asyncio.run(_sources([screenshot.file_id], None, "", "", context))
    assert named == "No text could be read from 'screenshot.txt'."


def test_a_call_naming_no_file_reads_this_turn_s_attachments_not_earlier_ones() -> None:
    """In a chat, the reports of earlier turns were extracted then: a call naming no file
    reads the message's own attachments; with none attached and several files, it asks
    for their ids. A run that holds only its own files (no turn known) reads them all."""
    from app.services.builtin_tools.stix_extraction import _sources

    context = FileContext()
    for name in ("a.txt", "b.txt", "c.txt"):
        context.add(FileRef.from_bytes(f"APT28 report {name}.".encode(), filename=name, content_type="text/plain"))
    newest = next(f for f in context.list_files() if f.filename == "c.txt")

    context.current_turn_ids = {newest.file_id}
    sources, _ = asyncio.run(_sources(None, None, "", "", context))
    assert [name.split(" ")[0] for name, _ in sources] == ["c.txt"]

    context.current_turn_ids = set()
    asked = asyncio.run(_sources(None, None, "", "", context))
    assert isinstance(asked, str) and "file_ids" in asked

    context.current_turn_ids = None
    sources, _ = asyncio.run(_sources(None, None, "", "", context))
    assert len(sources) == 3


def test_two_objects_of_one_stix_id_keep_both_ones_mentions() -> None:
    bundle = build_bundle(
        [
            ExtractedObject("E1", "intrusion-set", "APT28", spans=[(0, 5)]),
            ExtractedObject("E2", "intrusion-set", "APT28", spans=[(40, 45)]),
        ],
        [],
        document_name="report",
    )
    groups = [o for o in bundle["objects"] if o["type"] == "intrusion-set"]
    assert len(groups) == 1
    assert groups[0]["x_xtm_mentions"] == [[0, 5], [40, 45]]


def test_overlapping_patterns_keep_the_longest_first_match() -> None:
    text = "C2 at https://evil.com/x and mail admin@evil.org, then evil.net"
    found = [(m.kind, m.value) for m in find_pattern_entities(text)]
    assert ("url", "https://evil.com/x") in found
    assert ("domain-name", "evil.com") not in found  # inside the URL
    assert ("domain-name", "evil.net") in found


def test_attack_is_read_from_the_configured_mirror(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.integrations.providers import mitre_attack
    from app.services.stix_extraction import knowledge

    seen: list[tuple[str, str]] = []

    async def lookup(names: list[str], *, source_url: str, version: str, **_: object) -> dict:
        seen.append((source_url, version))
        return {}

    async def configured() -> tuple[str, str]:
        return "https://mirror.internal/attack", "17.1"

    monkeypatch.setattr(knowledge, "_configured_source", configured)
    monkeypatch.setattr(knowledge, "_source", None)
    monkeypatch.setattr(knowledge, "_down_until", {})
    monkeypatch.setattr(mitre_attack, "lookup_attack_names", lookup)
    assert asyncio.run(knowledge.attack_knowledge(["APT28"])) == {}
    assert seen == [("https://mirror.internal/attack", "17.1")]


def test_the_runs_bound_attack_integration_is_read_through_its_relay(monkeypatch) -> None:
    """A run bound to a MITRE ATT&CK integration reads ATT&CK from it, through its relay
    route, not from the deployment's most recent one: an air-gapped mirror behind a relay
    links techniques. A source that fails is skipped for a while, that source only."""
    from app.core.egress import EgressRoute, current_route
    from app.integrations.providers import mitre_attack
    from app.services.stix_extraction import knowledge

    seen: list[tuple[str, str, object]] = []

    async def lookup(names: list[str], *, source_url: str, version: str, **_: object) -> dict | None:
        seen.append((source_url, version, current_route()))
        return None if "down" in source_url else {}

    async def configured() -> tuple[str, str]:
        raise AssertionError("the deployment's source is not read when the run has one")

    monkeypatch.setattr(knowledge, "_configured_source", configured)
    monkeypatch.setattr(knowledge, "_source", None)
    monkeypatch.setattr(knowledge, "_down_until", {})
    monkeypatch.setattr(mitre_attack, "lookup_attack_names", lookup)
    route = EgressRoute(group_id="relay-group-1")
    bound, _ = knowledge.attack_functions(("https://mirror.internal/attack", "17.1", route))
    assert asyncio.run(bound(["APT28"])) == {}
    assert seen == [("https://mirror.internal/attack", "17.1", route)]

    down, _ = knowledge.attack_functions(("https://down.internal/attack", "17.1", None))
    assert asyncio.run(down(["APT28"])) is None
    assert asyncio.run(down(["APT28"])) is None  # skipped: no second request
    assert asyncio.run(bound(["APT28"])) == {}  # another source is not
    assert len(seen) == 3


def test_two_files_of_one_name_get_two_reports_and_one_file_keeps_its_own() -> None:
    """A report's standard id is its name and date: two different ``report.pdf`` extracted
    on one day must not be merged into one report by OpenCTI."""
    from app.services.builtin_tools.stix_extraction import _sources

    context = FileContext()
    context.add(FileRef.from_bytes(b"APT28 targeted Ukraine.", filename="report.txt", content_type="text/plain"))
    context.add(FileRef.from_bytes(b"Lazarus targeted banks.", filename="report.txt", content_type="text/plain"))
    context.add(FileRef.from_bytes(b"APT28  targeted\nUkraine.", filename="report.txt", content_type="text/plain"))
    sources, _ = asyncio.run(_sources(None, None, "", "", context))
    names = [name for name, _ in sources]
    assert names[0] != names[1] and names[0] == names[2]
    assert all(name.startswith("report.txt (") for name in names)


def test_a_name_in_any_script_or_with_accents_folds_to_one_key() -> None:
    from app.services.stix_extraction.grounding import core_key, surface_key

    assert surface_key("Café") == surface_key("CAFE") == "cafe"
    assert core_key("日本") == "日本"
    assert core_key("Пешка group") == "пешка"
    assert core_key("Sandworm_Team") == "sandworm"
    assert core_key("The group") == ""


def _location(kind: str, name: str, code: str = "") -> dict | None:
    bundle = build_bundle([ExtractedObject("E1", kind, name, spans=[(0, len(name))], code=code)], [], document_name="r")
    return next((o for o in bundle["objects"] if o["type"] == "location"), None)


def test_a_location_country_is_always_an_iso_alpha_2_code() -> None:
    assert _location("country", "United States", "USA")["country"] == "US"
    assert _location("country", "Ukraine")["country"] == "UA"
    assert _location("city", "Kyiv", "UA")["country"] == "UA"
    # A place whose country cannot be told has no country STIX can carry: left out, never
    # given its own name as a country.
    assert _location("city", "Kyiv") is None
    assert _location("country", "Atlantis", "XX") is None
    assert _location("region", "Eastern Europe")["region"] == "eastern-europe"


def test_a_value_outside_its_enum_is_left_out_and_one_differing_in_case_is_read() -> None:
    schema = {
        "type": "object",
        "properties": {
            "kinds": {"type": "array", "items": {"type": "string", "enum": ["malware", "tool"]}},
            "kind": {"type": "string", "enum": ["malware", "tool"]},
        },
    }
    assert conform({"kinds": ["ransomware", "Malware", "tool"], "kind": "ransomware"}, schema) == {
        "kinds": ["malware", "tool"]
    }



def test_the_extraction_tool_runs_under_its_own_timeout_within_the_runs_deadline() -> None:
    """One call reads whole reports (tens of model calls each): the loop's 300 s per-tool
    timeout would cancel calls already paid for. The tool declares its own, which the
    run's wall-clock deadline still caps."""
    import time

    from app.services.builtin_tools.stix_extraction import EXECUTION_TIMEOUT_SECONDS, build_stix_extraction_tool
    from app.tools import agent_loop as al

    (definition,), _ = build_stix_extraction_tool(None, None)
    assert definition.execution_timeout == EXECUTION_TIMEOUT_SECONDS > al.TOOL_EXECUTION_TIMEOUT_SECONDS
    assert al._tool_timeout_for(definition.name, 300, None, definition.execution_timeout) == EXECUTION_TIMEOUT_SECONDS
    near = time.monotonic() + 60 + al._IN_LOOP_DEADLINE_RESERVE_SECS
    assert al._tool_timeout_for(definition.name, 300, near, definition.execution_timeout) in (59, 60)
    assert al._tool_timeout_for("another_tool", 300) == 300


def test_relationships_rebuilt_after_a_recovered_entity_keep_how_they_were_read() -> None:
    """Once an entity the relationship step recovered is validated, the relationships are
    rebuilt onto the inventory: one a recall pass or a remap gave keeps that origin, so the
    verification still checks it instead of taking it as a stated extraction."""
    from app.services.stix_extraction import ExtractionOptions
    from app.services.stix_extraction.bundle import ExtractedRelation
    from app.services.stix_extraction.pipeline import _Candidate, _Pipeline

    text = "APT28 used X-Agent. Fancy Bear targeted Ukraine."
    pipeline = _Pipeline(text, None, Usage(), ExtractionOptions())  # type: ignore[arg-type]
    actor = _Candidate("E1", ["intrusion-set"], "APT28", "threat", spans=[(0, 5)])
    malware = _Candidate("E2", ["malware"], "X-Agent", "threat", spans=[(11, 18)])
    recovered = _Candidate("N1", ["intrusion-set"], "Fancy Bear", "threat", spans=[(20, 30)], recovered=True)

    async def resolve(candidates: list) -> list:
        return candidates

    pipeline.resolve = resolve  # type: ignore[method-assign]
    relations = [
        ExtractedRelation("E1", "uses", "E2", ["APT28 used X-Agent"], origin="gleaning", remapped=True, verified=0.4),
        ExtractedRelation("N1", "uses", "E2", ["Fancy Bear"], origin="pair"),
    ]
    rebuilt = asyncio.run(pipeline._validate_recovered([actor, malware, recovered], relations))
    by_source = {r.source: r for r in rebuilt}
    assert (by_source["E1"].origin, by_source["E1"].remapped, by_source["E1"].verified) == ("gleaning", True, 0.4)
    assert by_source["N1"].origin == "pair"


def test_a_technique_known_by_its_id_takes_attacks_name() -> None:
    """The report may write only the id or its own words; the technique is named as ATT&CK
    names it, a sub-technique as "Parent: Sub", the report's words kept as aliases."""
    from types import SimpleNamespace

    from app.services.stix_extraction.pipeline import _Candidate, _technique_names

    catalogue = [
        SimpleNamespace(attack_id="T1083", name="File and Directory Discovery"),
        SimpleNamespace(attack_id="T1003", name="OS Credential Dumping"),
        SimpleNamespace(attack_id="T1548", name="Abuse Elevation Control Mechanism"),
        SimpleNamespace(attack_id="T1548.002", name="Bypass User Account Control"),
    ]
    by_id = _Candidate("P1", ["attack-pattern-id"], "T1083", "technique", value="T1083")
    in_words = _Candidate("P2", ["attack-pattern-id"], "credential theft from LSASS", "technique", value="T1003")
    sub = _Candidate("P3", ["attack-pattern-id"], "T1548.002", "technique", value="T1548.002")
    unknown = _Candidate("P4", ["attack-pattern-id"], "T9999", "technique", value="T9999")
    _technique_names([by_id, in_words, sub, unknown], catalogue)
    assert (by_id.name, by_id.aliases) == ("File and Directory Discovery", [])
    assert (in_words.name, in_words.aliases) == ("OS Credential Dumping", ["credential theft from LSASS"])
    assert sub.name == "Abuse Elevation Control Mechanism: Bypass User Account Control"
    assert unknown.name == "T9999"
    assert [c.value for c in (by_id, in_words, sub)] == ["T1083", "T1003", "T1548.002"]
    # The object written keeps the sub-technique's own ATT&CK name as an alias, though the
    # "Parent: Sub" name contains it.
    from app.services.stix_extraction.pipeline import _to_object

    assert _to_object(sub).aliases == ["Bypass User Account Control"]


@pytest.mark.parametrize(
    ("answered", "named"),
    [
        ("Italian  Spyware Vendor Linked to Chrome Zero-Day Attacks", "Italian Spyware Vendor Linked to Chrome Zero-Day Attacks"),
        ("Italian spyware vendor tied to Chrome 0-days", "report.pdf (d00d)"),  # rewritten: not the text's
        ("", "report.pdf (d00d)"),
    ],
)
def test_the_report_is_named_by_the_title_its_text_writes(answered: str, named: str) -> None:
    from app.services.stix_extraction import extract_stix

    text = ("October 28, 2025 07:13:47 PM\nNews Analysis\nItalian Spyware Vendor Linked to Chrome\nZero-Day Attacks\n"
            "APT28 targeted Ukraine.")

    async def call(*, step: str, system: str, user: str, schema: dict) -> dict | None:
        return {"title": answered} if step == "title" else None

    result = asyncio.run(extract_stix(text, call, document_name="report.pdf (d00d)", read_title=True))
    assert result.document_name == named


@pytest.mark.parametrize(("answered", "author"), [("BleepingComputer", "BleepingComputer"), ("Reuters", None)])
def test_the_publisher_the_text_names_is_the_reports_author(answered: str, author: str | None) -> None:
    """The organization that published the report is its ``created_by_ref`` identity, as
    OpenCTI records a report's source; a publisher the text does not name is no author."""
    from app.services.stix_extraction import extract_stix

    text = "BleepingComputer\nNews > Security\nRansomware gang hits hospital\nThe gang exploited CVE-2024-3400."

    async def call(*, step: str, system: str, user: str, schema: dict) -> dict | None:
        return {"title": "Ransomware gang hits hospital", "publisher": answered} if step == "title" else None

    bundle = asyncio.run(extract_stix(text, call, read_title=True)).bundle
    report = next(o for o in bundle["objects"] if o["type"] == "report")
    sources = [o for o in bundle["objects"] if o["type"] == "identity" and o["id"] == report.get("created_by_ref")]
    assert [s["name"] for s in sources] == ([author] if author else [])


def test_a_report_the_caller_names_keeps_the_publisher_its_text_names() -> None:
    """A name the caller gives the report says nothing of who published it: the report keeps
    the caller's name and the publisher the text names is still its author."""
    from app.services.stix_extraction import extract_stix

    text = "BleepingComputer\nNews > Security\nRansomware gang hits hospital\nThe gang exploited CVE-2024-3400."

    async def call(*, step: str, system: str, user: str, schema: dict) -> dict | None:
        return {"title": "Ransomware gang hits hospital", "publisher": "BleepingComputer"} if step == "title" else None

    result = asyncio.run(extract_stix(text, call, document_name="Hospital attack", read_publisher=True))
    report = next(o for o in result.bundle["objects"] if o["type"] == "report")
    sources = [o for o in result.bundle["objects"] if o["type"] == "identity" and o["id"] == report.get("created_by_ref")]
    assert result.document_name == "Hospital attack"
    assert [s["name"] for s in sources] == ["BleepingComputer"]


def test_the_tool_reads_the_publisher_whether_or_not_the_caller_names_the_report(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.stix_extraction as extraction
    from app.services.builtin_tools.stix_extraction import build_stix_extraction_tool

    seen: list[tuple[bool, bool]] = []
    real = extraction.extract_stix

    async def spy(*args: object, **kwargs: object):
        seen.append((bool(kwargs.get("read_title")), bool(kwargs.get("read_publisher"))))
        return await real(*args, **kwargs)

    monkeypatch.setattr(extraction, "extract_stix", spy)
    _, executor = build_stix_extraction_tool(_Answers(), "m")
    handler = executor.handler("extract_stix_from_document")
    asyncio.run(handler(text="APT28 used X-Agent against Ukraine.", document_name="APT28 note"))
    asyncio.run(handler(text="APT28 used X-Agent against Ukraine."))
    assert seen == [(False, True), (True, True)]


def _pipeline_with(text: str):
    from app.services.stix_extraction import ExtractionOptions
    from app.services.stix_extraction.pipeline import _Pipeline

    return _Pipeline(text, None, Usage(), ExtractionOptions())  # type: ignore[arg-type]


def test_products_are_named_as_analysts_name_them() -> None:
    """One product per item of an enumeration, the vendor written before it in its name and
    vendor, the update or release in version, not in the name."""
    from app.services.stix_extraction.bundle import ExtractedRelation
    from app.services.stix_extraction.pipeline import _Candidate, _product_names, _to_object

    text = "A flaw in JNC IAQS and I6 lets anyone in. Microsoft Exchange Server 2016 Cumulative Update 23 is fixed."
    pipeline = _pipeline_with(text)
    vendor = _Candidate("O1", ["organization"], "JNC", "identity", spans=[(10, 13)])
    pair = _Candidate("P1", ["product"], "IAQS and I6", "software", spans=[(14, 25)])
    exchange = _Candidate("P2", ["product"], "Microsoft Exchange Server 2016 Cumulative Update 23", "software",
                          spans=[(42, 93)])
    flaw = _Candidate("V1", ["cve"], "CVE-2026-1364", "vulnerability", spans=[(2, 6)])
    objects, relations = _product_names(pipeline, [vendor, pair, exchange, flaw], [ExtractedRelation("P1", "has", "V1")])
    products = {o.name: (o.vendor, o.version) for o in objects if o.kind == "product"}
    assert products == {"JNC IAQS": ("JNC", ""), "JNC I6": ("JNC", ""), "Microsoft Exchange Server 2016": ("", "Cumulative Update 23")}
    assert len([r for r in relations if r.type == "has"]) == 2, "each product keeps the relationship"
    software = build_bundle([_to_object(o) for o in objects if o.kind == "product"], [], document_name="r")
    exch = next(o for o in software["objects"] if o.get("name") == "Microsoft Exchange Server 2016")
    assert exch["version"] == "Cumulative Update 23"


def test_a_brand_the_text_calls_ransomware_is_its_group_and_its_malware() -> None:
    from app.services.stix_extraction.pipeline import _Candidate, _ransomware_twins, _to_object

    text = "The BianLian ransomware gang claimed the attack. APT28 was not involved."
    pipeline = _pipeline_with(text)
    group = _Candidate("E1", ["intrusion-set"], "BianLian", "actor", spans=[(4, 12)])
    other = _Candidate("E2", ["intrusion-set"], "APT28", "actor", spans=[(49, 54)])
    objects, relations = _ransomware_twins(pipeline, [group, other], [])
    kinds = sorted((o.kind, o.name) for o in objects)
    assert kinds == [("intrusion-set", "APT28"), ("intrusion-set", "BianLian"), ("malware", "BianLian")]
    malware = next(o for o in objects if o.kind == "malware")
    assert [(r.source, r.type, r.target) for r in relations] == [("E1", "uses", malware.ref)]
    bundle = build_bundle([_to_object(o) for o in objects], [], document_name="r")
    assert next(o for o in bundle["objects"] if o["type"] == "malware")["malware_types"] == ["ransomware"]


def test_two_names_of_one_misp_galaxy_cluster_are_one_object_of_one_kind() -> None:
    from app.services.stix_extraction.pipeline import _Candidate, _galaxy_merge

    alias = _Candidate("E1", ["intrusion-set"], "Secret Blizzard", "actor", spans=[(0, 15)])
    attack = _Candidate("E2", ["intrusion-set"], "Turla", "actor", spans=[(30, 35)], external_ids=[("mitre-attack", "G0010")])
    malware = _Candidate("E3", ["malware"], "Turla", "software", spans=[(40, 45)])
    other = _Candidate("E4", ["intrusion-set"], "APT28", "actor", spans=[(50, 55)])
    kept, merged = _galaxy_merge([alias, attack, malware, other])
    assert merged == 1
    assert [(c.kind, c.name) for c in kept] == [("intrusion-set", "Turla"), ("malware", "Turla"), ("intrusion-set", "APT28")]
    assert "Secret Blizzard" in attack.aliases and attack.spans == [(0, 15), (30, 35)]


def test_an_ioc_list_under_a_generic_heading_indicates_the_reports_one_main_subject() -> None:
    from app.services.stix_extraction import ExtractionOptions
    from app.services.stix_extraction.pipeline import _Candidate, _Pipeline

    text = ("APT-K-47 ran WalkerShell. APT-K-47 stole data from the defense sector over several months of access.\n"
            "Indicators of Compromise\n74ba5883d989566a94e7c6c217b17102f054ffbe98bc9c878a7f700f9809e910\n")
    start = text.index("74ba")
    actor = _Candidate("E1", ["intrusion-set"], "APT-K-47", "actor", spans=[(0, 8), (26, 34)])
    digest = _Candidate("H1", ["sha256"], "74ba", "observable", spans=[(start, start + 64)], value=text[start:start + 64])
    for enabled, expected in ((True, [("H1", "E1")]), (False, [])):
        pipeline = _Pipeline(text, None, Usage(), ExtractionOptions(ioc_lists_main_subject=enabled))  # type: ignore[arg-type]
        found: dict = {}
        pipeline._structural_indicators([actor, digest], found)
        assert [(s, t) for s, verb, t in found if verb == "indicates"] == expected


def test_a_threat_that_targets_a_city_targets_its_country() -> None:
    from app.services.stix_extraction import ExtractionOptions
    from app.services.stix_extraction.bundle import ExtractedRelation
    from app.services.stix_extraction.pipeline import _Candidate, _Pipeline

    pipeline = _Pipeline("BianLian hit clinics in New York.", None, Usage(), ExtractionOptions(victim_countries=True))  # type: ignore[arg-type]
    group = _Candidate("E1", ["intrusion-set"], "BianLian", "actor", spans=[(0, 8)])
    city = _Candidate("L1", ["city"], "New York", "location", spans=[(24, 32)], code="US")
    objects = [group, city]
    found = {("E1", "targets", "L1"): ExtractedRelation("E1", "targets", "L1", ["clinics in New York"])}
    pipeline._victim_countries(objects, found)
    country = next(o for o in objects if o.kind == "country")
    assert country.code == "US" and ("E1", "targets", country.ref) in found


def test_a_product_is_named_by_the_vendor_the_report_names_and_never_by_an_unwritten_one() -> None:
    from app.services.stix_extraction.pipeline import _Candidate, _product_names

    text = "IAQS and I6 developed by JNC have a missing authentication flaw. Log4j was also exploited."
    pipeline = _pipeline_with(text)
    pair = _Candidate("P1", ["product"], "IAQS and I6", "software", spans=[(0, 11)], code="JNC")
    log4j = _Candidate("P2", ["product"], "Log4j", "software", spans=[(66, 71)], code="Apache")  # not in the text
    objects, _ = _product_names(pipeline, [pair, log4j], [])
    assert {(o.name, o.vendor) for o in objects} == {("JNC IAQS", "JNC"), ("JNC I6", "JNC"), ("Log4j", "")}


def test_a_sector_takes_opencti_s_name_and_keeps_the_text_s_as_an_alias() -> None:
    from app.services.stix_extraction.pipeline import _Candidate, _sector_names

    written = ["finance", "Financial Services", "government administration", "healthcare", "telecommunications industry"]
    sectors = [_Candidate(f"S{i}", ["sector"], name, "identity") for i, name in enumerate(written)]
    _sector_names(sectors)
    assert [s.name for s in sectors] == ["Finance", "Finance", "Government and administrations", "Health", "Telecommunications"]
    assert sectors[1].aliases == ["Financial Services"] and sectors[0].aliases == []

    # The model's name may already be OpenCTI's ("health") while the report writes
    # "healthcare": the report's word is kept once, as OpenCTI matches on aliases.
    health = _Candidate("S9", ["sector"], "health", "identity")
    health.surfaces = ["healthcare", "Healthcare", "health"]
    _sector_names([health])
    assert health.name == "Health" and health.aliases == ["healthcare"]

    # The object written keeps them, though "government" is a word of OpenCTI's name.
    from app.services.stix_extraction.pipeline import _to_object

    government = _Candidate("S8", ["sector"], "government", "identity")
    government.surfaces = ["government", "Government"]
    _sector_names([government])
    written = _to_object(government)
    assert written.name == "Government and administrations" and written.aliases == ["government"]
    assert _to_object(health).aliases == ["healthcare"]
    finance = _Candidate("S7", ["sector"], "Finance", "identity")
    finance.surfaces = ["\ufb01nance", "finance"]  # a PDF's "fi" ligature
    _sector_names([finance])
    assert finance.aliases == []


def test_a_rename_drops_the_name_with_generic_words_not_another_word() -> None:
    """Resolution renames a sector "health": "health sector" was the name plus a generic word
    and goes; "healthcare" only contains its letters, is the report's own word and stays (the
    sector naming keeps it as an alias)."""
    from app.services.stix_extraction import ExtractionOptions, Usage
    from app.services.stix_extraction.pipeline import _Candidate, _Pipeline

    text = "BianLian hit pediatric healthcare providers; the health sector is a target."
    pipeline = _Pipeline(text, None, Usage(), ExtractionOptions())  # type: ignore[arg-type]
    sector = _Candidate("S1", ["sector"], "healthcare", "identity")
    sector.surfaces = ["healthcare", "health sector"]
    sector.spans = [(text.index("healthcare"), text.index("healthcare") + 10),
                    (text.index("health sector"), text.index("health sector") + 13)]
    pipeline._rename(sector, "health")
    assert sector.name == "health" and sector.surfaces == ["health", "healthcare"]


def test_retrieval_gives_the_techniques_whose_definitions_are_nearest_a_quote() -> None:
    from types import SimpleNamespace

    from app.services.stix_extraction.pipeline import _nearest_techniques

    catalogue = [SimpleNamespace(attack_id="T1053.005", name="Scheduled Task", description="schedule a task"),
                 SimpleNamespace(attack_id="T1059.001", name="PowerShell", description="run powershell"),
                 SimpleNamespace(attack_id="T1486", name="Data Encrypted for Impact", description="encrypt data")]
    axes = {"task": [1.0, 0.0, 0.0], "powershell": [0.0, 1.0, 0.0], "encrypt": [0.0, 0.0, 1.0]}

    async def embed(texts: list[str]) -> list[list[float]]:
        return [next((v for word, v in axes.items() if word in t.lower()), [0.3, 0.3, 0.3]) for t in texts]

    nearest = asyncio.run(_nearest_techniques(embed, ["it created a scheduled task", "files were encrypted"], catalogue, 1))
    assert nearest == [["T1053.005"], ["T1486"]]

    async def broken(texts: list[str]) -> list[list[float]]:
        raise RuntimeError("no embedding provider")

    assert asyncio.run(_nearest_techniques(broken, ["x"], catalogue, 1)) is None


class _Declines:
    """A provider whose model declines every request."""

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, **_: object) -> ChatResponse:
        from app.llm.base import ModelDeclinedError

        self.calls += 1
        raise ModelDeclinedError(model="m", category="cyber")


def test_a_declined_call_is_counted_and_not_asked_again() -> None:
    llm = _Declines()
    usage = Usage()
    assert asyncio.run(_call(llm, usage)) is None
    assert llm.calls == 1, "the same request is declined again: no retry"
    assert usage.declined_calls == 1
    assert usage.as_dict()["declined_calls"] == 1


def test_declined_calls_make_the_tool_answer_invalid_with_the_reason() -> None:
    from app.services.builtin_tools.stix_extraction import _refused

    assert _refused({"declined_calls": 3}) == ["the model declined 3 extraction calls: the bundle is incomplete"]
    assert _refused({}) == []


def test_a_call_out_of_time_answers_the_reports_it_extracted(monkeypatch) -> None:
    """Several reports in one call share its time: a report not finished before the call's
    own deadline is given up, and the reports already extracted (already paid for) come
    back with a warning naming the rest, instead of a timeout that loses them all."""
    from app.services.builtin_tools import stix_extraction as tool
    from app.services.stix_extraction import knowledge

    monkeypatch.setattr(tool, "EXECUTION_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(tool, "_ANSWER_MARGIN_SECONDS", 0)

    async def _none(*_: object) -> None:
        return None

    monkeypatch.setattr(knowledge, "attack_functions", lambda _source=None: (_none, _none))

    class _SlowOnSecond:
        async def chat(self, **kwargs: object) -> ChatResponse:
            if "Turla" in json.dumps(str(kwargs.get("messages"))):
                await asyncio.sleep(5)
            return _answer({})

    context = FileContext()
    context.add(FileRef.from_bytes(b"APT28 targeted Ukraine.", filename="first.txt", content_type="text/plain"))
    context.add(FileRef.from_bytes(b"Turla targeted embassies.", filename="second.txt", content_type="text/plain"))
    _, executor = tool.build_stix_extraction_tool(_SlowOnSecond(), "m")
    answer = _payload(asyncio.run(executor.handler("extract_stix_from_document")(file_context=context)))
    assert [d["document"].split(" ")[0] for d in answer["stats"]["documents"]] == ["first.txt"]
    assert any("second.txt" in w and "time ran out" in w for w in answer["warnings"])
    assert not any("No report could be extracted" in e for e in answer["errors"])


def test_without_a_native_format_the_schema_is_in_the_prompt_and_an_empty_answer_is_counted() -> None:
    """A provider that cannot enforce the format (or a failover's fallback that drops it) is
    told the schema in the prompt; an answer off its schema of which nothing fits (a bare
    array) is counted, not silently kept as nothing."""
    from app.services.builtin_tools.stix_extraction import _warnings

    seen: list[str] = []

    class _Plain:
        def __init__(self, content: object) -> None:
            self.content = content

        async def chat(self, **kwargs: object) -> ChatResponse:
            seen.append(str(kwargs["messages"][0].content))
            return ChatResponse(content=json.dumps(self.content), model="m", usage={"input_tokens": 1, "output_tokens": 1})

    # A bare array fits nowhere: a failed call (retried once), counted as such.
    usage = Usage()
    assert asyncio.run(_call(_Plain([{"id": "E1"}]), usage)) is None
    assert seen and "entities" in seen[0]  # the schema's property, said in the system prompt
    assert usage.as_dict()["failed_calls"] == 2
    # An object of another shape: kept as nothing, and counted as off its format.
    usage = Usage()
    asyncio.run(_call(_Plain({"objects": [{"id": "E1"}]}), usage))
    assert usage.as_dict()["off_schema_calls"] == 1

    said = " ".join(_warnings({"failed_technique_checks": 44, "failed_pair_chunks": 17, "technique_catalogue_unavailable": 1}))
    assert "44 technique check" in said and "17 candidate pair" in said and "ATT&CK could not be loaded" in said


def test_the_deadline_follows_the_timeout_the_loop_applies(monkeypatch) -> None:
    """With less of the run left than the tool's declared 30 minutes, the loop applies the
    shorter timeout: the extraction keeps inside it and answers what it finished."""
    from app.services.builtin_tools import stix_extraction as tool
    from app.services.stix_extraction import knowledge
    from app.tools.base import timing_tool_call

    monkeypatch.setattr(tool, "_ANSWER_MARGIN_SECONDS", 0)

    async def _none(*_: object) -> None:
        return None

    monkeypatch.setattr(knowledge, "attack_functions", lambda _source=None: (_none, _none))

    class _SlowOnSecond:
        async def chat(self, **kwargs: object) -> ChatResponse:
            if "Turla" in json.dumps(str(kwargs.get("messages"))):
                await asyncio.sleep(5)
            return _answer({})

    context = FileContext()
    context.add(FileRef.from_bytes(b"APT28 targeted Ukraine.", filename="first.txt", content_type="text/plain"))
    context.add(FileRef.from_bytes(b"Turla targeted embassies.", filename="second.txt", content_type="text/plain"))
    _, executor = tool.build_stix_extraction_tool(_SlowOnSecond(), "m")

    async def run() -> object:
        with timing_tool_call(1.0):
            return await executor.handler("extract_stix_from_document")(file_context=context)

    answer = _payload(asyncio.run(run()))
    assert [d["document"].split(" ")[0] for d in answer["stats"]["documents"]] == ["first.txt"]
    assert any("second.txt" in w for w in answer["warnings"])


def test_a_declined_call_s_billed_tokens_are_the_run_s() -> None:
    """A decline is billed: its usage counts in the extraction's tokens and is recorded
    against the run (its budget, its total), priced as the model that declined."""
    from app.llm.base import ModelDeclinedError

    recorded: list[tuple[dict, str | None]] = []
    spend = ToolModelSpend(record=lambda usage, served: recorded.append((usage, served)),
                           reserve=lambda _i, _o: (lambda: None))

    class _Declines:
        async def chat(self, **_: object) -> ChatResponse:
            raise ModelDeclinedError(model="served-model", usage={"input_tokens": 700, "output_tokens": 5})

    usage = Usage()

    async def run() -> dict | None:
        with spending_for_tool(spend):
            return await _call(_Declines(), usage)

    assert asyncio.run(run()) is None
    assert usage.as_dict()["input_tokens"] == 700 and usage.as_dict()["declined_calls"] == 1
    assert recorded == [({"input_tokens": 700, "output_tokens": 5}, "served-model")]
