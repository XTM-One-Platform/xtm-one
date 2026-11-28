"""Every route commits its request-session writes before it answers.

``get_db`` commits on dependency teardown, which FastAPI runs AFTER the
response is sent. A route that returns without committing hands its writes to
that late commit, and a client acting on the response - a page refetching the
list it just changed, the next request presenting the id or the key it was
just handed - can read the state from before the write. That bug was fixed one
route at a time four times before an audit of the
whole route table found over a hundred more.

This guard covers every HTTP route of the application instead of a list:
``tests/commit_analysis.py`` walks each one statically, through its
dependencies and every application function it hands the request session (or
an object loaded from it) to, and reports the writes a return point can still
hold. The fix it asks for is the one the routes use: ``await db.commit()``
before the response is built.

Two escape hatches, both narrow and both self-cleaning:

- ``EXEMPTIONS`` - a route whose reported path provably writes nothing the
  analysis can see (it does not track values). Each names the only writers it
  may report; any other writer fails, and an exemption no longer needed fails.
- ``REVIEWED_ESCAPES`` - the places the session leaves the analysis (a class or
  an injected callable it is handed to). Each carries why it cannot hold a
  write past the response; a new one fails until it has been looked at.
"""

from __future__ import annotations

import inspect
import re
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRoute
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.agent import Agent
from app.models.user import User
from tests.commit_analysis import Engine, analyze_route, writer_of

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# route label -> (the writers its reported pending writes may come from, why
# the path reporting them writes nothing).
EXEMPTIONS: dict[str, tuple[frozenset[str], str]] = {
    "POST /a2a/{card_token}/jsonrpc": (
        frozenset(
            {
                "app.a2a.client.delegation.mark_cancel_owed",
                "app.a2a.tasks.request_cancel",
                "app.services.delegation.update_delegation",
                "app.services.flow_events.emit_flow_event",
                "app.services.flow_runner.create_flow_run",
                "app.services.orchestration_engine.cancel_child_runs",
                "app.services.orchestration_engine.cancel_execution",
                "app.services.orchestration_engine.create_execution",
                "app.services.sub_agent.mark_sub_agent_tasks_cancelled_for_runs",
                # The field writes mark_sub_agent_tasks_cancelled_for_runs
                # delegates to (shared with the emergency stop's sweep).
                "app.services.sub_agent._mark_task_cancelled",
                # Its child run ended cancelled with the task (#2721), same path.
                "app.services.sub_agent._cancel_child_run",
            }
        ),
        "the refused cancel ('Task is already terminal'): request_cancel returns "
        "False either before writing anything or when its guarded UPDATE matched "
        "no row, and every cancel write sits on the True path, which commits",
    ),
    "GET /api/v1/organization-context/pirs": (
        frozenset({"app.integrations.providers.opencti._common._drop_dead_draft"}),
        "the PIR picker's OpenCTI read: _drop_dead_draft marks the plain headers "
        "dict _user_creds builds (setattr on a non-ORM object), never a row of "
        "the request session",
    ),
    # The draft and the rows previews plan on current_profile (#1543): a pydantic copy of the profile, never a row.
    "POST /api/v1/organization-context/draft": (
        frozenset({"app.services.organization_context_draft.merge_draft.<_fill>"}),
        "merge_draft fills a model_copy of the OrganizationProfile current_profile answers "
        "(a pydantic model, not an ORM row); the draft route saves nothing",
    ),
    "GET /api/v1/organization-context/syncs/{sync_id}/rows-preview": (
        frozenset({"app.services.organization_context_sources.plan_rows"}),
        "plan_rows edits a model_copy of the OrganizationProfile current_profile answers "
        "(a pydantic model, not an ORM row); the preview saves nothing",
    ),
    "GET /api/v1/organization-context/sources/{source}/rows-preview": (
        frozenset({"app.services.organization_context_sources.plan_rows"}),
        "plan_rows edits a model_copy of the OrganizationProfile current_profile answers "
        "(a pydantic model, not an ORM row); the preview saves nothing",
    ),
    # The import filter picker (#11886) lists the platform's values with the same read.
    "GET /api/v1/organization-context/import/options": (
        frozenset({"app.integrations.providers.opencti._common._drop_dead_draft"}),
        "the import filter options' OpenCTI read: _drop_dead_draft marks the plain "
        "headers dict _user_creds builds, never a row of the request session",
    ),
    # The Disable dialog's cascade preview (#122) counts what a deactivation would touch.
    "GET /api/v1/users/{user_id}/deactivation-preview": (
        frozenset({"app.services.user_deactivation.preview_deactivation.<count>"}),
        "preview_deactivation's count() helper executes the select(func.count(...)) queries it is "
        "given, one per cascade step; the preview writes nothing",
    ),
    # The ROI ledger's figures (#12077 L4) read the open days' cost live from llm_traces.
    "GET /api/v1/roi": (
        frozenset({"app.services.roi_ledger.cost_facts", "app.services.roi_ledger._clip_cost_rows"}),
        "cost_facts executes _TRACE_FACTS_SQL, a text() SELECT over llm_traces (the read "
        "close_cost_days writes a closed day from); the route writes nothing",
    ),
    "GET /api/v1/roi/summary": (
        frozenset({"app.services.roi_ledger.cost_facts", "app.services.roi_ledger._clip_cost_rows"}),
        "cost_facts executes _TRACE_FACTS_SQL, a text() SELECT over llm_traces; the list column writes nothing",
    ),
    # The chat totals and the fleet datasheet on the ledger's figures (#12077 L4b).
    "GET /api/v1/roi/chat": (
        frozenset({"app.services.roi_ledger.cost_facts", "app.services.roi_ledger._clip_cost_rows"}),
        "cost_facts executes _TRACE_FACTS_SQL, a text() SELECT over llm_traces; the chat series writes nothing",
    ),
    # The CSV / PDF export on the ledger's figures (#12077 L4c).
    "GET /api/v1/roi/export": (
        frozenset({"app.services.roi_ledger.cost_facts", "app.services.roi_ledger._clip_cost_rows"}),
        "cost_facts executes _TRACE_FACTS_SQL, a text() SELECT over llm_traces; the export writes nothing",
    ),
    # _clip_cost_rows (#2580) runs the same _TRACE_FACTS_SQL SELECT from a reset fleet's mark.
    "GET /api/v1/fleets/{fleet_id}/documentation/datasheet.pdf": (
        frozenset({"app.services.roi_ledger.cost_facts", "app.services.roi_ledger._clip_cost_rows"}),
        "cost_facts executes _TRACE_FACTS_SQL, a text() SELECT over llm_traces; the datasheet writes nothing",
    ),
}

# pattern of an escape site -> why the session cannot hold a write past the
# response there.
REVIEWED_ESCAPES: dict[str, str] = {
    r"db\.run_sync\(\) in app\.a2a\.hooks\.converge_exposures": (
        "the A2A exposure converge (#11786) adds exposures and notices on the caller's "
        "session; its route callers, create_integration (provision_settings) and "
        "update_integration (change_direction), commit after it before answering"
    ),
    r"passes the session to resolver\.(load|of)\(\) in app\.services\.roi_backfill\.\w+": (
        "the ROI backfill resolver (#10006) only reads declarations, agents and home fleets "
        "through it; its only route caller, GET /roi/declarations/backfill-preview, runs "
        "the pass with dry_run=True and rolls back before answering"
    ),
    r"passes the session to llm_factory\(\) in app\.services\.health\.analysis\.analyse_module": (
        "Platform Health analysis (#101 S9): the factory only resolves the routing model "
        "and its provider (reads); analyse_module commits every write it makes (the "
        "reservation before the model call, the booking and the analysis row after) "
        "before POST /health/analysis/{module}/run answers"
    ),
    r"passes the session to handler\(\) in app\.services\.health\.consent\.apply_revocation": (
        "the Concierge revocation handlers (#8254) write in the caller's transaction; "
        "the only caller, the /api/v1/concierge routes, commits right after them "
        "(_commit_with_revocation) before answering"
    ),
    r"passes the session to build_(render_html|fetch_image_asset)_tool\(\) in "
    r"app\.services\.builtin_tools\.(media|web)\.build_\w+": (
        "the tools these builders return store files through persist_tool_artifact, "
        "which never writes on the session it is given (a dedicated short-lived one)"
    ),
    r"session captured by _PriceMemo\(\) in app\.services\.tracing\.repricing\.reprice_null_costs": (
        "a memo of model prices: it only reads"
    ),
    r"session captured by DeepInvestigationOrchestrator\(\) in "
    r"app\.services\.deep_investigation_chat\.run_deep_investigation_chat": (
        "the investigation run (continuation route): commit_each_step commits "
        "every step it writes, and the run's end, before the route answers; the "
        "report is written on a session of its own"
    ),
    r"passes the session to hook\(\) in app\.integrations\.lifecycle\.dispatch_on_(connect|disconnect|update)": (
        "a provider's on_connect / on_disconnect / on_update hook; its only callers, "
        "create_integration, delete_integration and update_integration, commit after it"
    ),
    r"session captured by ProbeContext\(\) in app\.api\.v1\.integration_testing\.test_provider_connectivity": (
        "connectivity probes read the integration's own state and never write"
    ),
    r"passes the session to _module\(provider\)\.deprovision\(\) in "
    r"app\.services\.trigger_provisioning\.deprovision_assignment": (
        "a provider's deprovision (#8991) reads the agent and integration rows and "
        "removes objects in the source platform; its only caller, delete_assignment, "
        "deletes the assignment and commits right after it"
    ),
    r"passes the session to trace_store\.\w+\(\) in app\.api\.v1\.traces\.\w+": (
        "the trace store's reads; its two writes, create_trace_score and "
        "delete_trace, commit on their own (PostgreSQL) or never touch the session "
        "(ClickHouse)"
    ),
    r"passes the session to handler\(\) in app\.api\.v1\.mcp_oauth\.token_endpoint": (
        "a registered grant handler (the A2A client-credentials grant), which only reads"
    ),
    r"passes the session to generate\(\) in app\.services\.persona_improvement\.propose": (
        "the persona rewrite (generate_rewrite unless a test injects one): it reads the "
        "caller's model, then releases the connection through "
        "release_db_connection_before_loop, which commits what is open, and writes nothing"
    ),
    r"passes the session to handler\.\w+\(\) in app\.(services\.improvement_proposals\.lifecycle"
    r"|api\.v1\.improvement_proposals|services\.builtin_tools\.improvements"
    r"|services\.health\.cycle_access)\.\w+": (
        "an improvement proposal's type handler (registry): load_target, describe, "
        "validate and stale_error read; apply and rollback write the target's new "
        "version in the caller's transaction, which every route commits before it "
        "answers (approve, reject, the persona request; propose_improvement commits itself); "
        "the Concierge's Improvements inbox and companion (#8261, cycle_access.readable_proposals) "
        "only read them (load_target, lock=False)"
    ),
    r"passes the session to visibility\.visible_ids\(\) in "
    r"app\.services\.trigger_integration_access\._report": (
        "TriggerIntegrationVisibility reads the acting users, their groups and the "
        "integrations they can see; it never writes"
    ),
    r"db\.run_sync\(\) in app\.services\.resource_exposure\._proposed_a2a_exposure\.<inbound>": (
        "app.a2a.providers.inbound_integration_ids on the sync session: one SELECT of "
        "the integrations' A2A directions"
    ),
    r"session captured by AudienceResolver\(\) in app\.services\.(resource_exposure\."
    r"(preview_change|resource_exposure)|integration_exposure\.lent_personal_credentials)": (
        "the exposure resolvers work out who a credential reaches: every use of the "
        "session is a SELECT or a get"
    ),
}


def _label(route: APIRoute) -> str:
    return f"{'/'.join(sorted(route.methods))} {route.path}"


def _where(route: APIRoute) -> str:
    fn = inspect.unwrap(route.endpoint)
    try:
        line = inspect.getsourcelines(fn)[1]
    except (OSError, TypeError):
        line = 0
    return f"{fn.__module__}.{fn.__qualname__}:{line}"


#: The analysis of every route takes 20 to 30 s on CI; a module fixture is built once per worker, and
#: ``--dist loadgroup`` spread its four tests over as many workers. One group builds it once.
_ONE_WORKER = pytest.mark.xdist_group("write_routes_analysis")


@pytest.fixture(scope="module")
def route_reports():
    from app.main import application

    engine = Engine()
    return [
        (route, analyze_route(engine, route))
        for route in application.routes
        if isinstance(route, APIRoute)
    ]


@_ONE_WORKER
def test_the_analysis_sees_the_whole_route_table(route_reports):
    # An import-order change hiding the routers would make every other test
    # here pass on an empty table.
    writes = [r for r, _ in route_reports if r.methods & _WRITE_METHODS]
    reads = [r for r, _ in route_reports if not r.methods & _WRITE_METHODS]
    assert len(writes) > 350, f"only {len(writes)} write routes found"
    assert len(reads) > 250, f"only {len(reads)} read routes found"


@_ONE_WORKER
def test_every_route_commits_its_writes_before_answering(route_reports):
    failures = []
    for route, report in route_reports:
        if not report.pending:
            continue
        allowed = EXEMPTIONS.get(_label(route), (frozenset(), ""))[0]
        unexpected = sorted(d for d in report.pending if writer_of(d) not in allowed)
        if unexpected:
            failures.append(
                f"{_label(route)}  ({_where(route)})\n"
                + "\n".join(f"      {d}" for d in unexpected)
            )
    assert not failures, (
        "These routes can answer while writes made through the request session are "
        "still uncommitted. get_db commits them only after the response is sent, so "
        "a client acting on the response can read the old state. Commit before "
        "building the response (await db.commit()), keeping anything that can still "
        "fail before the commit. If the reported path provably writes nothing, add "
        "an exemption to EXEMPTIONS with the reason.\n\n  " + "\n  ".join(failures)
    )


@_ONE_WORKER
def test_every_exemption_is_still_needed(route_reports):
    by_label = {_label(route): report for route, report in route_reports}
    stale = [
        f"{label}: {'no such route' if label not in by_label else 'reports nothing any more'}"
        for label in EXEMPTIONS
        if not by_label.get(label, SimpleNamespace(pending=None)).pending
    ]
    assert not stale, "Remove these entries from EXEMPTIONS:\n  " + "\n  ".join(stale)


@_ONE_WORKER
def test_the_session_only_escapes_where_it_was_reviewed(route_reports):
    escapes = sorted({e for _, report in route_reports for e in report.escapes})
    unreviewed = [e for e in escapes if not any(re.fullmatch(p, e) for p in REVIEWED_ESCAPES)]
    assert not unreviewed, (
        "The request session reaches code the analysis cannot follow. Check that "
        "nothing written through it there can stay uncommitted when the route "
        "answers, then add the site to REVIEWED_ESCAPES with the reason:\n  "
        + "\n  ".join(unreviewed)
    )
    stale = [p for p in REVIEWED_ESCAPES if not any(re.fullmatch(p, e) for e in escapes)]
    assert not stale, "Remove these entries from REVIEWED_ESCAPES:\n  " + "\n  ".join(stale)


# ---------------------------------------------------------------------------
# The analysis itself, on small routes whose answer is known.
# ---------------------------------------------------------------------------


async def _current_user(db: AsyncSession = Depends(get_db)) -> User:
    return await db.get(User, uuid.uuid4())


async def _helper_writes(db: AsyncSession) -> None:
    db.add(User(email="helper@example.com"))


async def _helper_commits(db: AsyncSession) -> None:
    db.add(User(email="helper@example.com"))
    await db.commit()


async def _helper_writes_when_asked(db: AsyncSession, source_id: str | None = None) -> None:
    if source_id:
        db.add(User(email="helper@example.com"))


async def _outside_call() -> None:
    return None


async def _add_without_commit(db: AsyncSession = Depends(get_db)):
    db.add(User(email="a@example.com"))
    return {"ok": True}


async def _add_then_commit(db: AsyncSession = Depends(get_db)):
    db.add(User(email="a@example.com"))
    await db.commit()
    return {"ok": True}


async def _audit_after_commit(db: AsyncSession = Depends(get_db)):
    await db.commit()
    await _helper_writes(db)
    return {"ok": True}


async def _helper_leaves_write(db: AsyncSession = Depends(get_db)):
    await _helper_writes(db)
    return {"ok": True}


async def _helper_commits_its_write(db: AsyncSession = Depends(get_db)):
    await _helper_commits(db)
    return {"ok": True}


async def _helper_not_asked_to_write(db: AsyncSession = Depends(get_db)):
    await _helper_writes_when_asked(db)
    return {"ok": True}


async def _sets_a_column(user: User = Depends(_current_user), db: AsyncSession = Depends(get_db)):
    user.full_name = "Renamed"
    return {"ok": True}


async def _sets_a_transient_attribute(user: User = Depends(_current_user)):
    user.auth_credential_kind = "api_key"
    return {"ok": True}


async def _early_return_skips_commit(flag: bool = False, db: AsyncSession = Depends(get_db)):
    db.add(User(email="a@example.com"))
    if flag:
        return {"early": True}
    await db.commit()
    return {"ok": True}


async def _raises_after_writing(db: AsyncSession = Depends(get_db)):
    db.add(User(email="a@example.com"))
    raise HTTPException(status_code=409, detail="conflict")


async def _swallows_a_failure_after_writing(db: AsyncSession = Depends(get_db)):
    db.add(User(email="a@example.com"))
    try:
        await _outside_call()
    except Exception:
        return {"ok": False}
    await db.commit()
    return {"ok": True}


async def _database_failure_rolls_back(db: AsyncSession = Depends(get_db)):
    try:
        db.add(User(email="a@example.com"))
        await db.flush()
    except IntegrityError:
        await db.rollback()
        return {"duplicate": True}
    await db.commit()
    return {"ok": True}


async def _update_statement(db: AsyncSession = Depends(get_db)):
    await db.execute(update(User).values(full_name="x"))
    return {"ok": True}


async def _select_statement(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(User))).scalars().all()
    return {"count": len(rows)}


async def _guarded_update_matching_nothing(db: AsyncSession = Depends(get_db)):
    result = await db.execute(update(User).where(User.full_name == "a").values(full_name="b"))
    if not result.rowcount:
        return {"changed": False}
    await db.commit()
    return {"changed": True}


async def _guarded_update_left_uncommitted(db: AsyncSession = Depends(get_db)):
    result = await db.execute(update(User).where(User.full_name == "a").values(full_name="b"))
    if result.rowcount:
        return {"changed": True}
    return {"changed": False}


async def _executes_an_outer_statement_inside(db: AsyncSession = Depends(get_db)):
    stmt = select(User)
    stmt = stmt.where(User.full_name == "a")

    async def run(session: AsyncSession):
        return (await session.execute(stmt)).scalars().all()

    return {"count": len(await run(db))}


async def _streams_its_writes(db: AsyncSession = Depends(get_db)):
    async def body():
        db.add(User(email="a@example.com"))
        await db.commit()
        yield b"done"

    return StreamingResponse(body())


async def _mutates_a_mapped_collection(user: User = Depends(_current_user)):
    user.channel_identities.append({"channel": "slack"})
    return {"ok": True}


async def _helper_builds_an_audience(db: AsyncSession) -> SimpleNamespace:
    return SimpleNamespace(user_ids=set())


async def _mutates_a_plain_collection(db: AsyncSession = Depends(get_db)):
    audience = await _helper_builds_an_audience(db)
    audience.user_ids.add(uuid.uuid4())
    return {"ok": True}


async def _calls_a_model_method_that_writes(db: AsyncSession = Depends(get_db)):
    agent = await db.get(Agent, uuid.uuid4())
    agent.prune_orphaned_knowledge_base_permissions()
    return {"ok": True}


async def _executes_statements_collected_in_a_list(db: AsyncSession = Depends(get_db)):
    queries = []
    queries.append(select(User))
    queries.append(select(User).where(User.full_name == "a"))
    for query in queries:
        await db.execute(query)
    return {"ok": True}


async def _executes_an_update_collected_in_a_list(db: AsyncSession = Depends(get_db)):
    queries = [select(User)]
    queries.append(update(User).values(full_name="x"))
    for query in queries:
        await db.execute(query)
    return {"ok": True}


def _collect(queries: list) -> None:
    queries.append(update(User).values(full_name="x"))


async def _executes_statements_a_helper_collects(db: AsyncSession = Depends(get_db)):
    queries = []
    _collect(queries)
    for query in queries:
        await db.execute(query)
    return {"ok": True}


_KNOWN_ANSWERS = [
    (_add_without_commit, True),
    (_add_then_commit, False),
    (_audit_after_commit, True),
    (_helper_leaves_write, True),
    (_helper_commits_its_write, False),
    (_helper_not_asked_to_write, False),
    (_sets_a_column, True),
    (_sets_a_transient_attribute, False),
    (_early_return_skips_commit, True),
    (_raises_after_writing, False),
    (_swallows_a_failure_after_writing, True),
    (_database_failure_rolls_back, False),
    (_update_statement, True),
    (_select_statement, False),
    (_guarded_update_matching_nothing, False),
    (_guarded_update_left_uncommitted, True),
    (_executes_an_outer_statement_inside, False),
    (_streams_its_writes, False),
    (_mutates_a_mapped_collection, True),
    (_mutates_a_plain_collection, False),
    (_calls_a_model_method_that_writes, True),
    (_executes_statements_collected_in_a_list, False),
    (_executes_an_update_collected_in_a_list, True),
    (_executes_statements_a_helper_collects, True),
]


@pytest.mark.parametrize(("endpoint", "leaves_writes"), _KNOWN_ANSWERS, ids=lambda v: getattr(v, "__name__", str(v)))
def test_the_analysis_on_routes_with_a_known_answer(endpoint, leaves_writes):
    app = FastAPI()
    app.post("/probe")(endpoint)
    (route,) = [r for r in app.routes if isinstance(r, APIRoute)]
    report = analyze_route(Engine(own_prefixes=("app.", "tests.")), route)
    assert bool(report.pending) is leaves_writes, sorted(report.pending)


# ---------------------------------------------------------------------------
# The two routes the audit started from, driven for real.
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._value or []))


class _RecordingSession:
    """Just enough ``AsyncSession`` for the routes below, in call order."""

    def __init__(self, *results):
        self.calls: list[str] = []
        self.added: list[object] = []
        self._results = list(results)

    async def execute(self, _stmt):
        self.calls.append("execute")
        return self._results.pop(0) if self._results else _Result()

    def add(self, obj):
        self.calls.append("add")
        self.added.append(obj)

    async def flush(self):
        self.calls.append("flush")
        now = datetime.now(UTC)
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()
            obj.created_at = now
            obj.updated_at = now

    async def delete(self, _obj):
        self.calls.append("delete")

    async def commit(self):
        self.calls.append("commit")

    async def rollback(self):
        self.calls.append("rollback")


async def _record_audit(db, **_kw) -> None:
    db.calls.append("audit")


def _request():
    return SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})


def _owner():
    return SimpleNamespace(id=uuid.uuid4(), is_admin=False, role="user", full_name="Owner")


async def test_create_knowledge_base_commits_before_building_the_response(monkeypatch):
    from app.api.v1 import knowledge_bases as kb_api
    from app.api.v1.schemas.knowledge_base import CreateKnowledgeBaseRequest

    session = _RecordingSession()

    async def _no_share_fields(*_a, **_kw) -> None:
        return None

    async def _respond(kb, db, **_kw):
        db.calls.append("respond")
        return {"id": str(kb.id)}

    monkeypatch.setattr(kb_api, "apply_user_share_fields", _no_share_fields)
    monkeypatch.setattr(kb_api, "log_audit", _record_audit)
    monkeypatch.setattr(kb_api, "_kb_to_response", _respond)

    response = await kb_api.create_knowledge_base(
        body=CreateKnowledgeBaseRequest(name="Runbooks", embedding_model="text-embedding-3-large"),
        request=_request(),
        user=_owner(),
        db=session,
        _agentic_gate=None,
    )

    # The page opens the new knowledge base the moment it gets the id.
    assert session.calls == ["add", "flush", "audit", "commit", "respond"], session.calls
    assert response == {"id": str(session.added[0].id)}


async def test_delete_integration_commits_before_answering(monkeypatch):
    from app.api.v1 import integrations as integrations_api

    user = _owner()
    integration = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user.id,
        name="GitHub",
        provider="github",
        is_company_managed=False,
        is_builtin=False,
    )

    async def _no_groups(*_a, **_kw):
        return []

    async def _get_integration(*_a, **_kw):
        return integration

    async def _nothing(*_a, **_kw) -> None:
        return None

    monkeypatch.setattr(integrations_api, "get_user_group_ids", _no_groups)
    monkeypatch.setattr(integrations_api, "get_user_integration", _get_integration)
    monkeypatch.setattr(integrations_api, "can_manage", lambda *_a, **_kw: True)
    monkeypatch.setattr(integrations_api, "dispatch_on_disconnect", _nothing)
    monkeypatch.setattr(integrations_api, "_forget_oauth_share_dismissal", _nothing)
    monkeypatch.setattr(integrations_api, "log_audit", _record_audit)
    # No linked platform registration, no agent binding it.
    session = _RecordingSession(_Result(None), _Result([]))

    await integrations_api.delete_integration(
        integration_id=str(integration.id),
        request=_request(),
        user=user,
        db=session,
    )

    # The integrations list refetched right after the 204 must not hold the row.
    assert session.calls == ["audit", "execute", "execute", "delete", "commit"], session.calls
