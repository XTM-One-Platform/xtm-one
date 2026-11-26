"""Assignment visibility is the visibility of the parent agent.

An assignment has no audience of its own, so ``assignment_visible_filter`` is
``user_visible_filter`` on ``Agent``, lifted to the assignment. The copies it
replaced read the mirrored ``Assignment.user_id`` / ``is_company_managed``
columns instead, which missed per-user shares and ignored
``access_restricted``, and admitted rows whose agent the caller cannot
load: the Manual Triggers panel listed them as "Unknown agent".

Fake-session convention of the backend suite (no real-DB harness): the
statements are captured and compiled with the PostgreSQL dialect.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.api.v1 import helpers
from app.api.v1.helpers import (
    assignment_visible_filter,
    caller_visible_filter,
    company_wide_visible_filter,
    get_user_assignment,
    user_visible_filter,
)
from app.models.agent import Agent
from app.models.assignment import Assignment
from app.models.knowledge_base import KnowledgeBase
from tests import app_sources


def _render(clause) -> tuple[str, dict]:
    compiled = clause.compile(dialect=postgresql.dialect())
    return str(compiled), compiled.params


def test_an_assignment_is_visible_exactly_when_its_agent_is():
    user_id = uuid.uuid4()
    group_ids = [str(uuid.uuid4())]

    expected = Assignment.agent_id.in_(select(Agent.id).where(user_visible_filter(Agent, user_id, group_ids)))

    assert _render(assignment_visible_filter(user_id, group_ids)) == _render(expected)


def test_the_predicate_reads_the_agent_not_the_mirrored_assignment_columns():
    sql, _ = _render(assignment_visible_filter(uuid.uuid4(), [str(uuid.uuid4())]))
    sql = sql.lower()

    # Every tier of the agent predicate, per-user shares and the
    # ``access_restricted`` narrowing included.
    assert "from agents" in sql
    assert "agents.user_id" in sql
    assert "agents.shared_user_ids" in sql
    assert "agents.shared_group_ids" in sql
    assert "agents.access_restricted is not true" in sql
    # The mirrors are what drifted from the agent's rules.
    assert "assignments.user_id" not in sql
    assert "assignments.is_company_managed" not in sql


def test_a_caller_with_no_account_gets_the_company_wide_tier_alone():
    """An anonymous channel sender holds no grant: company-managed agents,
    minus those narrowed to an audience - never an ownerless-row match."""
    sql, _ = _render(assignment_visible_filter(None, []))
    sql = sql.lower()

    assert "agents.is_company_managed is true" in sql
    assert "agents.access_restricted is not true" in sql
    assert "agents.user_id" not in sql
    assert "shared_user_ids" not in sql
    assert "shared_group_ids" not in sql


@pytest.mark.parametrize(("model", "internal"), [(Agent, False), (KnowledgeBase, True)], ids=["agent", "kb"])
def test_the_company_wide_tier_excludes_restricted_and_internal_rows(model, internal):
    sql, _ = _render(company_wide_visible_filter(model))
    sql = sql.lower()
    table = model.__tablename__

    assert f"{table}.is_company_managed is true" in sql
    assert f"{table}.access_restricted is not true" in sql
    assert (f"{table}.is_internal is false" in sql) is internal
    assert f"{table}.user_id" not in sql


def test_the_admin_bypass_is_unconditional():
    sql, params = _render(assignment_visible_filter(uuid.uuid4(), [], is_admin=True))

    assert sql.lower() == "true"
    assert params == {}


class _CapturingDB:
    def __init__(self, row=None):
        self.row = row
        self.statements = []

    async def execute(self, stmt, *args, **kwargs):
        self.statements.append(stmt)
        return SimpleNamespace(scalar_one_or_none=lambda: self.row)


@pytest.fixture
def group_calls(monkeypatch):
    calls: list[uuid.UUID] = []

    async def _groups(_db, uid):
        calls.append(uid)
        return [str(uuid.uuid4())]

    monkeypatch.setattr(helpers, "get_user_group_ids", _groups)
    return calls


@pytest.mark.asyncio
async def test_get_user_assignment_reads_through_the_agent(group_calls):
    user_id = uuid.uuid4()
    row = SimpleNamespace(id=uuid.uuid4())
    db = _CapturingDB(row)

    assert await get_user_assignment(db, user_id, str(row.id)) is row

    assert group_calls == [user_id]
    sql, _ = _render(db.statements[0].whereclause)
    sql = sql.lower()
    assert "assignments.id =" in sql
    assert "agents.shared_user_ids" in sql
    assert "agents.shared_group_ids" in sql
    assert "agents.access_restricted is not true" in sql


@pytest.mark.asyncio
async def test_get_user_assignment_hides_an_invisible_assignment_as_not_found(
    group_calls,
):
    with pytest.raises(HTTPException) as exc:
        await get_user_assignment(_CapturingDB(None), uuid.uuid4(), str(uuid.uuid4()))

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_user_assignment_admin_bypass_skips_groups_and_visibility(
    group_calls,
):
    row = SimpleNamespace(id=uuid.uuid4())
    db = _CapturingDB(row)

    assert await get_user_assignment(db, uuid.uuid4(), str(row.id), is_admin=True) is row

    assert group_calls == []
    sql, _ = _render(db.statements[0].whereclause)
    assert sql.lower().startswith("assignments.id =")
    assert "agents" not in sql.lower()


_APP_ROOT = Path(__file__).resolve().parents[1] / "app"
_HELPERS = _APP_ROOT / "api" / "v1" / "helpers.py"
_PRIVATE_VISIBILITY_BLOCKS = frozenset(
    {
        "_shared_group_ids_overlap",
        "_shared_user_ids_contains",
        "_company_managed_visible",
    }
)


def _private_block_references(source: str, tree: ast.Module | None = None) -> list[tuple[int, str]]:
    """Where *source* reaches a private visibility block: an import, an
    attribute, a bare name or a ``getattr`` lookup. Prose - a comment, a
    docstring, any other string - names it without using it."""
    found: set[tuple[int, str]] = set()
    for node in app_sources.walk(ast.parse(source) if tree is None else tree):
        if isinstance(node, ast.ImportFrom):
            found.update((node.lineno, alias.name) for alias in node.names if alias.name in _PRIVATE_VISIBILITY_BLOCKS)
        elif isinstance(node, ast.Attribute) and node.attr in _PRIVATE_VISIBILITY_BLOCKS:
            found.add((node.lineno, node.attr))
        elif isinstance(node, ast.Name) and node.id in _PRIVATE_VISIBILITY_BLOCKS:
            found.add((node.lineno, node.id))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in _PRIVATE_VISIBILITY_BLOCKS
        ):
            found.add((node.lineno, node.args[1].value))
    return sorted(found)


def test_the_tripwire_reads_code_not_prose():
    source = "\n".join(
        [
            '"""Never call _shared_group_ids_overlap from here."""',
            "# _company_managed_visible is private to helpers.py.",
            "from app.api.v1.helpers import _shared_user_ids_contains as contains",
            "from app.api.v1 import helpers",
            "tier = helpers._company_managed_visible(Agent)",
            'block = getattr(helpers, "_shared_group_ids_overlap")',
            'label = "_shared_group_ids_overlap"',
            "visible = helpers.user_visible_filter(Agent, uid, groups)",
        ]
    )

    assert _private_block_references(source) == [
        (3, "_shared_user_ids_contains"),
        (5, "_company_managed_visible"),
        (6, "_shared_group_ids_overlap"),
    ]


_BUILDER_MODULES = sorted((_APP_ROOT / "services" / "builtin_tools").glob("agent_builder*.py"))
_TIER_COMPARATORS = frozenset({"is_", "isnot", "is_not"})


def _is_flag_tier(node: ast.AST) -> bool:
    """``X.is_company_managed.is_(...)`` / ``.isnot`` / ``.is_not`` / ``== True``:
    the flag read as a tier in SQL (a Python ``if row.is_company_managed`` is not)."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _TIER_COMPARATORS
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "is_company_managed"
    ) or (
        isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Attribute)
        and node.left.attr == "is_company_managed"
        and any(isinstance(c, ast.Constant) and c.value is True for c in node.comparators)
    )


def _builder_tier_bypasses(source: str) -> list[tuple[int, str]]:
    """Where an agent-builder module decides visibility by hand.

    A read tool answers a caller with or without an account through
    ``caller_visible_filter``; ``user_visible_filter`` is only called with an
    explicit ``is_admin=`` (the loaders' and ``scope='all'``'s admin paths).
    Reading ``is_company_managed`` as a tier - ``.is_(True)``, ``== True`` -
    was the anonymous fallback that ignored ``access_restricted`` and
    ``is_internal``."""
    found: set[tuple[int, str]] = set()
    for node in ast.walk(ast.parse(source)):
        if _is_flag_tier(node):
            found.add((node.lineno, "is_company_managed tier"))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name | ast.Attribute)
            and getattr(node.func, "id", getattr(node.func, "attr", None)) == "user_visible_filter"
            and not any(k.arg == "is_admin" for k in node.keywords)
        ):
            found.add((node.lineno, "user_visible_filter without is_admin"))
    return sorted(found)


def test_the_builder_tripwire_flags_a_hand_decided_tier():
    source = "\n".join(
        [
            "q = q.where(Prompt.is_company_managed.is_(True))",
            "q = q.where(Prompt.is_company_managed == True)",
            "clause = user_visible_filter(Prompt, uid, group_ids) if uid else None",
            "q = q.where(caller_visible_filter(Prompt, uid, group_ids))",
            "q = q.where(user_visible_filter(Agent, uid, group_ids, is_admin=True))",
            "badge = 'company' if prompt.is_company_managed else ''",
            "stmt = table.update().values(is_company_managed=True)",
        ]
    )

    assert _builder_tier_bypasses(source) == [
        (1, "is_company_managed tier"),
        (2, "is_company_managed tier"),
        (3, "user_visible_filter without is_admin"),
    ]


def test_no_builder_read_tool_decides_a_tier_by_hand():
    assert _BUILDER_MODULES, "the agent-builder module moved; point the tripwire at it"
    offenders = [
        f"{path.relative_to(_APP_ROOT)}:{lineno}: {what}"
        for path in _BUILDER_MODULES
        for lineno, what in _builder_tier_bypasses(path.read_text(encoding="utf-8"))
    ]

    assert not offenders, (
        "Agent-builder read tools take their visibility from "
        f"caller_visible_filter (or user_visible_filter with is_admin=): {offenders}"
    )


_SERVICES = _APP_ROOT / "services"

# Every place under ``services/`` that still reads the flag as a tier, and why
# that is not a read offered to a caller with no account. Anything else is
# routed through ``caller_visible_filter`` / ``assignment_visible_filter``.
_FLAG_TIER_ALLOWED: dict[tuple[str, str], str] = {
    ("services/builtin_tools/fleets.py", "_set_fleet_company_managed"): (
        "the creation rule of a company-managed fleet (#398): names the personal home members of a "
        "fleet the administrator already manages; lists nothing to a caller"
    ),
    ("services/builtin_tools/workspaces.py", "_set_workspace_company_managed"): (
        "counts the company-managed fleets a workspace the administrator manages hosts before it turns "
        "personal (#398); lists nothing to a caller"
    ),
    ("services/fleet_spec.py", "_own_fleet"): (
        "refuses a fleet spec applied over a company-managed fleet of the spec's slug owned by someone "
        "else (#657): the flag is a property of the target fleet, the answer a refusal; lists nothing to a caller"
    ),
    ("services/stix_extraction/knowledge.py", "_configured_source"): (
        "the deployment's ATT&CK source (mirror URL, pinned release) an administrator "
        "configured on a company-managed MITRE ATT&CK integration, read for no acting user; "
        "lists nothing to a caller (#9126)"
    ),
    # A company-managed agent's key lookup ORDERS the rows it already sees,
    # company-managed first, so one key never splits by run context. The
    # visible set is decided by the caller filter above it, not by the flag.
    ("services/shared_state.py", "_visible_state_query"): (
        "orders the visible rows of a company-managed agent's lookup; it filters nothing"
    ),
    # Platform Health fleet seed (#8261) finds its own rows: the fleet's agents,
    # assignments, fleet and skills are company-managed by construction, and a
    # personal row holding a fleet slug or role is never adopted. No caller's read.
    ("services/health/fleet_seed.py", "_skill_ids_by_slug"): (
        "the seeded skills the fleet carries are company-managed ones; a personal skill with the slug is not one"
    ),
    ("services/health/fleet_seed.py", "_ensure_fleet"): (
        "the seeded fleet and workspace are company-managed; a personal fleet of the same name is not adopted"
    ),
    ("services/health/fleet_seed.py", "fleet_assignment"): (
        "the fleet assignment a cycle starts runs on a company-managed fleet agent, never a personal look-alike"
    ),
    ("services/health/fleet_seed.py", "fleet_agent_ids"): (
        "the digest's 'by the Concierge' counts the fleet's own agents, company-managed by construction"
    ),
    # (b) not a read offered to the caller, SQL unchanged
    ("services/health/platform_checks.py", "integration_broken"): (
        "the administrator health check of company-managed integrations (#101): the flag IS "
        "the scope, read for no acting user; personal ones go to their owner; lists nothing to a caller"
    ),
    ("services/health/platform_checks.py", "mcp_server_broken"): (
        "the administrator health check of company-managed MCP servers (#101): the flag IS "
        "the scope, read for no acting user; personal ones go to their owner; lists nothing to a caller"
    ),
    ("services/health/sandbox/side_effects.py", "company_mcp_server_ids"): (
        "the flag IS the policy: a sandbox trusts a tool name only from a server an "
        "administrator vouched for, the set the HITL derivation trusts (#8255)"
    ),
    ("services/hitl_whitelist_derived.py", "connected_read_tools"): (
        "the flag IS the policy: only company-managed servers may exempt a "
        "tool deployment-wide, so a personal server cannot un-gate a name for all"
    ),
    ("services/intent_binding_writes.py", "agent_shared_scope_filter"): (
        "the intent catalog's export scope (company-managed or group-shared), ANDed with "
        "user_visible_filter for the caller; it never widens what a caller sees"
    ),
    ("services/builtin_tools/skills.py", "_visible_skill_rows"): (
        "the identity is the agent's OWNER; narrowing an ownerless agent would drop skills its automations load today"
    ),
    ("services/variable_resolver.py", "load_variables"): (
        "a run with no account resolves the agent's own configuration; narrowing "
        "it would leave {{var_id}} unresolved in existing anonymous-channel bots"
    ),
    ("services/agent_chat.py", "_fetch_handover_agents"): (
        "the handover targets the agent's own configuration names are its reach, "
        "like its bound knowledge bases; narrowing them would stop existing "
        "anonymous-channel bots handing over"
    ),
    ("services/builtin_tools/work_items.py", "load_work_item_peers"): (
        "the work-item peer roster the agent's own configuration names is its reach"
    ),
    ("services/builtin_tools/computer_use.py", "_eligible_runners"): (
        "the runners the agent's own selection names for a run with no user; a "
        "chat with no account is never offered computer_use"
    ),
    ("services/action_register.py", "_resolve_agent_names"): (
        "maps an owner name in the register to an agent id for the work item it creates; lists nothing to the caller"
    ),
    ("services/agent_bundle_projection.py", "bindable_ids"): (
        "a signed-in installer's by-reference binding: the row must be company-managed on top of user_visible_filter"
    ),
    ("services/agent_bindings.py", "strip_personal_bindings"): (
        "an agent turning company-managed keeps only company-managed bindings: the flag classifies "
        "the agent's own bindings, it lists nothing to a caller"
    ),
    ("services/general_assistant_skills.py", "default_skill_ids"): (
        "finds the platform's own built-in skills by their slug tag (a seed lookup) to bind them to the "
        "General Assistant; lists nothing to a caller"
    ),
    ("services/general_assistant_skills.py", "unbindable_skills"): (
        "a write guard: the General Assistant serves every user, so it only carries company-managed skills"
    ),
    ("services/shared_state.py", "_visible_clause"): (
        "SharedState has no access_restricted / is_internal: the flag is the whole company-wide tier"
    ),
    ("services/work_item.py", "_visible_clause"): (
        "WorkItem has no access_restricted / is_internal: the flag is the whole company-wide tier"
    ),
    ("services/shared_state_pull_requests.py", "_held_rows"): (
        "the inbox dispatch reads the leases held on a work item's pull requests (its user's or "
        "the company's entries); SharedState has no access_restricted, and it lists nothing to a caller"
    ),
    ("services/flow_events.py", "emit_flow_event"): ("a signed-in user's event dispatch (own + company-managed flows)"),
    ("services/investigation_pack_resolver.py", "only_registered_opencti"): (
        "on top of user_visible_query: names the integration a registered OpenCTI creates "
        "(company-managed, platform_managed), the one a run reads as its caller"
    ),
    ("services/integration_tools.py", "template_candidate_clause"): (
        "credential eligibility for a signed-in run, not visibility"
    ),
    ("services/integration_credentials.py", "resolve_session_integration_bindings"): (
        "a company-managed agent's run keeps only the company-managed rows of a session "
        "override, on top of the caller's visible filter: the flag classifies the agent's "
        "bindings, it lists nothing to a caller (#11866)"
    ),
    # (c) system lookups, seeds, built-ins and write guards
    ("services/assignment_pause.py", "owner_not_deactivated"): (
        "the owner-deactivation run gate: a company-managed automation "
        "runs whoever created it; it decides what runs, never what a caller sees"
    ),
    ("services/builtin_tools/flow_builder.py", "_get_flow_examples"): "seeded examples",
    ("services/ctem_metrics_collector.py", "_user_creds"): "system credential lookup",
    ("services/evaluation_engine.py", "_load_default_judge_prompt_from_skill"): ("seeded built-in skill"),
    ("services/agent_template_skills.py", "visible_skill_ids_by_slug"): (
        "seeded built-in skill: a template default names one by slug, and the "
        "caller's tier is the user_visible_filter of the same clause"
    ),
    ("services/flow_examples.py", "seed_example_flow"): "seed idempotency",
    ("services/flow_examples.py", "_seed_one_flow"): "seed idempotency",
    ("services/integration_state.py", "any_provider_connected"): "platform state",
    ("services/kb_sync.py", "resolve_sync_integration"): "sync credential selection",
    ("services/kb_sync.py", "_get_drata_integration"): "sync credential selection",
    ("services/nova_engine.py", "load_rule_rows"): "platform rules",
    ("services/nova_engine.py", "resolve_nova_models"): "platform integration",
    ("services/nova_guard.py", "load_platform_rules"): "platform rules",
    ("services/nova_rule_sync.py", "sync_nova_rule_source"): "platform rules sync",
    ("services/nova_rule_sync.py", "process_scheduled_nova_syncs"): "scheduler",
    ("services/presentations.py", "resolve_default_template_id"): (
        "the configured default template must be company-managed"
    ),
    ("services/resource_exposure.py", "per_user_integrations"): (
        "which integration an assignment run picks per user: the acting "
        "person's own private one, else the company-wide one "
        "template_candidate_filter admits; it describes a run, never "
        "what a caller sees"
    ),
    ("services/skill_package_bindings.py", "_company_managed_kb_ids"): "write guard",
    ("services/shared_state.py", "_deletable_clause"): (
        "write guard: a non-admin's bulk delete never reaches a company-managed entry"
    ),
    ("services/work_item.py", "_deletable_clause"): (
        "write guard: a non-admin's bulk delete never reaches a company-managed item"
    ),
    ("services/work_item.py", "_inbox_clauses"): (
        "write guard: an inbox never lists the company-managed items it may not claim; "
        "the dispatch and the work-item views share it"
    ),
    ("services/work_item.py", "_writable_clause"): (
        "write guard: the unfiled company-managed items a writer may change, "
        "the company_managed_writes rule then deciding who"
    ),
    ("services/work_item.py", "_owner_agent_clause"): (
        "an agent's own dispatch scope (#11900): a company-managed item filed in a "
        "fleet workspace the agent is a home member of, for its owner or claimer "
        "only; a caller with no account or no agent gets false()"
    ),
    ("services/shared_state.py", "_narrowed_clause"): (
        "an agent's narrowing on top of the visible scope: the company-managed "
        "entries whose owner agent keeps that agent out"
    ),
    ("services/work_item.py", "_narrowed_clause"): (
        "an agent's narrowing on top of the visible scope: the company-managed "
        "items whose creator keeps that agent out"
    ),
    ("services/telemetry.py", "refresh_telemetry_data"): "a count",
    ("services/template_container_seed.py", "builtin_container_filter"): "seed",
    ("services/template_container_seed.py", "_load_company_managed"): "seed",
    ("services/custom_dashboards.py", "list_visible_dashboards"): (
        "the dashboards list's mine / shared / company narrowing on top of the "
        "visible scope user_visible_filter already decided"
    ),
    ("services/dashboard_widget_packs.py", "list_visible_packs"): (
        "the widget pack library's mine / shared / company narrowing on top of "
        "the visible scope user_visible_filter already decided"
    ),
    ("services/platform_insights/objects.py", "query_platform_objects"): (
        "a filter the caller names on the admin object union, read through the "
        "platform-or-own-slice scope (platform_insights.scope), never a caller tier"
    ),
    ("services/workspaces.py", "_add_fleet_produced"): (
        "a count that must equal the Worksets list it summarises: that list's own "
        "rule (api/v1/worksets.py, own or company-managed) for a signed-in caller, "
        "never reached without an account"
    ),
    ("services/library_filters.py", "library_filters"): (
        "a library page's mine / shared / company narrowing (the presentations, "
        "the Videos tab) on top of the visible scope user_visible_filter already "
        "decided"
    ),
    ("services/integration_tool_slugs.py", "validate_tool_slug_change"): (
        "the owner scope of the integration being edited (company-managed, or one "
        "user's own) for a tool-suffix uniqueness count, never a caller tier; the "
        "route already checked the caller may edit it"
    ),
    ("services/organization_context_opencrq.py", "_opencrq_connection"): (
        "the company-wide OpenCRQ an administrator's sync writes the profile against, "
        "on top of integration_visible_filter for that signed-in administrator; never a caller tier"
    ),
    ("services/organization_opencti.py", "opencti_gql"): (
        "the company-wide OpenCTI an administrator's identity link and PIR sync write against, "
        "on top of integration_visible_filter for that signed-in administrator; never a caller tier"
    ),
    ("services/organization_pirs.py", "_opencti_integrations"): (
        "the company-wide OpenCTIs the scheduled Intelligence policy refresh reads PIRs from, "
        "with no acting user; lists nothing to a caller, never a caller tier (#2435)"
    ),
    ("services/sub_agent.py", "_background_tasks_query"): (
        "the Background Tasks Company / Shared split of an admin's tasks that are "
        "not theirs, by the delegating agent, on top of the admin-only scope the "
        "route already decided; a non-admin gets no rows, never a caller tier"
    ),
    ("services/integration_credentials.py", "shared_agent_personal_integrations"): (
        "the agent owner's own personal rows the agent binds (user_id is the owner), "
        "read for the agent settings banner; lists nothing to a caller (#8824)"
    ),
    ("services/fleets.py", "grant_fleet_health_advisory"): (
        "finds the platform-seeded fleet-health-advisory skill by its slug tag to bind it "
        "to a fleet orchestrator during the fleet seed; answers no row to a caller, never a caller tier (#11700)"
    ),
    ("services/builtin_learned_skill.py", "find_learned_skill"): (
        "finds a built-in agent's own learned skill, a company-managed skill by construction, by its "
        "learned-for-agent tag for the record_learned_lesson tool; lists nothing to a caller (#914)"
    ),
    ("services/fleet_reset.py", "_seeded_bindings"): (
        "finds the platform-seeded skills a built-in agent's seed binds, by their slug tag, to rewrite "
        "that agent at an administrator's fleet reset; lists nothing to a caller, never a caller tier (#1508)"
    ),
}


def _flag_tier_reads(source: str, tree: ast.Module | None = None) -> list[tuple[int, str]]:
    """``(line, enclosing function)`` of every flag tier read in *source*."""
    found: list[tuple[int, str]] = []

    def visit(node: ast.AST, function: str) -> None:
        for child in ast.iter_child_nodes(node):
            if _is_flag_tier(child):
                found.append((child.lineno, function))
            inner = child.name if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else function
            visit(child, inner)

    visit(ast.parse(source) if tree is None else tree, "<module>")
    return sorted(found)


def test_the_services_tripwire_names_the_function_reading_the_flag():
    source = "\n".join(
        [
            "async def load(db, uid):",
            "    if uid is None:",
            "        return select(Agent).where(Agent.is_company_managed.is_(True))",
            "    return select(Agent).where(caller_visible_filter(Agent, uid))",
            "FLEET = Agent.is_company_managed == True",
        ]
    )

    assert _flag_tier_reads(source) == [(3, "load"), (5, "<module>")]


def test_no_service_reads_the_company_managed_flag_as_a_caller_tier():
    """A caller with no account reaches services as ``None`` or the
    ``"anonymous"`` sentinel; its tier is ``caller_visible_filter``'s. A new
    read on the flag fails here until it is routed through it, or allow-listed
    with the reason it is not a caller's read."""
    flagged = {
        (path.relative_to(_APP_ROOT).as_posix(), function): lineno
        for path in app_sources.python_files(_SERVICES)
        for lineno, function in _flag_tier_reads(app_sources.source(path), app_sources.tree(path))
    }
    offenders = sorted(
        f"{path}:{lineno} ({function})"
        for (path, function), lineno in flagged.items()
        if (path, function) not in _FLAG_TIER_ALLOWED
    )
    stale = sorted(f"{p} ({f})" for p, f in _FLAG_TIER_ALLOWED if (p, f) not in flagged)

    assert not offenders, (
        "These services read is_company_managed as a tier; use "
        f"caller_visible_filter (or allow-list the site with its reason): {offenders}"
    )
    assert not stale, f"Allow-listed sites that no longer read the flag: {stale}"


def test_the_anonymous_sentinel_reads_like_no_account():
    """``"anonymous"`` never reaches a UUID comparison: it is the company-wide tier."""
    assert _render(caller_visible_filter(KnowledgeBase, "anonymous", [])) == _render(
        company_wide_visible_filter(KnowledgeBase)
    )
    sql, params = _render(assignment_visible_filter("anonymous"))
    assert "agents.user_id" not in sql.lower()
    assert "anonymous" not in {str(v) for v in params.values()}


def test_a_caller_with_an_account_gets_its_three_tiers_and_one_without_the_company_wide_one():
    user_id = uuid.uuid4()
    group_ids = [str(uuid.uuid4())]

    assert _render(caller_visible_filter(KnowledgeBase, user_id, group_ids)) == _render(
        user_visible_filter(KnowledgeBase, user_id, group_ids)
    )
    assert _render(caller_visible_filter(KnowledgeBase, str(user_id), group_ids)) == _render(
        user_visible_filter(KnowledgeBase, user_id, group_ids)
    )
    assert _render(caller_visible_filter(KnowledgeBase, None, group_ids)) == _render(
        company_wide_visible_filter(KnowledgeBase)
    )


def test_no_module_rebuilds_a_visibility_predicate_from_the_private_blocks():
    """Visibility is composed from ``user_visible_filter`` /
    ``assignment_visible_filter`` / ``company_wide_visible_filter``, never
    re-assembled from their parts: every hand-assembled copy found so far
    covered some tiers and not others."""
    offenders = [
        f"{path.relative_to(_APP_ROOT)}:{lineno}: {name}"
        for path in app_sources.python_files(_APP_ROOT)
        if path != _HELPERS
        for lineno, name in _private_block_references(app_sources.source(path), app_sources.tree(path))
    ]

    assert not offenders, (
        "These modules assemble a visibility predicate by hand; use "
        "user_visible_filter / assignment_visible_filter / "
        f"company_wide_visible_filter instead: {offenders}"
    )


_SHARE_COLUMNS = frozenset({"shared_group_ids", "shared_user_ids"})

# Every place outside helpers.py that reads a sharing column in SQL, and why it
# is not a caller's tier. A share predicate built by hand covers the tiers its
# author thought of: variable resolution matched ``shared_group_ids`` and never
# ``shared_user_ids``.
_SHARE_COLUMN_READS_ALLOWED: dict[tuple[str, str], str] = {
    ("services/intent_binding_writes.py", "agent_shared_scope_filter"): (
        "whether an agent is shared at all, never with whom"
    ),
    ("services/integration_credentials.py", "shared_agent_personal_integrations"): (
        "which of the owner's own rows an agent group already gets, for the agent "
        "settings banner; decides no visibility (#8824)"
    ),
    ("services/roi_access.py", "reader_scope"): (
        "which workspaces are shared with a group the caller manages, the group "
        "managers' ROI rule of fleet_roi_reader on top of user_visible_filter; "
        "never a caller tier (#9836)"
    ),
    ("services/roi_chat_backfill.py", "_presentations"): (
        "whether a past chat deck was granted to anyone, a use signal of the ROI "
        "backfill run by an admin; decides no visibility (#10544)"
    ),
    ("services/roi_chat_backfill.py", "_videos"): (
        "whether a past chat video was granted to anyone, a use signal of the ROI "
        "backfill run by an admin; decides no visibility (#10544)"
    ),
}


def _share_column_sql_reads(source: str, tree: ast.Module | None = None) -> list[tuple[int, str]]:
    """``(line, enclosing function)`` of every sharing column read off a model
    class (``Agent.shared_group_ids``, ``model_cls.shared_user_ids``): the SQL
    column. A row's own list (``agent.shared_group_ids``) is not such a read."""
    found: list[tuple[int, str]] = []

    def visit(node: ast.AST, function: str) -> None:
        for child in ast.iter_child_nodes(node):
            if (
                isinstance(child, ast.Attribute)
                and child.attr in _SHARE_COLUMNS
                and isinstance(child.value, ast.Name)
                and (child.value.id[:1].isupper() or child.value.id == "model_cls")
            ):
                found.append((child.lineno, function))
            inner = child.name if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else function
            visit(child, inner)

    visit(ast.parse(source) if tree is None else tree, "<module>")
    return sorted(found)


def test_the_share_column_tripwire_reads_the_sql_column_not_a_rows_list():
    source = "\n".join(
        [
            "def load(db, groups):",
            "    return select(Variable).where(Variable.shared_group_ids.cast(JSONB).contains(groups))",
            "def clean(model_cls, gid):",
            "    return cast(model_cls.shared_user_ids, Text).contains(gid)",
            "def grants(var):",
            "    return var.shared_group_ids or []",
        ]
    )

    assert _share_column_sql_reads(source) == [(2, "load"), (4, "clean")]


def test_no_module_reads_a_sharing_column_in_sql_outside_the_helpers():
    """Who an entity is shared with is decided by ``user_visible_filter`` (and
    the filters built on it) alone. A new read fails here until it goes through
    them, or is allow-listed with the reason it decides no visibility."""
    flagged = {
        (path.relative_to(_APP_ROOT).as_posix(), function): lineno
        for path in app_sources.python_files(_APP_ROOT)
        if path != _HELPERS
        for lineno, function in _share_column_sql_reads(app_sources.source(path), app_sources.tree(path))
    }
    offenders = sorted(
        f"{path}:{lineno} ({function})"
        for (path, function), lineno in flagged.items()
        if (path, function) not in _SHARE_COLUMN_READS_ALLOWED
    )
    stale = sorted(f"{p} ({f})" for p, f in _SHARE_COLUMN_READS_ALLOWED if (p, f) not in flagged)

    assert not offenders, (
        "These modules read shared_group_ids / shared_user_ids in SQL; use "
        f"user_visible_filter / caller_visible_filter (or allow-list the site with its reason): {offenders}"
    )
    assert not stale, f"Allow-listed sites that no longer read a sharing column: {stale}"
