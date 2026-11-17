"""Platform-wide lookups give a caller with no account the company-wide tier
alone, against real PostgreSQL.

A caller with no account reaches these paths as ``None`` (an ownerless run) or
as the ``"anonymous"`` sentinel an unidentified channel sender carries
(``channels/router.py``). Either way it holds no grant, so a lookup across the
platform (the template reads) answers it with ``caller_visible_filter``:
company-managed rows minus those narrowed to an audience by
``access_restricted``. What the agent's own configuration names - its
runners, work-item peers, register owners - is its reach, as on main. A
signed-in caller keeps its own three tiers, unchanged, and a row the caller
cannot see answers exactly like an unknown id.

``integration``-marked and skips when PostgreSQL is unreachable, like its
neighbours; the temporary-database harness is theirs, repeated rather than
shared (see ``test_ai_disabled_read_surface_postgres.py``).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.core.database as database
import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api.v1.helpers import company_managed_run_scoped, mark_company_managed_run
from app.models.agent import Agent
from app.models.assignment import Assignment, AssignmentRun
from app.models.runner import Runner
from app.models.template_container import TemplateContainer
from app.models.user import User
from app.services import action_register, presentations, template_containers
from app.services.builtin_tools import computer_use, document_vision, template_authoring, templates
from app.services.builtin_tools import work_items as work_item_tools
from app.tools.base import ToolCall
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

ANONYMOUS = "anonymous"
KINDS = ("open", "restricted", "private")
# What each signed-in persona sees of the three kinds (unchanged by the fix).
SIGNED_IN = {"owner": {"open", "private"}, "audience": {"open", "restricted"}}
# What a caller with no account reaches through what the agent's own
# configuration names (runners, work-item peers, register owners): every
# company-managed row, as on main.
CONFIGURED_REACH = {"open", "restricted"}


@asynccontextmanager
async def _temp_database(prefix: str) -> AsyncIterator[async_sessionmaker]:
    """Isolated database with the full schema, dropped on exit."""
    async with scratch_database(
        prefix,
        tables=ALL_TABLES,
        extensions=("vector",),
        skip_without_extensions=True,
        bind_app_sessions=False,
    ) as db:
        yield db.sessions


@dataclass
class World:
    users: dict[str, User]
    agents: dict[str, Agent]
    assignments: dict[str, Assignment]
    templates: dict[str, TemplateContainer]
    runners: dict[str, Runner]


def _tiered(kind: str, users: dict[str, User]) -> dict:
    """Owner / visibility columns of one row of *kind*."""
    if kind == "private":
        return {"user_id": users["owner"].id}
    fields = {"user_id": users["admin"].id, "is_company_managed": True}
    if kind == "restricted":
        audience = str(users["audience"].id)
        fields.update(
            access_restricted=True,
            shared_user_ids=[audience],
            shared_user_permissions={audience: "read_only"},
        )
    return fields


async def _seed(session_factory: async_sessionmaker) -> World:
    async with session_factory() as db:
        users = {
            name: User(
                email=f"{name}-{uuid.uuid4().hex[:8]}@example.com",
                hashed_password="x",
                full_name=name,
                role="admin" if name == "admin" else "user",
            )
            for name in ("owner", "audience", "admin")
        }
        db.add_all(users.values())
        await db.flush()

        def name(prefix: str, kind: str) -> str:
            return f"{prefix}-{kind}-{uuid.uuid4().hex[:8]}"

        agents = {
            kind: Agent(
                name=name("agent", kind), slug=name("agent", kind), persona="p", **_tiered(kind, users)
            )
            for kind in KINDS
        }
        templates_ = {
            kind: TemplateContainer(name=name("template", kind), **_tiered(kind, users))
            for kind in KINDS
        }
        runners = {
            kind: Runner(
                name=name("runner", kind),
                platform="linux",
                key_prefix=uuid.uuid4().hex[:12],
                key_hash="x",
                **_tiered(kind, users),
            )
            for kind in KINDS
        }
        db.add_all([*agents.values(), *templates_.values(), *runners.values()])
        await db.flush()
        assignments = {
            kind: Assignment(
                agent_id=agent.id,
                user_id=agent.user_id,
                is_company_managed=agent.is_company_managed,
                name=name("assignment", kind),
                trigger_type="manual",
                trigger_config={},
                prompt="p",
                enabled=True,
            )
            for kind, agent in agents.items()
        }
        db.add_all(assignments.values())
        await db.commit()
    return World(users, agents, assignments, templates_, runners)


@pytest.fixture
def use_db(monkeypatch):
    """Point every service's session factory at a test database."""

    def _use(session_factory: async_sessionmaker) -> None:
        monkeypatch.setattr(database, "async_session", session_factory)
        monkeypatch.setattr(computer_use, "async_session", session_factory)

    return _use


def _callers(world: World) -> dict[str, tuple[str | None, set[str]]]:
    """Every caller shape and the kinds it must see: the two ways a caller with
    no account arrives, then the signed-in personas (unchanged)."""
    return {
        "none": (None, {"open"}),
        "sentinel": (ANONYMOUS, {"open"}),
        **{p: (str(world.users[p].id), kinds) for p, kinds in SIGNED_IN.items()},
    }


def _uuid_or_none(user_id: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(user_id) if user_id else None
    except ValueError:
        return None


async def _run(executor, tool: str, **arguments) -> str:
    result = await executor.execute_tool(
        ToolCall(id=str(uuid.uuid4()), name=tool, arguments=arguments)
    )
    return result.content


def _masked(content: str, row_id) -> str:
    return content.replace(str(row_id), "<id>")


# ---------------------------------------------------------------------------
# Template containers
# ---------------------------------------------------------------------------


async def test_template_search_gives_each_caller_its_tier(use_db):
    async with _temp_database("anonbt_search") as session_factory:
        use_db(session_factory)
        world = await _seed(session_factory)
        for caller, (user_id, kinds) in _callers(world).items():
            async with session_factory() as db:
                rows = await template_containers.search_containers(
                    db, user_id=_uuid_or_none(user_id), user_group_ids=[]
                )
            found = {k for k, t in world.templates.items() if t.id in {r.id for r in rows}}
            assert found == kinds, caller


async def test_presentation_template_lookups_give_each_caller_its_tier(use_db):
    async with _temp_database("anonbt_theme") as session_factory:
        use_db(session_factory)
        world = await _seed(session_factory)
        for caller, (user_id, kinds) in _callers(world).items():
            uid = _uuid_or_none(user_id)
            async with session_factory() as db:
                for kind, template in world.templates.items():
                    row = await presentations._load_visible_template(db, template.id, user_id=uid)
                    assert (row is not None) is (kind in kinds), (caller, kind)
                missing = await presentations._load_visible_template(db, uuid.uuid4(), user_id=uid)
                assert missing is None
                names = await presentations.resolve_template_names(
                    db, [t.id for t in world.templates.values()], user_id=uid, user_group_ids=[]
                )
            assert {k for k, t in world.templates.items() if str(t.id) in names} == kinds, caller


@pytest.mark.parametrize(
    ("builder", "tool", "extra"),
    [
        (templates.build_template_search_tools, "get_template", {}),
        (
            template_authoring.build_template_authoring_tools,
            "validate_presentation",
            {"spec": {"title": "t", "slides": []}},
        ),
        (
            document_vision.build_document_vision_tools,
            "compare_palette",
            {"colors": {"accent": "#00e1b4"}},
        ),
    ],
    ids=["get_template", "validate_presentation", "compare_palette"],
)
async def test_template_tools_answer_a_hidden_template_like_a_missing_one(
    use_db, builder, tool, extra
):
    async with _temp_database("anonbt_tpltool") as session_factory:
        use_db(session_factory)
        world = await _seed(session_factory)
        for caller, (user_id, kinds) in _callers(world).items():
            _, executor = builder(None, user_id or "")
            missing = uuid.uuid4()
            on_missing = _masked(await _run(executor, tool, template_id=str(missing), **extra), missing)
            for kind, template in world.templates.items():
                answer = _masked(await _run(executor, tool, template_id=str(template.id), **extra), template.id)
                if kind in kinds:
                    assert answer != on_missing, (caller, kind, answer)
                else:
                    assert answer == on_missing, (caller, kind, answer)


# ---------------------------------------------------------------------------
# What the agent's own configuration names is not narrowed: runners,
# work-item peers, Action Register owners
# ---------------------------------------------------------------------------


def _configured_callers(world: World) -> dict[str, tuple[str | None, set[str]]]:
    """Every caller shape and the kinds it reaches through the agent's own
    configuration: with no account, every company-managed row (as on main);
    signed in, its own tiers (unchanged)."""
    return {
        "none": (None, CONFIGURED_REACH),
        "sentinel": (ANONYMOUS, CONFIGURED_REACH),
        **{p: (str(world.users[p].id), kinds) for p, kinds in SIGNED_IN.items()},
    }


async def _listed_runners(world: World, user_id: str | None) -> set[str]:
    _, executor = computer_use.build_computer_use_tools(
        user_id=user_id,
        is_admin=False,
        user_group_ids=[],
        agent_runner_ids=None,
        agent_runner_labels=None,
    )
    content = await _run(executor, "list_runners")
    if not content.startswith("["):
        return set()
    ids = {entry["id"] for entry in json.loads(content)}
    return {k for k, r in world.runners.items() if str(r.id) in ids}


async def _peer_kinds(world: World, user_id: str | None) -> set[str]:
    peers = await work_item_tools.load_work_item_peers(
        owner_agent_ids=[str(a.id) for a in world.agents.values()], user_id=user_id
    )
    text_ = json.dumps(peers, default=str)
    return {k for k, a in world.agents.items() if str(a.id) in text_}


async def _owner_kinds(world: World, user_id: str | None) -> set[str]:
    lookup = await action_register._resolve_agent_names(user_id)
    return {k for k, a in world.agents.items() if a.name.lower() in lookup}


async def test_list_runners_keeps_the_company_managed_tier_without_an_account(use_db):
    async with _temp_database("anonbt_runners") as session_factory:
        use_db(session_factory)
        world = await _seed(session_factory)
        for caller, (user_id, kinds) in _configured_callers(world).items():
            assert await _listed_runners(world, user_id) == kinds, caller


async def test_work_item_peers_keep_the_company_managed_tier_without_an_account(use_db):
    async with _temp_database("anonbt_peers") as session_factory:
        use_db(session_factory)
        world = await _seed(session_factory)
        for caller, (user_id, kinds) in _configured_callers(world).items():
            assert await _peer_kinds(world, user_id) == kinds, caller


async def test_action_register_owner_names_keep_the_company_managed_tier_without_an_account(use_db):
    async with _temp_database("anonbt_register") as session_factory:
        use_db(session_factory)
        world = await _seed(session_factory)
        for caller, (user_id, kinds) in _configured_callers(world).items():
            assert await _owner_kinds(world, user_id) == kinds, caller


async def test_delegation_answers_a_hidden_assignment_like_a_missing_one(use_db):
    """A caller with no account can delegate to nothing (company-managed is
    admin-only), but a restricted or private assignment must not be told apart
    from an unknown id on the way."""
    async with _temp_database("anonbt_delegate") as session_factory:
        use_db(session_factory)
        world = await _seed(session_factory)
        for user_id in (None, ANONYMOUS):
            missing = uuid.uuid4()
            on_missing = await work_item_tools._run_delegate_to_assignment(
                assignment_id=str(missing), task="t", acting_user_id=user_id, delegating_agent_id=None
            )
            for kind in ("restricted", "private"):
                assignment = world.assignments[kind]
                answer = await work_item_tools._run_delegate_to_assignment(
                    assignment_id=str(assignment.id),
                    task="t",
                    acting_user_id=user_id,
                    delegating_agent_id=None,
                )
                assert _masked(answer, assignment.id) == _masked(on_missing, missing), (user_id, kind)
            opened = await work_item_tools._run_delegate_to_assignment(
                assignment_id=str(world.assignments["open"].id),
                task="t",
                acting_user_id=user_id,
                delegating_agent_id=None,
            )
            assert "Only admins can delegate" in opened, opened


# ---------------------------------------------------------------------------
# The run of a company-managed assignment with no user keeps its tier
# ---------------------------------------------------------------------------

# What a scheduled company-managed run with no "run as" user reaches, as on
# main: every company-managed row, the restricted one included.
COMPANY_RUN = {"open", "restricted"}


async def _ownerless_company_run(session_factory, world: World) -> tuple[uuid.UUID, uuid.UUID]:
    """A scheduled company-managed assignment with no "run as" user, and a run of it."""
    async with session_factory() as db:
        agent = world.agents["open"]
        assignment = Assignment(
            agent_id=agent.id,
            user_id=None,
            run_as_user_id=None,
            is_company_managed=True,
            name=f"fleet-audit-{uuid.uuid4().hex[:8]}",
            trigger_type="schedule",
            trigger_config={"cron": "0 * * * *"},
            prompt="p",
            enabled=True,
        )
        db.add(assignment)
        await db.flush()
        run = AssignmentRun(
            assignment_id=assignment.id,
            agent_id=agent.id,
            user_id=None,
            is_company_managed=True,
            status="pending",
            trigger_payload={},
            trigger_context={"type": "schedule"},
        )
        db.add(run)
        await db.commit()
        return assignment.id, run.id


async def _as_that_run(session_factory, assignment_id, run_id, read):
    """Run *read* the way the engine runs the tools of that run: in a run scope
    marked from the two rows it loads."""

    @company_managed_run_scoped
    async def _execute():
        async with session_factory() as db:
            run = await db.get(AssignmentRun, run_id)
            assignment = await db.get(Assignment, assignment_id)
        assert mark_company_managed_run(assignment, run) is True
        return await read()

    return await _execute()


async def _no_account_reads(world: World, session_factory) -> dict[str, set[str]]:
    """The kinds each platform-wide lookup gives a caller with no account
    (``None``): the template reads."""
    _, template_tools = templates.build_template_search_tools(None, "")
    missing = uuid.uuid4()
    on_missing = _masked(await _run(template_tools, "get_template", template_id=str(missing)), missing)
    readable_templates = set()
    for kind, template in world.templates.items():
        answer = _masked(
            await _run(template_tools, "get_template", template_id=str(template.id)), template.id
        )
        if answer != on_missing:
            readable_templates.add(kind)

    async with session_factory() as db:
        themed = {
            kind
            for kind, template in world.templates.items()
            if await presentations._load_visible_template(db, template.id, user_id=None)
        }
    return {"templates": readable_templates, "template themes": themed}


async def _configured_reads(world: World) -> dict[str, set[str]]:
    """The kinds a caller with no account reaches through the agent's own
    configuration: runners, work-item peers, register owners."""
    return {
        "runners": await _listed_runners(world, None),
        "peer agents": await _peer_kinds(world, None),
        "owner names": await _owner_kinds(world, None),
    }


async def test_an_ownerless_company_managed_run_keeps_its_restricted_templates(use_db):
    async with _temp_database("anonbt_fleet") as session_factory:
        use_db(session_factory)
        world = await _seed(session_factory)
        assignment_id, run_id = await _ownerless_company_run(session_factory, world)

        inside = await _as_that_run(
            session_factory, assignment_id, run_id, lambda: _no_account_reads(world, session_factory)
        )
        assert inside == {read: COMPANY_RUN for read in inside}

        # The same reads with no run in scope: the caller with no account.
        outside = await _no_account_reads(world, session_factory)
        assert outside == {read: {"open"} for read in outside}

        # What the agent's own configuration names is the same in and out of it.
        configured_inside = await _as_that_run(
            session_factory, assignment_id, run_id, lambda: _configured_reads(world)
        )
        assert configured_inside == await _configured_reads(world)
        assert configured_inside == {read: CONFIGURED_REACH for read in configured_inside}


async def test_an_ownerless_company_managed_run_delegates_as_on_main(use_db):
    """``delegate_to_assignment`` read the company-wide tier on main already:
    a restricted assignment still answers like an unknown one."""
    async with _temp_database("anonbt_fleetdel") as session_factory:
        use_db(session_factory)
        world = await _seed(session_factory)
        assignment_id, run_id = await _ownerless_company_run(session_factory, world)

        async def _read():
            missing = uuid.uuid4()
            on_missing = await work_item_tools._run_delegate_to_assignment(
                assignment_id=str(missing), task="t", acting_user_id=None, delegating_agent_id=None
            )
            restricted = world.assignments["restricted"]
            answer = await work_item_tools._run_delegate_to_assignment(
                assignment_id=str(restricted.id), task="t", acting_user_id=None, delegating_agent_id=None
            )
            return _masked(answer, restricted.id), _masked(on_missing, missing)

        answer, on_missing = await _as_that_run(session_factory, assignment_id, run_id, _read)
        assert answer == on_missing


async def test_the_security_audit_lists_exactly_the_ownerless_company_managed_assignments(use_db):
    from app.api.v1.security_audit import _check_ownerless_company_managed_assignments

    async with _temp_database("anonbt_audit") as session_factory:
        use_db(session_factory)
        world = await _seed(session_factory)
        agent = world.agents["open"]

        def assignment(label: str, **fields) -> Assignment:
            values = {
                "agent_id": agent.id,
                "user_id": None,
                "run_as_user_id": None,
                "is_company_managed": True,
                "name": f"audit-{label}-{uuid.uuid4().hex[:6]}",
                "trigger_type": "schedule",
                "trigger_config": {},
                "prompt": "p",
                "enabled": True,
            }
            values.update(fields)
            return Assignment(**values)

        async with session_factory() as db:
            rows = {
                "scheduled": assignment("scheduled"),
                "webhook": assignment("webhook", trigger_type="webhook"),
                "event": assignment("event", trigger_type="integration_event"),
                "manual": assignment("manual", trigger_type="manual"),
                "run-as": assignment("run-as", run_as_user_id=world.users["audience"].id),
                "disabled": assignment("disabled", enabled=False),
                "personal": assignment(
                    "personal", user_id=world.users["owner"].id, is_company_managed=False
                ),
            }
            db.add_all(rows.values())
            await db.commit()

            findings: list = []
            await _check_ownerless_company_managed_assignments(db, findings)

        assert len(findings) == 1, findings
        finding = findings[0]
        assert (finding.id, finding.severity) == ("access.ownerless_assignments_restricted_reach", "info")
        listed = {label for label, row in rows.items() if str(row.id) in finding.description}
        assert listed == {"scheduled", "webhook", "event"}
        assert "run as" in (finding.recommendation or "")


async def test_the_security_audit_is_silent_without_a_restricted_resource(use_db):
    from sqlalchemy import update

    from app.api.v1.security_audit import (
        _RESTRICTABLE_MODELS,
        _check_ownerless_company_managed_assignments,
    )

    async with _temp_database("anonbt_audit0") as session_factory:
        use_db(session_factory)
        world = await _seed(session_factory)
        await _ownerless_company_run(session_factory, world)
        async with session_factory() as db:
            for model in _RESTRICTABLE_MODELS:
                await db.execute(update(model).values(access_restricted=False))
            await db.commit()

            findings: list = []
            await _check_ownerless_company_managed_assignments(db, findings)

        assert findings == []


# ---------------------------------------------------------------------------
# The global MCP endpoint's platform key and owner-less evaluations keep main's tier
# ---------------------------------------------------------------------------

GLOBAL_KEY = "mcp-" + "k" * 40


def _allow_agentic_ai(monkeypatch) -> None:
    async def _allowed(*_args, **_kwargs):
        return True

    monkeypatch.setattr("app.services.ai_state.is_ai_enabled", _allowed)
    monkeypatch.setattr("app.api.v1.platform.is_chat_licensed", _allowed)


def _chat_that_reads(monkeypatch, world: World, session_factory, seen: list) -> None:
    """Stand in for the agentic loop: run the no-account reads the agent's
    tools would, in whatever provenance the entry point opened."""
    from app.api.v1.helpers import no_account_provenance

    async def _chat(*_args, **_kwargs):
        seen.append((no_account_provenance(), await _no_account_reads(world, session_factory)))
        return "done", None

    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", _chat)


def _mcp_request(token: str, tool: str):
    from starlette.requests import Request

    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": {"message": "hi"}},
        }
    ).encode()

    async def _receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/mcp/global",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
            "query_string": b"",
        },
        _receive,
    )


async def _expose_on_the_global_endpoint(session_factory, agent: Agent) -> None:
    import bcrypt

    from app.api.v1.platform_settings_helpers import get_platform_settings

    async with session_factory() as db:
        ps = await get_platform_settings(db)
        ps.mcp_global_api_key_hash = bcrypt.hashpw(GLOBAL_KEY.encode(), bcrypt.gensalt()).decode()
        ps.mcp_exposed_agent_ids = [str(agent.id)]
        await db.commit()


async def test_the_global_mcp_platform_key_keeps_mains_tier_and_oauth_does_not(use_db, monkeypatch):
    from app.api.v1 import mcp_endpoint
    from app.api.v1.helpers import MCP_GLOBAL_KEY

    async with _temp_database("anonbt_mcpkey") as session_factory:
        use_db(session_factory)
        world = await _seed(session_factory)
        agent = world.agents["open"]
        await _expose_on_the_global_endpoint(session_factory, agent)
        _allow_agentic_ai(monkeypatch)
        seen: list = []
        _chat_that_reads(monkeypatch, world, session_factory, seen)

        async with session_factory() as db:
            await mcp_endpoint.mcp_global_endpoint(_mcp_request(GLOBAL_KEY, agent.slug), db=db)
        assert seen == [(MCP_GLOBAL_KEY, {read: COMPANY_RUN for read in seen[0][1]})]

        # A wrong key never reaches the agent.
        seen.clear()
        async with session_factory() as db:
            with pytest.raises(HTTPException) as refused:
                await mcp_endpoint.mcp_global_endpoint(_mcp_request("mcp-" + "x" * 40, agent.slug), db=db)
        assert refused.value.status_code == 401 and seen == []

        # An OAuth caller of the same endpoint is a user: the scope is not opened.
        owner = world.users["owner"]

        async def _oauth(*_args, **_kwargs):
            return owner

        monkeypatch.setattr(mcp_endpoint, "try_oauth_token", _oauth)
        async with session_factory() as db:
            await mcp_endpoint.mcp_global_endpoint(_mcp_request("an-oauth-token", agent.slug), db=db)
        assert seen == [(None, {read: {"open"} for read in seen[0][1]})]


async def test_the_per_user_mcp_endpoint_does_not_keep_mains_tier(use_db, monkeypatch):
    from app.api.v1 import mcp_endpoint

    async with _temp_database("anonbt_mcpuser") as session_factory:
        use_db(session_factory)
        world = await _seed(session_factory)
        _allow_agentic_ai(monkeypatch)
        seen: list = []
        _chat_that_reads(monkeypatch, world, session_factory, seen)
        owner = world.users["owner"]

        async def _authenticated(*_args, **_kwargs):
            return owner

        async def _agents(*_args, **_kwargs):
            return [world.agents["open"]]

        monkeypatch.setattr(mcp_endpoint, "authenticate_user", _authenticated)
        monkeypatch.setattr(mcp_endpoint, "_get_user_agents", _agents)
        async with session_factory() as db:
            await mcp_endpoint.mcp_user_endpoint(
                _mcp_request("fcp-token", world.agents["open"].slug), db=db
            )
        assert seen == [(None, {read: {"open"} for read in seen[0][1]})]


async def _evaluation(session_factory, *, owner, execution_mode="full_loop"):
    from app.models.evaluation import Evaluation

    async with session_factory() as db:
        row = Evaluation(
            user_id=owner.id if owner is not None else None,
            name=f"eval-{uuid.uuid4().hex[:8]}",
            execution_mode=execution_mode,
            model_ids=[],
            trigger_config={},
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row


async def test_an_ownerless_evaluation_keeps_mains_tier_and_an_owned_one_does_not(use_db, monkeypatch):
    from app.api.v1.helpers import OWNERLESS_EVALUATION
    from app.services import evaluation_engine

    async with _temp_database("anonbt_eval") as session_factory:
        use_db(session_factory)
        world = await _seed(session_factory)
        seen: list = []
        _chat_that_reads(monkeypatch, world, session_factory, seen)
        agent = world.agents["open"]

        for owner in (None, world.users["owner"]):
            evaluation = await _evaluation(session_factory, owner=owner)
            async with session_factory() as db:
                await evaluation_engine._execute_agent_full_loop(
                    db, agent, "model-x", "prompt", evaluation=evaluation
                )

        ownerless, owned = seen
        assert ownerless == (OWNERLESS_EVALUATION, {read: COMPANY_RUN for read in ownerless[1]})
        assert owned == (None, {read: {"open"} for read in owned[1]})


async def test_the_security_audit_lists_the_platform_key_agents_and_ownerless_evaluations(use_db):
    from app.api.v1.security_audit import (
        _check_global_mcp_key_reach,
        _check_ownerless_evaluations,
    )

    async with _temp_database("anonbt_audit2") as session_factory:
        use_db(session_factory)
        world = await _seed(session_factory)

        async with session_factory() as db:
            findings: list = []
            await _check_global_mcp_key_reach(db, findings)
            await _check_ownerless_evaluations(db, findings)
        assert findings == []  # no key, no evaluation yet

        await _expose_on_the_global_endpoint(session_factory, world.agents["open"])
        evaluations = {
            "ownerless full loop": await _evaluation(session_factory, owner=None),
            "ownerless llm only": await _evaluation(session_factory, owner=None, execution_mode="llm_only"),
            "owned full loop": await _evaluation(session_factory, owner=world.users["owner"]),
        }
        async with session_factory() as db:
            findings = []
            await _check_global_mcp_key_reach(db, findings)
            await _check_ownerless_evaluations(db, findings)

        by_id = {f.id: f for f in findings}
        assert set(by_id) == {
            "access.global_mcp_key_restricted_reach",
            "access.ownerless_evaluations_restricted_reach",
        }
        assert all(f.severity == "info" and f.category == "access" for f in findings)
        mcp = by_id["access.global_mcp_key_restricted_reach"]
        assert {k for k, a in world.agents.items() if str(a.id) in mcp.description} == {"open"}
        evals = by_id["access.ownerless_evaluations_restricted_reach"]
        assert {k for k, e in evaluations.items() if str(e.id) in evals.description} == {
            "ownerless full loop"
        }

