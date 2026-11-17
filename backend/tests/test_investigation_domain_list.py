"""A pasted list of domains investigated in one run.

The Checkfirst demo starts from the 56 domains of a Storm-1516 campaign, not
from one site. A list is its own subject type: the run reads the newest IP of
every seed from passive DNS, pivots only on the IPs several seeds share (a
lighter pivot: fewer pages, no fingerprints, one OpenCTI lookup per IP), and
is stored under a summary of the list, since a subject column holds 500
characters and the seeds ride in the sources instead.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest

from app.models.investigation import DETAIL_CODE_PARAMS
from app.services import investigation_queriers as queriers
from app.services.deep_investigation_chat import (
    MAX_SUBJECT_LENGTH,
    TooManyDomainsError,
    validate_turn_request,
)
from app.services.investigation_events import goal_plan_payload
from app.services.investigation_opencti import build_knowledge
from app.services.investigation_orchestrator import DeepInvestigationOrchestrator, SourceOutcome
from app.services.investigation_packs import (
    ACTION_CHECK_OPENCTI,
    ACTION_MAP_INFRASTRUCTURE,
    ACTION_PIVOT_NEIGHBOURS,
    DISINFO_GOAL,
    DISINFO_INFRASTRUCTURE_SLUG,
    DISINFO_LIST_GOAL,
    MAX_LIST_SEEDS,
    MAX_SHARED_IP_PIVOTS,
    SHARED_IP_MAX_PAGES,
    SourceDefinition,
    classify_subject,
    disinfo_infrastructure_pack,
    domain_list,
    goal_plan_for,
    list_summary,
    plan_for,
    too_many_domains,
    with_seeds,
)

from .test_investigation_orchestrator import _StubSession

VT = ("11111111-1111-1111-1111-111111111111", "Team VirusTotal")
OCTI = ("22222222-2222-2222-2222-222222222222", "OpenCTI")
LIST_SOURCE = "VirusTotal passive DNS of the seeds"


def _list_pack(seeds):
    return with_seeds(disinfo_infrastructure_pack(VT, OCTI), seeds)


def _list_source(seeds) -> SourceDefinition:
    return next(s for s in _list_pack(seeds).sources if s.name == LIST_SOURCE)


# ── What a list is ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "seeds"),
    [
        ("armdailynews.com\nyerevantimes.am", ("armdailynews.com", "yerevantimes.am")),
        ("armdailynews.com, yerevantimes.am;news.am", ("armdailynews.com", "yerevantimes.am", "news.am")),
        ("  ArmDailyNews.com   yerevantimes.am  ", ("armdailynews.com", "yerevantimes.am")),
        # Once each, whichever way it is spelt.
        ("լրատու.հայ\nxn--y9av1b1adp.xn--y9a3aq\nnews.am", ("լրատու.հայ", "news.am")),
    ],
)
def test_a_pasted_list_is_its_seeds_once_each_in_order(text, seeds):
    assert domain_list(text) == seeds
    assert classify_subject(text) == "domain_list"


@pytest.mark.parametrize(
    "text",
    [
        "armdailynews.com",  # one domain is a domain
        "armdailynews.com armdailynews.com",  # the same one twice too
        "armdailynews.com is fake",  # a sentence naming a domain
        "armdailynews.com 1.2.3.4",  # an IP is not a seed
        "https://armdailynews.com yerevantimes.am",  # nor is a URL
        "",
    ],
)
def test_anything_else_is_not_a_list(text):
    assert domain_list(text) is None
    assert classify_subject(text) != "domain_list"


def test_a_list_longer_than_one_run_takes_is_refused_never_cut():
    text = "\n".join(f"site{i}.com" for i in range(MAX_LIST_SEEDS + 1))
    assert domain_list(text) is None and too_many_domains(text) is True
    with pytest.raises(TooManyDomainsError, match=f"up to {MAX_LIST_SEEDS} domains"):
        validate_turn_request(text, None)
    assert too_many_domains("\n".join(f"site{i}.com" for i in range(MAX_LIST_SEEDS))) is False


def test_the_turn_keeps_a_list_whole_and_still_cuts_other_text():
    text = "\n".join(f"newsroom-number-{i}.com" for i in range(40))
    assert len(text) > MAX_SUBJECT_LENGTH
    assert validate_turn_request(text, None) == text
    assert validate_turn_request("A" * (MAX_SUBJECT_LENGTH + 5), None) == "A" * MAX_SUBJECT_LENGTH


def test_a_list_is_stored_under_a_summary_the_column_holds():
    seeds = tuple(f"newsroom-number-{i}.com" for i in range(MAX_LIST_SEEDS))
    summary = list_summary(seeds)
    assert summary == "60 domains: newsroom-number-0.com, newsroom-number-1.com, newsroom-number-2.com and 57 more"
    assert len(summary) <= MAX_SUBJECT_LENGTH
    assert list_summary(("a.com", "b.com")) == "2 domains: a.com, b.com"
    assert domain_list(summary) is None, "the summary is not read back as a list"


# ── The plan ────────────────────────────────────────────────────────────────


def test_a_list_follows_its_own_plan_through_the_shared_ips():
    assert goal_plan_for([DISINFO_INFRASTRUCTURE_SLUG], "domain_list")[1] is DISINFO_LIST_GOAL
    assert goal_plan_for([DISINFO_INFRASTRUCTURE_SLUG], "domain")[1] is DISINFO_GOAL
    plan = DISINFO_LIST_GOAL
    assert [a.slug for a in plan.plan(plan.initial_state("domain_list"))] == [
        ACTION_CHECK_OPENCTI,
        ACTION_MAP_INFRASTRUCTURE,
        ACTION_PIVOT_NEIGHBOURS,
        "apply_doctrine",
        "write_report",
    ]


def test_the_list_sources_carry_the_seeds_and_the_budget_fits_the_pivot():
    seeds = ("armdailynews.com", "yerevantimes.am", "լրատու.հայ")
    pack = _list_pack(seeds)
    _planned, executable = plan_for([pack], "domain_list", list_summary(seeds))
    by_name = {source.name: source for _slug, source in executable}
    assert list(by_name) == ["OpenCTI knowledge", LIST_SOURCE]
    assert by_name["OpenCTI knowledge"].opencti_values == seeds
    assert by_name[LIST_SOURCE].seed_values == seeds
    assert by_name[LIST_SOURCE].action == ACTION_MAP_INFRASTRUCTURE
    # Every list source, then two steps (sites, OpenCTI) per shared IP.
    assert pack.max_iterations == 2 + 2 * MAX_SHARED_IP_PIVOTS
    plain = disinfo_infrastructure_pack(VT, OCTI)
    assert with_seeds(plain, ()).max_iterations == plain.max_iterations
    assert all(not s.seed_values for s in plain.sources)


def test_a_seeds_source_and_its_pivot_survive_their_stored_address():
    source = _list_source(("armdailynews.com", "լրատու.հայ"))
    hop = queriers.seed_ip_leads(source, ["208.109.215.186"])[0]
    for definition in (source, hop):
        rebuilt = SourceDefinition.from_ref(definition.to_ref(), subject_types=definition.subject_types)
        assert rebuilt.identity() == definition.identity()
        assert (rebuilt.seed_values, rebuilt.max_pages, rebuilt.hop_fingerprint) == (
            definition.seed_values,
            definition.max_pages,
            definition.hop_fingerprint,
        )
    assert (hop.subject, hop.max_pages, hop.hop_fingerprint, hop.action) == (
        "208.109.215.186",
        SHARED_IP_MAX_PAGES,
        False,
        ACTION_PIVOT_NEIGHBOURS,
    )


@pytest.mark.parametrize(
    "ref",
    [
        {"kind": "passive_dns", "name": "P", "integration_id": "a", "seeds": "armdailynews.com"},
        {"kind": "passive_dns", "name": "P", "integration_id": "a", "seeds": [1, 2]},
        {"kind": "passive_dns", "name": "P", "integration_id": "a", "max_pages": 0},
        {"kind": "passive_dns", "name": "P", "integration_id": "a", "max_pages": 26},
        {"kind": "passive_dns", "name": "P", "integration_id": "a", "seeds": [f"s{i}.com" for i in range(61)]},
    ],
)
def test_a_stored_address_with_an_unusable_list_is_refused(ref):
    with pytest.raises(ValueError):
        SourceDefinition.from_ref(ref, subject_types=("domain_list",))


def test_the_block_draws_the_list_plan_for_a_list_run():
    run = SimpleNamespace(
        source_pack_slugs=[DISINFO_INFRASTRUCTURE_SLUG], subject_type="domain_list", subject_value="2 domains: a, b"
    )
    payload = goal_plan_payload(run, [])
    assert payload["objective"] == DISINFO_LIST_GOAL.objective
    assert payload["declared"] is True
    assert payload["initial_state"] == ["seed_defined"]
    assert [a["slug"] for a in payload["actions"]] == [
        ACTION_CHECK_OPENCTI,
        ACTION_MAP_INFRASTRUCTURE,
        ACTION_PIVOT_NEIGHBOURS,
        "apply_doctrine",
        "write_report",
    ]


# ── The seeds' passive DNS ──────────────────────────────────────────────────


def _resolution(host: str, ip: str, date: int = 1780000000) -> dict:
    return {"type": "resolution", "attributes": {"host_name": host, "ip_address": ip, "date": date, "resolver": "VT"}}


def _vt(answers: dict[str, object], seen: list[str]):
    """VirusTotal by domain: a list of IPs (newest first), a status, or an exception."""

    def _handler(request: httpx.Request) -> httpx.Response:
        domain = request.url.path.split("/")[-2]
        seen.append(domain)
        answer = answers[domain]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, int):
            return httpx.Response(answer, json={})
        return httpx.Response(200, json={"data": [_resolution(domain, ip) for ip in answer]})

    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(_handler))


async def _key(_integration_id, _user_id):
    return "vt-key"


async def _seeds(answers: dict[str, object], seeds, seen: list[str]):
    querier = queriers.make_passive_dns_querier(user_id="u", key_loader=_key, client_factory=_vt(answers, seen))
    source = _list_source(seeds)
    return await querier(source, list_summary(seeds), source.describe(list_summary(seeds)))


@pytest.mark.asyncio
async def test_each_seed_is_read_once_and_the_shared_ips_are_the_pivot():
    seen: list[str] = []
    answers = {
        "a.com": ["1.1.1.1", "9.9.9.9"],
        "b.com": ["1.1.1.1"],
        "c.com": ["2.2.2.2"],
        "d.com": ["2.2.2.2"],
        "e.com": ["2.2.2.2"],
        "f.com": ["3.3.3.3"],
        "xn--y9av1b1adp.xn--y9a3aq": ["4.4.4.4"],
    }
    seeds = ("a.com", "b.com", "c.com", "d.com", "e.com", "f.com", "լրատու.հայ")
    outcome = await _seeds(answers, seeds, seen)
    assert sorted(seen) == sorted(answers), "one request per seed, an IDN by its A-labels"
    assert [e.label for e in outcome.evidence] == [
        "a.com -> 1.1.1.1",
        "b.com -> 1.1.1.1",
        "c.com -> 2.2.2.2",
        "d.com -> 2.2.2.2",
        "e.com -> 2.2.2.2",
        "f.com -> 3.3.3.3",
        "լրատու.հայ -> 4.4.4.4",
    ], "the newest IP of each seed, in the seeds' order"
    assert outcome.findings_count == 7 and outcome.partial is False
    assert outcome.detail_code == "source.passive_dns_seeds"
    assert outcome.detail_params == {"resolved": 7, "seeds": 7, "ips": 4, "shared": 2, "pivoted": 2}
    assert set(outcome.detail_params) == DETAIL_CODE_PARAMS["source.passive_dns_seeds"]
    # Most-shared first; an IP one seed resolved to is not pivoted on.
    assert [(lead.name, lead.subject) for lead in outcome.leads] == [
        ("VirusTotal co-hosted news domains", "2.2.2.2"),
        ("VirusTotal co-hosted news domains", "1.1.1.1"),
    ]


@pytest.mark.asyncio
async def test_a_seed_virustotal_did_not_answer_for_is_a_gap_said():
    seen: list[str] = []
    answers = {"a.com": ["1.1.1.1"], "b.com": 404, "c.com": 500, "d.com": httpx.ConnectError("down")}
    outcome = await _seeds(answers, ("a.com", "b.com", "c.com", "d.com"), seen)
    assert [e.label for e in outcome.evidence] == ["a.com -> 1.1.1.1"]
    assert outcome.partial is True, "two seeds unread: the map has a gap"
    # The gap is said, not only its size: VirusTotal did not answer for two.
    assert outcome.detail_params == {"resolved": 1, "seeds": 4, "ips": 1, "shared": 0, "pivoted": 0, "failed": 2}
    assert outcome.leads == ()


@pytest.mark.asyncio
async def test_a_virustotal_that_answers_for_no_seed_fails_the_step():
    outcome = await _seeds({"a.com": 500, "b.com": httpx.ConnectError("down")}, ("a.com", "b.com"), [])
    assert outcome.error_type == "unreachable"
    assert outcome.evidence == ()


@pytest.mark.asyncio
async def test_the_pivot_is_capped_on_the_most_shared_ips():
    seeds = tuple(f"s{i}.com" for i in range(2 * (MAX_SHARED_IP_PIVOTS + 2)))
    # Pairs of seeds share an IP; the last pair's IP is shared by a third.
    answers = {seed: [f"45.84.205.{i // 2}"] for i, seed in enumerate(seeds)}
    seeds = (*seeds, "extra.com")
    answers["extra.com"] = [f"45.84.205.{MAX_SHARED_IP_PIVOTS + 1}"]
    outcome = await _seeds(answers, seeds, [])
    pivots = [lead.subject for lead in outcome.leads]
    assert len(pivots) == MAX_SHARED_IP_PIVOTS
    assert pivots[0] == f"45.84.205.{MAX_SHARED_IP_PIVOTS + 1}"
    assert pivots[1:] == [f"45.84.205.{i}" for i in range(MAX_SHARED_IP_PIVOTS - 1)]
    assert outcome.detail_params["shared"] == MAX_SHARED_IP_PIVOTS + 2
    assert outcome.detail_params["pivoted"] == MAX_SHARED_IP_PIVOTS


@pytest.mark.asyncio
async def test_a_list_subject_without_its_seeds_asks_nothing():
    seen: list[str] = []
    querier = queriers.make_passive_dns_querier(user_id="u", key_loader=_key, client_factory=_vt({}, seen))
    bare = next(s for s in disinfo_infrastructure_pack(VT, OCTI).sources if s.name == LIST_SOURCE)
    outcome = await querier(bare, "2 domains: a.com, b.com", "")
    assert outcome.error_type == "invalid_response"
    assert seen == []


@pytest.mark.asyncio
async def test_a_shared_ip_is_read_lightly_and_looked_up_not_fingerprinted():
    pages: list[str | None] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor")
        pages.append(cursor)
        n = int(cursor or 0)
        body = {
            "data": [_resolution(f"news-site-{n}-{k}.com", "1.1.1.1") for k in range(3)],
            "meta": {"cursor": str(n + 1)},
            "links": {"next": f"https://www.virustotal.com/api/v3/ip_addresses/1.1.1.1/resolutions?cursor={n + 1}"},
        }
        return httpx.Response(200, json=body)

    querier = queriers.make_passive_dns_querier(
        user_id="u", key_loader=_key, client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    )
    hop = queriers.seed_ip_leads(_list_source(("a.com", "b.com")), ["1.1.1.1"])[0]
    outcome = await querier(hop, "1.1.1.1", hop.describe("1.1.1.1"))
    assert len(pages) == SHARED_IP_MAX_PAGES
    assert [(lead.kind, lead.action) for lead in outcome.leads] == [("opencti", ACTION_PIVOT_NEIGHBOURS)]
    assert len(outcome.leads[0].opencti_values) == 3 * SHARED_IP_MAX_PAGES


# ── The run ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_list_run_is_stored_under_its_summary_and_reads_every_seed():
    seeds = ("armdailynews.com", "yerevantimes.am", "լրատու.հայ")
    asked: list[tuple[str, tuple, tuple]] = []

    async def _query(source, subject_value, _url):
        asked.append((source.name, source.seed_values, source.opencti_values))
        return SourceOutcome(findings_count=1)

    result = await DeepInvestigationOrchestrator(_StubSession(), _query).run(
        subject_value="\n".join(seeds),
        packs=[disinfo_infrastructure_pack(VT, OCTI)],
    )
    run = result.investigation
    assert run.subject_type == "domain_list"
    assert run.subject_value == list_summary(seeds)
    assert run.max_iterations == 2 + 2 * MAX_SHARED_IP_PIVOTS
    assert asked == [("OpenCTI knowledge", (), seeds), (LIST_SOURCE, seeds, ())]
    assert [s.source_ref.get("seeds") for s in result.steps] == [None, list(seeds)]


# ── What is recorded in OpenCTI ─────────────────────────────────────────────


def test_each_resolved_seed_is_recorded_with_the_ip_it_resolves_to():
    step = SimpleNamespace(
        id=uuid.uuid4(),
        position=1,
        status="degraded",
        evidence=[
            {"label": "armdailynews.com -> 208.109.215.186", "quote": "q1"},
            {"label": "yerevantimes.am -> 208.109.215.186", "quote": "q2"},
            {"label": "not a pair", "quote": "q3"},
            {"label": "evil.com -> not-an-ip", "quote": "q4"},
        ],
        source_ref={"kind": "passive_dns", "name": LIST_SOURCE, "seeds": ["armdailynews.com", "yerevantimes.am"]},
    )
    knowledge = build_knowledge("2 domains: armdailynews.com, yerevantimes.am", [step])
    assert [(o.type, o.value) for o in knowledge.observables] == [
        ("Domain-Name", "armdailynews.com"),
        ("IPv4-Addr", "208.109.215.186"),
        ("Domain-Name", "yerevantimes.am"),
    ]
    assert [(a, t, b, d) for a, b, t, d in knowledge.relationships] == [
        ("armdailynews.com", "resolves-to", "208.109.215.186", "q1"),
        ("yerevantimes.am", "resolves-to", "208.109.215.186", "q2"),
    ]


# ── What a list is not given to ─────────────────────────────────────────────


def test_a_catch_all_pack_does_not_query_a_list_s_summary():
    """A web search, a corpus or a tool handed `56 domains: a, b, c and 53
    more` would answer for that sentence: only the sources that read the seeds
    apply to a list."""
    from app.services.investigation_pack_resolver import _kb_pack, _mcp_pack
    from app.services.investigation_packs import (
        BUILTIN_PACKS,
        ONE_SUBJECT_TYPES,
        SUBJECT_TYPES,
        WEB_RESEARCH,
        KnowledgeBaseSelector,
        recommend_packs,
    )

    assert set(SUBJECT_TYPES) - set(ONE_SUBJECT_TYPES) == {"domain_list"}
    assert WEB_RESEARCH.sources_for("domain_list") == ()
    general_web = BUILTIN_PACKS["web-general"]
    assert general_web.sources_for("domain_list") == ()
    assert _kb_pack([("kb-1", "Corpus")]).sources_for("domain_list") == ()
    assert _mcp_pack([("srv", "Server", "lookup", None)]).sources_for("domain_list") == ()
    assert "domain_list" not in KnowledgeBaseSelector(slug="x").subject_types
    assert [p.slug for p in recommend_packs("domain_list")] == ["web-general"], "the fallback, which plans nothing"
    pack = _list_pack(("armdailynews.com", "yerevantimes.am"))
    assert [s.name for s in pack.sources_for("domain_list")] == ["OpenCTI knowledge", LIST_SOURCE]


def test_the_picker_names_a_list_by_its_summary():
    from .test_investigation_api import _plan

    seeds = [f"newsroom-number-{i}.com" for i in range(40)]
    plan = _plan("\n".join(seeds))
    assert plan.subject_summary == list_summary(seeds)
    assert _plan("armdailynews.com").subject_summary == "armdailynews.com"


@pytest.mark.asyncio
async def test_the_seeds_follow_the_relay_route_of_their_virustotal_integration(monkeypatch):
    """A list's seeds are read with the same `egress_client` as one domain:
    pinned to the route of the VirusTotal integration the source names."""
    import app.core.egress as egress

    route = object()
    asked: list = []
    built: list[dict] = []

    async def _route(integration_id):
        asked.append(integration_id)
        return route

    def _spy(*_args, **kwargs):
        built.append(dict(kwargs))
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(404)))

    monkeypatch.setattr(queriers, "load_virustotal_route", _route)
    monkeypatch.setattr(egress, "egress_client", _spy)
    seeds = ["yerevandaily.am", "haynewsnow.com"]
    source = _list_source(seeds)
    querier = queriers.make_passive_dns_querier(user_id="u", key_loader=_key)
    await querier(source, list_summary(seeds), source.describe(list_summary(seeds)))
    assert asked == [source.integration_id]
    assert len(built) == 1 and built[0]["route"] is route


@pytest.mark.asyncio
@pytest.mark.parametrize("given", [True, False], ids=["parent-type-given", "re-read-from-summary"])
async def test_a_continued_list_keeps_its_type_rather_than_reading_its_summary(monkeypatch, given):
    """A list run is stored under its summary; a continuation names the
    parent's type. Re-read from the summary, the type is `unknown`: the list's
    passive DNS never runs and only a catch-all source reads the summary text,
    the run the review caught being started (and charged) for nothing."""
    from app.services import deep_investigation_chat as dic

    seeds = ("armdailynews.com", "yerevantimes.am")
    asked: list[str] = []

    async def _query(source, _subject_value, _query_text):
        asked.append(source.name)
        return SourceOutcome(findings_count=1)

    async def _write(_db, _result, **_kw):
        return None

    monkeypatch.setattr(dic, "make_dispatching_querier", lambda *_a, **_k: _query)
    monkeypatch.setattr(dic, "_write_report", _write, raising=True)
    _text, result = await dic.run_deep_investigation_chat(
        _StubSession(),
        subject=list_summary(seeds),
        packs=[_list_pack(seeds)],
        **({"subject_type": "domain_list"} if given else {}),
    )
    if given:
        assert result.investigation.subject_type == "domain_list"
        assert LIST_SOURCE in asked
    else:
        assert result.investigation.subject_type == "unknown"
        assert LIST_SOURCE not in asked


@pytest.mark.asyncio
async def test_the_continuation_route_hands_the_turn_its_parent_s_type(monkeypatch):
    """The real route: `POST /investigations/{id}/continuation` passes the
    parent's stored type to the turn, never leaving it to the summary."""
    from app.api.v1 import investigations as api
    from app.api.v1.schemas.investigation import ContinuationRequest

    seeds = ("armdailynews.com", "yerevantimes.am")
    parent = SimpleNamespace(
        id=uuid.uuid4(), subject_value=list_summary(seeds), subject_type="domain_list", conversation_id=None
    )
    child = SimpleNamespace(id=uuid.uuid4())
    pack = _list_pack(seeds)
    turns: list[dict] = []

    async def _load(_db, _investigation_id, _user):
        return parent

    async def _resolve(_db, _investigation, _user, _step_ids):
        return pack, None, []

    async def _turn(_db, **kwargs):
        turns.append(kwargs)
        kwargs["result_out"].append(SimpleNamespace(investigation=child))
        return "continued"

    monkeypatch.setattr(api, "_load_readable", _load)
    monkeypatch.setattr(api, "resolve_selected", _resolve)
    monkeypatch.setattr(api, "run_deep_investigation_turn", _turn)
    response = await api.start_continuation(
        str(parent.id), ContinuationRequest(step_ids=[str(uuid.uuid4())]), user=SimpleNamespace(id=uuid.uuid4()), db=None
    )
    assert len(turns) == 1
    assert (turns[0]["subject"], turns[0]["subject_type"]) == (list_summary(seeds), "domain_list")
    assert turns[0]["continues_investigation_id"] == parent.id
    assert response.investigation_id == str(child.id)


# ── A list's gaps, closed ───────────────────────────────────────────


def test_a_list_without_a_picker_selection_plans_no_web_search_and_no_catch_all():
    """The catch-all packs cover single values only: given a list's summary a
    web search answers for that sentence. Without a selection a list plans
    nothing, rather than three of its domains searched as text."""
    from app.services.investigation_pack_resolver import _kb_pack, _mcp_pack
    from app.services.investigation_packs import ONE_SUBJECT_TYPES, WEB_GENERAL, WEB_RESEARCH, recommend_packs

    assert "domain_list" not in ONE_SUBJECT_TYPES
    assert "domain_list" not in WEB_GENERAL.subject_types
    assert "domain_list" not in WEB_RESEARCH.subject_types
    _planned, executable = plan_for(recommend_packs("domain_list"), "domain_list", list_summary(("a.com", "b.com")))
    assert executable == []
    kb = _kb_pack([("11111111-1111-1111-1111-111111111111", "Doctrine")])
    mcp = _mcp_pack([("22222222-2222-2222-2222-222222222222", "Team MCP", "lookup", None)])
    for pack in (kb, mcp):
        assert "domain_list" not in pack.subject_types
        assert all("domain_list" not in s.subject_types for s in pack.sources)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.1", "0.0.0.0", "192.168.1.10", "not-an-ip"],  # noqa: S104 - resolutions, nothing binds
)
async def test_a_seed_resolving_only_to_a_non_public_address_is_set_aside_and_counted(address):
    seen: list[str] = []
    answers = {"a.com": [address], "b.com": [address], "c.com": ["91.239.146.131"], "d.com": ["91.239.146.131"]}
    outcome = await _seeds(answers, tuple(answers), seen)
    assert [lead.subject for lead in outcome.leads] == ["91.239.146.131"]
    assert outcome.findings_count == 2
    assert outcome.detail_code == "source.passive_dns_seeds"
    required = DETAIL_CODE_PARAMS["source.passive_dns_seeds"]
    if address == "not-an-ip":
        # Not an address at all: no resolution, not a non-public one.
        assert set(outcome.detail_params) == required
    else:
        assert outcome.detail_params == {
            "resolved": 2, "seeds": 4, "ips": 1, "shared": 1, "pivoted": 1, "set_aside": 2
        }
    assert outcome.partial is False


@pytest.mark.asyncio
async def test_a_seed_s_newest_public_address_is_its_pivot_past_a_sinkhole():
    seen: list[str] = []
    answers = {"a.com": ["127.0.0.1", "45.84.205.141"], "b.com": ["45.84.205.141"]}
    outcome = await _seeds(answers, tuple(answers), seen)
    assert [lead.subject for lead in outcome.leads] == ["45.84.205.141"]
    assert outcome.detail_code == "source.passive_dns_seeds"


def test_a_summary_of_the_longest_names_fits_the_subject_column():
    from app.models.investigation import Investigation
    from app.services.investigation_packs import LIST_SUMMARY_MAX_LENGTH

    assert Investigation.__table__.c.subject_value.type.length == LIST_SUMMARY_MAX_LENGTH
    longest = tuple(f"{'a' * 63}.{'b' * 63}.{'c' * 63}.{chr(100 + i) * 61}" for i in range(5))
    assert all(len(name) == 253 for name in longest)
    summary = list_summary(longest)
    assert len(summary) <= LIST_SUMMARY_MAX_LENGTH
    assert summary.startswith("5 domains: ") and summary.endswith(" and 2 more")
    assert summary.count("…") == 3


def test_a_list_past_the_longest_text_is_refused_rather_than_run_as_text():
    from app.services.investigation_packs import MAX_LIST_TEXT

    text = "\n".join(f"{'a' * 60}.{'b' * 60}.site{i}.example.com" for i in range(130))
    assert len(text) > MAX_LIST_TEXT
    assert too_many_domains(text) is True
    with pytest.raises(TooManyDomainsError):
        validate_turn_request(text, None)
    prose = "word " * 4000
    assert len(prose) > MAX_LIST_TEXT and too_many_domains(prose) is False


def _vt_429(answers: dict[str, list], seen: list[str], retry_after: dict[str, list] | None = None):
    """VirusTotal by domain: 429s first (with an optional Retry-After each), then the IPs."""
    pending = {domain: list(values) for domain, values in (retry_after or {}).items()}

    def _handler(request: httpx.Request) -> httpx.Response:
        domain = request.url.path.split("/")[-2]
        seen.append(domain)
        if pending.get(domain):
            header = pending[domain].pop(0)
            return httpx.Response(429, headers={"retry-after": header} if header is not None else {}, json={})
        if answers.get(domain) == 429:
            return httpx.Response(429, json={})
        return httpx.Response(200, json={"data": [_resolution(domain, ip) for ip in answers[domain]]})

    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(_handler))


@pytest.mark.asyncio
async def test_a_seeds_read_answered_only_by_429_is_rate_limited_and_stops_asking():
    seeds = tuple(f"site{i}.com" for i in range(20))
    seen: list[str] = []
    querier = queriers.make_passive_dns_querier(
        user_id="u", key_loader=_key, client_factory=_vt_429(dict.fromkeys(seeds, 429), seen)
    )
    source = _list_source(seeds)
    outcome = await querier(source, list_summary(seeds), source.describe(list_summary(seeds)))
    assert outcome.error_type == "rate_limited"
    assert (outcome.detail_code, outcome.detail_params) == ("source.http_status", {"status": 429})
    # The first 429 stops the read: what was in flight, never the whole list.
    assert 1 <= len(seen) <= queriers.SEED_CONCURRENCY


@pytest.mark.asyncio
async def test_a_short_retry_after_is_waited_out_once():
    seeds = ("a.com", "b.com")
    seen: list[str] = []
    answers = {"a.com": ["91.239.146.131"], "b.com": ["91.239.146.131"]}
    querier = queriers.make_passive_dns_querier(
        user_id="u", key_loader=_key, client_factory=_vt_429(answers, seen, {"a.com": ["0"]})
    )
    source = _list_source(seeds)
    outcome = await querier(source, list_summary(seeds), source.describe(list_summary(seeds)))
    assert outcome.error_type is None and outcome.findings_count == 2
    assert sorted(seen) == ["a.com", "a.com", "b.com"]


@pytest.mark.asyncio
async def test_a_step_that_set_seeds_aside_and_hit_the_rate_limit_says_both():
    """One code, every cause: the counts ride as optional params, each said by
    its own clause in the block, so the 429 is not hidden by the set-aside."""
    from app.models.investigation import DETAIL_CODE_OPTIONAL_PARAMS

    seeds = ("a.com", "b.com", "c.com", "d.com", "e.com")
    answers = {"a.com": ["127.0.0.1"], "b.com": ["10.0.0.1"], "c.com": ["91.239.146.131"], "d.com": ["91.239.146.131"]}
    answers["e.com"] = 429
    seen: list[str] = []
    querier = queriers.make_passive_dns_querier(user_id="u", key_loader=_key, client_factory=_vt_429(answers, seen))
    source = _list_source(seeds)
    outcome = await querier(source, list_summary(seeds), source.describe(list_summary(seeds)))
    assert outcome.detail_code == "source.passive_dns_seeds"
    assert outcome.detail_params == {
        "resolved": 2, "seeds": 5, "ips": 1, "shared": 1, "pivoted": 1, "set_aside": 2, "not_read": 1
    }
    declared = DETAIL_CODE_PARAMS[outcome.detail_code] | DETAIL_CODE_OPTIONAL_PARAMS[outcome.detail_code]
    assert set(outcome.detail_params) <= declared
    assert outcome.partial is True


def test_the_limit_counts_distinct_sites_not_tokens():
    """Repeats, and both spellings of an internationalised name, count once."""
    distinct = [f"site{i}.com" for i in range(50)]
    text = "\n".join([*distinct, *distinct[:20]])
    assert len(text.split()) == 70
    assert domain_list(text) == tuple(distinct)
    assert too_many_domains(text) is False
    validate_turn_request(text, None)
    both_spellings = "\n".join([*(f"other{i}.com" for i in range(59)), "լրատու.հայ", "xn--y9av1b1adp.xn--y9a3aq"])
    assert len(domain_list(both_spellings)) == MAX_LIST_SEEDS
    over = "\n".join(f"site{i}.com" for i in range(MAX_LIST_SEEDS + 1))
    assert domain_list(over) is None and too_many_domains(over) is True
