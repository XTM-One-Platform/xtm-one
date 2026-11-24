"""The built-in tool group registry: one description per suite, pinned to the code."""

from __future__ import annotations

import json
from pathlib import Path

from app.models.general_assistant_config import (
    ADMIN_ONLY_GA_BUILTIN_TOOL_IDS,
    DEFAULT_GA_BUILTIN_TOOL_IDS,
)
from app.services.builtin_tool_budget import (
    DEFAULT_BASELINE,
    TOTAL_SCHEMA_CHARS_CEILING,
    breakdown_definitions,
    compute_budget,
    format_breakdown,
    format_budget,
    load_baseline,
    measure_schema_sizes,
    repeated_arguments,
    save_baseline,
)
from app.services.builtin_tool_catalog import build_builtin_tool_catalog
from app.services.builtin_tool_groups import (
    BUILTIN_TOOL_GROUPS,
    builtin_source,
    builtin_tool_groups_payload,
    schema_chars,
)
from app.services.builtin_tools.filtering import AGENT_KEYED_BUILTIN_TOOL_IDS

_IDS = [group.id for group in BUILTIN_TOOL_GROUPS]

#: Function counts per group as built today (stage 1 assessment, measured here).
#: A change is deliberate: update the figure with the PR that adds or removes a function.
_MIN_FUNCTIONS = {
    "files": 7,
    "web": 3,
    "design": 12,
    "cti": 6,
    "coordination": 25,
    "generate_report": 3,
    "deep_investigation": 2,
    "memory_steward": 3,
}


def _canonical(ids):
    from app.services.builtin_tool_aliases import alias_group

    return {alias_group(tool_id) or tool_id for tool_id in ids}


def test_one_group_per_catalog_id():
    catalog = build_builtin_tool_catalog(include_disabled=True)
    # 16 ids: the two trace ids merge into ``observability``, twelve ids into
    # design, web, cti, coordination and files, and agent_builder, fleets and
    # flow_builder into platform; 17 with Platform Health sandbox, 18 with its
    # improvement proposals, 19 with Platform Health evaluation, 20 with its memory
    # steward, 22 with its quality diagnosis and experiments, 23 with its Flow
    # Lab, 24 with its orchestrator, 25 with organization profile editing (#11594), 26 with
    # the grounded STIX extraction, its own group so Threat Intelligence never carries it,
    # 27 with user administration (#250), 28 with the Module Analyst's (#101 S14),
    # 29 with the read-only CTEM signals and cases (#1337), 30 with the CTEM repairs (#1778),
    # 31 with the CTEM signal and case tools on any agent (#1748).
    assert len(_IDS) == len(set(_IDS)) == 31
    assert set(_IDS) == set(catalog)


def test_every_group_text_is_in_every_catalog():
    """``useBuiltinTools`` renders name, tagline, detail and category through
    ``t_i18n``: a key missing from a catalog shows the English text in that
    locale, silently (#877 dropped the Health Modules keys in a merge)."""
    lang = Path(__file__).resolve().parents[2] / "frontend/src/i18n/lang"
    for locale in ("en", "fr", "es", "ja"):
        catalog = json.loads((lang / f"{locale}.json").read_text(encoding="utf-8"))
        missing = sorted(
            {
                key
                for group in BUILTIN_TOOL_GROUPS
                for key in (
                    None if group.named_after_platform else group.name,
                    group.tagline,
                    group.detail,
                    group.category,
                )
                if key and key not in catalog
            }
        )
        assert not missing, f"{locale}.json lacks built-in tool group keys: {missing}"


def test_audience_flags_match_the_backend_sets():
    # The backend sets still name former ids where stored rows carry them.
    assert {g.id for g in BUILTIN_TOOL_GROUPS if g.agent_keyed} == _canonical(AGENT_KEYED_BUILTIN_TOOL_IDS)
    assert {g.id for g in BUILTIN_TOOL_GROUPS if g.admin_only} == _canonical(ADMIN_ONLY_GA_BUILTIN_TOOL_IDS)
    assert {g.id for g in BUILTIN_TOOL_GROUPS if g.default_on} == _canonical(DEFAULT_GA_BUILTIN_TOOL_IDS)
    assert not {g.id for g in BUILTIN_TOOL_GROUPS if g.agent_keyed and g.default_on}


def test_health_only_groups_are_the_specialists_suites():
    """A chat's session tools never offer a suite that only a Platform Health specialist's run may use."""
    from app.services.health.fleet_seed import FLEET_AGENTS

    health_only = {g.id for g in BUILTIN_TOOL_GROUPS if g.health_only}
    assert health_only == {"memory_steward", "health_diagnosis", "health_experiments", "flow_lab", "health_modules"}
    for group in BUILTIN_TOOL_GROUPS:
        if group.health_only:
            assert group.gate == "health" and group.agent_keyed, group.id
    # Carried by the fleet's specialists, none of which takes a chat; the
    # chattable orchestrator carries none of them.
    carriers = {gid: [a for a in FLEET_AGENTS if gid in a.builtin_tool_ids] for gid in health_only}
    for gid, agents in carriers.items():
        assert agents, gid
        assert all(a.disable_chat for a in agents), gid
    payload = builtin_tool_groups_payload({})
    assert {entry["id"] for entry in payload if entry["health_only"]} == health_only


def test_every_group_has_its_card():
    for group in BUILTIN_TOOL_GROUPS:
        assert group.name and group.tagline and group.category and group.icon, group.id


def test_every_group_icon_is_resolved_by_the_frontend_map():
    """An ``icon`` the frontend map does not know renders as the ``Sparkles`` fallback."""
    import re

    source = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "builtin-tool-icons.ts").read_text(
        encoding="utf-8"
    )
    body = re.search(r"BUILTIN_TOOL_ICONS[^=]*=\s*\{(.*?)\};", source, re.S)
    assert body, "BUILTIN_TOOL_ICONS not found in builtin-tool-icons.ts"
    mapped = set(re.findall(r"\b([A-Z][A-Za-z0-9]*)\b", body.group(1)))
    missing = {group.id: group.icon for group in BUILTIN_TOOL_GROUPS if group.icon not in mapped}
    assert not missing, f"add these icons to frontend/src/lib/builtin-tool-icons.ts: {missing}"


def test_no_card_copy_carries_an_em_dash():
    """The card copy is product UI text, with no em dash (U+2014) in it."""
    for group in BUILTIN_TOOL_GROUPS:
        for field in ("name", "tagline", "detail", "category"):
            assert "\u2014" not in getattr(group, field), (group.id, field)


def test_group_aliases_match_the_alias_registry():
    """The card's [DEPRECATED] ids are the registry's, and no alias is a group id."""
    from app.services.builtin_tool_aliases import BUILTIN_GROUP_ALIASES, group_aliases

    for group in BUILTIN_TOOL_GROUPS:
        assert group.aliases == group_aliases(group.id), group.id
    assert set(BUILTIN_GROUP_ALIASES).isdisjoint(_IDS)
    assert {group for group, _fns in BUILTIN_GROUP_ALIASES.values()} <= set(_IDS)


def test_every_function_is_stamped_with_its_group_source():
    """``source`` is ``builtin:<group id>`` on every function a group owns.

    A function listed by several groups (the trace payload reader, the
    ``agent_builder`` alias's trace and evaluation tools) is owned by the first
    group that lists it, the narrowest grant.
    """
    from app.services import builtin_tools as bt
    from app.services.builtin_tools.health_modules import build_health_module_tools
    from app.services.builtin_tools.roi import build_roi_tools

    owner: dict[str, str] = {}
    for group_id, definitions in build_builtin_tool_catalog(include_disabled=True).items():
        for definition in definitions:
            owner.setdefault(definition["name"], group_id)

    built = {
        "files": bt.build_generate_file_tool(db=None, user_id="", conversation_id=None)[0],
        "web": bt.build_web_fetch_tool()[0],
        "generate_presentation": bt.build_presentation_tools(db=None, user_id="", conversation_id=None)[0],
        "generate_report": bt.build_report_tools(db=None, user_id="", conversation_id=None)[0],
        "platform_insights": bt.build_platform_insights_tools("")[0],
        "memory_steward": bt.build_memory_steward_tools()[0],
        "deep_investigation": bt.build_deep_investigation_tool(None, "")[0],
        "observability": bt.build_trace_tools(db=None, user_id="", agent_id="")[0],
        # The four builders of the platform group.
        "platform": [
            *bt.build_agent_builder_tool(None, "")[0],
            *bt.build_fleet_tools("")[0],
            *bt.build_flow_builder_tool(None, "00000000-0000-0000-0000-000000000000")[0],
            *build_roi_tools("")[0],
        ],
        "health_diagnosis": bt.build_health_diagnosis_tools("")[0],
        "health_experiments": bt.build_health_experiment_tools("")[0],
        "flow_lab": bt.build_flow_lab_tools("")[0],
        "health_modules": build_health_module_tools("")[0],
    }
    for group_id, definitions in built.items():
        for definition in definitions:
            assert definition.source == builtin_source(owner[definition.name]), (group_id, definition.name)


def test_payload_lists_every_group_with_its_functions_and_size():
    catalog = build_builtin_tool_catalog(include_disabled=True)
    payload = builtin_tool_groups_payload(catalog)
    assert [entry["id"] for entry in payload] == _IDS
    for entry in payload:
        definitions = catalog[entry["id"]]
        assert entry["offered"] is True
        assert entry["functions"] == [d["name"] for d in definitions]
        assert entry["function_count"] == len(definitions)
        assert entry["schema_chars"] == sum(schema_chars(d) for d in definitions) > 0
        assert entry["function_count"] >= _MIN_FUNCTIONS.get(entry["id"], 1)


def test_a_withheld_group_keeps_its_card_without_functions():
    catalog = build_builtin_tool_catalog(include_disabled=True)
    catalog.pop("video_studio")
    entry = next(e for e in builtin_tool_groups_payload(catalog) if e["id"] == "video_studio")
    assert entry["offered"] is False
    assert entry["functions"] == [] and entry["function_count"] == 0 and entry["schema_chars"] == 0
    assert entry["gate"] == "video_studio"


def test_schema_size_per_group_stays_under_its_ceiling():
    """Measure what each group costs on the wire against the ceilings of ``builtin_tool_budget``.

    Growing past a ceiling is a deliberate change: raise the figure in
    ``app/services/builtin_tool_budget.py`` in the PR that adds the weight, and
    say why. ``python -m scripts.tool_schema_budget`` prints this table without
    a test run; with a baseline saved on the base checkout, the failure below
    also names the delta per group and the functions that grew most.
    """
    payload = builtin_tool_groups_payload(build_builtin_tool_catalog(include_disabled=True))
    sizes = {entry["id"]: entry["schema_chars"] for entry in payload}
    assert all(size > 0 for size in sizes.values()), sizes
    measured = measure_schema_sizes()
    assert {group: sum(functions.values()) for group, functions in measured.items()} == sizes
    budget = compute_budget(measured, load_baseline(Path(__file__).resolve().parents[1] / DEFAULT_BASELINE))
    assert not budget.over, "\n" + format_budget(budget)


def test_the_budget_names_the_group_over_its_ceiling_with_its_delta():
    baseline = {"alpha": {"read": 100, "write": 50}, "beta": {"list": 40}}
    sizes = {"alpha": {"read": 100, "write": 80, "move": 15}, "beta": {"list": 40}}
    budget = compute_budget(sizes, baseline, total_ceiling=1_000, group_ceilings={"alpha": 190})
    alpha = next(group for group in budget.groups if group.group == "alpha")
    assert (alpha.size, alpha.ceiling, alpha.headroom, alpha.delta) == (195, 190, -5, 45)
    assert budget.over == ["alpha"]
    assert budget.total == 235 and budget.headroom == 765 and budget.total_delta == 45
    assert budget.grown == [("alpha", "write", 30), ("alpha", "move", 15)]
    table = format_budget(budget)
    assert "OVER THE CEILING: alpha" in table
    assert "alpha.write +30" in table
    assert table.splitlines()[1].split() == ["alpha", "195", "190", "-5", "+45"]


def test_the_budget_under_its_ceilings_reports_headroom_and_the_total_over_it():
    sizes = {"alpha": {"read": 100}, "beta": {"list": 40}}
    under = compute_budget(sizes, total_ceiling=200, group_ceilings={})
    assert under.over == [] and under.headroom == 60 and under.total_delta is None
    assert "no baseline" in format_budget(under)
    over = compute_budget(sizes, sizes, total_ceiling=120, group_ceilings={})
    assert over.over == ["total"] and over.headroom == -20 and over.total_delta == 0
    assert "nothing grew" in format_budget(over)


def test_the_baseline_round_trips(tmp_path):
    path = tmp_path / ".xtm" / "baseline.json"
    assert load_baseline(path) is None
    save_baseline({"alpha": {"read": 3}}, path)
    assert load_baseline(path) == {"alpha": {"read": 3}}


def test_the_readout_script_prints_the_table_and_exits_on_the_ceiling(tmp_path, capsys):
    from scripts import tool_schema_budget

    baseline = tmp_path / "baseline.json"
    assert tool_schema_budget.main(["--save-baseline", "--baseline", str(baseline)]) == 0
    assert tool_schema_budget.main(["--baseline", str(baseline)]) == 0
    out = capsys.readouterr().out
    assert "TOTAL" in out and "nothing grew against the baseline" in out
    assert f"{TOTAL_SCHEMA_CHARS_CEILING:,}" in out


def _definition(name: str, description: str, **properties) -> dict:
    return {"name": name, "description": description, "input_schema": {"type": "object", "properties": properties}}


_SHARED = {"type": "object", "description": "x" * 120}
_GROUP = [
    _definition("read", "Read one.", id={"type": "string"}),
    _definition("create", "Create one." * 5, impact=_SHARED, title={"type": "string", "description": "t" * 40}),
    _definition("update", "Update one.", impact=dict(reversed(list(_SHARED.items()))), limit={"type": "integer"}),
]


def test_the_breakdown_lists_functions_by_size_with_their_largest_arguments():
    rows = breakdown_definitions(_GROUP)
    assert [row.name for row in rows] == ["create", "update", "read"]
    create = rows[0]
    assert create.size == schema_chars(_GROUP[1]) and create.description == len("Create one." * 5)
    assert [name for name, _ in create.arguments] == ["impact", "title"]
    assert create.arguments[0][1] > create.arguments[1][1]
    assert breakdown_definitions([{"name": "bare", "description": "", "input_schema": {}}])[0].arguments == []


def test_an_argument_with_the_same_schema_in_several_functions_is_named_repeated():
    # Key order does not make a different text; small repeats are left out.
    assert repeated_arguments(_GROUP) == [
        ("impact", len('{"description":"' + "x" * 120 + '","type":"object"}'), ["create", "update"])
    ]
    assert repeated_arguments(_GROUP, min_chars=10_000) == []


def test_the_breakdown_text_names_headroom_functions_and_repeats():
    text = format_breakdown("alpha", _GROUP, ceiling=10_000, top=2)
    size = sum(schema_chars(definition) for definition in _GROUP)
    assert text.splitlines()[0] == f"alpha: {size:,} chars in 3 functions, ceiling 10,000, headroom {10_000 - size:,}"
    assert "arguments: impact" in text.splitlines()[1]
    assert "... 1 more (--group alpha lists them all)" in text
    assert "impact" in text.split("repeated arguments")[1] and "x2: create, update" in text
    assert "ceiling" not in format_breakdown("alpha", _GROUP).splitlines()[0]


def test_the_readout_script_breaks_a_named_group_down_and_refuses_an_unknown_one(tmp_path, capsys):
    from scripts import tool_schema_budget

    baseline = tmp_path / "baseline.json"
    assert tool_schema_budget.main(["--baseline", str(baseline), "--group", "coordination"]) == 0
    out = capsys.readouterr().out
    assert "\ncoordination: " in out and "create_work_item" in out and "arguments: " in out
    assert tool_schema_budget.main(["--group", "no_such_group"]) == 2
    assert "Unknown group: no_such_group" in capsys.readouterr().err
