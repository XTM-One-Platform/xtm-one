"""Goal plans: how every Deep Investigation run is read and drawn.

A pack may declare a goal plan (GOAP: predicates, actions with preconditions,
effects and a cost, a path A* computes); a pack that declares none is drawn
with the default plan read from the kinds of source its run planned. What is
pinned here is what keeps the rendering pack-agnostic and honest: the path is
computed and not listed, every step says which action it serves, the strings
the server sends are translated in every catalog, and a declared plan orders
the run's work without changing what the run is allowed to query.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import investigation_packs as packs
from app.services.investigation_events import goal_plan_payload, step_action, step_payload
from app.services.investigation_orchestrator import DeepInvestigationOrchestrator, SourceOutcome
from app.services.investigation_packs import (
    ACTION_APPLY_DOCTRINE,
    ACTION_CHECK_OPENCTI,
    ACTION_FOLLOW_LEADS,
    ACTION_MAP_INFRASTRUCTURE,
    ACTION_PIVOT_NEIGHBOURS,
    ACTION_QUALIFY_NEIGHBOURS,
    ACTION_QUALIFY_SEED,
    ACTION_READ_SOURCES,
    ACTION_SEARCH_CORPUS,
    ACTION_SEARCH_WEB,
    ACTION_WRITE_REPORT,
    DEFAULT_ACTION_BY_KIND,
    DISINFO_GOAL,
    DISINFO_INFRASTRUCTURE_SLUG,
    GOAL_PLANS,
    GOAL_PLANS_BY_SUBJECT,
    MAX_NEIGHBOURS_QUALIFIED,
    SOURCE_KINDS,
    GoalAction,
    GoalPlan,
    SourceDefinition,
    SourcePack,
    declared_plan_path,
    default_action_for,
    default_goal_plan,
    disinfo_infrastructure_pack,
)
from app.services.investigation_queriers import _goal_hops

from .test_investigation_orchestrator import _install, _StubSession

LANG = Path(__file__).resolve().parents[2] / "frontend" / "src" / "i18n" / "lang"
VT = ("11111111-1111-1111-1111-111111111111", "Team VirusTotal")


def _path(plan: GoalPlan, subject_type: str) -> list[str]:
    return [a.slug for a in plan.plan(plan.initial_state(subject_type))]


# ── A* over a declared plan ─────────────────────────────────────────────────


def test_a_domain_walks_the_whole_method():
    assert _path(DISINFO_GOAL, "domain") == [
        ACTION_CHECK_OPENCTI,
        ACTION_QUALIFY_SEED,
        ACTION_MAP_INFRASTRUCTURE,
        ACTION_PIVOT_NEIGHBOURS,
        ACTION_QUALIFY_NEIGHBOURS,
        ACTION_APPLY_DOCTRINE,
        ACTION_WRITE_REPORT,
    ]


def test_a_subject_further_along_gets_a_shorter_path():
    """An IP already is the infrastructure; a campaign has no host at all.
    What OpenCTI knows is checked first whatever the subject."""
    assert _path(DISINFO_GOAL, "ip") == [
        ACTION_CHECK_OPENCTI,
        ACTION_PIVOT_NEIGHBOURS,
        ACTION_QUALIFY_NEIGHBOURS,
        ACTION_APPLY_DOCTRINE,
        ACTION_WRITE_REPORT,
    ]
    assert _path(DISINFO_GOAL, "campaign") == [ACTION_CHECK_OPENCTI, ACTION_APPLY_DOCTRINE, ACTION_WRITE_REPORT]


def test_a_reached_goal_plans_nothing_and_an_unreachable_one_says_so():
    assert DISINFO_GOAL.plan(frozenset(DISINFO_GOAL.goal)) == ()
    assert DISINFO_GOAL.plan(frozenset()) is None, "nothing defines the seed"


def test_a_planner_picks_the_cheaper_way():
    cheap = GoalAction("cheap", "c", "", 1, ("a",), ("b",))
    dear = GoalAction("dear", "d", "", 5, ("a",), ("b",))
    plan = GoalPlan(
        objective="o", predicates=(("a", "A"), ("b", "B")), goal=("b",), actions=(dear, cheap), initial={"*": ("a",)}
    )
    assert _path(plan, "x") == ["cheap"]


def test_a_plan_naming_what_it_does_not_declare_is_refused():
    with pytest.raises(ValueError, match="unknown predicates"):
        GoalPlan(
            objective="o",
            predicates=(("a", "A"),),
            goal=("a",),
            actions=(GoalAction("x", "x", "", 1, ("a",), ("ghost",)),),
            initial={},
        )
    with pytest.raises(ValueError, match="unknown icon"):
        GoalAction("x", "x", "", 1, (), (), icon="rocket")
    # What A* relies on: a positive cost, at most one goal predicate per action.
    with pytest.raises(ValueError, match="cost at least 1"):
        GoalAction("x", "x", "", 0, (), ())
    with pytest.raises(ValueError, match="more than one goal predicate"):
        GoalPlan(
            objective="o",
            predicates=(("a", "A"), ("b", "B"), ("c", "C")),
            goal=("b", "c"),
            actions=(GoalAction("x", "x", "", 1, ("a",), ("b", "c")),),
            initial={"*": ("a",)},
        )


def test_an_unreachable_declared_plan_is_sent_as_such():
    steps = [
        _step(0, "opencti", action=ACTION_CHECK_OPENCTI),
        _step(1, "site_fingerprint", action=ACTION_QUALIFY_SEED),
        _step(2, "passive_dns", action=ACTION_MAP_INFRASTRUCTURE),
        _step(3, "knowledge_base", action=ACTION_APPLY_DOCTRINE),
    ]
    payload = goal_plan_payload(_run([DISINFO_INFRASTRUCTURE_SLUG], "domain"), steps)
    assert payload["reachable"] is True
    blocked = GoalPlan(
        objective="o",
        predicates=(("a", "A"), ("b", "B"), ("never", "Never")),
        goal=("b",),
        actions=(GoalAction("x", "x", "", 1, ("never",), ("b",)),),
        initial={"*": ("a",)},
    )
    assert blocked.plan(blocked.initial_state("domain")) is None


# ── The default plan ────────────────────────────────────────────────────────


def test_every_kind_of_source_serves_a_default_action():
    """A new kind with no default action would be drawn as 'Other sources'."""
    assert set(DEFAULT_ACTION_BY_KIND) == set(SOURCE_KINDS)
    assert default_action_for("http", is_lead=True) == ACTION_FOLLOW_LEADS
    assert default_action_for("knowledge_base", is_lead=False) == ACTION_SEARCH_CORPUS


def test_the_default_plan_keeps_the_run_order_and_ends_on_the_report():
    plan = default_goal_plan([ACTION_SEARCH_WEB, ACTION_READ_SOURCES, ACTION_SEARCH_WEB, "not-an-action"])
    assert [a.slug for a in plan.actions] == [ACTION_SEARCH_WEB, ACTION_READ_SOURCES, ACTION_WRITE_REPORT]
    report = plan.actions[-1]
    assert report.produces_report
    assert set(report.preconditions) == {"web_searched", "sources_read"}
    assert set(plan.goal) == {"web_searched", "sources_read", "report_written"}
    # The path A* would take is the plan itself.
    assert [a.slug for a in plan.plan(plan.initial_state("domain"))] == [
        ACTION_SEARCH_WEB,
        ACTION_READ_SOURCES,
        ACTION_WRITE_REPORT,
    ]
    assert default_goal_plan([]).actions[-1].slug == ACTION_WRITE_REPORT


def test_a_lead_found_mid_run_never_grows_the_default_goal():
    """Whether a source points anywhere is unknown when the run is planned."""
    before = default_goal_plan([ACTION_READ_SOURCES])
    after = default_goal_plan([ACTION_READ_SOURCES, ACTION_FOLLOW_LEADS])
    assert [a.slug for a in after.actions] == [ACTION_READ_SOURCES, ACTION_FOLLOW_LEADS, ACTION_WRITE_REPORT]
    assert set(after.goal) == set(before.goal) == {"sources_read", "report_written"}
    assert set(after.actions[-1].preconditions) == {"sources_read"}


def test_a_declared_path_is_computed_once_per_subject_type():
    declared_plan_path.cache_clear()
    initial, path = declared_plan_path(DISINFO_INFRASTRUCTURE_SLUG, "domain")
    plan = GOAL_PLANS[DISINFO_INFRASTRUCTURE_SLUG]
    assert initial == plan.initial_state("domain")
    assert path == plan.plan(initial)
    assert declared_plan_path(DISINFO_INFRASTRUCTURE_SLUG, "domain")[1] is path
    assert declared_plan_path.cache_info().hits == 1


# ── What the block is sent ──────────────────────────────────────────────────


def _step(position: int, kind: str, *, action: str | None = None, subject: str | None = None, lead: bool = False):
    ref: dict = {"kind": kind, "name": f"S{position}"}
    if action:
        ref["action"] = action
    if subject:
        ref["subject"] = subject
    return SimpleNamespace(
        id=uuid.uuid4(),
        position=position,
        status="pending",
        source_name=f"S{position}",
        source_pack_slug=None,
        query=None,
        error_type=None,
        status_detail=None,
        detail_code=None,
        detail_params=None,
        findings_count=0,
        confidence=None,
        evidence=[],
        answer=None,
        iteration=None,
        lead_from_step_id=uuid.uuid4() if lead else None,
        source_ref=ref,
    )


def _run(slugs: list[str], subject_type: str = "domain"):
    return SimpleNamespace(source_pack_slugs=slugs, subject_type=subject_type, subject_value="yerevandaily.am")


def test_a_step_says_which_action_it_serves_and_what_it_queried():
    hop = step_payload(_step(3, "passive_dns", action=ACTION_PIVOT_NEIGHBOURS, subject="208.109.215.186", lead=True))
    assert hop["action"] == ACTION_PIVOT_NEIGHBOURS
    assert hop["subject_value"] == "208.109.215.186"
    plain = step_payload(_step(1, "web_search"))
    assert plain["action"] == ACTION_SEARCH_WEB
    assert plain["subject_value"] is None
    assert step_action(_step(2, "http", lead=True)) == ACTION_FOLLOW_LEADS


def test_a_declared_plan_is_sent_as_its_a_star_path():
    payload = goal_plan_payload(_run([DISINFO_INFRASTRUCTURE_SLUG], "ip"), [])
    assert payload["declared"] is True
    assert payload["pack_slug"] == DISINFO_INFRASTRUCTURE_SLUG
    assert payload["objective"] == DISINFO_GOAL.objective, "the template: the block places the subject"
    assert [a["slug"] for a in payload["actions"]] == _path(DISINFO_GOAL, "ip")
    assert payload["initial_state"] == ["seed_defined", "seed_qualified", "infrastructure_mapped"]
    assert payload["path_cost"] == sum(a.cost for a in DISINFO_GOAL.plan(DISINFO_GOAL.initial_state("ip")))
    assert payload["actions"][-1]["produces_report"] is True


def test_a_run_recorded_before_its_pack_declared_a_plan_reads_as_the_default_one():
    steps = [_step(1, "site_fingerprint"), _step(2, "passive_dns")]
    payload = goal_plan_payload(_run([DISINFO_INFRASTRUCTURE_SLUG]), steps)
    assert payload["declared"] is False
    assert [a["slug"] for a in payload["actions"]] == [ACTION_READ_SOURCES, ACTION_WRITE_REPORT]
    # One step serving the plan is enough for the plan to be the run's.
    steps.append(_step(3, "http", action=ACTION_QUALIFY_SEED))
    assert goal_plan_payload(_run([DISINFO_INFRASTRUCTURE_SLUG]), steps)["declared"] is True


def test_any_other_run_is_sent_the_default_plan_of_its_steps():
    steps = [_step(1, "http"), _step(2, "knowledge_base"), _step(3, "http"), _step(4, "http", lead=True)]
    payload = goal_plan_payload(_run(["web-general"]), steps)
    assert payload["declared"] is False
    assert payload["pack_slug"] is None
    assert [a["slug"] for a in payload["actions"]] == [
        ACTION_READ_SOURCES,
        ACTION_SEARCH_CORPUS,
        ACTION_FOLLOW_LEADS,
        ACTION_WRITE_REPORT,
    ]
    assert {a["icon"] for a in payload["actions"]} <= packs.GOAL_ACTION_ICONS


def _plan_strings() -> set[str]:
    plans = [
        *GOAL_PLANS.values(),
        *GOAL_PLANS_BY_SUBJECT.values(),
        default_goal_plan([*DEFAULT_ACTION_BY_KIND.values(), ACTION_FOLLOW_LEADS]),
    ]
    strings: set[str] = set()
    for plan in plans:
        strings.add(plan.objective)
        strings.update(label for _slug, label in plan.predicates)
        for action in plan.actions:
            strings.update((action.label, action.description))
    return strings


@pytest.mark.parametrize("locale", ["en", "fr", "es", "ja"])
def test_every_string_a_plan_sends_is_translated(locale):
    """The block renders them through `t_i18n`: a missing key reads English."""
    catalog = json.loads((LANG / f"{locale}.json").read_text(encoding="utf-8"))
    missing = sorted(_plan_strings() - set(catalog))
    assert not missing, f"{locale}.json lacks {missing}"
    # Present is not translated: an English copy under the key reads English.
    untranslated = sorted(s for s in _plan_strings() if (catalog[s] == s) != (locale == "en"))
    assert not untranslated, f"{locale}.json does not translate {untranslated}"


def test_an_objective_places_the_subject_through_one_placeholder():
    for plan in [*GOAL_PLANS.values(), *GOAL_PLANS_BY_SUBJECT.values(), default_goal_plan([])]:
        assert plan.objective.count("{value}") == 1


# ── The hops ────────────────────────────────────────────────────────────────


def _vt_source(action: str) -> SourceDefinition:
    return SourceDefinition(
        name="VT", subject_types=("domain", "ip"), kind="passive_dns", integration_id=VT[0], action=action
    )


def test_a_domain_hops_to_the_ip_it_last_resolved_to():
    kept = [("yerevandaily.am", "208.109.215.186", None, None), ("yerevandaily.am", "91.239.146.131", None, None)]
    leads = _goal_hops(_vt_source(ACTION_MAP_INFRASTRUCTURE), False, kept)
    assert [(lead.name, lead.subject, lead.action) for lead in leads] == [
        ("RDAP registration (IP)", "208.109.215.186", ACTION_PIVOT_NEIGHBOURS),
        ("VirusTotal co-hosted news domains", "208.109.215.186", ACTION_PIVOT_NEIGHBOURS),
    ]


def test_an_ip_hops_to_its_newest_sites_once_each():
    kept = [
        ("cpanel.lratvakannotaner.am", "208.109.215.186", None, None),
        ("lratvakannotaner.am", "208.109.215.186", None, None),
        *[(f"site{i}news.com", "208.109.215.186", None, None) for i in range(10)],
    ]
    leads = _goal_hops(_vt_source(ACTION_PIVOT_NEIGHBOURS), True, kept)
    assert len(leads) == MAX_NEIGHBOURS_QUALIFIED
    assert leads[0].subject == "lratvakannotaner.am", "a panel host is its site"
    assert len({lead.subject for lead in leads}) == len(leads)
    assert {lead.action for lead in leads} == {ACTION_QUALIFY_NEIGHBOURS}
    assert {lead.kind for lead in leads} == {"site_fingerprint"}


def test_a_source_serving_no_hop_action_hops_nowhere():
    kept = [("a.news", "1.2.3.4", None, None)]
    assert _goal_hops(_vt_source(ACTION_QUALIFY_SEED), False, kept) == ()
    assert _goal_hops(_vt_source(ACTION_MAP_INFRASTRUCTURE), False, []) == ()


def test_the_pack_declares_its_plan_and_every_source_an_action_of_it():
    pack = disinfo_infrastructure_pack(VT)
    assert pack.goal is DISINFO_GOAL
    declared = {a.slug for a in DISINFO_GOAL.actions}
    assert all(source.action in declared for source in pack.sources)


# ── The run follows the plan ────────────────────────────────────────────────


FIRST = GoalAction("first", "First", "", 1, ("start",), ("one",))
SECOND = GoalAction("second", "Second", "", 1, ("one",), ("two",))
TWO_STEP = GoalPlan(
    objective="Investigate {value}",
    predicates=(("start", "Start"), ("one", "One"), ("two", "Two")),
    goal=("two",),
    actions=(FIRST, SECOND),
    initial={"*": ("start",)},
)


def _http(name: str, action: str, subject: str | None = None) -> SourceDefinition:
    return SourceDefinition(
        name=name,
        url_template=f"https://{name.lower()}.test/{{value}}",
        subject_types=("unknown",),
        action=action,
        subject=subject,
    )


@pytest.mark.asyncio
async def test_with_two_packs_each_keeps_its_place_and_an_unknown_action_runs_last(monkeypatch):
    from app.services import investigation_packs as packs_module

    goal_pack = SourcePack(
        slug="goal-pack",
        label="goal",
        description="",
        subject_types=("unknown",),
        sources=(_http("Later", "second"), _http("Stray", "not-in-plan"), _http("Seed", "first")),
        max_iterations=10,
        goal=TWO_STEP,
    )
    plain_pack = SourcePack(
        slug="plain-pack", label="plain", description="", subject_types=("unknown",),
        sources=(_http("Plain", "first"),), max_iterations=10,
    )
    monkeypatch.setattr(packs_module, "BUILTIN_PACKS", {goal_pack.slug: goal_pack, plain_pack.slug: plain_pack})
    monkeypatch.setattr(packs_module, "DEFAULT_PACK_SLUG", goal_pack.slug)
    calls: list[str] = []

    async def _query(source, _subject_value, _url):
        calls.append(source.name)
        return SourceOutcome(findings_count=1)

    await DeepInvestigationOrchestrator(_StubSession(), _query).run(
        subject_value="s", source_pack_slugs=["goal-pack", "plain-pack"]
    )
    assert calls == ["Seed", "Later", "Stray", "Plain"]


@pytest.mark.asyncio
async def test_a_declared_plan_orders_the_work_and_its_hops_query_their_own_subject(monkeypatch):
    hop = _http("Hop", "first", subject="1.2.3.4")
    pack = SourcePack(
        slug="goal-pack",
        label="goal",
        description="",
        subject_types=("unknown",),
        # Listed second-action first: the plan, not the list, decides.
        sources=(_http("Later", "second"), _http("Seed", "first")),
        max_iterations=10,
        goal=TWO_STEP,
    )
    _install(monkeypatch, pack)
    calls: list[tuple[str, str]] = []

    async def _query(source, subject_value, _url):
        calls.append((source.name, subject_value))
        if source.name == "Seed":
            return SourceOutcome(findings_count=1, leads=(hop,))
        return SourceOutcome(findings_count=1)

    db = _StubSession()
    result = await DeepInvestigationOrchestrator(db, _query).run(subject_value="seed-subject")
    assert calls == [("Seed", "seed-subject"), ("Hop", "1.2.3.4"), ("Later", "seed-subject")]
    hop_step = next(s for s in result.steps if s.source_name == "Hop")
    assert hop_step.source_ref["subject"] == "1.2.3.4"
    assert hop_step.source_ref["action"] == "first"


@pytest.mark.asyncio
async def test_a_budget_spent_mid_plan_skips_the_rest_and_counts_leads_past_the_cap(monkeypatch):
    """What a run did not reach is recorded, never dropped: skipped hop steps
    keep their subject (a continuation can query them), the action after them
    is skipped, and leads past the plan cap are counted."""
    from app.services.investigation_orchestrator import MAX_LEAD_STEPS

    hops = tuple(_http(f"Hop {i}", "first", subject=f"10.0.0.{i}") for i in range(MAX_LEAD_STEPS + 2))
    pack = SourcePack(
        slug="goal-pack",
        label="goal",
        description="",
        subject_types=("unknown",),
        sources=(_http("Later", "second"), _http("Seed", "first")),
        max_iterations=10,
        goal=TWO_STEP,
    )
    _install(monkeypatch, pack)

    async def _query(source, _subject_value, _url):
        return SourceOutcome(findings_count=1, leads=hops if source.name == "Seed" else ())

    result = await DeepInvestigationOrchestrator(_StubSession(), _query).run(
        subject_value="seed-subject", max_iterations=2
    )
    assert result.investigation.end_reason_code == "run.budget_spent"
    assert result.investigation.leads_dropped == 2
    hop_steps = [s for s in result.steps if s.source_name.startswith("Hop")]
    assert [s.status for s in hop_steps] == ["completed"] + ["skipped"] * (MAX_LEAD_STEPS - 1)
    assert [s.source_ref["subject"] for s in hop_steps] == [f"10.0.0.{i}" for i in range(MAX_LEAD_STEPS)]
    assert [s.status for s in result.steps if s.source_name == "Later"] == ["skipped"]


def test_the_api_response_keeps_the_plan_and_each_step_action():
    """The route's response model is a filter: a field it does not declare is
    dropped on the way out, and the block falls back to its flat list."""
    from app.api.v1.schemas.investigation import InvestigationResponse

    steps = [_step(1, "http"), _step(2, "passive_dns", action=ACTION_PIVOT_NEIGHBOURS, subject="1.2.3.4")]
    response = InvestigationResponse.model_validate(
        {
            "id": str(uuid.uuid4()),
            "subject_type": "domain",
            "subject_value": "yerevandaily.am",
            "status": "completed",
            "max_iterations": 10,
            "iterations_used": 2,
            "revision": 3,
            "goal_plan": goal_plan_payload(_run([DISINFO_INFRASTRUCTURE_SLUG]), steps),
            "steps": [step_payload(s) for s in steps],
            "summary": {"total": 2, "all_succeeded": False, "failed": 0, "unfinished": 0},
        }
    ).model_dump()
    assert [a["slug"] for a in response["goal_plan"]["actions"]] == _path(DISINFO_GOAL, "domain")
    assert response["goal_plan"]["actions"][0]["icon"] == "database"
    assert response["goal_plan"]["actions"][1]["icon"] == "fingerprint"
    assert response["steps"][1]["action"] == ACTION_PIVOT_NEIGHBOURS
    assert response["steps"][1]["subject_value"] == "1.2.3.4"
    assert response["steps"][0]["action"] == ACTION_READ_SOURCES


# ── The executive summary ───────────────────────────────────────────────────


def _reported_run(slugs: list[str], subject_type: str = "domain", report: str | None = "# Report\n\nFound [1]."):
    run = _run(slugs, subject_type)
    run.report = report
    run.id = uuid.UUID("22222222-2222-2222-2222-222222222222")
    return run


def test_no_report_no_executive_summary():
    from app.services.investigation_events import executive_summary_payload

    assert executive_summary_payload(_reported_run([DISINFO_INFRASTRUCTURE_SLUG], report=None), []) is None


def test_the_executive_summary_names_the_xtm_one_deck_and_the_plan_steps():
    from app.services.investigation_events import executive_summary_payload

    payload = executive_summary_payload(_reported_run([DISINFO_INFRASTRUCTURE_SLUG]), [])
    assert payload["template_name"] == "XTM One Brand Deck"
    prompt = payload["prompt"]
    assert '"XTM One Brand Deck" template' in prompt
    assert "yerevandaily.am" in prompt
    for action in DISINFO_GOAL.plan(DISINFO_GOAL.initial_state("domain")):
        assert (action.label in prompt) is not action.produces_report
    assert "Add nothing the report does not say." in prompt
    # The agent reads the stored report rather than writing from memory.
    assert "read_investigation_report (investigation_id 22222222-2222-2222-2222-222222222222)" in prompt


def test_a_run_without_a_declared_plan_gets_one_too():
    from app.services.investigation_events import executive_summary_payload

    payload = executive_summary_payload(_reported_run(["web-general"]), [_step(1, "http"), _step(2, "web_search")])
    assert '"Read the external sources"; "Search the web"' in payload["prompt"]


def test_the_instruction_settles_the_template_choice():
    """Naming the template is what lets `generate_presentation` create the deck
    on an interactive turn without asking which template to use."""
    from app.services.investigation_events import executive_summary_payload
    from app.services.presentations import TemplateOption, template_choice_settled

    prompt = executive_summary_payload(_reported_run([DISINFO_INFRASTRUCTURE_SLUG]), [])["prompt"]
    xtm = TemplateOption(
        id=uuid.uuid4(), name="XTM One Brand Deck", description=None, supported_schemes=("dark",), is_default=True, is_company_managed=True
    )
    other = TemplateOption(
        id=uuid.uuid4(), name="Filigran Brand Deck", description=None, supported_schemes=("dark",), is_default=False, is_company_managed=True
    )
    assert template_choice_settled(prompt, str(xtm.id), [xtm, other]) is True
    assert template_choice_settled(prompt, str(other.id), [xtm, other]) is False


class _ReadDb:
    def __init__(self, investigation):
        self.investigation = investigation

    async def get(self, _model, pk):
        return self.investigation if self.investigation and self.investigation.id == pk else None

    async def execute(self, _stmt):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))


def _stored(owner: str):
    return SimpleNamespace(
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


async def _read(executor, investigation_id: str) -> str:
    from app.tools.base import ToolCall

    result = await executor.execute_tool(
        ToolCall(id=str(uuid.uuid4()), name="read_investigation_report", arguments={"investigation_id": investigation_id})
    )
    return str(result.content)


@pytest.mark.asyncio
async def test_an_agent_reads_the_stored_report_with_its_key():
    from app.services.builtin_tools.investigation import build_deep_investigation_tool

    owner = "44444444-4444-4444-4444-444444444444"
    definitions, executor = build_deep_investigation_tool(_ReadDb(_stored(owner)), owner)
    assert [d.name for d in definitions] == ["deep_investigation", "read_investigation_report"]
    text = await _read(executor, "33333333-3333-3333-3333-333333333333")
    assert "It resolved to 208.109.215.186. [1]" in text
    assert "[1] 208.109.215.186" in text


@pytest.mark.asyncio
async def test_another_users_run_is_answered_like_an_unknown_one():
    from app.services.builtin_tools.investigation import build_deep_investigation_tool

    _defs, executor = build_deep_investigation_tool(
        _ReadDb(_stored("44444444-4444-4444-4444-444444444444")), "55555555-5555-5555-5555-555555555555"
    )
    assert "is available to you" in await _read(executor, "33333333-3333-3333-3333-333333333333")
    assert "is available to you" in await _read(executor, "66666666-6666-6666-6666-666666666666")



def test_an_action_no_source_of_the_run_can_serve_is_said_up_front():
    """Without VirusTotal there is no passive DNS: the hosting cannot be mapped,
    and the hops that map feeds can never run."""
    without_vt = [
        _step(0, "opencti", action=ACTION_CHECK_OPENCTI),
        _step(1, "site_fingerprint", action=ACTION_QUALIFY_SEED),
        _step(2, "knowledge_base", action=ACTION_APPLY_DOCTRINE),
    ]
    payload = goal_plan_payload(_run([DISINFO_INFRASTRUCTURE_SLUG]), without_vt)
    servable = {a["slug"]: a["servable"] for a in payload["actions"]}
    assert servable == {
        ACTION_CHECK_OPENCTI: True,
        ACTION_QUALIFY_SEED: True,
        ACTION_MAP_INFRASTRUCTURE: False,
        ACTION_PIVOT_NEIGHBOURS: False,
        ACTION_QUALIFY_NEIGHBOURS: False,
        ACTION_APPLY_DOCTRINE: True,
        ACTION_WRITE_REPORT: True,
    }
    assert payload["reachable"] is False
    with_vt = [*without_vt, _step(3, "passive_dns", action=ACTION_MAP_INFRASTRUCTURE)]
    payload = goal_plan_payload(_run([DISINFO_INFRASTRUCTURE_SLUG]), with_vt)
    assert all(a["servable"] for a in payload["actions"])
    assert payload["reachable"] is True
