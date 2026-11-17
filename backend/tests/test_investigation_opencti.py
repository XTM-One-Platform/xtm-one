"""OpenCTI in the Deep Investigation pack: read what the platform knows,
and hand the agent exactly what to record back.

Pinned here: the lookup reports what OpenCTI holds and what it does not (the
new is the finding an analyst acts on), never a verdict and never on behalf of
nobody; the pack checks OpenCTI first and at every hop when the caller has one;
and the objects an agent records come from the run's steps, not its prose.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services.investigation_opencti import build_knowledge, format_knowledge, opencti_record_request
from app.services.investigation_packs import (
    ACTION_CHECK_OPENCTI,
    ACTION_PIVOT_NEIGHBOURS,
    ACTION_QUALIFY_NEIGHBOURS,
    SourceDefinition,
    disinfo_infrastructure_pack,
    neighbour_leads,
    opencti_source,
    pivot_leads,
    plan_for,
)
from app.services.investigation_queriers import OpenCTIAccess, make_opencti_querier
from app.services.investigation_report import is_quotable

VT = ("11111111-1111-1111-1111-111111111111", "Team VirusTotal")
OCTI = ("22222222-2222-2222-2222-222222222222", "OpenCTI")
ACCESS = OpenCTIAccess(
    base_url="http://opencti:4000", public_url="https://cti.example", headers={"Authorization": "Bearer t"}
)

REPORT = {
    "id": "r1",
    "entity_type": "Report",
    "name": "Noise without effect",
    "published": "2026-06-15T00:00:00Z",
    "objects": {"edges": [{"node": {"id": "is1", "name": "Storm-1516"}}]},
}


def _node(value: str, oid: str, kind: str = "Domain-Name") -> dict:
    return {
        "id": oid,
        "entity_type": kind,
        "observable_value": value,
        "x_opencti_score": 70,
        "objectLabel": [{"value": "storm-1516"}],
        "containers": {"edges": [{"node": REPORT}]},
    }


def _runner(
    nodes: list[dict], threats: list[dict] | None = None, named: list[dict] | None = None, err: str | None = None
):
    calls: list[tuple[str, dict]] = []

    async def _run(_access, query, variables):
        calls.append((query, variables))
        if err:
            return {}, err
        if "stixCyberObservables" in query:
            return {"stixCyberObservables": {"edges": [{"node": n} for n in nodes]}}, None
        if "stixCoreRelationships" in query:
            return {"stixCoreRelationships": {"edges": [{"node": t} for t in threats or []]}}, None
        return {"stixDomainObjects": {"edges": [{"node": n} for n in named or []]}}, None

    _run.calls = calls  # type: ignore[attr-defined]
    return _run


async def _access(_user_id):
    return ACCESS


async def _no_access(_user_id):
    return None


# ── The lookup ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_known_domain_is_cited_with_its_report_and_campaign():
    threats = [
        {
            "relationship_type": "related-to",
            "from": {"id": "o1"},
            "to": {"id": "c1", "name": "Storm-1516 - Armenia", "entity_type": "Campaign"},
        }
    ]
    query = make_opencti_querier(
        user_id="u", access_loader=_access, runner=_runner([_node("yerevantimes.am", "o1")], threats)
    )
    out = await query(opencti_source(), "https://yerevantimes.am/news/1", "")
    assert out.findings_count == 1
    assert out.detail_code == "source.opencti_known"
    assert out.detail_params == {"known": 1, "checked": 1}
    first = out.evidence[0]
    assert first.href == "https://cti.example/dashboard/id/o1", "linked where a person opens it, not the internal URL"
    assert "yerevantimes.am" in first.quote and '"Noise without effect"' in first.quote
    assert 'campaign "Storm-1516 - Armenia"' in first.quote and 'intrusion set "Storm-1516"' in first.quote
    assert is_quotable(first.quote)
    assert any(e.label == "OpenCTI Report: Noise without effect" for e in out.evidence)


@pytest.mark.asyncio
async def test_a_list_says_which_values_are_new_to_the_platform():
    source = opencti_source(values=["arevmedia.am", "parispresslibre.com", "lratvakannotaner.am"])
    runner = _runner([_node("arevmedia.am", "o2")])
    out = await make_opencti_querier(user_id="u", access_loader=_access, runner=runner)(source, "208.109.215.186", "")
    assert runner.calls[0][1]["values"] == ["arevmedia.am", "parispresslibre.com", "lratvakannotaner.am"]
    assert out.findings_count == 1
    assert out.detail_params == {"known": 1, "checked": 3}
    new = out.evidence[-1]
    assert new.label == "Not in OpenCTI yet: parispresslibre.com, lratvakannotaner.am"
    assert new.href is None


@pytest.mark.asyncio
async def test_an_internationalised_site_is_known_under_either_spelling():
    """OpenCTI may hold an Armenian-script site as typed or as its A-labels:
    both are asked, and the site is one value, known or not."""
    a_labels = "xn--y9aayks5adipc6dll6ad.xn--y9a3aq"
    run = _runner([_node(a_labels, "o9")])
    source = opencti_source(subject_types=("domain",), values=("հայլրատվություն.հայ", "lratu.com"))
    outcome = await make_opencti_querier(user_id="u", access_loader=_access, runner=run)(source, "x", "")
    _query, variables = run.calls[0]
    assert variables["values"] == ["հայլրատվություն.հայ", a_labels, "lratu.com"]
    assert outcome.findings_count == 1
    assert outcome.detail_params == {"known": 1, "checked": 2}
    assert any(e.label == "Not in OpenCTI yet: lratu.com" for e in outcome.evidence)


@pytest.mark.asyncio
async def test_nothing_known_is_empty_and_said():
    out = await make_opencti_querier(user_id="u", access_loader=_access, runner=_runner([]))(
        opencti_source(), "new.example", ""
    )
    assert out.findings_count == 0
    assert out.detail_code == "source.opencti_none_known"
    assert out.error_type is None, "an answer that holds nothing is not a failure"


@pytest.mark.asyncio
async def test_a_name_is_looked_up_among_the_threats():
    named = [
        {"id": "is1", "entity_type": "Intrusion-Set", "name": "Storm-1516", "reports": {"edges": [{"node": REPORT}]}}
    ]
    runner = _runner([], named=named)
    out = await make_opencti_querier(user_id="u", access_loader=_access, runner=runner)(
        opencti_source(), "Storm-1516", ""
    )
    assert "stixDomainObjects" in runner.calls[0][0]
    assert out.findings_count == 1
    assert '"Storm-1516"' in out.evidence[0].quote


@pytest.mark.asyncio
async def test_without_an_opencti_the_step_says_so_and_asks_nothing():
    runner = _runner([])
    out = await make_opencti_querier(user_id="u", access_loader=_no_access, runner=runner)(
        opencti_source(), "a.example", ""
    )
    assert out.error_type == "not_found"
    assert out.detail_code == "source.opencti_unavailable"
    assert runner.calls == []


@pytest.mark.asyncio
async def test_an_opencti_error_fails_the_step_only():
    out = await make_opencti_querier(user_id="u", access_loader=_access, runner=_runner([], err="Forbidden"))(
        opencti_source(), "a.example", ""
    )
    assert out.error_type == "invalid_response"


# ── The pack and its hops ───────────────────────────────────────────────────


def _names(pack, subject_type: str, value: str) -> list[str]:
    _planned, executable = plan_for([pack], subject_type, value)
    return [s.name for _slug, s in executable]


def test_with_opencti_the_pack_checks_it_first_and_at_every_hop():
    pack = disinfo_infrastructure_pack(VT, OCTI)
    assert _names(pack, "domain", "yerevantimes.am")[0] == "OpenCTI knowledge"
    passive = next(s for s in pack.sources if s.name == "VirusTotal passive DNS")
    assert passive.hop_opencti is True
    pivot = pivot_leads(passive, "208.109.215.186")
    assert [(s.name, s.subject, s.action) for s in pivot if s.kind == "opencti"] == [
        ("OpenCTI knowledge (IP)", "208.109.215.186", ACTION_PIVOT_NEIGHBOURS)
    ]
    co_hosted = next(s for s in pivot if s.kind == "passive_dns")
    assert co_hosted.hop_opencti is True
    neighbours = neighbour_leads(["a.am", "b.am"], opencti=co_hosted.hop_opencti)
    lookup = [s for s in neighbours if s.kind == "opencti"]
    assert [(s.opencti_values, s.action) for s in lookup] == [(("a.am", "b.am"), ACTION_QUALIFY_NEIGHBOURS)]
    assert "OpenCTI 'OpenCTI' is read as you" in pack.description


def test_without_opencti_nothing_asks_it():
    pack = disinfo_infrastructure_pack(VT, None)
    assert all(s.kind != "opencti" for s in pack.sources)
    passive = next(s for s in pack.sources if s.name == "VirusTotal passive DNS")
    assert all(s.kind != "opencti" for s in pivot_leads(passive, "1.2.3.4"))
    assert all(s.kind != "opencti" for s in neighbour_leads(["a.am"], opencti=False))
    assert "Connect OpenCTI" in pack.description


def test_an_opencti_source_survives_its_stored_address():
    source = opencti_source(
        "OpenCTI knowledge of the co-hosted sites", action=ACTION_QUALIFY_NEIGHBOURS, values=["a.am", "b.am"]
    )
    rebuilt = SourceDefinition.from_ref(source.to_ref(), subject_types=source.subject_types)
    assert rebuilt.opencti_values == ("a.am", "b.am")
    assert rebuilt.identity() == source.identity()
    assert source.identity() != opencti_source(values=["c.am"]).identity(), "two lists are two lookups"
    assert source.access_identity() == opencti_source().access_identity(), "one place: the caller's OpenCTI"
    passive = SourceDefinition(
        name="VT", subject_types=("domain",), kind="passive_dns", integration_id=VT[0], hop_opencti=True
    )
    assert SourceDefinition.from_ref(passive.to_ref(), subject_types=("domain",)).hop_opencti is True


def test_a_value_list_belongs_to_an_opencti_source_only():
    with pytest.raises(ValueError, match="only an opencti source"):
        SourceDefinition(name="x", subject_types=("domain",), url_template="https://x/{value}", opencti_values=("a",))


def test_the_check_is_the_plan_s_first_action():
    from app.services.investigation_packs import DISINFO_GOAL

    for subject_type in ("domain", "ip", "campaign"):
        path = DISINFO_GOAL.plan(DISINFO_GOAL.initial_state(subject_type))
        assert path[0].slug == ACTION_CHECK_OPENCTI


# ── What the agent records ──────────────────────────────────────────────────


def _step(position: int, kind: str, evidence: list[dict], *, subject: str | None = None, status: str = "completed"):
    ref = {"kind": kind, "name": f"S{position}"}
    if subject:
        ref["subject"] = subject
    return SimpleNamespace(id=uuid.uuid4(), position=position, status=status, evidence=evidence, source_ref=ref)


STEPS = [
    _step(1, "site_fingerprint", [{"label": "Rank Math SEO plugin — https://yerevantimes.am/"}]),
    _step(
        2,
        "passive_dns",
        [
            {
                "label": "208.109.215.186",
                "quote": "VirusTotal passive DNS records that yerevantimes.am resolved to 208.109.215.186.",
            },
            {"label": "78.46.139.150", "quote": "... 78.46.139.150 ..."},
        ],
    ),
    _step(
        3,
        "passive_dns",
        [
            {
                "label": "lratvakannotaner.am",
                "href": "https://www.virustotal.com/gui/domain/lratvakannotaner.am",
                "quote": "q1",
            },
            {"label": "Ազատ մամուլ", "href": "https://www.virustotal.com/gui/domain/xn--example.am", "quote": "q2"},
            {"label": "livechat.rs", "href": "https://www.virustotal.com/gui/domain/livechat.rs", "quote": "q3"},
        ],
        subject="208.109.215.186",
    ),
    _step(
        4,
        "site_fingerprint",
        [{"label": "WordPress REST API — https://lratvakannotaner.am/"}],
        subject="lratvakannotaner.am",
    ),
    _step(5, "passive_dns", [{"label": "9.9.9.9"}], status="error"),
    _step(
        6, "opencti", [{"label": "xn--example.am - known in OpenCTI", "href": "https://cti.example/dashboard/id/o1"}]
    ),
]


def test_the_objects_come_from_the_steps():
    knowledge = build_knowledge("yerevantimes.am", STEPS)
    assert [(o.type, o.value) for o in knowledge.observables] == [
        ("Domain-Name", "yerevantimes.am"),
        ("IPv4-Addr", "208.109.215.186"),
        ("IPv4-Addr", "78.46.139.150"),
        ("Domain-Name", "lratvakannotaner.am"),
        ("Domain-Name", "xn--example.am"),
    ], "the IDN site is its punycode name, read off the link; a failed step adds nothing"
    assert [(a, t, b) for a, b, t, _d in knowledge.relationships] == [
        ("yerevantimes.am", "resolves-to", "208.109.215.186"),
        ("yerevantimes.am", "resolves-to", "78.46.139.150"),
        ("lratvakannotaner.am", "resolves-to", "208.109.215.186"),
        ("xn--example.am", "resolves-to", "208.109.215.186"),
    ]
    assert [v for v, _c in knowledge.notes] == ["yerevantimes.am", "lratvakannotaner.am"]
    assert "Rank Math SEO plugin" in knowledge.notes[0][1] and "not a verdict" in knowledge.notes[0][1]
    assert not knowledge.has("livechat.rs"), "a site whose name alone passed the triage stays in the report"


def test_a_co_hosted_site_is_recorded_once_qualified_or_known():
    pivot = _step(
        1,
        "passive_dns",
        [
            {"label": "dailyrecipes.com", "href": "https://www.virustotal.com/gui/domain/dailyrecipes.com"},
            {"label": "լուր.am", "href": f"https://www.virustotal.com/gui/domain/{'լուր.am'.encode('idna').decode()}"},
            {"label": "mail.armnews.am"},
        ],
        subject="208.109.215.186",
    )
    named_only = build_knowledge("yerevantimes.am", [pivot])
    assert [o.value for o in named_only.observables] == ["yerevantimes.am", "208.109.215.186"]
    assert named_only.relationships == []

    vouched = build_knowledge(
        "yerevantimes.am",
        [
            pivot,
            _step(2, "opencti", [{"label": "լուր.am - known in OpenCTI"}, {"label": "OpenCTI Report: Noise"}]),
            _step(
                3, "site_fingerprint", [{"label": "Rank Math SEO plugin — https://armnews.am/"}], subject="armnews.am"
            ),
            _step(4, "site_fingerprint", [], subject="dailyrecipes.com", status="empty"),
        ],
    )
    assert [o.value for o in vouched.observables] == [
        "yerevantimes.am",
        "208.109.215.186",
        "լուր.am".encode("idna").decode(),
        "armnews.am",
    ], "known in its Unicode spelling, matched on its punycode; a fingerprint with no signal vouches for nothing"


def test_the_agent_is_handed_the_list_and_told_to_keep_to_it():
    text = format_knowledge(build_knowledge("yerevantimes.am", STEPS))
    assert text.startswith("## Knowledge to record in OpenCTI")
    assert "- Domain-Name lratvakannotaner.am" in text
    assert "- yerevantimes.am resolves-to 208.109.215.186" in text
    prompt = opencti_record_request("yerevantimes.am", "33333333-3333-3333-3333-333333333333")
    assert "read_investigation_report (investigation_id 33333333-3333-3333-3333-333333333333)" in prompt
    assert "new draft" in prompt and "Create nothing the list does not hold" in prompt
    assert format_knowledge(build_knowledge("Storm-1516", [])) == "", "a name establishes no observable"


class _ReadDb:
    def __init__(self, investigation, steps):
        self.investigation, self.steps = investigation, steps

    async def get(self, _model, pk):
        return self.investigation if self.investigation.id == pk else None

    async def execute(self, _stmt):
        steps = self.steps
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: steps))


@pytest.mark.asyncio
async def test_the_report_tool_appends_what_to_record():
    from app.services.builtin_tools.investigation import build_deep_investigation_tool
    from app.tools.base import ToolCall

    owner = "44444444-4444-4444-4444-444444444444"
    investigation = SimpleNamespace(
        id=uuid.UUID("33333333-3333-3333-3333-333333333333"),
        user_id=uuid.UUID(owner),
        subject_value="yerevantimes.am",
        subject_type="domain",
        report="## Hosting\n- It resolved to 208.109.215.186. [1]",
        report_sources=[{"n": 1, "source_name": "VirusTotal passive DNS", "label": "208.109.215.186", "quote": "q"}],
        report_claims=1,
        report_uncited_claims=0,
        leads_dropped=0,
    )
    steps = [
        SimpleNamespace(
            **{
                **vars(s),
                "source_name": "S",
                "findings_count": 1,
                "error_type": None,
                "status_detail": None,
                "lead_from_step_id": None,
            }
        )
        for s in STEPS
    ]
    _defs, executor = build_deep_investigation_tool(_ReadDb(investigation, steps), owner)
    result = await executor.execute_tool(
        ToolCall(id="c", name="read_investigation_report", arguments={"investigation_id": str(investigation.id)})
    )
    assert "## Knowledge to record in OpenCTI" in str(result.content)


def test_an_ipv6_address_is_recorded_as_an_ipv6_observable():
    """A VirusTotal resolution may be an AAAA record: the observable is typed
    by the address's family, for a seed's resolutions, a list's seeds and a
    hop alike, never as an `IPv4-Addr`."""
    from app.services.investigation_opencti import SEED_IP_SEPARATOR

    v6, v4 = "2606:4700:3033::6815:3e2", "91.239.146.131"
    seed_resolutions = [
        _step(1, "passive_dns", [{"label": v6, "quote": "q6"}, {"label": v4, "quote": "q4"}]),
        _step(2, "passive_dns", [{"label": "news.am", "href": "https://www.virustotal.com/gui/domain/news.am"}], subject=v6),
    ]
    knowledge = build_knowledge("yerevantimes.am", seed_resolutions)
    assert [(o.type, o.value) for o in knowledge.observables] == [
        ("Domain-Name", "yerevantimes.am"),
        ("IPv6-Addr", v6),
        ("IPv4-Addr", v4),
    ]
    list_step = _step(1, "passive_dns", [{"label": f"armdailynews.com{SEED_IP_SEPARATOR}{v6}", "quote": "q"}])
    list_step.source_ref["seeds"] = ["armdailynews.com", "yerevantimes.am"]
    listed = build_knowledge("2 domains: armdailynews.com, yerevantimes.am", [list_step])
    assert ("IPv6-Addr", v6) in [(o.type, o.value) for o in listed.observables]
    assert [(a, t, b) for a, b, t, _d in listed.relationships] == [("armdailynews.com", "resolves-to", v6)]
    assert "IPv6-Addr" in format_knowledge(knowledge)
