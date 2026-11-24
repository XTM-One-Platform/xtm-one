"""AI-drafted fleet documentation (#9174 slice 7), without a database.

Pinned here: a built-in fleet is refused before the model; an unknown section
is refused before the model; the whole draft asks for every section and a
regenerate for one; the answer is found fenced, bare or in prose, unknown keys
are dropped and noted, a long section is cut; a cut or unreadable answer is
refused; the context names members by name and carries their configuration,
assignments and a bounded persona excerpt, never an id; nothing is written; the
route is AI-gated.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from app.services import fleet_doc_generation as gen
from app.services.fleet_docs import DOC_SECTIONS, SECTION_MAX_CHARS
from app.services.fleets import FleetActionRefusedError


def _fleet(**fields) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "name": "Alert Desk",
        "description": "Triages alerts.",
        "skills": [],
        "is_builtin": False,
        "documentation": None,
        "orchestrator_agent_id": None,
        "default_context_inclusion": "none",
        "default_collaboration_instruction": "",
        "organization_scope": {"mode": "primary"},
    }
    return SimpleNamespace(**{**base, **fields})


class _Model:
    def __init__(self, content: str, stop_reason: str = "end_turn") -> None:
        self.content = content
        self.stop_reason = stop_reason
        self.messages: list = []

    async def chat(self, messages, *, model, temperature, max_tokens):
        self.messages.append(messages)
        return SimpleNamespace(content=self.content, stop_reason=self.stop_reason)


@pytest.fixture
def no_database(monkeypatch):
    async def _context(_db, _fleet):
        return {"fleet": {"name": "Alert Desk"}, "members": [], "connected": []}

    monkeypatch.setattr(gen, "load_draft_context", _context)


async def test_a_builtin_fleet_is_generated_like_a_custom_one(no_database) -> None:
    """#9768 S2: the draft reads a built-in fleet like a custom one (the save refuses it, #1956)."""
    answer = {k: f"Text of {k}." for k in DOC_SECTIONS}
    model = _Model(json.dumps(answer))
    draft = await gen.draft_fleet_documentation(object(), uuid.uuid4(), _fleet(is_builtin=True), llm=model)
    assert list(draft.sections) == list(DOC_SECTIONS)


@pytest.fixture
def fingerprint(monkeypatch):
    from app.services import fleet_docs

    async def fake(_db, _fleet):
        return "f" * 16

    monkeypatch.setattr(fleet_docs, "current_fingerprint", fake)


async def test_a_regenerate_saves_the_next_version_and_never_needs_an_editor(no_database, fingerprint) -> None:
    answer = {k: f"Text of {k}." for k in DOC_SECTIONS}
    fleet = _fleet()
    author = uuid.uuid4()
    await gen.regenerate_fleet_documentation(object(), author, fleet, base_version=0, llm=_Model(json.dumps(answer)))
    assert fleet.documentation["version"] == 1
    assert fleet.documentation["updated_by"] == str(author)
    assert fleet.documentation["sections"]["purpose"] == "Text of purpose."
    assert "local" not in fleet.documentation, "a custom fleet has no shipped version"


async def test_a_one_section_regenerate_keeps_the_other_sections_and_the_structured_parts(
    no_database, fingerprint
) -> None:
    structured = {"purpose_line": "Why"}
    fleet = _fleet(
        documentation={
            "sections": {"purpose": "Old purpose.", "flow": "Old flow."},
            "version": 2,
            "structured": structured,
        }
    )
    await gen.regenerate_fleet_documentation(
        object(), uuid.uuid4(), fleet, section="flow", base_version=2, llm=_Model(json.dumps({"flow": "New flow."}))
    )
    assert fleet.documentation["sections"] == {"purpose": "Old purpose.", "flow": "New flow."}
    assert fleet.documentation["structured"] == structured
    assert fleet.documentation["version"] == 3


async def test_a_stale_regenerate_is_refused_before_the_model(no_database, fingerprint) -> None:
    model = _Model("{}")
    fleet = _fleet(documentation={"sections": {"purpose": "x"}, "version": 4})
    with pytest.raises(FleetActionRefusedError) as stale:
        await gen.regenerate_fleet_documentation(object(), uuid.uuid4(), fleet, base_version=3, llm=model)
    assert stale.value.status_code == 409
    assert model.messages == []


async def test_a_builtin_regenerate_is_refused_before_the_model(no_database, fingerprint) -> None:
    """#1956: the seed writes a built-in fleet's documentation; no regenerate, no local version."""
    shipped = {"sections": {"purpose": "Shipped."}, "structured": None}
    stored = {"sections": {"purpose": "Shipped."}, "version": 1, "updated_by": "seed", "shipped": shipped}
    fleet = _fleet(is_builtin=True, documentation=dict(stored))
    model = _Model(json.dumps({k: f"Text of {k}." for k in DOC_SECTIONS}))
    with pytest.raises(FleetActionRefusedError) as refused:
        await gen.regenerate_fleet_documentation(object(), uuid.uuid4(), fleet, llm=model)
    assert refused.value.status_code == 422
    assert model.messages == []
    assert fleet.documentation == stored


async def test_an_unknown_section_is_refused_before_the_model(no_database) -> None:
    model = _Model("{}")
    with pytest.raises(gen.FleetDocDraftError):
        await gen.draft_fleet_documentation(object(), uuid.uuid4(), _fleet(), section="secret", llm=model)
    assert model.messages == []


async def test_the_whole_draft_asks_for_every_section_and_writes_nothing(no_database) -> None:
    answer = {k: f"Text of {k}." for k in DOC_SECTIONS}
    model = _Model("```json\n" + json.dumps(answer) + "\n```")
    fleet = _fleet()
    draft = await gen.draft_fleet_documentation(object(), uuid.uuid4(), fleet, llm=model, model="m")
    assert list(draft.sections) == list(DOC_SECTIONS)
    # A flat answer carries no structured part: it is asked once more, and said (#9979).
    assert draft.notes[0] == {"code": "second_call_asked", "params": {}}
    assert len(model.messages) == 2
    assert fleet.documentation is None, "a draft is never saved"
    asked = model.messages[0][1].content
    assert all(f"- {k}:" in asked for k in DOC_SECTIONS)


async def test_a_regenerate_asks_for_one_section_and_keeps_the_rest_in_view(no_database) -> None:
    fleet = _fleet(documentation={"sections": {"purpose": "Old purpose.", "flow": "Old flow."}, "version": 2})
    model = _Model(json.dumps({"flow": "New flow.", "purpose": "Not asked."}))
    draft = await gen.draft_fleet_documentation(object(), uuid.uuid4(), fleet, section="flow", llm=model)
    assert draft.sections == {"flow": "New flow."}
    assert any(n["code"] == "section_left_out" for n in draft.notes)
    asked = model.messages[0][1].content
    assert "- flow:" in asked and "- purpose:" not in asked
    assert "Old purpose." in asked, "the current documentation is given for coherence"
    assert fleet.documentation["version"] == 2


def test_the_answer_is_found_bare_or_in_prose_and_long_sections_are_cut() -> None:
    body = json.dumps({"purpose": "x" * (SECTION_MAX_CHARS + 5), "outcomes": "  ", "members": 3})
    draft = gen.extract_sections(f"Here: {body} done", ["purpose", "outcomes", "members"])
    assert len(draft.sections["purpose"]) == SECTION_MAX_CHARS
    assert {"code": "section_cut", "params": {"max": SECTION_MAX_CHARS}} in draft.notes
    assert {"code": "sections_not_drafted", "params": {"count": 2}} in draft.notes
    with pytest.raises(gen.FleetDocDraftError):
        gen.extract_sections("no json here", ["purpose"])
    with pytest.raises(gen.FleetDocDraftError):
        gen.extract_sections(json.dumps({"purpose": ""}), ["purpose"])


async def test_an_answer_always_cut_still_drafts_every_section(no_database) -> None:
    """#11292: a cut is never an error; each section is drafted on its own and kept as written."""
    cut = _Model("A first line of text.\nA second line that the limit", stop_reason="max_tokens")
    draft = await gen.draft_fleet_documentation(object(), uuid.uuid4(), _fleet(), llm=cut)
    assert list(draft.sections) == list(DOC_SECTIONS)
    # The continuations joined, then cut back to the last whole line: never empty, never an error.
    assert all(text.startswith("A first line of text.") for text in draft.sections.values())
    assert not any(text.endswith("the limit") for text in draft.sections.values())
    assert draft.pending == []


def test_the_context_names_members_with_their_configuration_never_an_id() -> None:
    lead_id, analyst_id = uuid.uuid4(), uuid.uuid4()
    lead_row, analyst_row = uuid.uuid4(), uuid.uuid4()
    agents = {
        lead_id: SimpleNamespace(
            id=lead_id,
            name="Desk Lead",
            slug="desk-lead",
            description="Routes alerts.",
            persona="You route alerts. " * 100,
            handover_agent_ids=[str(analyst_id)],
            handover_types={str(analyst_id): "delegate"},
        ),
        analyst_id: SimpleNamespace(
            id=analyst_id,
            name="Analyst",
            slug="analyst",
            description="",
            persona="You analyse.",
            handover_agent_ids=[],
            handover_types={},
        ),
    }
    rows = [
        SimpleNamespace(
            id=lead_row,
            agent_id=lead_id,
            target_fleet_id=None,
            role="member",
            role_label="Lead",
            parent_member_id=None,
            is_exposed=True,
            context_inclusion=None,
            collaboration_instruction=None,
            organization_scope=None,
        ),
        SimpleNamespace(
            id=analyst_row,
            agent_id=analyst_id,
            target_fleet_id=None,
            role="member",
            role_label="Analyst",
            parent_member_id=lead_row,
            is_exposed=False,
            context_inclusion="summary",
            collaboration_instruction="Report the verdict.",
            organization_scope={"mode": "all"},
        ),
    ]
    assignments = [SimpleNamespace(agent_id=analyst_id, name="Nightly triage", trigger_type="schedule", enabled=False)]
    fleet = _fleet(orchestrator_agent_id=lead_id)
    context = gen.draft_context(fleet, rows, agents, assignments)
    lead, analyst = context["members"]
    assert lead["orchestrator"] and lead["handovers"] == ["analyst (delegate)"]
    assert len(lead["persona_excerpt"]) <= gen.PERSONA_EXCERPT_CHARS + 3
    assert analyst["parent"] == "desk-lead"
    assert analyst["conversation_shared"] == "summary"
    assert analyst["collaboration_instruction"] == "Report the verdict."
    assert analyst["organizations"] == {"mode": "all"}
    assert analyst["assignments"] == [{"name": "Nightly triage", "trigger": "schedule", "enabled": False}]
    dumped = json.dumps(context, default=str)
    for raw in (lead_id, analyst_id, lead_row, analyst_row):
        assert str(raw) not in dumped, "no id reaches the model"


# ── #9768 S1: one generator writes the sections and the structured parts ──


def _assignment(**fields) -> SimpleNamespace:
    base = {
        "agent_id": None,
        "name": "Weekly review",
        "trigger_type": "schedule",
        "trigger_config": {"cron": "0 8 * * 1", "timezone": "Europe/Paris"},
        "enabled": True,
        "run_as_user_id": None,
        "run_as_all_users": False,
        "human_in_the_loop": None,
    }
    return SimpleNamespace(**{**base, **fields})


def test_a_cron_reads_in_plain_words_and_an_unusual_one_is_named_as_is() -> None:
    assert gen.describe_cron("0 8 * * 1", "Europe/Paris") == "Every Monday at 08:00 (Europe/Paris)"
    assert gen.describe_cron("30 6 * * *") == "Every day at 06:30 (UTC)"
    assert gen.describe_cron("0 9 * * 1-5") == "Every weekday at 09:00 (UTC)"
    assert gen.describe_cron("*/15 * * * *") == "Every 15 minutes"
    assert gen.describe_cron("0 */4 * * *") == "Every 4 hours"
    assert gen.describe_cron("0 7 1 * *") == "Every month on day 1 at 07:00 (UTC)"
    assert gen.describe_cron("5 4 1-7 * 2") == "Cron 5 4 1-7 * 2 (UTC)"
    assert gen.describe_cron(None) == ""


def test_a_schedule_quota_is_computed_from_its_cron_and_labelled_an_estimate() -> None:
    daily = gen.schedule_quota(_assignment(trigger_config={"cron": "0 6 * * *"}))
    assert daily is not None and daily["runs_max"] == gen.QUOTA_DAYS
    assert daily["basis"].startswith("Estimate:")
    off = gen.schedule_quota(_assignment(enabled=False, trigger_config={"cron": "0 6 * * *"}))
    assert off is not None and off["runs_min"] == 0 and off["runs_max"] == gen.QUOTA_DAYS
    assert gen.schedule_quota(_assignment(trigger_type="integration_event", trigger_config={})) is None
    assert gen.schedule_quota(_assignment(trigger_config={"cron": "nope"})) is None


def test_an_automation_reads_its_trigger_event_and_filter_never_an_id_or_secret() -> None:
    user = uuid.uuid4()
    event = _assignment(
        name="New report",
        trigger_type="integration_event",
        trigger_config={
            "provider": "opencti",
            "event": "report_created",
            "filter": {"label": "apt"},
            "secret": "s3cr3t",
        },
        run_as_user_id=user,
    )
    facts = gen.automation_facts(event, "Analyst")
    assert facts == {
        "name": "New report",
        "member": "Analyst",
        "trigger": "Event",
        "schedule": "",
        "event": "opencti report_created where label = apt",
        "enabled": True,
        "run_as": "a named user",
    }
    dumped = json.dumps(facts)
    assert str(user) not in dumped and "s3cr3t" not in dumped
    weekly = gen.automation_facts(_assignment(), "Lead")
    assert weekly["trigger"] == "Schedule" and weekly["schedule"] == "Every Monday at 08:00 (Europe/Paris)"


def test_the_context_carries_automations_tools_and_approvals_by_name() -> None:
    lead_id, mcp_id, skill_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    lead = SimpleNamespace(
        id=lead_id,
        name="Desk Lead",
        slug="desk-lead",
        description="",
        persona="",
        handover_agent_ids=[],
        handover_types={},
        integration_bindings={"opencti": [str(uuid.uuid4())]},
        integration_template_bindings=["openaev"],
        mcp_server_ids=[str(mcp_id)],
        builtin_tool_ids=["coordination", "files"],
        skill_ids=[str(skill_id)],
        human_in_the_loop=True,
        approve_tool_calls=None,
    )
    row = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=lead_id,
        target_fleet_id=None,
        role="member",
        role_label="Lead",
        parent_member_id=None,
        is_exposed=True,
        context_inclusion=None,
        collaboration_instruction=None,
        organization_scope=None,
    )
    context = gen.draft_context(
        _fleet(orchestrator_agent_id=lead_id),
        [row],
        {lead_id: lead},
        [_assignment(agent_id=lead_id)],
        mcp_names={str(mcp_id): "Ticketing MCP"},
        skill_names={str(skill_id): "Triage playbook"},
    )
    assert context["tools"] == [
        {
            "member": "Desk Lead",
            "integrations": ["openaev", "opencti"],
            "mcp_servers": ["Ticketing MCP"],
            "tool_groups": ["coordination", "files"],
            "skills": ["Triage playbook"],
        }
    ]
    assert context["automations"][0]["member"] == "Desk Lead"
    assert context["schedule_quota"][0]["trigger"] == "Weekly review"
    assert context["approvals"] == {"Desk Lead": ["asks a person before acting (human in the loop)"]}
    dumped = json.dumps(context, default=str)
    for raw in (lead_id, mcp_id, skill_id):
        assert str(raw) not in dumped, "no id reaches the model"


def test_structured_keeps_the_valid_items_drops_the_wrong_ones_and_puts_the_computed_facts_on_top() -> None:
    context = {
        "automations": [gen.automation_facts(_assignment(), "Lead")],
        "schedule_quota": [gen.schedule_quota(_assignment())],
        "tools": [{"member": "Lead", "integrations": ["opencti"], "mcp_servers": [], "tool_groups": [], "skills": []}],
    }
    model_part = {
        "purpose_line": "Turns reports into reviewed rules.",
        "steps": [{"title": "Triage"}, {"title": ""}],
        "gates": [{"action": "Approve", "module": "mars"}],
        "prerequisites": [{"check": "integration:opencti", "label": "OpenCTI"}, {"check": "made-up", "label": "x"}],
        "quota": [
            {"trigger": "Weekly review", "runs_min": 1, "runs_max": 999},
            {"trigger": "New report", "basis": "Estimate: one per report", "runs_min": 5, "runs_max": 2},
            {"trigger": "New case", "basis": "Estimate: one per case", "runs_min": 2, "runs_max": 9},
        ],
        "automations": [{"name": "invented", "trigger": "Schedule"}],
        "unknown": 1,
    }
    notes: list[str] = []
    parts = gen.build_structured(model_part, context, notes)
    assert parts is not None
    assert parts["purpose_line"] == "Turns reports into reviewed rules."
    assert parts["steps"] == [{"title": "Triage"}]
    assert "gates" not in parts, "an unknown module is dropped, never guessed"
    assert [p["check"] for p in parts["prerequisites"]] == ["integration:opencti", "manual"], (
        "an unknown check keeps its line as a manual one (#10094)"
    )
    quota = {q["trigger"]: q for q in parts["quota"]}
    assert set(quota) == {"New case", "New report", "Weekly review"}
    assert (quota["New report"]["runs_min"], quota["New report"]["runs_max"]) == (2, 5), (
        "runs_max below runs_min is put in order, never dropped (#10094)"
    )
    assert quota["Weekly review"]["runs_max"] == 5, "a schedule is computed from its cron, never the model's figure"
    assert parts["automations"][0]["name"] == "Weekly review", "automations come from the configuration"
    assert parts["integrations"][0]["integrations"] == ["opencti"]
    assert sum(n["code"] == "part_not_model" for n in notes) == 2, "'automations' and 'unknown'"
    assert {"code": "part_item_left_out", "params": {"block": "gates"}} in notes


async def test_a_whole_draft_asks_for_and_returns_the_structured_parts(no_database) -> None:
    answer = {
        "sections": {k: f"Text of {k}." for k in DOC_SECTIONS},
        "structured": {"purpose_line": "Triages alerts.", "steps": [{"title": "Triage", "gate": True}]},
    }
    model = _Model(json.dumps(answer))
    draft = await gen.draft_fleet_documentation(object(), uuid.uuid4(), _fleet(), llm=model)
    assert list(draft.sections) == list(DOC_SECTIONS)
    assert draft.structured == {"purpose_line": "Triages alerts.", "steps": [{"title": "Triage", "gate": True}]}
    assert "Structured parts to write" in model.messages[0][1].content


async def test_a_one_section_regenerate_asks_for_no_structured_part(no_database) -> None:
    model = _Model(json.dumps({"flow": "New flow."}))
    draft = await gen.draft_fleet_documentation(object(), uuid.uuid4(), _fleet(), section="flow", llm=model)
    assert draft.structured is None
    assert "Structured parts to write" not in model.messages[0][1].content


def test_the_regenerate_route_is_ai_gated_and_no_hand_edit_route_is_left() -> None:
    from app.api.deps import require_agentic_ai
    from app.api.v1.fleets import router

    route = next(r for r in router.routes if getattr(r, "path", "").endswith("/documentation/regenerate"))
    assert "POST" in route.methods
    assert any(d.call is require_agentic_ai for d in route.dependant.dependencies)
    # #9768 S2: the documentation is never typed by hand, so no PUT and no draft-only route.
    doc_routes = [r for r in router.routes if "/documentation" in getattr(r, "path", "")]
    assert not any("PUT" in r.methods for r in doc_routes)
    assert not any(r.path.endswith("/documentation/draft") for r in doc_routes)


class _ExpiringFleet:
    """A fleet whose attributes cannot be read once the session rolled back (SQLAlchemy expiry).

    Reading an expired attribute of an async session's object lazy-loads it
    outside the greenlet: MissingGreenlet in production, a RuntimeError here.
    """

    is_builtin = False

    def __init__(self) -> None:
        self._id = uuid.uuid4()
        self.expired = False

    @property
    def id(self) -> uuid.UUID:
        if self.expired:
            raise RuntimeError("MissingGreenlet: expired attribute read after rollback")
        return self._id


async def test_a_refused_regenerate_answers_422_and_clears_the_marker_after_the_rollback(monkeypatch) -> None:
    """#10837 UX round 1 B1: the rollback expired ``fleet``; ``finally`` read ``fleet.id`` and answered 500."""
    from fastapi import HTTPException

    from app.api.v1 import fleets as fleets_api
    from app.api.v1.schemas.fleet import FleetDocumentationRegenerateRequest

    fleet = _ExpiringFleet()
    released: list[tuple[uuid.UUID, str | None]] = []

    class _Db:
        async def rollback(self) -> None:
            fleet.expired = True

        async def commit(self) -> None:  # pragma: no cover - the draft is refused first
            raise AssertionError("nothing is committed on a refused draft")

    async def _manageable(_db, _user, _fleet_id):
        return fleet

    async def _claim(fleet_id) -> bool:
        return True

    async def _release(fleet_id, failure=None) -> None:
        released.append((fleet_id, failure))

    async def _refused(*_a, **_kw):
        raise gen.FleetDocDraftError("The answer was cut.")

    monkeypatch.setattr(fleets_api, "_manageable_fleet", _manageable)
    monkeypatch.setattr(fleets_api, "claim_generation", _claim)
    monkeypatch.setattr(fleets_api, "release_generation", _release)
    monkeypatch.setattr(gen, "regenerate_fleet_documentation", _refused)

    with pytest.raises(HTTPException) as caught:
        await fleets_api.regenerate_fleet_documentation_endpoint(
            fleet_id=str(fleet._id),
            body=FleetDocumentationRegenerateRequest(),
            request=SimpleNamespace(),
            user=SimpleNamespace(id=uuid.uuid4()),
            db=_Db(),
            _gate=None,
        )
    assert caught.value.status_code == 422
    assert released == [(fleet._id, "The answer was cut.")], "the marker is cleared and the failure kept readable"
