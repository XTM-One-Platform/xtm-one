"""Organizations (#9157, slice M1): the tracked contract, the tool argument, the API wiring.

No database here (the migration is covered by the migration suites): the
rules of a tracked profile, what an agent reads for each value of the
``organization`` argument, and that an existing reader still reads the
primary with no argument at all.
"""

from __future__ import annotations

import ast
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import organization_context as oc
from app.services import organizations as orgs
from app.services.builtin_tools import ORGANIZATION_CONTEXT_TOOL, build_organization_context_tool
from app.tools.base import ToolCall

APP_DIR = Path(__file__).resolve().parents[1] / "app"


def _org(role: str = "tracked", name: str = "Acme Cloud", relationship: str | None = "provider"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        role=role,
        relationship=None if role == "primary" else relationship,
        criticality="high",
        monitoring="off",
        archived_at=None,
    )


def _row(profile: dict, version: int = 2):
    return SimpleNamespace(
        organization_id=None,  # no asset records read: the session is a stub
        version=version,
        profile=profile,
        source="form",
        created_at=datetime(2026, 11, 6, 11, 0, tzinfo=UTC),
    )


def test_a_tracked_profile_is_normalized_and_its_missing_fields_named():
    profile = orgs.parse_tracked_profile(
        {"sector": "cloud", "regions": ["fr", "FR", "de"], "services": [{"name": "Hosting", "crown_jewels": ["ERP"]}]}
    )
    assert profile.regions == ["FR", "DE"]
    assert profile.services[0].direction == "supplies"
    assert orgs.tracked_missing_fields(profile) == ["contacts_owner"]
    assert orgs.tracked_missing_fields(orgs.parse_tracked_profile({})) == [
        path for path, _ in orgs.TRACKED_CONTRACT_FIELDS
    ]


@pytest.mark.parametrize(
    ("raw", "where"),
    [
        ({"regions": ["France"]}, "regions"),
        ({"crown_jewels": [{"name": "x"}]}, "crown_jewels"),
        ({"services": [{"name": "x", "direction": "both"}]}, "services"),
    ],
)
def test_a_tracked_profile_refuses_a_bad_or_primary_only_field(raw, where):
    with pytest.raises(ValueError, match=where):
        orgs.parse_tracked_profile(raw)


def test_the_contract_follows_the_role():
    primary, tracked = _org("primary", "Us"), _org()
    assert isinstance(orgs.parse_for(primary, {}), oc.OrganizationProfile)
    assert isinstance(orgs.parse_for(tracked, {}), orgs.TrackedOrganizationProfile)
    assert orgs.missing_for(primary, orgs.parse_for(primary, {}))[0] == "name"
    assert orgs.missing_for(tracked, orgs.parse_for(tracked, {}))[0] == "sector"


@pytest.mark.parametrize(("field", "value"), [("relationship", "supplier"), ("criticality", "extreme")])
async def test_a_tracked_organization_refuses_an_unknown_value(field, value):
    kwargs = {"name": "Acme", "relationship": "provider", "criticality": "medium", field: value}
    with pytest.raises(ValueError, match=field):
        await orgs.create_tracked(None, **kwargs)  # type: ignore[arg-type]


async def test_a_tracked_organization_needs_a_name():
    with pytest.raises(ValueError, match="name"):
        await orgs.create_tracked(None, name="  ", relationship="provider")  # type: ignore[arg-type]


def test_the_primary_cannot_be_archived_and_a_tracked_one_is_kept_dated():
    with pytest.raises(ValueError, match="primary"):
        orgs.archive(_org("primary"))  # type: ignore[arg-type]
    tracked = _org()
    orgs.archive(tracked)  # type: ignore[arg-type]
    assert tracked.archived_at is not None


def test_the_tracked_list_names_each_one_and_monitoring_off():
    text = orgs.render_tracked_list([_org("primary", "Us"), _org()])  # type: ignore[list-item]
    assert "Acme Cloud" in text and "provider" in text and "monitoring off" in text
    assert "Us" not in text.split("\n", 1)[1]
    assert "No tracked organization" in orgs.render_tracked_list([_org("primary", "Us")])  # type: ignore[list-item]


def test_an_agent_reads_a_tracked_profile_quarantined_and_pointed_at_the_primary_loss():
    text = orgs.render_tracked_for_agent(_org(), _row({"sector": "cloud"}))  # type: ignore[arg-type]
    assert "provider of the primary organization" in text
    assert "UNTRUSTED" in text and "cloud" in text
    assert "Fields not filled: regions of operation" in text
    assert "primary organization's" in text
    assert "No profile is saved yet" in orgs.render_tracked_for_agent(_org(), None)  # type: ignore[arg-type]


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def tool(monkeypatch):
    import app.core.database as database

    monkeypatch.setattr(database, "async_session", lambda: _Session())
    _, executor = build_organization_context_tool()

    async def call(**arguments):
        result = await executor.execute_tool(ToolCall(id="1", name=ORGANIZATION_CONTEXT_TOOL, arguments=arguments))
        return str(result.content)

    return call


async def test_no_argument_reads_the_primary_as_before(monkeypatch, tool):
    seen: list[object] = []

    async def _current(_db, organization_id=None):
        seen.append(organization_id)
        return _row({"name": "Us"}, version=5)

    monkeypatch.setattr(oc, "current_version", _current)
    assert "version 5" in await tool()
    assert seen == [None]


async def test_star_lists_the_tracked_organizations(monkeypatch, tool):
    async def _list(_db, **_kw):
        return [_org("primary", "Us"), _org()]

    monkeypatch.setattr(orgs, "list_organizations", _list)
    assert "Acme Cloud" in await tool(organization="*")


async def test_a_name_reads_that_tracked_profile(monkeypatch, tool):
    tracked = _org()

    async def _resolve(_db, ref):
        return tracked if ref == "acme cloud" else None

    async def _current(_db, organization_id=None):
        assert organization_id == tracked.id
        return _row({"sector": "cloud"})

    monkeypatch.setattr(orgs, "resolve", _resolve)
    monkeypatch.setattr(oc, "current_version", _current)
    text = await tool(organization="acme cloud")
    assert "Tracked organization Acme Cloud" in text
    assert "No active organization" in await tool(organization="nobody")


_STACK = [{"product": f"Product {i}", "vendor": "Microsoft" if i % 10 == 0 else "Other"} for i in range(500)]


@pytest.fixture
def with_stack(monkeypatch):
    async def _current(_db, organization_id=None):
        return _row({"name": "Us", "tech_stack": _STACK, "crown_jewels": [{"name": "ERP", "criticality": "high"}]})

    async def _list(_db, **_kw):
        return [_org("primary", "Us"), _org()]

    monkeypatch.setattr(oc, "current_version", _current)
    monkeypatch.setattr(orgs, "list_organizations", _list)


async def test_category_star_lists_every_category_with_its_size(with_stack, tool):
    from app.services.organization_context_categories import CATEGORY_NAMES

    text = await tool(category="*")
    for name in CATEGORY_NAMES:
        assert f"- {name} (" in text
    assert "): 500 rows" in text and "tracked organizations): 1 row" in text


async def test_a_full_500_row_software_list_is_read_whole_page_by_page(with_stack: None, tool: Any) -> None:
    seen: list[str] = []
    offset = 0
    for _ in range(10):
        page = await tool(category="software", offset=offset, limit=200)
        seen += [f"Product {i}" for i in range(500) if f'"Product {i}"' in page]
        if "End of the category." in page:
            break
        assert "offset=" in page
        offset += 200
    assert seen == [f"Product {i}" for i in range(500)], "every row once, in order"
    assert "UNTRUSTED" in page


async def test_a_query_narrows_a_category_and_the_limit_is_bounded(with_stack, tool):
    page = await tool(category="tech_stack", query="microsoft", limit=1000)
    assert "of 50 matching 'microsoft'" in page
    assert "End of the category." in page, "50 rows fit the 200-row page cap"
    assert '"Other"' not in page


async def test_the_deprecated_tech_stack_category_answers_the_software_category(with_stack: None, tool: Any) -> None:
    page = await tool(category="tech_stack", query="microsoft", limit=10)
    assert page.startswith("Category software ("), "#1495: tech_stack is an alias for one release"
    assert "of 50 matching 'microsoft'" in page


async def test_an_unknown_category_names_the_accepted_ones(with_stack, tool):
    text = await tool(category="vendors")
    assert "No category 'vendors'" in text and "software" in text


async def test_the_tracked_organizations_category_lists_them(with_stack, tool):
    text = await tool(category="tracked_organizations")
    assert "Acme Cloud" in text and "rows 1-1 of 1" in text


def test_the_tool_schema_takes_optional_organization_and_category_arguments():
    definitions, _ = build_organization_context_tool()
    schema = definitions[0].input_schema
    assert set(schema["properties"]) == {"organization", "category", "query", "offset", "limit"}
    assert schema["required"] == []


def test_the_router_is_mounted():
    source = (APP_DIR / "main.py").read_text(encoding="utf-8")
    assert 'prefix="/api/v1/organizations"' in source
    tree = ast.parse((APP_DIR / "api" / "v1" / "organizations.py").read_text(encoding="utf-8"))
    routes = {n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}
    assert {"list_organizations", "create_organization", "archive_organization", "save_organization_profile"} <= routes
