"""The CTI pack seeded in XTM One mode: Feedly-derived templates and cti-skills skills.

Offline checks of the seed data itself (``app.agents.cti_templates``,
``app.agents.cti_skills`` and their markdown under ``seed_data/``): which modes
each built-in ships in, that every template assignment is a manual run whose
prompt reads the inputs it declares, that every default skill a template names
is seeded in the same mode, and that every tool the content tells the agent to
call is a tool an integration or the platform actually registers - content that
names a tool nobody answers to sends the model after a call it cannot make.
"""

from __future__ import annotations

import re

import pytest

from app.agents.builtin_agents import BUILTIN_SKILLS
from app.agents.cti_skills import CTI_SKILLS
from app.agents.cti_templates import CTI_TEMPLATES, CTI_TEMPLATES_DIR
from app.agents.seed import SEED_DATA_DIR, SKILL_RESOURCES_DIR, seed_matches_platform
from app.agents.templates import BUILTIN_TEMPLATES
from app.models.agent_template import default_assignment_manual_parameters

_CTI_TEMPLATE_SLUGS = [
    "cti-intrusion-analyst",
    "cti-adversary-analyst",
    "cti-detection-engineer",
    "cti-threat-hunter",
    "cti-vulnerability-analyst",
    "cti-strategic-analyst",
    "cti-intelligence-qa",
    "cti-risk-communications",
    "cti-financial-services",
]

#: The skills' licence notice (``AgentSkill.license``), outside the body.
_LICENSE_NOTICE = (
    "MIT License. Copyright (c) 2026 Liberty91 Ltd. Adapted for XTM One from the "
    "cti-skills pack (https://github.com/Liberty91LTD/cti-skills); the full "
    "licence text is bundled with the skill as LICENSE.md."
)

_GENERAL_TEMPLATE_SLUGS = [
    "email-triage",
    "teams-support",
    "channel-digest",
    "news-digest",
    "github-sentinel",
    "social-media-manager",
    "deal-desk",
    "hr-operations",
    "compliance-watchdog",
    "seo-research-analyst",
]

_PARAMETER_TYPES = {"text", "textarea", "number", "boolean", "select"}

#: Platform tools (not integration tools) the CTI content may name. Each is a
#: built-in tool or the skill loader every agent with skills carries.
_PLATFORM_TOOLS = {
    "web_fetch",
    "browser",
    "code_interpreter",
    "generate_image",
    "generate_report",
    "generate_file",
    "export_report",
    "stix_bundle_validator",
    "deep_investigation",
    "search_knowledge",
    "store_knowledge",
    "list_knowledge_documents",
    "get_knowledge_document",
    "search_memory",
    "store_memory",
    "load_skill",
    "read_skill_resource",
}

#: Backticked names shaped like a tool that the content uses as data field
#: names (the collection record schema of the dark-web references).
_FIELD_NAMES = {"query_time"}

# A backticked snake_case name led by a verb: how the content cites a tool.
_TOOL_CITATION = re.compile(
    r"`((?:get|list|search|lookup|query|check|create|add|update|delete|manage|run|"
    r"submit|scan|count|find|enrich|analyse|analyze|download|export|import|map|assess|"
    r"promote|merge|upload|use|remove|bulk|identify|resolve|reverse|triage|publish|"
    r"describe|rescan|validate|request|parse|contain|lift|isolate)_[a-z0-9_]+)`"
)


def _cti_skill_slugs() -> set[str]:
    return {s["slug"] for s in CTI_SKILLS}


def _cti_texts() -> dict[str, str]:
    """Every piece of CTI content the agent reads: skills, resources, personas, prompts."""
    texts = {
        f"skill {s['slug']}": (SEED_DATA_DIR / "skill_content" / f"{s['slug']}.md").read_text()
        for s in CTI_SKILLS
    }
    for skill in CTI_SKILLS:
        if skill.get("resources"):
            for path in sorted((SKILL_RESOURCES_DIR / skill["slug"]).rglob("*.md")):
                texts[f"resource {path.relative_to(SKILL_RESOURCES_DIR)}"] = path.read_text()
    for tmpl in CTI_TEMPLATES:
        texts[f"persona {tmpl['slug']}"] = tmpl["default_persona"]
        for entry in tmpl["default_assignments"]:
            texts[f"prompt {tmpl['slug']} / {entry['name']}"] = entry["prompt"]
    return texts


def _registered_integration_tool_names() -> set[str]:
    from app.api.v1.integration_catalog import build_dummy_token
    from app.services.integration_tools import CHANNEL_BOT_TOOLS, PROVIDER_TOOLS
    from app.tools.base import CallableToolExecutor

    names: set[str] = set()
    for provider, builder in {**PROVIDER_TOOLS, **CHANNEL_BOT_TOOLS}.items():
        # Every builder answers its dummy (``tests/test_dummy_token_shapes.py``).
        defs = builder(build_dummy_token(provider), CallableToolExecutor(), refresh_fn=lambda: None)
        names.update(d.name for d in defs if d.name)
    # OpenCTI's opt-in Live writes setting adds apply_opencti_draft and create_opencti_label.
    opencti = PROVIDER_TOOLS["opencti"]
    defs = opencti(build_dummy_token("opencti"), CallableToolExecutor(), refresh_fn=lambda: None, live_writes=True)
    names.update(d.name for d in defs if d.name)
    return names


# ---------------------------------------------------------------------------
# Platform modes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("xtm_one", _CTI_TEMPLATE_SLUGS),
        ("copilot", _GENERAL_TEMPLATE_SLUGS),
        ("dev", _GENERAL_TEMPLATE_SLUGS + _CTI_TEMPLATE_SLUGS),
    ],
)
def test_each_mode_ships_its_own_templates(mode, expected):
    shipped = [t["slug"] for t in BUILTIN_TEMPLATES if seed_matches_platform(t, mode)]
    assert shipped == expected


def test_the_cti_skills_ship_in_xtm_one_and_dev_only():
    for skill in CTI_SKILLS:
        assert skill["platform_targets"] == ["xtm_one", "dev"], skill["slug"]
    xtm_one = {s["slug"] for s in BUILTIN_SKILLS if seed_matches_platform(s, "xtm_one")}
    copilot = {s["slug"] for s in BUILTIN_SKILLS if seed_matches_platform(s, "copilot")}
    assert _cti_skill_slugs() <= xtm_one
    assert not _cti_skill_slugs() & copilot


def test_builtin_skill_slugs_are_unique():
    slugs = [s["slug"] for s in BUILTIN_SKILLS]
    assert len(slugs) == len(set(slugs))
    names = [s["name"] for s in BUILTIN_SKILLS if seed_matches_platform(s, "xtm_one")]
    # The model loads a skill BY NAME: two seeded skills sharing one would
    # make every load of it ambiguous.
    assert len(names) == len(set(names))


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def test_every_template_default_skill_is_seeded_in_its_modes():
    seeded = {
        mode: {s["slug"] for s in BUILTIN_SKILLS if seed_matches_platform(s, mode)}
        for mode in ("xtm_one", "dev")
    }
    for tmpl in CTI_TEMPLATES:
        assert tmpl["default_skills"], tmpl["slug"]
        for mode in tmpl["platform_targets"]:
            missing = set(tmpl["default_skills"]) - seeded[mode]
            assert not missing, f"{tmpl['slug']} in {mode}: {sorted(missing)}"


@pytest.mark.parametrize("tmpl", CTI_TEMPLATES, ids=lambda t: t["slug"])
def test_every_cti_assignment_is_a_manual_run_reading_its_inputs(tmpl):
    assert tmpl["default_assignments"]
    for entry in tmpl["default_assignments"]:
        assert entry["trigger_type"] == "manual"
        assert entry["prompt"].strip()
        assert len(entry["name"]) <= 80
        params = entry["manual_trigger_parameters"]
        # The create paths keep every parameter as declared.
        assert default_assignment_manual_parameters(entry) == params
        keys = [p["key"] for p in params]
        assert len(keys) == len(set(keys)), entry["name"]
        for param in params:
            assert re.fullmatch(r"[a-z][a-z0-9_]*", param["key"]), param
            assert param["type"] in _PARAMETER_TYPES, param
            assert param["label"].strip(), param
            if param["type"] == "select":
                assert param["options"], param
                assert param.get("default") in param["options"], param
            # The values reach the run in the trigger payload, under their key:
            # a prompt that never names one ignores what the user typed.
            assert f"`{param['key']}`" in entry["prompt"], (entry["name"], param["key"])


@pytest.mark.parametrize("tmpl", CTI_TEMPLATES, ids=lambda t: t["slug"])
def test_cti_template_text_carries_no_template_placeholder(tmpl):
    """``{{...}}`` is rendered from the run context (blank when absent, logged
    as unresolved otherwise): the inputs are read from the payload by key."""
    assert "{{" not in tmpl["default_persona"]
    assert "Organization context" in tmpl["default_persona"]
    # A slot the user fills is a TO FILL IN marker, not a ``<...>`` token the
    # trace renderer drops as an unknown tag.
    assert "TO FILL IN" in tmpl["default_persona"]
    assert "<placeholder>" not in tmpl["default_persona"]
    assert not re.search(r":\s*<[^<>\n]+>", tmpl["default_persona"])
    for entry in tmpl["default_assignments"]:
        assert "{{" not in entry["prompt"], entry["name"]
        assert "<placeholder>" not in entry["prompt"], entry["name"]
        # Feedly's "[PASTE ... HERE]" input markers became payload fields.
        assert not re.search(r"\[paste[^\]]*here\]", entry["prompt"], re.I), entry["name"]


def test_every_template_prompt_file_is_used():
    """An orphan prompt file is an assignment nobody seeds."""
    for tmpl in CTI_TEMPLATES:
        directory = CTI_TEMPLATES_DIR / tmpl["slug"]
        on_disk = {p.name for p in directory.glob("*.md")} - {"persona.md"}
        declared = {
            p.name
            for p in directory.glob("*.md")
            if any(p.read_text().strip() == e["prompt"].strip() for e in tmpl["default_assignments"])
        }
        assert on_disk == declared, tmpl["slug"]


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("skill", CTI_SKILLS, ids=lambda s: s["slug"])
def test_every_cti_skill_has_its_body_and_a_loadable_description(skill):
    body = (SEED_DATA_DIR / "skill_content" / f"{skill['slug']}.md").read_text()
    assert body.startswith(f"# {skill['name']}\n"), skill["slug"]
    assert not body.startswith("---"), "the body carries no frontmatter"
    # MIT: the copyright notice and the licence text travel with every copy,
    # as metadata and a bundled file - never in the text the agent reads,
    # where a notice and its link read like an instruction.
    assert skill["license"] == _LICENSE_NOTICE
    assert skill["resource_files"] == {"LICENSE.md": "licenses/cti-skills-LICENSE.md"}
    for marker in ("Copyright", "Liberty91", "MIT License", "LICENSE.md"):
        assert marker not in body, (skill["slug"], marker)
    # The catalog line the model chooses a skill from (cut at 1024 chars).
    assert 0 < len(skill["description"]) <= 1024
    assert "builtin" in skill["tags"] and "cti" in skill["tags"]


def test_the_bundled_licence_is_the_upstream_mit_text():
    """``LICENSE.md`` of every CTI skill: the pack's licence, copyright notice
    and permission notice included, word for word."""
    text = (SEED_DATA_DIR / "licenses" / "cti-skills-LICENSE.md").read_text()
    assert "Copyright (c) 2026 Liberty91 Ltd" in text
    assert "Permission is hereby granted, free of charge" in text
    assert "The above copyright notice and this permission notice shall be included" in text
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in text


def test_no_bundled_reference_carries_licence_text():
    """The references are read by the agent: the licence stays in metadata."""
    for path in sorted(SKILL_RESOURCES_DIR.rglob("*.md")):
        text = path.read_text()
        for marker in ("Copyright", "Liberty91", "LICENSE.md"):
            assert marker not in text, (path, marker)


def test_the_skill_catalog_does_not_advertise_a_licence_file():
    """A bundled-file hint invites the model to read the file: a licence at the
    root of a skill is not one to work from."""
    import uuid

    from app.services.builtin_tools.skills import (
        ResolvedSkill,
        build_skills_catalog_prompt,
        is_licence_file,
    )

    skill = ResolvedSkill(
        id=uuid.uuid4(),
        name="Dark Web Collection",
        raw_name="Dark Web Collection",
        description="d",
        compatibility=None,
        content="# body",
        always_load=False,
        resource_paths=("LICENSE.md", "references/forums.md", "references/opsec.md"),
    )
    catalog = build_skills_catalog_prompt([skill])
    assert "<bundled_files>references/forums.md, references/opsec.md</bundled_files>" in catalog
    assert "LICENSE" not in catalog
    # The package format declares one place for the licence; nothing is
    # guessed from a file name (invariant 18).
    assert is_licence_file("LICENSE.md")
    assert not any(is_licence_file(p) for p in ("references/LICENSE.md", "LICENSE", "COPYING", "license.md"))


def test_skills_with_resources_ship_them():
    with_resources = [s for s in CTI_SKILLS if s.get("resources")]
    assert [s["slug"] for s in with_resources] == ["cti-darkweb-collection"]
    files = sorted(
        p.relative_to(SKILL_RESOURCES_DIR / "cti-darkweb-collection").as_posix()
        for p in (SKILL_RESOURCES_DIR / "cti-darkweb-collection").rglob("*")
        if p.is_file()
    )
    assert files == [
        "references/access-methods.md",
        "references/forums.md",
        "references/opsec.md",
        "references/passive-monitoring.md",
        "references/telegram-channels.md",
    ]


def test_no_cti_content_tells_the_agent_to_run_a_script_or_read_a_key():
    """The point of the conversion: integrations instead of the pack's CLIs."""
    for where, text in _cti_texts().items():
        for marker in ("tools/clis", "node tools", "python3 tools", "_API_KEY", "settings.local.json", "/cti-setup"):
            assert marker not in text, f"{where}: {marker}"


def test_every_tool_the_cti_content_cites_is_registered():
    known = _registered_integration_tool_names() | _PLATFORM_TOOLS | _FIELD_NAMES
    unknown = sorted(
        {
            f"{where}: {name}"
            for where, text in _cti_texts().items()
            for name in _TOOL_CITATION.findall(text)
            if name not in known
        }
    )
    assert not unknown, unknown


def test_every_skill_the_cti_content_names_is_seeded():
    """The model loads a skill by its display name: "load the **X** skill"
    must name one XTM One mode actually seeds."""
    seeded = {s["name"] for s in BUILTIN_SKILLS if seed_matches_platform(s, "xtm_one")}
    unknown = sorted(
        {
            f"{where}: {name}"
            for where, text in _cti_texts().items()
            for name in re.findall(r"\*\*([^*\n]{3,60})\*\*\s+skills?\b", text)
            if name not in seeded
        }
    )
    assert not unknown, unknown
