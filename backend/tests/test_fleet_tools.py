"""The ``fleets`` built-in suite, without a database.

What is pinned here: the suite is registered where every built-in is (the
catalog, the agent builder's valid ids, the General Assistant's suites), its
seven tools are the ones the program names, and the permission gate is the
agent builder's: no account, no fleet; a refusal of the loader or of the fleet
service is handed back and the session rolled back, never committed. The
PostgreSQL half (own, shared, admin, refused) is ``test_fleet_tools_postgres.py``.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.services.builtin_tools import fleets as fleet_tools

TOOLS = {
    "list_fleets",
    "get_fleet",
    "create_fleet",
    "update_fleet",
    "delete_fleet",
    "set_fleet_automations",
    "set_fleet_members",
    "set_fleet_exposure",
    "share_fleet_workspace",
    "set_fleet_member_parent",
    "export_fleet_spec",
    "apply_fleet_spec",
    "move_fleet_member",
    "generate_fleet_spec",
    "regenerate_fleet_documentation",
    "get_fleet_prerequisites",
    "get_fleet_setup",
    "get_fleet_health",
    "dismiss_fleet_health_findings",
    "confirm_fleet_prerequisite",
    "list_fleet_doc_findings",
}


class _Session:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


@pytest.fixture
def session(monkeypatch) -> _Session:
    db = _Session()

    @asynccontextmanager
    async def _factory():
        yield db

    monkeypatch.setattr(fleet_tools.database, "async_session", _factory)
    return db


def _caller(monkeypatch, *, is_admin: bool = False) -> SimpleNamespace:
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=is_admin)

    async def _load(_db, _uid):
        return user

    monkeypatch.setattr(fleet_tools._builder, "_load_caller_user_for_builder", _load)
    return user


async def _call(user_id: str, tool: str, **args) -> str:
    _defs, executor = fleet_tools.build_fleet_tools(user_id)
    return await executor.handler(tool)(**args)


def test_the_suite_has_every_tool():
    definitions, _executor = fleet_tools.build_fleet_tools("")
    assert {d.name for d in definitions} == TOOLS
    # The fleet functions belong to the platform group.
    assert {d.source for d in definitions} == {"builtin:platform"}


def test_the_suite_is_registered_where_every_builtin_is():
    from app.models.general_assistant_config import DEFAULT_GA_BUILTIN_TOOL_IDS, GENERAL_ASSISTANT_BUILTIN_TOOL_IDS
    from app.services.builtin_tool_aliases import alias_group, alias_scoped_functions
    from app.services.builtin_tool_catalog import build_builtin_tool_catalog
    from app.services.builtin_tools.agent_builder import _VALID_BUILTIN_TOOL_IDS

    # A sub-area of the platform group; ``fleets`` is its alias.
    catalog = build_builtin_tool_catalog(include_disabled=True)
    assert {d["name"] for d in catalog["platform"]} >= TOOLS
    assert alias_group("fleets") == "platform"
    assert set(alias_scoped_functions("fleets")) == TOOLS
    assert "fleets" in _VALID_BUILTIN_TOOL_IDS
    assert "fleets" in GENERAL_ASSISTANT_BUILTIN_TOOL_IDS
    assert "fleets" in DEFAULT_GA_BUILTIN_TOOL_IDS


def test_the_migration_adds_the_suite_once():
    import importlib.util
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "alembic/versions/f7030fleetsga_add_fleets_to_ga_tools.py"
    spec = importlib.util.spec_from_file_location("f7030fleetsga", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    assert json.loads(module.with_added_tools('["web_fetch"]')) == ["web_fetch", "fleets"]
    assert module.with_added_tools('["fleets"]') is None
    assert module.with_added_tools("not json") is None
    assert json.loads(module.with_added_tools(None)) == ["fleets"]


def test_the_fleet_link_is_the_fleet_page(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "frontend_url", "https://copilot.example/")
    assert fleet_tools.fleet_link("abc") == "https://copilot.example/fleets/abc"


@pytest.mark.asyncio
async def test_an_anonymous_caller_gets_nothing(session):
    for tool, args in (("list_fleets", {}), ("create_fleet", {"name": "F"}), ("delete_fleet", {"fleet_id": "x"})):
        assert await _call("", tool, **args) == fleet_tools._ANONYMOUS_REFUSAL
    assert session.commits == 0


@pytest.mark.asyncio
async def test_a_non_admin_cannot_create_a_company_managed_fleet(monkeypatch, session):
    user = _caller(monkeypatch)
    answer = await _call(str(user.id), "create_fleet", name="Company", is_company_managed=True)
    assert answer == "Only admins can create company-managed fleets."
    assert (session.commits, session.rollbacks) == (0, 1)


@pytest.mark.asyncio
async def test_a_fleet_the_caller_may_not_manage_is_refused_and_rolled_back(monkeypatch, session):
    user = _caller(monkeypatch)

    async def _refuse(_db, _model, _uid, entity_uuid, *, action="modify", entity_label="entity", lock=False):
        return None, f"You don't have permission to {action} this {entity_label}."

    monkeypatch.setattr(fleet_tools._builder, "_load_manageable_entity_for_builder", _refuse)
    fleet_id = str(uuid.uuid4())
    assert await _call(str(user.id), "update_fleet", fleet_id=fleet_id, name="X") == (
        "You don't have permission to edit this fleet."
    )
    assert await _call(str(user.id), "delete_fleet", fleet_id=fleet_id) == (
        "You don't have permission to delete this fleet."
    )
    assert await _call(str(user.id), "set_fleet_exposure", fleet_id=fleet_id, agent_ids=[]) == (
        "You don't have permission to edit this fleet."
    )
    assert session.commits == 0
    assert session.rollbacks == 3


@pytest.mark.asyncio
async def test_a_refusal_of_the_fleet_service_is_handed_back(monkeypatch, session):
    from app.services import fleets as fleet_service

    user = _caller(monkeypatch)
    fleet = SimpleNamespace(id=uuid.uuid4(), name="F", slug="f", is_company_managed=False)

    locks: list[bool] = []

    async def _manageable(_db, _model, _uid, _entity_uuid, **kw):
        locks.append(kw.get("lock", False))
        return fleet, None

    async def _refuse(_db, _fleet, _ids):
        raise fleet_service.FleetActionRefusedError(400, "'x' is not a home member of this fleet.")

    monkeypatch.setattr(fleet_tools._builder, "_load_manageable_entity_for_builder", _manageable)
    monkeypatch.setattr(fleet_service, "set_exposed_agents", _refuse)
    answer = await _call(str(user.id), "set_fleet_exposure", fleet_id=str(fleet.id), agent_ids=[str(uuid.uuid4())])
    assert answer == "'x' is not a home member of this fleet. Nothing was saved."
    assert (session.commits, session.rollbacks) == (0, 1)
    # A write holds the fleet's row until the commit (concurrent replacements serialize).
    assert locks == [True]


@pytest.mark.asyncio
async def test_ids_are_checked_before_anything_is_read(monkeypatch, session):
    user = _caller(monkeypatch)
    assert await _call(str(user.id), "get_fleet", fleet_id="nope") == (
        "fleet_id is required: pass the id `list_fleets` shows."
    )
    answer = await _call(str(user.id), "create_fleet", name="F", autonomy_level="wild")
    assert answer == ("Unknown autonomy level 'wild': one of full_gates, medium, low, autonomous. Nothing was created.")
    answer = await _call(str(user.id), "create_fleet", name="F", member_agent_ids=["not-an-id"])
    assert answer == "member_agent_ids: `not-an-id` is not an id."
    answer = await _call(str(user.id), "create_fleet", name="F", connected=[{"agent_id": str(uuid.uuid4()), "context_inclusion": "all"}])
    assert answer == "context_inclusion is one of none, summary, full."
    member = str(uuid.uuid4())
    answer = await _call(
        str(user.id), "create_fleet", name="F", member_agent_ids=[{"agent_id": member, "role": "lead"}]
    )
    assert answer == (
        "member_agent_ids: unknown field(s) role; a member takes agent_id, role_label, parent_agent_id. Nothing was created."
    )
    answer = await _call(
        str(user.id), "create_fleet", name="F", member_agent_ids=[{"agent_id": member, "parent_agent_id": "boss"}]
    )
    assert answer == "member_agent_ids parent_agent_id: `boss` is not an id. Nothing was created."
    answer = await _call(
        str(user.id), "create_fleet", name="F", member_agent_ids=[{"agent_id": member, "parent_agent_id": member}]
    )
    assert answer == f"member_agent_ids: agent `{member}` cannot report to itself. Nothing was created."
    answer = await _call(str(user.id), "create_fleet", name="F", member_agent_ids=[member, {"agent_id": member}])
    assert answer == f"member_agent_ids: agent `{member}` is listed twice. Nothing was created."
    answer = await _call(
        str(user.id), "create_fleet", name="F", member_agent_ids=[{"agent_id": member, "role_label": "x" * 500}]
    )
    assert answer.startswith("member_agent_ids role_label is 500 characters, at most ")
    assert session.commits == 0
    fleet_id = str(uuid.uuid4())
    answer = await _call(str(user.id), "set_fleet_member_parent", fleet_id=fleet_id, agent_id="x")
    assert answer == "agent_id is required: pass the id `list_fleets` shows."
    answer = await _call(
        str(user.id), "set_fleet_member_parent", fleet_id=fleet_id, agent_id=str(uuid.uuid4()), parent_agent_id="boss"
    )
    assert answer == "parent_agent_id: `boss` is not an id (null puts the member at top level)."
    answer = await _call(
        str(user.id), "move_fleet_member", fleet_id=fleet_id, agent_id=str(uuid.uuid4()), target_fleet_id="x"
    )
    assert answer == "target_fleet_id is required: pass the id `list_fleets` shows."
    answer = await _call(
        str(user.id), "move_fleet_member", fleet_id=fleet_id, agent_id=str(uuid.uuid4()), target_fleet_id=fleet_id
    )
    assert answer == "target_fleet_id is the fleet the agent is already in. Nothing was saved."
    assert session.commits == 0


def _big_spec(members: int = 7, persona_chars: int = 250_000) -> dict:
    """A spec shaped like an export: each member carrying a heavy agent bundle."""
    return {
        "apiVersion": "xtm-one/fleet/v1",
        "kind": "Fleet",
        "metadata": {"name": "Office", "slug": "office"},
        "spec": {
            "orchestrator": "m0",
            "members": [
                {
                    "key": f"m{i}",
                    "role_label": f"Role {i}",
                    "parent": None if i == 0 else "m0",
                    "exposed": i == 1,
                    "handovers": [{"to": "m1", "type": "delegate"}] if i == 0 else [],
                    "agent": {"version": 3, "agent": {"slug": f"m{i}", "persona": "x" * persona_chars}},
                }
                for i in range(members)
            ],
            "connected": [],
        },
    }


def test_the_structural_spec_keeps_the_structure_and_drops_the_bundles():
    from app.services.fleet_spec import structural_spec, validate_fleet_spec

    spec = _big_spec()
    spec["spec"]["members"][2]["agent"] = {"ref": {"slug": "m2"}}
    out = structural_spec(spec)
    assert [m["agent"] for m in out["spec"]["members"]] == [{"ref": {"slug": f"m{i}"}} for i in range(7)]
    for before, after in zip(spec["spec"]["members"], out["spec"]["members"], strict=True):
        assert {k: v for k, v in before.items() if k != "agent"} == {k: v for k, v in after.items() if k != "agent"}
    assert out["metadata"] == spec["metadata"] and out["spec"]["orchestrator"] == "m0"
    # The input is untouched and the output is a valid spec.
    assert "persona" in spec["spec"]["members"][0]["agent"]["agent"]
    validate_fleet_spec(out)


def _export_stubs(monkeypatch, spec: dict) -> SimpleNamespace:
    from app.services import fleet_spec as fleet_spec_service

    fleet = SimpleNamespace(id=uuid.uuid4(), name="Office", slug="office")

    async def _export(_db, _fleet, _user, _groups, **_kw):
        return spec

    async def _groups(_db, _uid):
        return []

    monkeypatch.setattr(fleet_spec_service, "export_fleet_spec", _export)
    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", _groups)
    return fleet


@pytest.mark.asyncio
async def test_an_export_without_bundles_answers_the_structure_and_names_the_file(monkeypatch, session):
    user = _caller(monkeypatch)
    spec = _big_spec()
    fleet = _export_stubs(monkeypatch, spec)
    session.execute = _scalar(fleet)  # type: ignore[attr-defined]
    stored: dict = {}

    async def _persist(**kw):
        stored.update(kw)
        return SimpleNamespace(file_id="f-123")

    monkeypatch.setattr(fleet_tools, "persist_tool_artifact", _persist)
    _defs, executor = fleet_tools.build_fleet_tools(str(user.id), conversation_id=str(uuid.uuid4()))
    answer = await executor.handler("export_fleet_spec")(fleet_id=str(fleet.id), bundles=False)
    assert len(answer) < 20_000, len(answer)
    assert "[[FILE:f-123]]" in answer and 'apply_fleet_spec(file_id="f-123")' in answer
    assert "persona" not in answer
    import json

    assert json.loads(stored["raw_bytes"]) == spec
    assert stored["source"] == "agent_working" and stored["filename"] == "fleet-spec-office.json"
    assert session.commits == 0


@pytest.mark.asyncio
async def test_a_full_export_too_long_to_answer_inline_falls_back_to_the_file(monkeypatch, session):
    user = _caller(monkeypatch)
    fleet = _export_stubs(monkeypatch, _big_spec())
    session.execute = _scalar(fleet)  # type: ignore[attr-defined]

    async def _persist(**_kw):
        return SimpleNamespace(file_id="f-9")

    monkeypatch.setattr(fleet_tools, "persist_tool_artifact", _persist)
    _defs, executor = fleet_tools.build_fleet_tools(str(user.id), assignment_run_id=str(uuid.uuid4()))
    answer = await executor.handler("export_fleet_spec")(fleet_id=str(fleet.id))
    assert "too long to answer inline" in answer and "[[FILE:f-9]]" in answer
    assert len(answer) < 20_000


@pytest.mark.asyncio
async def test_a_small_export_stays_inline(monkeypatch, session):
    user = _caller(monkeypatch)
    fleet = _export_stubs(monkeypatch, _big_spec(members=2, persona_chars=10))
    session.execute = _scalar(fleet)  # type: ignore[attr-defined]
    answer = await _call(str(user.id), "export_fleet_spec", fleet_id=str(fleet.id))
    assert "Pass it whole to apply_fleet_spec" in answer and '"persona": "xxxxxxxxxx"' in answer


def _scalar(value):
    class _Result:
        def scalar_one_or_none(self):
            return value

    async def _execute(_stmt):
        return _Result()

    return _execute


@pytest.mark.asyncio
async def test_apply_reads_the_spec_from_a_file_of_the_conversation(monkeypatch, session):
    import json

    from app.llm.base import FileRef
    from app.services import fleet_spec as fleet_spec_service
    from app.tools.base import FileContext

    user = _caller(monkeypatch)
    spec = _big_spec(members=2, persona_chars=10)
    ctx = FileContext()
    ctx.add(FileRef.from_bytes(json.dumps(spec).encode(), "fleet-spec-office.json", "application/json", file_id="f-1"))
    seen: list = []

    async def _groups(_db, _uid):
        return []

    async def _apply(_db, _user, parsed, _groups, **kw):
        seen.append((parsed, kw["dry_run"]))
        return SimpleNamespace(
            dry_run=True, applied=False, fleet_id=None, fleet_slug="office",
            changes=[{"action": "unchanged", "object": "member", "name": "m0", "detail": ""}],
            unresolved=[], warnings=[],
        )

    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", _groups)
    monkeypatch.setattr(fleet_spec_service, "apply_fleet_spec", _apply)
    _defs, executor = fleet_tools.build_fleet_tools(str(user.id))
    answer = await executor.handler("apply_fleet_spec")(file_id="f-1", file_context=ctx)
    assert answer.startswith("Dry run: nothing was written."), answer
    assert "unchanged member m0" in answer
    assert seen == [(spec, True)]


@pytest.mark.asyncio
async def test_apply_wants_one_source_and_an_existing_file(monkeypatch, session):
    from app.tools.base import FileContext

    user = _caller(monkeypatch)
    assert await _call(str(user.id), "apply_fleet_spec") == (
        "Pass spec (an object or JSON / YAML text) or file_id (a file holding it)."
    )
    assert await _call(str(user.id), "apply_fleet_spec", spec="{}", file_id="f-1") == (
        "Pass spec or file_id, not both."
    )
    _defs, executor = fleet_tools.build_fleet_tools(str(user.id))
    answer = await executor.handler("apply_fleet_spec")(file_id="f-missing", file_context=FileContext())
    assert "f-missing" in answer
    assert session.commits == 0


def test_both_spec_tools_document_the_file_round_trip():
    definitions, _executor = fleet_tools.build_fleet_tools("")
    tools = {d.name: d for d in definitions}
    assert "bundles" in tools["export_fleet_spec"].input_schema["properties"]
    assert "file_id" in tools["export_fleet_spec"].description
    assert "file_id" in tools["apply_fleet_spec"].input_schema["properties"]
    assert tools["apply_fleet_spec"].input_schema["required"] == []


def _doc_fleet(*, builtin: bool = False, fingerprint: str | None = "old") -> SimpleNamespace:
    documentation = {"sections": {"purpose": "P"}, "version": 3, "fingerprint": fingerprint} if fingerprint else None
    return SimpleNamespace(
        id=uuid.uuid4(), name="Desk", slug="desk", is_builtin=builtin, is_company_managed=False,
        documentation=documentation,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("builtin", [False, True])
async def test_regenerate_saves_the_next_version_on_a_custom_fleet_and_refuses_a_builtin_one(
    monkeypatch, session, builtin
):
    from app.services import fleet_doc_generation as gen

    user = _caller(monkeypatch)
    fleet = _doc_fleet(builtin=builtin)
    calls: list = []

    async def _manageable(_db, _model, _uid, _entity_uuid, **_kw):
        return fleet, None

    async def _regenerate(_db, user_id, target, *, section=None, **_kw):
        calls.append((user_id, target, section))
        target.documentation = {**target.documentation, "version": 4}
        return gen.FleetDocDraft(sections={"purpose": "New"}, notes=["quota: estimate"])

    async def _audit(*_a, **_kw):
        return None

    monkeypatch.setattr(fleet_tools._builder, "_load_manageable_entity_for_builder", _manageable)
    monkeypatch.setattr(gen, "regenerate_fleet_documentation", _regenerate)
    monkeypatch.setattr("app.services.audit.log_audit", _audit)
    answer = await _call(str(user.id), "regenerate_fleet_documentation", fleet_id=str(fleet.id), section="purpose")
    if builtin:
        # #1956: the seed writes a built-in fleet's documentation; nothing is generated.
        assert "is built in" in answer and "Nothing was generated." in answer, answer
        assert calls == []
        return
    assert answer.startswith("Fleet **Desk**: section `purpose` regenerated, saved as version 4."), answer
    assert "quota: estimate" in answer
    assert "local version" not in answer
    assert calls == [(user.id, fleet, "purpose")]
    # The version commits before the running marker is cleared (#10817), then the tool runner's own commit.
    assert (session.commits, session.rollbacks) == (2, 0)


@pytest.mark.asyncio
async def test_get_fleet_prerequisites_lists_the_live_checklist_without_writing(monkeypatch, session):
    # #10258 S3: the About page's derived checklist, read-only, no model call.
    from app.services import fleet_about

    user = _caller(monkeypatch)
    fleet = _doc_fleet()
    fleet.documentation = {**fleet.documentation, "structured": {"prerequisites": [{"label": "Doc"}]}}
    seen: list = []

    class _Found:
        def scalar_one_or_none(self):
            return fleet

    async def _execute(_query):
        return _Found()

    async def _groups(_db, _uid):
        return []

    async def _live(_db, caller, target, documented, **_kw):
        seen.append((caller, target, documented))
        return [
            fleet_about.LivePrerequisite(
                check="binding:1",
                kind="integration",
                label="GitHub",
                state=fleet_about.MET,
                reason="",
                member_ids=["a1"],
                fix_route="/integrations/1",
                provider="github",
            ),
            fleet_about.LivePrerequisite(
                check="agent_account:2",
                kind="agent_account",
                label="Mailbox",
                state="not_met",
                reason="agent account paused",
                member_ids=["a2"],
                fix_route="/profile/agent-accounts/2",
            ),
            fleet_about.LivePrerequisite(
                check="manual:0",
                kind="manual",
                label="Doc",
                state="to_confirm",
                reason="",
                member_ids=[],
                fix_route="",
            ),
        ]

    session.execute = _execute
    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", _groups)
    monkeypatch.setattr(fleet_about, "live_prerequisites", _live)
    answer = await _call(str(user.id), "get_fleet_prerequisites", fleet_id=str(fleet.id))
    assert answer.startswith("Fleet **Desk**: prerequisites 1/2 met."), answer
    assert "[not_met] Mailbox; agent account paused; members a2; fix at /profile/agent-accounts/2" in answer
    assert "fix at /integrations/1" not in answer
    assert "[to_confirm] Doc" in answer
    assert seen == [(user, fleet, [{"label": "Doc"}])]
    assert session.commits == 0


@pytest.mark.asyncio
async def test_a_second_generation_is_refused_without_waiting_on_the_fleet_row(monkeypatch, session):
    # #429: the tool read the fleet FOR UPDATE and held the row across the model call, so a
    # second call waited on it until the statement timeout and answered the generic failure.
    from app.services import fleet_doc_generation as gen
    from app.services import fleet_doc_on_create as on_create

    user = _caller(monkeypatch)
    fleet = _doc_fleet()
    locks: list = []

    async def _manageable(_db, _model, _uid, _entity_uuid, **kw):
        locks.append(kw.get("lock"))
        return fleet, None

    async def _taken(_fleet_id):
        return False

    async def _state(_fleet_id):
        return on_create.DocGeneration(generating=True, started_at="2026-11-19T10:38:00+00:00")

    async def _never(*_a, **_kw):
        raise AssertionError("no second generation starts")

    monkeypatch.setattr(fleet_tools._builder, "_load_manageable_entity_for_builder", _manageable)
    monkeypatch.setattr(on_create, "claim_generation", _taken)
    monkeypatch.setattr(on_create, "generation_state", _state)
    monkeypatch.setattr(gen, "regenerate_fleet_documentation", _never)
    answer = await _call(str(user.id), "regenerate_fleet_documentation", fleet_id=str(fleet.id))
    assert answer.startswith("A generation of the documentation of fleet Desk is already running."), answer
    assert "documentation_generating: true (started 2026-11-19T10:38:00+00:00 UTC" in answer
    assert locks == [False]
    assert (session.commits, session.rollbacks) == (0, 1)


@pytest.mark.asyncio
async def test_a_refused_generation_saves_nothing(monkeypatch, session):
    from app.services import fleet_doc_generation as gen

    user = _caller(monkeypatch)
    fleet = _doc_fleet()

    async def _manageable(_db, _model, _uid, _entity_uuid, **_kw):
        return fleet, None

    async def _cut(*_a, **_kw):
        raise gen.FleetDocDraftError("The answer was cut.")

    monkeypatch.setattr(fleet_tools._builder, "_load_manageable_entity_for_builder", _manageable)
    monkeypatch.setattr(gen, "regenerate_fleet_documentation", _cut)
    answer = await _call(str(user.id), "regenerate_fleet_documentation", fleet_id=str(fleet.id))
    assert answer == "No documentation was generated: The answer was cut. Nothing was saved."
    assert (session.commits, session.rollbacks) == (0, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(("stored", "now", "hinted"), [("old", "new", True), ("same", "same", False), (None, "x", False)])
async def test_a_fleet_answer_names_the_tool_when_the_documentation_is_out_of_date(monkeypatch, stored, now, hinted):
    from app.services import fleet_docs

    fleet = _doc_fleet(fingerprint=stored)

    async def _current(_db, _fleet):
        return now

    monkeypatch.setattr(fleet_docs, "current_fingerprint", _current)
    # The hint is the last line every write answer (``_describe``) and an applied spec carry.
    hint = await fleet_tools.documentation_hint(object(), fleet)  # type: ignore[arg-type]
    assert bool(hint) is hinted
    if hinted:
        assert "documentation_out_of_date: true" in hint
        assert f'regenerate_fleet_documentation(fleet_id="{fleet.id}")' in hint


def _manageable_fleet_stub(monkeypatch, **attrs) -> SimpleNamespace:
    fleet = SimpleNamespace(
        id=uuid.uuid4(), name="F", slug="f", is_company_managed=False, is_builtin=False, **attrs
    )

    async def _manageable(_db, _model, _uid, _entity_uuid, **_kw):
        return fleet, None

    monkeypatch.setattr(fleet_tools._builder, "_load_manageable_entity_for_builder", _manageable)
    return fleet


async def _call_in(user_id: str, tool: str, *, conversation_id=None, assignment_run_id=None, **args) -> str:
    _defs, executor = fleet_tools.build_fleet_tools(
        user_id, conversation_id=conversation_id, assignment_run_id=assignment_run_id
    )
    return await executor.handler(tool)(**args)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("is_admin", "scope", "why"),
    [
        # a non-administrator, even in a chat and confirmed
        (False, {"conversation_id": "c-1"}, fleet_tools._RAISE_NOT_ADMIN),
        # an unattended run, even an administrator's
        (True, {"assignment_run_id": "r-1", "conversation_id": "c-1"}, fleet_tools._RAISE_NOT_IN_CHAT),
        (True, {}, fleet_tools._RAISE_NO_CHAT),  # no chat at all
    ],
)
async def test_an_agent_raise_is_refused_outside_an_administrators_chat(
    monkeypatch: pytest.MonkeyPatch, session: _Session, is_admin: bool, scope: dict[str, str], why: str
) -> None:
    user = _caller(monkeypatch, is_admin=is_admin)
    fleet = _manageable_fleet_stub(monkeypatch, autonomy_level="low")
    answer = await _call_in(
        str(user.id), "update_fleet", fleet_id=str(fleet.id), autonomy_level="autonomous", confirm=True, **scope
    )
    # The refusal keeps the wording the fleet chat card matches, then says why, what to do and the level kept.
    assert answer == f"{fleet_tools._AUTONOMY_RAISE_REFUSAL} {why} The fleet stays at Low (low)."
    assert answer.startswith("An agent can only lower a fleet's autonomy level.")
    assert fleet.autonomy_level == "low"
    assert (session.commits, session.rollbacks) == (0, 1)
    answer = await _call(str(user.id), "update_fleet", fleet_id=str(fleet.id), autonomy_level="everything")
    assert "Unknown autonomy level 'everything'" in answer


@pytest.mark.asyncio
async def test_an_administrators_chat_agent_raises_the_level_only_once_confirmed_and_audited(monkeypatch, session):
    # #10439: the administrator asks in chat; the first call only names the change.
    from app.services import fleets as fleet_service

    user = _caller(monkeypatch, is_admin=True)
    user.full_name = "Ada Admin"
    fleet = _manageable_fleet_stub(
        monkeypatch,
        autonomy_level="full_gates",
        description="",
        is_archived=False,
        workspace_id=uuid.uuid4(),
        orchestrator_agent_id=None,
        organization_scope=None,
    )
    audits: list[dict] = []

    async def _log(_db, **kw):
        audits.append(kw)

    async def _none(*_a, **_kw):
        return None

    async def _no_members(*_a, **_kw):
        return []

    async def _no_hint(*_a, **_kw):
        return ""

    session.flush = _none  # type: ignore[attr-defined]
    session.get = _none  # type: ignore[attr-defined]
    monkeypatch.setattr("app.services.audit.log_audit", _log)
    monkeypatch.setattr(fleet_service, "fleet_members", _no_members)
    monkeypatch.setattr(fleet_service, "exposed_agent_ids", lambda *_a, **_kw: set())
    monkeypatch.setattr(fleet_tools, "documentation_hint", _no_hint)

    answer = await _call_in(
        str(user.id), "update_fleet", conversation_id="c-1", fleet_id=str(fleet.id), autonomy_level="low"
    )
    assert answer.startswith("Not applied. Raising the autonomy level of fleet **F** from full_gates to low")
    assert "Why: confirm=true is missing." in answer
    assert answer.endswith("the fleet stays at Full human gates (full_gates).")
    assert fleet.autonomy_level == "full_gates" and audits == []
    assert (session.commits, session.rollbacks) == (0, 1)

    answer = await _call_in(
        str(user.id), "update_fleet", conversation_id="c-1", fleet_id=str(fleet.id), autonomy_level="low", confirm=True
    )
    assert answer.startswith("Fleet **F** updated. Autonomy level applied: Low (low)."), answer
    assert "  • Autonomy level: Low (low)" in answer
    assert fleet.autonomy_level == "low"
    assert (session.commits, session.rollbacks) == (1, 1)
    change = next(a for a in audits if a["action"] == "autonomy_level_change")
    assert change["summary"] == "Raised the autonomy level of fleet 'F' to low, by an agent on Ada Admin's instruction"
    assert change["details"]["from"] == "full_gates" and change["details"]["to"] == "low"
    assert change["details"]["raised_by_agent_on_instruction_of"] == str(user.id)
    assert change["details"]["via"] == "agent"


@pytest.mark.asyncio
async def test_an_update_to_the_current_level_says_it_is_already_there(
    monkeypatch: pytest.MonkeyPatch, session: _Session
) -> None:
    user = _caller(monkeypatch, is_admin=True)
    fleet = _manageable_fleet_stub(monkeypatch, autonomy_level="medium")
    answer = await _call_in(
        str(user.id), "update_fleet", conversation_id="c-1", fleet_id=str(fleet.id), autonomy_level="medium"
    )
    assert answer == "Nothing to change: fleet **F** is already at autonomy level Medium (medium)."
    assert fleet.autonomy_level == "medium"
    assert (session.commits, session.rollbacks) == (0, 1)


@pytest.mark.parametrize(
    ("stored", "shown"),
    [
        ("full_gates", "Full human gates (full_gates)"),
        ("medium", "Medium (medium)"),
        ("low", "Low (low)"),
        ("autonomous", "Full autonomous (autonomous)"),
        (None, "Full human gates (full_gates)"),  # unset reads as the default, fail closed
    ],
)
def test_the_fleet_read_states_the_autonomy_level(stored: str | None, shown: str) -> None:
    # get_fleet and every write answer (``_describe``) state it as "Autonomy level: <shown>"; list_fleets
    # as "autonomy <shown>" (the applied raise test reads it through ``_describe``).
    assert fleet_tools._level_text(stored) == shown


@pytest.mark.parametrize(
    ("position", "announced", "shown"),
    [
        (None, None, "not set"),
        ("bogus", True, "not set"),
        ("medium", False, "Medium (medium), not announced to the members, not enforced by the platform"),
        (
            "full_gates_medium",
            True,
            "between Full human gates and Medium (full_gates_medium), announced to the members, "
            "not enforced by the platform",
        ),
    ],
)
def test_the_fleet_read_states_the_instructions_position(position, announced, shown: str) -> None:
    # get_fleet (``_describe``) states it as "Instructions position: <shown>", so no audit log
    # read is needed to learn where a person set it.
    assert fleet_tools._instructions_text(position, announced) == shown


@pytest.mark.asyncio
async def test_an_agent_still_lowers_the_level_without_confirmation(monkeypatch, session):
    # A non-administrator in an unattended run: lowering stays free, no confirmation asked.
    user = _caller(monkeypatch)
    fleet = _manageable_fleet_stub(monkeypatch, autonomy_level="autonomous")
    answer = await _call_in(
        str(user.id), "update_fleet", assignment_run_id="r-1", fleet_id=str(fleet.id), autonomy_level="medium"
    )
    assert answer != fleet_tools._AUTONOMY_RAISE_REFUSAL
    assert not answer.startswith("Not applied.")
    assert fleet.autonomy_level == "medium"


@pytest.mark.asyncio
async def test_only_an_administrators_agent_sets_the_instructions_position(monkeypatch, session):
    user = _caller(monkeypatch)
    fleet = _manageable_fleet_stub(monkeypatch, instructions_position=None, instructions_position_announced=None)
    answer = await _call(str(user.id), "update_fleet", fleet_id=str(fleet.id), instructions_position="full_gates")
    assert answer == fleet_tools._INSTRUCTIONS_POSITION_ADMIN_ONLY
    assert fleet.instructions_position is None
    assert (session.commits, session.rollbacks) == (0, 1)


@pytest.mark.asyncio
async def test_pause_all_is_only_counted_until_the_user_confirms(monkeypatch, session):
    from app.services import fleet_control

    user = _caller(monkeypatch)
    fleet = _manageable_fleet_stub(monkeypatch)
    paused: list[str] = []

    async def _count(_db, _fleet):
        return 3

    async def _pause(_db, _fleet, **_kw):
        paused.append("x")
        return fleet_control.FleetControlOutcome(changed=3, names=("a", "b", "c"))

    monkeypatch.setattr(fleet_control, "pausable_count", _count)
    monkeypatch.setattr(fleet_control, "pause_fleet", _pause)
    answer = await _call(str(user.id), "set_fleet_automations", fleet_id=str(fleet.id), action="pause")
    assert answer.startswith("Not applied. Pause all would switch off 3 automation(s)")
    assert "confirm=true" in answer and paused == []
    assert await _call(str(user.id), "set_fleet_automations", fleet_id=str(fleet.id), action="stop") == (
        "action is pause, resume, unset or enable."
    )


@pytest.mark.asyncio
async def test_unset_is_only_counted_until_the_user_confirms_then_names_what_a_stop_holds(monkeypatch, session):
    """Back to not set up (#11060): a dry-run count without confirm, the reset with it."""
    from app.services import fleet_control

    user = _caller(monkeypatch)
    fleet = _manageable_fleet_stub(monkeypatch)
    reset: list[str] = []

    async def _count(_db, _fleet):
        return 2, ("Stopped",)

    async def _unset(_db, _fleet, **_kw):
        reset.append("x")
        return fleet_control.FleetUnsetOutcome(changed=2, names=("a", "b"), held_by_stop=("Stopped",))

    audited: list[dict] = []

    async def _log_audit(_db, **kw):
        audited.append(kw)

    monkeypatch.setattr(fleet_control, "unsettable_count", _count)
    monkeypatch.setattr(fleet_control, "unset_fleet", _unset)
    monkeypatch.setattr("app.services.audit.log_audit", _log_audit)
    answer = await _call(str(user.id), "set_fleet_automations", fleet_id=str(fleet.id), action="unset")
    assert answer.startswith("Not applied. Back to not set up would switch off and reset 2 automation(s)")
    assert "Stopped" in answer and "confirm=true" in answer and reset == []
    done = await _call(str(user.id), "set_fleet_automations", fleet_id=str(fleet.id), action="unset", confirm=True)
    assert "2 automation(s) set back to not set up" in done and "Stopped" in done and reset == ["x"]
    assert [a["action"] for a in audited] == ["fleet_unset"]


def test_set_fleet_automations_offers_unset():
    definitions, _executor = fleet_tools.build_fleet_tools("")
    tool = next(d for d in definitions if d.name == "set_fleet_automations")
    assert tool.input_schema["properties"]["action"]["enum"] == ["pause", "resume", "unset", "enable"]
    assert "Not set up" in tool.description


@pytest.mark.asyncio
async def test_a_non_admin_cannot_switch_a_built_in_fleet_s_automations(monkeypatch, session):
    user = _caller(monkeypatch)
    fleet = _manageable_fleet_stub(monkeypatch)
    fleet.is_builtin = True
    answer = await _call(str(user.id), "set_fleet_automations", fleet_id=str(fleet.id), action="resume")
    assert answer == "Only an administrator can resume the automations of a built-in fleet."
    assert (session.commits, session.rollbacks) == (0, 1)


def test_the_parent_tool_says_the_parent_is_layout_only():
    definitions, _executor = fleet_tools.build_fleet_tools("")
    tool = next(d for d in definitions if d.name == "set_fleet_member_parent")
    assert "Layout only" in tool.description
    assert tool.input_schema["required"] == ["fleet_id", "agent_id"]
