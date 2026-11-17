"""Admin Objects browser (``/api/v1/admin/objects``) — the one place an admin
finds a colleague's PRIVATE object.

Every library page (agents, prompts, presentations, templates, ...) lists only
what the caller owns, is shared with, or the company published — admins
included — so a type the browser does not union is a type an admin cannot
reach at all. These tests pin the union's coverage and its frontend mirror.

Enrolled machines are the same story: the XTM One Desktop page lists a
PER-USER fleet, and that fix deferred cross-user fleet oversight to
this browser — which is what makes the ``runner`` arm mandatory, together with
the machine details (platform, live status, last seen, build) that say
whether a machine is actually deployed rather than merely a row.

Flows, datasets, evaluations, variables and channel bots joined when their
library lists became three-tier for admins too. A type whose pages the
deployment does not offer is not offered here either.

The work and the places it lives joined with the platform insights tools,
which count and list the same union: assignments, runs,
conversations, reports, files, workspaces, worksets, work items, flow runs and
browser profiles. The types whose tables grow with usage are listed when their
type is picked, never in the unfiltered page.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.api.v1 import admin_objects
from app.core.config import settings
from app.services import runners as runner_service

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = BACKEND_ROOT.parent / "frontend"

# The types an admin must be able to find here because their library LIST is
# three-tier for admins too — and which had no entry at all before.
REQUIRED_TYPES = {
    "presentation",
    "template_container",
    "integration",
    "runner",
    "flow",
    "dataset",
    "evaluation",
    "variable",
    "channel_bot",
    "video",
}

# The insight arms: what an admin asks about once configuration is found.
INSIGHT_TYPES = {
    "assignment",
    "assignment_run",
    "conversation",
    "report",
    "file",
    "workspace",
    "workset",
    "work_item",
    "flow_run",
    "browser_profile",
    "fleet",
}

# Listed when their type is picked: their tables grow with usage.
NOT_LISTED_BY_DEFAULT = {"assignment_run", "conversation", "file", "work_item", "flow_run"}

# Types with no page an admin can open another person's object on: their rows
# are listed for oversight and open nothing (``route: null``).
WITHOUT_ADMIN_PAGE = {"conversation", "file", "workspace", "workset", "work_item", "flow_run", "browser_profile"}


def _default_arms() -> list:
    return [arm for arm in admin_objects.active_entity_arms().values() if arm.listed_by_default]


def _offer_every_type(monkeypatch):
    """Turn on every switch an arm follows, so the whole map is in the union."""
    monkeypatch.setattr(settings, "runners_enabled", True)
    monkeypatch.setattr(settings, "disable_custom_variables", False)
    monkeypatch.setattr(settings, "platform_mode", "copilot")
    monkeypatch.setattr(admin_objects, "is_feature_enabled", lambda flag: True)


def _compiled(query) -> str:
    return str(
        query.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


class _Result:
    def __init__(self, *, scalar=None, rows=None):
        self._scalar = scalar
        self._rows = rows or []

    def scalar(self):
        return self._scalar

    def all(self):
        return self._rows


class _FakeSession:
    """Records every statement; answers the count with 0 and the page with []."""

    def __init__(self):
        self.statements: list = []

    async def execute(self, statement, *a, **k):
        self.statements.append(statement)
        return _Result(scalar=0) if len(self.statements) == 1 else _Result(rows=[])


def _list(**params):
    session = _FakeSession()
    response = asyncio.run(
        admin_objects.list_platform_objects(
            admin=SimpleNamespace(id=uuid.uuid4(), is_admin=True),
            db=session,
            search=params.get("search"),
            object_type=params.get("object_type"),
            user_id=params.get("user_id"),
            status=params.get("status"),
            is_company_managed=params.get("is_company_managed"),
            sort=params.get("sort", "created_at"),
            direction=params.get("direction", "desc"),
            limit=params.get("limit", 50),
            offset=params.get("offset", 0),
        )
    )
    return response, session


def test_entity_map_covers_every_type_whose_library_list_is_three_tier():
    assert set(admin_objects.ENTITY_MAP) >= REQUIRED_TYPES | INSIGHT_TYPES
    for key, arm in admin_objects.ENTITY_MAP.items():
        assert arm.label == key, f"{key}: the object_type label is the filter value"
        for column in ("id", "user_id", "created_at"):
            assert hasattr(arm.model, column), f"{key}: {arm.model.__name__} has no {column!r} column"
        assert arm.name is not None or hasattr(arm.model, arm.name_column), f"{key}: no name"
        assert arm.updated is not None or hasattr(arm.model, "updated_at"), f"{key}: no updated_at"
    assert {key for key, arm in admin_objects.ENTITY_MAP.items() if not arm.listed_by_default} == NOT_LISTED_BY_DEFAULT


def test_every_arm_selects_the_unified_columns_in_one_order():
    """The arms whose tables name a column otherwise (a run named after its
    assignment, a conversation without a company-managed flag, a file without
    ``updated_at``) still select the same positions, typed alike."""
    expected = ["id", "name", "object_type", "user_id", "is_company_managed", "created_at", "updated_at",
                *admin_objects.DETAIL_COLUMNS]
    for key, arm in admin_objects.ENTITY_MAP.items():
        select = admin_objects._entity_select(arm, now=datetime(2026, 10, 21, tzinfo=UTC))
        assert [column.name for column in select.selected_columns] == expected, key
    run_sql = _compiled(admin_objects._entity_select(admin_objects.ENTITY_MAP["assignment_run"]))
    assert "FROM assignments" in run_sql and "coalesce(" in run_sql.lower()
    conversation_sql = _compiled(admin_objects._entity_select(admin_objects.ENTITY_MAP["conversation"]))
    assert "CAST(false AS BOOLEAN) AS is_company_managed" in conversation_sql
    assert "conversations.channel AS subtype" in conversation_sql


def test_presentations_surface_their_title_as_the_row_name():
    """A deck has no ``name`` — its ``title`` is what the browser lists and searches."""
    sql = _compiled(admin_objects._entity_select(admin_objects.ENTITY_MAP["presentation"]))
    assert "presentations.title AS name" in sql
    assert "'presentation' AS object_type" in sql


def test_videos_surface_their_title_as_the_row_name():
    """A video is titled like a deck: its ``title`` is the row's name."""
    sql = _compiled(admin_objects._entity_select(admin_objects.ENTITY_MAP["video"]))
    assert "video_projects.title AS name" in sql
    assert "'video' AS object_type" in sql


@pytest.mark.usefixtures("evaluation_enabled")
def test_union_of_every_type_compiles_and_names_every_table(monkeypatch):
    _offer_every_type(monkeypatch)
    _response, session = _list()
    count_sql, page_sql = (_compiled(s) for s in session.statements)
    for table in (
        "agents",
        "knowledge_bases",
        "prompts",
        "agent_skills",
        "mcp_servers",
        "custom_tools",
        "integrations",
        "presentations",
        "template_containers",
        "runners",
        "flows",
        "datasets",
        "evaluations",
        "variables",
        "channel_bots",
        "video_projects",
        "assignments",
        "reports",
        "workspaces",
        "worksets",
        "browser_profiles",
        "fleets",
    ):
        assert f"FROM {table}" in page_sql, f"{table} missing from the union"
        assert f"FROM {table}" in count_sql
    for table in ("assignment_runs", "conversations", "file_attachments", "work_items", "flow_runs"):
        assert f"FROM {table}" not in page_sql, f"{table} is listed when its type is picked only"
    assert page_sql.count("UNION ALL") == len(_default_arms()) - 1

    _response, session = _list(object_type="conversation")
    assert "FROM conversations" in _compiled(session.statements[1])


def test_every_arm_carries_every_detail_position_typed():
    """A union needs every position typed the same way on every arm: an arm
    without a notion of ``status`` / ``subtype`` / ``last_seen_at`` / ``version``
    contributes a typed NULL, never a missing column, and the outer page query
    reads the same four positions back for the response."""
    agent_sql = _compiled(admin_objects._entity_select(admin_objects.ENTITY_MAP["agent"]))
    assert "CAST(NULL AS VARCHAR) AS status" in agent_sql
    assert "CAST(NULL AS VARCHAR) AS subtype" in agent_sql
    assert "CAST(NULL AS TIMESTAMP WITH TIME ZONE) AS last_seen_at" in agent_sql
    assert "CAST(NULL AS VARCHAR) AS version" in agent_sql

    _response, session = _list()
    page_sql = _compiled(session.statements[1])
    for column in admin_objects.DETAIL_COLUMNS:
        assert f"objects.{column}" in page_sql, f"{column} is not read back by the page query"
    # The response schema has an optional field per detail position.
    fields = admin_objects.PlatformObjectItem.model_fields
    for column in admin_objects.DETAIL_COLUMNS:
        assert column in fields and not fields[column].is_required(), column


def test_fleet_rows_are_marked_archived_and_nothing_else():
    """A fleet row opens on the fleet page and says when the fleet is
    archived: archived fleets stay objects, deleted ones are gone. A fleet
    that is not archived carries no status (never a made-up "active")."""
    sql = _compiled(admin_objects._entity_select(admin_objects.ENTITY_MAP["fleet"]))
    assert "FROM fleets" in sql
    assert "fleets.name AS name" in sql
    status = re.search(r"CASE WHEN \(?fleets\.is_archived IS true\)? THEN '(\w+)' END AS status", sql)
    assert status and status.groups() == ("archived",), sql
    assert "'active'" not in sql
    assert "fleets.is_company_managed AS is_company_managed" in sql
    assert admin_objects.ENTITY_MAP["fleet"].listed_by_default
    # No switch hides it: `query_platform_objects(object_types=["fleet"])` reads
    # the offered arms and refuses a type that is not one of them.
    assert admin_objects.ENTITY_MAP["fleet"].enabled is None
    assert "fleet" in admin_objects.active_entity_arms()


def test_runner_rows_say_what_a_machine_is_at_a_glance():
    """The runner arm is the one that fills the detail positions: the
    OS family as ``subtype``, the runner build as ``version``, ``last_seen_at``
    verbatim, and a ``status`` DERIVED from ``last_seen_at`` recency — the
    reading the Desktop page and ``GET /runners`` give — never the cached
    ``runners.status`` column, which reads "online" for a machine that died
    without disconnecting."""
    now = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)
    sql = _compiled(admin_objects._entity_select(admin_objects.ENTITY_MAP["runner"], now=now))
    assert "'runner' AS object_type" in sql
    assert "runners.name AS name" in sql
    assert "runners.platform AS subtype" in sql
    assert "runners.agent_version AS version" in sql
    assert "runners.last_seen_at AS last_seen_at" in sql
    online_since = now - timedelta(seconds=settings.runner_offline_after_seconds)
    assert re.search(
        r"CASE WHEN \(runners\.is_active IS false\) THEN 'revoked' "
        r"WHEN \(runners\.last_seen_at >= '" + re.escape(online_since.strftime("%Y-%m-%d %H:%M:%S")) + r"[^']*'\) THEN 'online' "
        r"ELSE 'offline' END AS status",
        sql,
    ), sql
    assert "runners.status" not in sql, "the cached status column is not the live reading"


def test_runner_status_expression_is_the_sql_twin_of_runner_status():
    """The SQL ``CASE`` and the Python rule decide the same three cases in the
    same order, with the same inclusive window: the boundary machine (seen
    exactly ``runner_offline_after_seconds`` ago) is online on both sides."""
    now = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)
    window = timedelta(seconds=settings.runner_offline_after_seconds)

    def python(is_active: bool, last_seen_at: datetime | None) -> str:
        return runner_service.runner_status(
            SimpleNamespace(is_active=is_active, last_seen_at=last_seen_at), now
        )

    # is_active decides first, whatever the recency says.
    assert python(False, now) == runner_service.RUNNER_STATUS_REVOKED
    # Never connected -> offline (a NULL fails the SQL comparison the same way).
    assert python(True, None) == runner_service.RUNNER_STATUS_OFFLINE
    # The window is inclusive on both sides (``<=`` in Python, ``>=`` in SQL).
    assert python(True, now - window) == runner_service.RUNNER_STATUS_ONLINE
    assert python(True, now - window - timedelta(seconds=1)) == runner_service.RUNNER_STATUS_OFFLINE

    sql = _compiled(runner_service.runner_status_expression(now))
    assert sql.index("runners.is_active IS false") < sql.index("runners.last_seen_at >="), sql
    assert (now - window).strftime("%Y-%m-%d %H:%M:%S") in sql, sql
    assert sql.endswith("ELSE 'offline' END"), sql


def test_the_online_window_is_a_bound_timezone_aware_parameter():
    """The clock enters the statement as a BOUND ``timestamptz`` parameter -
    never a string spliced into the SQL - so the database compares instants,
    not renderings, and the same statement is safe to cache and re-bind."""
    now = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)
    compiled = runner_service.runner_status_expression(now).compile(
        dialect=postgresql.dialect()
    )
    sql = str(compiled)
    assert "2026-09-25" not in sql, sql
    bound = [v for v in compiled.params.values() if isinstance(v, datetime)]
    assert bound == [now - timedelta(seconds=settings.runner_offline_after_seconds)]
    assert bound[0].tzinfo is not None and bound[0].utcoffset() == timedelta(0)
    # The page statement carries that clock the same way: one ``now`` is taken
    # per request and bound once (only the runner arm reads it), never spelled
    # into the union's SQL.
    _response, session = _list()
    page_compiled = session.statements[1].compile(dialect=postgresql.dialect())
    clocks = {v for v in page_compiled.params.values() if isinstance(v, datetime)}
    assert len(clocks) == 1, clocks
    assert not re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", str(page_compiled))


@pytest.mark.usefixtures("evaluation_enabled")
def test_runner_type_is_not_offered_when_the_subsystem_is_off(monkeypatch):
    """``RUNNERS_ENABLED=false`` hides the Desktop page and 404s every runner
    route, so the browser neither lists the machines nor offers the type: the
    arm leaves the union and ``object_type=runner`` is an unknown type (the
    whole union), never a lone read of a table whose rows could not be opened."""
    _offer_every_type(monkeypatch)
    monkeypatch.setattr(settings, "runners_enabled", False)
    assert "runner" not in admin_objects.active_entity_arms()
    assert set(admin_objects.active_entity_arms()) == set(admin_objects.ENTITY_MAP) - {"runner"}

    _response, session = _list()
    page_sql = _compiled(session.statements[1])
    assert "FROM runners" not in page_sql
    assert page_sql.count("UNION ALL") == len(_default_arms()) - 1

    _response, session = _list(object_type="runner")
    page_sql = _compiled(session.statements[1])
    assert "UNION ALL" in page_sql and "FROM runners" not in page_sql

    monkeypatch.setattr(settings, "runners_enabled", True)
    assert "runner" in admin_objects.active_entity_arms()


@pytest.mark.usefixtures("evaluation_enabled")
def test_types_follow_the_pages_this_deployment_offers(monkeypatch):
    """A row must open: flows open in the builder (``FLOW_BUILDER``), datasets
    and evaluations on the Evaluations pages (every mode where AI runs, never
    ``ai_disabled``), variables on the Variables page (``DISABLE_CUSTOM_VARIABLES``)."""
    _offer_every_type(monkeypatch)
    assert set(admin_objects.active_entity_arms()) == set(admin_objects.ENTITY_MAP)

    monkeypatch.setattr(admin_objects, "is_feature_enabled", lambda flag: flag != "FLOW_BUILDER")
    monkeypatch.setattr(settings, "platform_mode", "ai_disabled")
    monkeypatch.setattr(settings, "disable_custom_variables", True)
    hidden = set(admin_objects.ENTITY_MAP) - set(admin_objects.active_entity_arms())
    assert hidden == {"flow", "flow_run", "dataset", "evaluation", "variable"}

    _response, session = _list()
    page_sql = _compiled(session.statements[1])
    for table in ("flows", "datasets", "evaluations", "variables"):
        assert f"FROM {table}" not in page_sql, table
    assert "FROM channel_bots" in page_sql


def test_single_type_filter_reads_one_table_and_searches_the_title():
    _response, session = _list(object_type="presentation", search="Board")
    page_sql = _compiled(session.statements[1])
    assert "FROM presentations" in page_sql
    assert "UNION ALL" not in page_sql
    assert "template_containers" not in page_sql and "agents" not in page_sql
    # ``name`` in the outer query IS the deck title, so the search hits it
    # (the literal-binds compile escapes ``%`` as ``%%``).
    assert "presentations.title AS name" in page_sql
    assert re.search(r"lower\(objects\.name\) LIKE '%+board%+'", page_sql), page_sql

    _response, session = _list(object_type="template_container")
    page_sql = _compiled(session.statements[1])
    assert "FROM template_containers" in page_sql and "UNION ALL" not in page_sql

    # A single-type read of the runner arm is the fleet inventory:
    # one table, searched by the machine's name, with the machine details.
    _response, session = _list(object_type="runner", search="macbook")
    page_sql = _compiled(session.statements[1])
    assert "FROM runners" in page_sql and "UNION ALL" not in page_sql
    assert "runners.platform AS subtype" in page_sql
    assert re.search(r"lower\(objects\.name\) LIKE '%+macbook%+'", page_sql), page_sql

    # An unknown type is not a 422 here — it falls back to the whole union.
    _response, session = _list(object_type="no_such_type")
    assert "UNION ALL" in _compiled(session.statements[1])


def test_a_colleagues_private_deck_is_found_by_its_title():
    """The counterpart of the library tests: ``GET /presentations`` is
    three-tier for admins too, so THIS is where an admin finds a colleague's
    private deck. The union carries no owner / audience predicate at all - the
    only ``WHERE`` is the admin's own search, which matches the title
    literally (``_`` and ``%`` in a title are characters, not wildcards)."""
    _response, session = _list(object_type="presentation", search="Q3_board 100%")
    count_sql, page_sql = (_compiled(s) for s in session.statements)
    for sql in (count_sql, page_sql):
        # None of the three-tier predicate's columns are consulted.
        for tier_column in ("is_company_managed =", "shared_user_ids", "shared_group_ids", "access_restricted"):
            assert tier_column not in sql, sql
        assert sql.count("WHERE") == 1, sql
        # The search is the whole predicate, escaped (literal binds render a
        # ``%`` as ``%%`` and double the backslashes, hence the ``+`` runs).
        assert re.search(r"WHERE lower\(objects\.name\) LIKE '%+q3\\+_board 100\\+%+' ESCAPE '\\+'", sql), sql
    # Ties on created_at (the seeded built-ins share one to the microsecond)
    # are broken deterministically, so paging never repeats or skips a row.
    assert "ORDER BY objects.created_at DESC NULLS LAST, objects.object_type, objects.id" in page_sql, page_sql


def test_several_types_read_their_union_and_an_unoffered_one_is_ignored():
    """The Type pill sends one value or several (comma-separated): several types
    read the union of their tables only, one reads its table alone, and a type
    the deployment does not offer never widens or empties the list."""
    _response, session = _list(object_type="presentation,video")
    page_sql = _compiled(session.statements[1])
    assert "FROM presentations" in page_sql and "FROM video_projects" in page_sql
    assert page_sql.count("UNION ALL") == 1, page_sql
    assert "FROM agents" not in page_sql

    _response, session = _list(object_type="presentation,no_such_type")
    page_sql = _compiled(session.statements[1])
    assert "FROM presentations" in page_sql and "UNION ALL" not in page_sql

    # A usage type (not in the default union) is read when it is named.
    _response, session = _list(object_type="work_item")
    assert "FROM work_items" in _compiled(session.statements[1])


def test_owner_status_and_company_filters_narrow_both_the_count_and_the_page():
    owner_a, owner_b = str(uuid.uuid4()), str(uuid.uuid4())
    _response, session = _list(
        object_type="assignment",
        user_id=f"{owner_a},not-a-uuid,{owner_b}",
        status="enabled,paused",
        is_company_managed=True,
    )
    count_sql, page_sql = (_compiled(s) for s in session.statements)
    for sql in (count_sql, page_sql):
        assert f"objects.user_id IN ('{owner_a}', '{owner_b}')" in sql, sql
        assert "not-a-uuid" not in sql
        assert "objects.status IN ('enabled', 'paused')" in sql, sql
        assert "objects.is_company_managed IS true" in sql, sql

    _response, session = _list(is_company_managed=False)
    assert "objects.is_company_managed IS false" in _compiled(session.statements[0])

    # No filter: the search-free, filter-free statements carry no WHERE.
    _response, session = _list()
    assert "WHERE" not in _compiled(session.statements[0])


@pytest.mark.parametrize(
    ("sort", "direction", "expected"),
    [
        ("name", "asc", "ORDER BY lower(objects.name) ASC NULLS LAST, objects.object_type, objects.id"),
        ("updated_at", "desc", "ORDER BY objects.updated_at DESC NULLS LAST, objects.object_type, objects.id"),
        ("object_type", "asc", "ORDER BY objects.object_type ASC NULLS LAST, objects.object_type, objects.id"),
        ("status", "desc", "ORDER BY objects.status DESC NULLS LAST, objects.object_type, objects.id"),
        (
            "owner",
            "asc",
            "ORDER BY lower(coalesce(users.user_name, users.user_email)) ASC NULLS LAST, objects.object_type, objects.id",
        ),
        ("created_at", "asc", "ORDER BY objects.created_at ASC NULLS LAST, objects.object_type, objects.id"),
        # An unknown sort reads as the default order.
        ("no_such_column", "desc", "ORDER BY objects.created_at DESC NULLS LAST, objects.object_type, objects.id"),
    ],
)
def test_every_column_sorts_on_the_server_with_a_stable_tiebreak(sort, direction, expected):
    _response, session = _list(sort=sort, direction=direction)
    page_sql = _compiled(session.statements[1])
    assert expected in page_sql, page_sql


def test_a_member_is_refused_by_the_route_before_any_query():
    """``require_admin`` is what makes a browser with no tier predicate safe to
    offer: a non-admin is refused by the dependency itself, and the database is
    never asked anything."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import deps
    from app.core.database import get_db

    session = _FakeSession()
    app = FastAPI()
    app.include_router(admin_objects.router, prefix="/api/v1/admin/objects")
    app.dependency_overrides[deps.get_current_user] = lambda: SimpleNamespace(
        id=uuid.uuid4(), is_admin=False, role="user"
    )
    app.dependency_overrides[get_db] = lambda: session

    response = TestClient(app).get("/api/v1/admin/objects", params={"object_type": "presentation", "search": "Q3"})

    assert response.status_code == 403
    assert response.json()["detail"] == "Admin privileges required"
    assert session.statements == []


def test_frontend_mirrors_every_entity_type():
    """The browser renders one row per ``ENTITY_MAP`` key: the TypeScript union,
    the icon/route config and the type dropdown must all know every key, and
    every route must be a detail page the app actually mounts."""
    types_source = (FRONTEND_ROOT / "src/types/index.ts").read_text(encoding="utf-8")
    union = types_source.split("export type PlatformObjectType =", 1)[1].split(";", 1)[0]
    page_source = (FRONTEND_ROOT / "src/pages/AdminObjects.tsx").read_text(encoding="utf-8")
    config_block = page_source.split("const OBJECT_TYPE_CONFIG", 1)[1].split("};", 1)[0]
    dropdown_block = page_source.split("const OBJECT_TYPES", 1)[1].split("];", 1)[0]
    app_source = (FRONTEND_ROOT / "src/App.tsx").read_text(encoding="utf-8")

    for key in admin_objects.ENTITY_MAP:
        assert f'"{key}"' in union, f"{key} missing from PlatformObjectType"
        entry = re.search(rf"^\s*{key}: \{{[^\n]*", config_block, re.MULTILINE)
        assert entry, f"{key} missing from OBJECT_TYPE_CONFIG"
        if key in WITHOUT_ADMIN_PAGE:
            assert "route: null" in entry.group(0), f"{key} has no admin page: its route is null"
        assert f'value: "{key}"' in dropdown_block, f"{key} missing from the type dropdown"

    routes = re.findall(r'route: "(/[^"]+)"', config_block)
    assert len(routes) == len(admin_objects.ENTITY_MAP) - len(WITHOUT_ADMIN_PAGE), "every other type opens a page"
    pages_source = "\n".join(
        path.read_text(encoding="utf-8") for path in (FRONTEND_ROOT / "src/pages").rglob("*.tsx")
    )
    for route in routes:
        path, _, query = route.partition("?")
        assert ":id" in route, f"{route} does not name the object it opens"
        if ":id" in path:
            # The mounted route may name its parameter (`/fleets/:fleetId`).
            before = path.split(":id", 1)[0]
            assert re.search(rf'path="{re.escape(before)}:\w+', app_source), f"{route} is not a mounted detail route"
        else:
            # An object configured on a tab of a page (channel bots, on the
            # Settings tab) is addressed by a query parameter that page reads.
            # The route is either `<page>/<tab>` (mounted as `<page>/:tab?`)
            # or `<page>/<tab>/<section>` (mounted as `<page>/:tab?/:section?`).
            page = path.rsplit("/", 1)[0]
            parent = page.rsplit("/", 1)[0]
            assert f'path="{page}/:tab?' in app_source or f'path="{parent}/:tab?/:section?' in app_source, (
                f"{route} is not a tab of a mounted page"
            )
            param = query.split("=", 1)[0]
            assert f'searchParams.get("{param}")' in pages_source, f"no page reads ?{param}="

    # The per-type detail positions the union carries are optional fields of
    # the TypeScript item too, so the runner row can render them.
    item = types_source.split("export interface PlatformObjectItem {", 1)[1].split("}", 1)[0]
    for column in admin_objects.DETAIL_COLUMNS:
        assert re.search(rf"^\s*{column}\??: ", item, re.MULTILINE), f"{column} missing from PlatformObjectItem"


def test_the_browser_polls_only_while_a_machine_row_is_loaded():
    """The machine status is the browser's one LIVE field: the backend reads
    it off ``last_seen_at`` at request time, so the page must refetch - on a
    shared ``REFETCH_INTERVAL_`` cadence, never a literal - and only while the
    loaded page carries a ``runner`` row (a page of agents and prompts has
    nothing that moves, and a deployment without runners never gets one)."""
    hook_source = (FRONTEND_ROOT / "src/hooks/api/useMiscApi.ts").read_text(encoding="utf-8")
    hook = hook_source.split("export function useAdminObjects", 1)[1].split("\n}\n", 1)[0]
    interval = re.search(r"refetchInterval:\s*\(query\)\s*=>\s*(.+)", hook, re.DOTALL)
    assert interval, "useAdminObjects has no data-driven refetchInterval"
    body = interval.group(1)
    assert 'item.object_type === "runner"' in body
    assert re.search(r"\bREFETCH_INTERVAL_(SLOW|STANDARD|FAST)\b", body)
    assert not re.search(r"\b\d{4,}\b", body), "the interval is a shared constant, not a literal"
    assert ": false" in body, "the poll stops when no machine row is on the page"


def test_new_types_are_translated_in_every_catalog():
    """The dropdown and the row badge go through ``t_i18n``: a label with no
    catalog entry falls back to its English id in every other language."""
    page_source = (FRONTEND_ROOT / "src/pages/AdminObjects.tsx").read_text(encoding="utf-8")
    labels = set(re.findall(r'label: "([^"]+)"', page_source))
    assert labels >= {
        "Presentation",
        "Presentations",
        "Template",
        "Templates",
        "Integration",
        "Integrations",
        "Flow",
        "Variable",
        "Channel Bot",
        "Channel Bots",
    }
    for lang in ("en", "fr", "es", "ja"):
        catalog = (FRONTEND_ROOT / f"src/i18n/lang/{lang}.json").read_text(encoding="utf-8")
        for label in labels:
            assert f'\n  "{label}": ' in catalog, f"{lang}.json lacks {label!r}"
