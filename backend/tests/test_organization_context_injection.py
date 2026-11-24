"""The organization summary cascade (#11533): platform > agent > assignment.

The resolver over every combination, the summary's content and hard cap,
the assignment-run rule that keeps the fleet block and never duplicates the
profile, and that every prompt builder calls the one resolver.
"""

from __future__ import annotations

import ast
import itertools
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import organization_context as oc
from app.services import organization_context_injection as inj

APP_DIR = Path(__file__).resolve().parents[1] / "app"
MODES = ("inherit", "always", "off")
#: The reads are monkeypatched: no session is ever used.
_NO_DB = cast(AsyncSession, None)


def _row(profile: dict[str, Any] | None, version: int = 2) -> SimpleNamespace:
    return SimpleNamespace(
        organization_id=None,  # no asset records read: the session is a stub
        version=version,
        created_at=datetime(2026, 11, 13, 6, 0, tzinfo=UTC),
        source="form",
        profile=profile,
    )


PROFILE = {
    "name": "Acme Energy",
    "sector": "energy",
    "sub_sector": "electricity distribution",
    "regions": ["FR", "DE"],
    "size": "1001-5000",
    "crown_jewels": [
        {"name": "Billing", "criticality": "medium"},
        {"name": "SCADA", "criticality": "critical"},
    ],
    "pir_ids": ["pir-1"],
    "pir_names": {"pir-1": "Ransomware against utilities"},
    "risk_appetite": {"annual_loss_threshold": 2_000_000},
    "currency": "EUR",
}


def _expected(platform: bool, agent: str, assignment: str) -> bool:
    for mode in (assignment, agent):
        if mode != "inherit":
            return mode == "always"
    return platform


# -- The resolver: 3 x 3 x 2, chat path, general chat ------------------------


@pytest.mark.parametrize(
    ("platform", "agent", "assignment"),
    list(itertools.product((True, False), MODES, MODES)),
)
def test_assignment_path_most_specific_explicit_value_wins(platform: bool, agent: str, assignment: str) -> None:
    assert inj.resolve(platform_enabled=platform, agent_mode=agent, assignment_mode=assignment) is _expected(
        platform, agent, assignment
    )


@pytest.mark.parametrize(("platform", "agent"), list(itertools.product((True, False), MODES)))
def test_chat_path_resolves_agent_then_platform(platform: bool, agent: str) -> None:
    assert inj.resolve(platform_enabled=platform, agent_mode=agent) is _expected(platform, agent, "inherit")


@pytest.mark.parametrize("platform", [True, False])
def test_general_chat_follows_the_platform(platform: bool) -> None:
    assert inj.resolve(platform_enabled=platform) is platform


@pytest.mark.parametrize("value", [None, "", "INHERIT", "bogus"])
def test_an_unknown_or_empty_mode_inherits(value: str | None) -> None:
    assert inj.normalize_mode(value) == "inherit"
    assert inj.resolve(platform_enabled=True, agent_mode=value) is True
    assert inj.resolve(platform_enabled=False, agent_mode=value) is False


def test_check_mode_refuses_an_unknown_value() -> None:
    assert inj.check_mode(" Always ") == "always"
    with pytest.raises(ValueError, match="inherit, always, off"):
        inj.check_mode("sometimes")


# -- The basic block: identity + category index -------------------------------


def test_basic_block_names_the_identity_and_indexes_every_category() -> None:
    from app.services import organization_context_categories as cats

    text = inj.render_summary(_row(PROFILE), tracked_count=3)
    assert text.startswith(inj.SUMMARY_HEADER)
    assert "Acme Energy" in text and "energy / electricity distribution" in text
    assert "2e+06 EUR" in text
    assert "SCADA" not in text, "a category's rows are not in the basic block"
    for category in cats.CATEGORIES:
        if category.name in cats.OPTIONAL_CATEGORIES:
            # Named only once filled (shipped products): the block keeps its size.
            assert f"- {category.name} (" not in text
            continue
        assert f"- {category.name} (" in text, f"{category.name} missing from the index"
    assert "- primary_assets (" in text and "): 2 rows;" in text
    assert "- crown_jewels (" not in text, "#1591: the deprecated alias is never announced"
    assert ": 1 row;" in text
    assert "- tracked_organizations (tracked organizations): 3 rows" in text
    assert "- software (" in text and "): not filled" in text
    assert "- tech_stack (" not in text, "the deprecated alias is never announced"
    assert 'get_organization_context(category="primary_assets")' in text
    assert "UNTRUSTED" in text, "the profile is data a person typed: quarantined"
    assert len(text) <= inj.SUMMARY_MAX_CHARS


def test_the_index_is_generated_from_the_model() -> None:
    from typing import get_origin

    from app.services import organization_context_categories as cats

    list_fields = {
        field
        for field, info in oc.OrganizationProfile.model_fields.items()
        if get_origin(info.annotation) is list and field not in ("regions", "onboarding_skipped", "fx_rates")
    }
    # The one object field read as a category (#11543): its rules are its rows.
    assert {c.field for c in cats.CATEGORIES if c.field} == list_fields | {"prioritization_policy"}
    assert {"primary_assets", "software", "pirs", "tracked_organizations"} <= set(cats.CATEGORY_NAMES)
    assert "crown_jewels" not in cats.CATEGORY_NAMES, "#1591: a deprecated alias, never a category of its own"
    assert "tech_stack" not in cats.CATEGORY_NAMES, "#1495: a deprecated alias, never a category of its own"
    assert {"assets_exposure", "prioritization_policy", "enterprise_knowledge"} <= set(cats.CATEGORY_NAMES)


def test_no_saved_profile_means_no_summary() -> None:
    assert inj.render_summary(None) == ""


def test_the_basic_block_cap_holds_whatever_the_list_lengths() -> None:
    huge = {
        **PROFILE,
        "name": "N" * 200,
        "regions": ["FR", "DE", "US", "GB", "JP", "AU", "SG", "SA", "ES", "IT"] * 3,
        "crown_jewels": [{"name": f"Jewel {i} " + "x" * 150, "criticality": "high"} for i in range(200)],
        "tech_stack": [{"product": f"Product {i}", "vendor": "Vendor", "cpe": "cpe:2.3:a:v:p:1"} for i in range(500)],
        "pir_ids": [f"pir-{i}" for i in range(100)],
        "pir_names": {f"pir-{i}": "P" * 250 for i in range(100)},
    }
    text = inj.render_summary(_row(huge), tracked_count=10_000)
    small = inj.render_summary(_row(PROFILE))
    assert len(text) <= inj.SUMMARY_MAX_CHARS
    # About 500 tokens: 4 characters a token is the bound the cap encodes.
    assert inj.SUMMARY_MAX_CHARS // 4 <= 500
    assert "Product 499" not in text, "the tech stack stays behind the tool"
    assert "): 500 rows" in text
    assert not text.endswith("..."), "never cut: the block does not grow with a list"
    assert len(text) - len(small) < 400, "only the counts and the name grew"


# -- The category cascade -----------------------------------------------------


@pytest.mark.parametrize(
    ("platform", "agent", "assignment", "expected"),
    [
        ([], None, None, []),
        (["pirs"], None, None, ["pirs"]),
        (["pirs"], ["software"], None, ["software"]),
        (["pirs"], ["software"], [], []),
        # #1495: a stored selection naming the deprecated tech_stack reads as software.
        (["pirs"], ["tech_stack"], None, ["software"]),
        # #1591: a stored selection naming the deprecated crown_jewels reads as primary_assets.
        (["pirs"], [], ["crown_jewels"], ["primary_assets"]),
        (["crown_jewels", "primary_assets"], None, None, ["primary_assets"]),
        ([], ["tech_stack", "gone_from_the_model", "software"], None, ["software"]),
    ],
)
def test_categories_most_specific_explicit_list_wins(
    platform: list[str], agent: list[str] | None, assignment: list[str] | None, expected: list[str]
) -> None:
    from app.services.organization_context_categories import resolve_categories

    assert (
        resolve_categories(platform_categories=platform, agent_categories=agent, assignment_categories=assignment)
        == expected
    )


def test_check_categories_reads_lists_strings_and_refuses_unknown_names() -> None:
    from app.services.organization_context_categories import check_categories

    assert check_categories(None) is None
    assert check_categories("inherit") is None
    assert check_categories("none") == []
    assert check_categories("Software, primary_assets") == ["software", "primary_assets"]
    assert check_categories("crown_jewels") == ["primary_assets"], "#1591: the deprecated alias answers primary_assets"
    assert check_categories("Tech_Stack, software") == ["software"], "the deprecated alias answers software"
    with pytest.raises(ValueError, match="software"):
        check_categories(["vendors"])


@pytest.mark.parametrize(
    ("agent", "assignment", "expected"),
    [("inherit", "inherit", True), ("off", "inherit", False), ("off", "always", True), ("always", "off", False)],
)
def test_an_explicit_off_removes_the_tools(agent: str, assignment: str, expected: bool) -> None:
    assert inj.tools_enabled(agent_mode=agent, assignment_mode=assignment) is expected


def test_the_platform_switch_never_removes_the_tools() -> None:
    assert inj.tools_enabled() is True


# -- The entry points ---------------------------------------------------------


async def _with(
    monkeypatch: pytest.MonkeyPatch,
    *,
    platform: bool,
    profile: dict[str, Any] | None = PROFILE,
    categories: list[str] | None = None,
) -> None:
    async def _platform(_db: object) -> bool:
        return platform

    async def _categories(_db: object) -> list[str]:
        return list(categories or [])

    async def _current(_db: object, _organization_id: object = None) -> SimpleNamespace | None:
        return _row(profile) if profile is not None else None

    async def _tracked(_db: object) -> list[Any]:
        return []

    monkeypatch.setattr(inj, "platform_enabled", _platform)
    monkeypatch.setattr(inj, "platform_categories", _categories)
    monkeypatch.setattr(inj, "_tracked", _tracked)
    monkeypatch.setattr(oc, "current_version", _current)


BIG_STACK = {
    **PROFILE,
    "tech_stack": [{"product": f"Product {i}", "vendor": f"Vendor {i % 7}"} for i in range(500)],
}


async def test_an_agent_with_software_selected_gets_it_preloaded_capped_with_the_rest_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import organization_context_categories as cats

    await _with(monkeypatch, platform=True, profile=BIG_STACK)
    agent = SimpleNamespace(
        id=uuid.uuid4(), organization_context_mode="inherit", organization_context_categories=["software"]
    )
    text = await inj.organization_summary_for(_NO_DB, agent=agent)
    preloaded = "Preloaded software" + text.split("Preloaded software", 1)[1]
    assert "Product 0" in preloaded
    assert "Product 499" not in preloaded
    assert 'get_organization_context(category="software", offset=' in preloaded
    assert len(preloaded) <= cats.PRELOAD_MAX_CHARS
    # An assignment's own empty list preloads nothing, for that assignment only.
    assignment = SimpleNamespace(organization_context_mode="inherit", organization_context_categories=[])
    assert "Preloaded" not in await inj.organization_summary_for(_NO_DB, agent=agent, assignment=assignment)
    assert "Preloaded software" in await inj.organization_summary_for(_NO_DB, agent=agent)


async def test_global_default_preloads_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    await _with(monkeypatch, platform=True, profile=BIG_STACK)
    text = await inj.organization_summary_for(_NO_DB, agent=SimpleNamespace(id=uuid.uuid4()))
    assert text.startswith(inj.SUMMARY_HEADER) and "Preloaded" not in text
    assert len(text) <= inj.SUMMARY_MAX_CHARS


async def test_custom_agent_outside_a_fleet_chat_gets_the_block_and_off_removes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _with(monkeypatch, platform=True)
    agent = SimpleNamespace(id=uuid.uuid4(), organization_context_mode="inherit")
    assert (await inj.organization_summary_for(_NO_DB, agent=agent)).startswith(inj.SUMMARY_HEADER)
    agent.organization_context_mode = "off"
    assert await inj.organization_summary_for(_NO_DB, agent=agent) == ""


async def test_assignment_always_brings_it_back_on_an_off_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.fleet_runs as fleet_runs

    await _with(monkeypatch, platform=True)

    async def _no_fleet(_db: object, _agent_id: object) -> object:
        return fleet_runs.NO_FLEET

    monkeypatch.setattr(fleet_runs, "home_fleet_defaults", _no_fleet)
    agent = SimpleNamespace(id=uuid.uuid4(), organization_context_mode="off")
    assignment = SimpleNamespace(organization_context_mode="always")
    block, summary = await inj.assignment_run_organization_context(
        _NO_DB,
        agent=agent,
        assignment=assignment,
        fleet=None,
    )
    assert block == ""
    assert summary.startswith(inj.SUMMARY_HEADER)
    assignment.organization_context_mode = "inherit"
    assert await inj.assignment_run_organization_context(
        _NO_DB,
        agent=agent,
        assignment=assignment,
        fleet=None,
    ) == ("", "")


async def test_fleet_run_with_defaults_keeps_its_block_and_no_duplicate(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.fleet_runs as fleet_runs

    await _with(monkeypatch, platform=True)
    fleet = fleet_runs.FleetRunDefaults(fleet_id=uuid.uuid4())
    agent = SimpleNamespace(id=uuid.uuid4(), organization_context_mode="inherit")
    assignment = SimpleNamespace(organization_context_mode="inherit")
    block, summary = await inj.assignment_run_organization_context(
        _NO_DB,
        agent=agent,
        assignment=assignment,
        fleet=fleet,
    )
    today = await oc.fleet_run_context(_NO_DB, agent.id, fleet=fleet)
    unbounded = re.compile(r"UNTRUSTED-[0-9a-f]+")
    assert unbounded.sub("U", block) == unbounded.sub("U", today), "identical to today's fleet block"
    assert block.startswith(f"\n\n{oc.FLEET_RUN_CONTEXT_HEADER}\n")
    assert summary == "", "the profile appears once"


async def test_an_unreadable_profile_never_fails_a_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(_db: object) -> bool:
        raise RuntimeError("db down")

    monkeypatch.setattr(inj, "platform_enabled", _boom)
    assert await inj.organization_summary_for(_NO_DB, agent=SimpleNamespace()) == ""


@pytest.mark.parametrize(
    ("source", "callee"),
    [
        ("services/agent_chat.py", "organization_summary_for"),
        ("services/general_chat.py", "organization_summary_for"),
        ("services/assignment_engine.py", "assignment_run_organization_context"),
    ],
)
def test_every_prompt_builder_calls_the_one_resolver(source: str, callee: str) -> None:
    tree = ast.parse((APP_DIR / source).read_text(encoding="utf-8"))
    assert any(isinstance(node, ast.Call) and getattr(node.func, "id", None) == callee for node in ast.walk(tree)), (
        f"{source} does not call {callee}"
    )


def test_every_seeded_agent_resolves_on_with_the_tools() -> None:
    """Sam, 13 Nov 2026: organization context on for every seeded agent, the tools included."""
    from app.agents.builtin_agents import BUILTIN_AGENTS
    from app.services.organization_context_categories import check_categories

    for agent in BUILTIN_AGENTS:
        assert "organization_context_mode" not in agent, agent["slug"]
        seeded = SimpleNamespace(organization_context_mode="inherit")
        assert inj.resolve(platform_enabled=True, agent_mode=seeded.organization_context_mode) is True
        assert inj.tools_enabled_for(agent=seeded) is True
        if "organization_context_categories" in agent:
            assert (
                check_categories(agent["organization_context_categories"]) == agent["organization_context_categories"]
            )


def test_vulnerability_agents_preload_the_software() -> None:
    from app.agents.builtin_agents import BUILTIN_AGENTS

    by_slug = {agent["slug"]: agent for agent in BUILTIN_AGENTS}
    for slug in ("ctem-vuln-prioritization-orchestrator", "ctem-vuln-triage-analyst", "ctem-remediation-coordinator"):
        if slug in by_slug:
            assert "software" in by_slug[slug]["organization_context_categories"], slug


def test_the_seed_never_writes_the_mode_so_an_opt_out_survives_a_refresh() -> None:
    source = (APP_DIR / "agents" / "seed.py").read_text(encoding="utf-8")
    assert "agent.organization_context_mode =" not in source


@pytest.mark.parametrize(
    ("source", "needle"),
    [
        ("services/agent_chat.py", "tools_enabled_for(agent=agent)"),
        ("services/assignment_engine.py", "tools_enabled_for(agent=agent, assignment=assignment)"),
    ],
)
def test_the_tool_is_built_only_when_the_cascade_keeps_it(source: str, needle: str) -> None:
    text = (APP_DIR / source).read_text(encoding="utf-8")
    guard = text.index(needle)
    # The tool is built with the person the turn acts for (#11543), right under the guard.
    assert text.index("build_organization_context_tool(", guard) - guard < 300


# -- The brief notice (#11586): incomplete context, one info line ------------


COMPLETE = {
    **PROFILE,
    "tech_stack": [{"product": "Windows", "vendor": "Microsoft"}],
    "ai_systems": [{"name": "Copilot"}],
    "risk_appetite": {"annual_loss_threshold": 2_000_000, "approval_threshold": 50_000},
    "loaded_hourly_rate": 120,
    "ticketing": {"integration": "jira", "project": "SEC"},
}


def _complete_profile() -> dict[str, Any]:
    return dict(COMPLETE)


def test_an_incomplete_profile_tells_every_brief_to_carry_the_info_notice() -> None:
    text = inj.render_summary(_row(PROFILE))
    missing = oc.missing_fields(oc.parse_profile(PROFILE))
    assert missing, "PROFILE leaves contract fields empty"
    labels = dict(oc.CONTRACT_FIELDS)
    assert "Organization context incomplete: missing " in text
    assert "The analysis would be sharper with them." in text
    for path in missing:
        assert labels[path] in text.split("Organization context incomplete: missing ", 1)[1]
    assert "not a warning" in text
    assert len(text) <= inj.SUMMARY_MAX_CHARS


def test_a_complete_profile_adds_no_notice() -> None:
    data = _complete_profile()
    assert oc.missing_fields(oc.parse_profile(data)) == []
    assert oc.brief_notice_instruction([]) == ""
    assert "Organization context incomplete" not in inj.render_summary(_row(data))
    assert "Organization context incomplete" not in oc.render_for_agent(cast(Any, _row(data)))


def test_the_tool_reading_carries_the_notice_too() -> None:
    assert "Organization context incomplete: missing " in oc.render_for_agent(cast(Any, _row(PROFILE)))
    assert "Organization context incomplete: missing " in oc.render_for_agent(None)


# -- The notice is CTEM-scoped (#1384) ----------------------------------------


def test_a_run_that_is_not_ctem_work_gets_no_notice_and_no_ctem_only_field() -> None:
    text = inj.render_summary(_row(PROFILE), ctem_work=False)
    assert text.startswith(inj.SUMMARY_HEADER)
    assert "Acme Energy" in text and "EUR" in text, "the identity is kept"
    assert "Categories:" in text, "the category index is kept"
    assert "Not filled: " in text and "Never guess a missing field." in text
    assert "Organization context incomplete" not in text
    tail = text.split("Not filled: ", 1)[1]
    labels = dict(oc.CONTRACT_FIELDS)
    for path in oc.CTEM_ONLY_FIELDS:
        assert labels[path] not in tail, path
    for reading in (
        oc.render_for_agent(cast(Any, _row(PROFILE)), ctem_work=False),
        oc.render_for_agent(None, ctem_work=False),
    ):
        assert "Organization context incomplete" not in reading
        assert "loaded hourly rate" not in reading


def test_a_ctem_run_keeps_the_notice_as_today() -> None:
    assert inj.render_summary(_row(PROFILE), ctem_work=True) == inj.render_summary(_row(PROFILE))
    assert "Organization context incomplete: missing " in inj.render_summary(_row(PROFILE), ctem_work=True)


@pytest.mark.parametrize(
    ("agent", "expected"),
    [
        (SimpleNamespace(slug="ctem-risk-quantifier", is_builtin=True, builtin_tool_ids=["ctem_read"]), True),
        (SimpleNamespace(slug="ctem-threat-relevance-analyst", is_builtin=True, builtin_tool_ids=[]), True),
        (SimpleNamespace(slug="ctem-remediation-coordinator", is_builtin=True, builtin_tool_ids=None), True),
        (SimpleNamespace(slug="ctem-threat-relevance-analyst", is_builtin=False, builtin_tool_ids=[]), False),
        (SimpleNamespace(slug="chief-of-staff", is_builtin=False, builtin_tool_ids=["files", "web"]), False),
        (None, False),
    ],
)
def test_holding_ctem_tools_is_the_built_in_ctem_agents_and_the_read_group(agent: object, expected: bool) -> None:
    assert oc.holds_ctem_tools(agent) is expected


@pytest.mark.parametrize(
    ("slug", "is_builtin", "forked_from", "expected"),
    [
        ("ctem-loop", True, None, True),
        ("ctem-threat-response", True, None, True),
        ("ctem-loop", False, None, False),
        ("my-loop", False, {"slug": "ctem-loop", "is_builtin": True}, True),
        ("my-loop", False, {"slug": "ctem-loop", "is_builtin": False}, False),
        ("ceo-office", False, None, False),
    ],
)
def test_a_ctem_fleet_is_a_seeded_ctem_fleet_or_a_fork_of_one(
    slug: str, is_builtin: bool, forked_from: object, expected: bool
) -> None:
    assert oc.is_ctem_fleet(slug=slug, is_builtin=is_builtin, forked_from=forked_from) is expected


async def test_the_summary_of_an_agent_in_no_fleet_without_ctem_tools_has_no_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.fleet_runs as fleet_runs

    await _with(monkeypatch, platform=True)

    async def _no_fleet(_db: object, _agent_id: object) -> fleet_runs.FleetRunDefaults:
        return fleet_runs.NO_FLEET

    monkeypatch.setattr(fleet_runs, "home_fleet_defaults", _no_fleet)
    plain = SimpleNamespace(
        id=uuid.uuid4(), slug="writer", is_builtin=False, builtin_tool_ids=[], organization_context_mode="inherit"
    )
    text = await inj.organization_summary_for(_NO_DB, agent=plain)
    assert text.startswith(inj.SUMMARY_HEADER) and "Never guess a missing field." in text
    assert "Organization context incomplete" not in text
    ctem = SimpleNamespace(
        id=uuid.uuid4(), slug="x", is_builtin=False, builtin_tool_ids=["ctem_read"], organization_context_mode="inherit"
    )
    assert "Organization context incomplete: missing " in await inj.organization_summary_for(_NO_DB, agent=ctem)


async def test_the_summary_of_a_ctem_fleet_member_has_the_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.fleet_runs as fleet_runs

    await _with(monkeypatch, platform=True)
    fleet_id = uuid.uuid4()

    async def _home(_db: object, _agent_id: object) -> fleet_runs.FleetRunDefaults:
        return fleet_runs.FleetRunDefaults(fleet_id=fleet_id)

    async def _ctem(_db: object, wanted: object) -> bool:
        return wanted == fleet_id

    monkeypatch.setattr(fleet_runs, "home_fleet_defaults", _home)
    monkeypatch.setattr(oc, "ctem_fleet", _ctem)
    member = SimpleNamespace(
        id=uuid.uuid4(), slug="m", is_builtin=False, builtin_tool_ids=[], organization_context_mode="inherit"
    )
    assert "Organization context incomplete: missing " in await inj.organization_summary_for(_NO_DB, agent=member)
