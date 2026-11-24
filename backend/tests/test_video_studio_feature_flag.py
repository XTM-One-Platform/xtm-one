"""The Video Studio stays behind ``VIDEO_STUDIO``, off by default.

A render needs a worker sized for Chromium and ffmpeg, so a deployment
turns the Studio on when its worker is. Like ``DEEP_INVESTIGATION`` the flag is
an enforcement boundary: off, the ``/api/v1/video-studio`` routes answer 403,
public video links answer like unknown ones, the ``video_studio`` tool is never
built, and the seeded Video Production skill and video templates are not
seeded. An agent that already carries the tool id keeps it in its
configuration: the flag decides what runs, not what is stored.

The frontend has no unit-test harness for these screens, so its half is pinned
by source assertions, as in ``test_deep_investigation_feature_flag.py``.

See ``dev-docs/video-studio-feature-flag.md``.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest

from app.agents import seed as seed_module
from app.agents.builtin_agents import BUILTIN_SKILLS, FEATURE_GATED_BUILTIN_SKILLS
from app.core import feature_flags
from app.core.feature_flags import KNOWN_FEATURE_FLAGS, is_feature_enabled
from app.services.builtin_tool_aliases import is_former_builtin_id
from app.services.builtin_tools import agent_builder
from app.services.template_container_seed import BUILTIN_TEMPLATE_CONTAINERS, builtin_containers_for

BACKEND = Path(__file__).resolve().parent.parent
FRONTEND = BACKEND.parent / "frontend"

FLAG = "VIDEO_STUDIO"
SKILL_SLUG = "video-production"
PLATFORM_MODES = ("copilot", "xtm_one", "dev")

requires_flag_off = pytest.mark.skipif(
    is_feature_enabled(FLAG),
    reason=f"ENABLED_FEATURES has {FLAG} on; these assert the flag-off shape",
)


def _squash(text: str) -> str:
    return " ".join(text.split())


def _has(source: str, snippet: str) -> bool:
    return _squash(snippet) in _squash(source)


def _read_backend(*parts: str) -> str:
    return BACKEND.joinpath(*parts).read_text(encoding="utf-8")


def _read_frontend(*parts: str) -> str:
    return FRONTEND.joinpath(*parts).read_text(encoding="utf-8")


def _line(source: str, needle: str) -> str:
    return next(line for line in source.splitlines() if needle in line)


def _video_slugs() -> set[str]:
    return {c["slug"] for c in BUILTIN_TEMPLATE_CONTAINERS if c["kind"] == "video"}


def test_the_flag_is_in_both_registries():
    assert FLAG in KNOWN_FEATURE_FLAGS
    assert f'"{FLAG}",' in _read_frontend("src", "lib", "utils.ts")
    assert FEATURE_GATED_BUILTIN_SKILLS[SKILL_SLUG] == FLAG
    assert agent_builder._FEATURE_GATED_BUILTIN_TOOL_IDS["video_studio"] == FLAG


@requires_flag_off
async def test_the_video_studio_api_answers_403_and_is_unpublished():
    """The flag's own 403 answers before authentication, and neither the
    Studio's operations nor the public link's are in the published schema."""
    from app.main import application

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for path in (
            "/api/v1/video-studio/projects",
            "/api/v1/video-studio/templates",
            f"/api/v1/video-studio/projects/{uuid.uuid4()}",
        ):
            response = await client.get(path)
            assert response.status_code == 403, path
            assert FLAG in response.json()["detail"], path

    leaked = [
        p
        for p in application.openapi()["paths"]
        if p.startswith(("/api/v1/video-studio", "/api/v1/public/videos"))
    ]
    assert not leaked, f"video endpoints still published: {leaked}"


@requires_flag_off
def test_public_links_answer_like_unknown_ones_whatever_the_security_option(monkeypatch):
    from app.services.video_studio import public_links

    monkeypatch.setattr(public_links, "security_option", lambda _key: True)
    assert public_links.public_links_enabled() is False

    monkeypatch.setattr(public_links, "is_feature_enabled", lambda flag: flag == FLAG)
    assert public_links.public_links_enabled() is True


@requires_flag_off
async def test_a_conversation_bound_to_a_video_reads_no_studio_context():
    from app.services.video_studio.studio import load_video_studio_context

    context = await load_video_studio_context(None, user_id=uuid.uuid4(), video_project_id=uuid.uuid4())  # type: ignore[arg-type]
    assert context is None


@requires_flag_off
async def test_the_catalog_withholds_the_tool_but_the_registry_keeps_it():
    """Skill-package validation reads the registry's keys, so the id must stay
    valid there; the endpoint an integrator reads must not offer it."""
    from app.api.v1.settings import get_builtin_tool_catalog
    from app.services.builtin_tool_catalog import build_builtin_tool_catalog

    assert "video_studio" in build_builtin_tool_catalog(include_disabled=True)
    assert "video_studio" not in await get_builtin_tool_catalog(_user=None)
    assert _has(
        _read_backend("app", "api", "v1", "settings.py"),
        'if not is_feature_enabled("VIDEO_STUDIO"): catalog.pop("video_studio", None) for definitions in catalog.values():',
    ), "the prompt-guard scope offers the tool"


def test_the_tool_is_only_built_with_the_flag_on():
    for module, gate in (
        ("agent_chat.py", 'if "video_studio" in agent_builtin_tool_ids and user_id and is_feature_enabled("VIDEO_STUDIO"):'),
        (
            "assignment_engine.py",
            'if "video_studio" in agent_builtin_tool_ids and _run_file_owner_id and is_feature_enabled("VIDEO_STUDIO"):',
        ),
        ("general_chat.py", 'if "video_studio" in ga_builtin_tool_ids and is_feature_enabled("VIDEO_STUDIO"):'),
    ):
        assert _has(_read_backend("app", "services", module), gate), module


@requires_flag_off
def test_the_agent_builder_does_not_name_the_tool_but_still_accepts_it():
    """An agent that already carries the id stays editable through the
    builder; the model is just never told the tool exists."""
    assert "video_studio" not in agent_builder._advertised_builtin_tool_ids()
    assert "video_studio" in agent_builder._VALID_BUILTIN_TOOL_IDS
    # [DEPRECATED] aliases are accepted but never advertised either.
    current = {t for t in agent_builder._VALID_BUILTIN_TOOL_IDS if not is_former_builtin_id(t)}
    # Every flag-gated id whose flag is off is withheld, this one included.
    withheld = {t for t, flag in agent_builder._FEATURE_GATED_BUILTIN_TOOL_IDS.items() if not is_feature_enabled(flag)}
    assert "video_studio" in withheld
    assert set(agent_builder._advertised_builtin_tool_ids()) == current - withheld


def test_with_the_flag_on_the_agent_builder_names_every_tool(monkeypatch):
    monkeypatch.setattr(agent_builder, "is_feature_enabled", lambda _flag: True)
    assert agent_builder._advertised_builtin_tool_ids() == tuple(
        t for t in agent_builder._VALID_BUILTIN_TOOL_IDS if not is_former_builtin_id(t)
    )


@requires_flag_off
@pytest.mark.parametrize("platform_mode", PLATFORM_MODES)
def test_the_seed_withholds_the_video_production_skill(platform_mode):
    seeded = {skill["slug"] for skill in seed_module._seeded_builtin_skills(platform_mode)}
    assert SKILL_SLUG not in seeded
    # Every other skill of the mode is kept, but for another flag's own skills.
    declared = {s["slug"] for s in BUILTIN_SKILLS if seed_module.seed_matches_platform(s, platform_mode)}
    other_flags_off = {
        slug for slug, flag in FEATURE_GATED_BUILTIN_SKILLS.items() if slug != SKILL_SLUG and not is_feature_enabled(flag)
    }
    assert seeded == declared - {SKILL_SLUG} - other_flags_off
    # The declaration itself is untouched: the flag decides what is seeded.
    assert any(s["slug"] == SKILL_SLUG for s in BUILTIN_SKILLS)


@pytest.mark.parametrize("platform_mode", PLATFORM_MODES)
def test_with_the_flag_on_the_skill_seed_is_the_declaration(monkeypatch, platform_mode):
    monkeypatch.setattr(seed_module, "is_feature_enabled", lambda _flag: True)
    assert seed_module._seeded_builtin_skills(platform_mode) == [
        s for s in BUILTIN_SKILLS if seed_module.seed_matches_platform(s, platform_mode)
    ]


@requires_flag_off
@pytest.mark.parametrize("platform_mode", PLATFORM_MODES)
def test_the_seed_withholds_the_video_templates(platform_mode):
    """``builtin_containers_for`` feeds the template seed, the every-boot
    removal of unshipped built-ins and the template asset seed alike."""
    assert _video_slugs(), "no built-in video template; the test would prove nothing"
    shipped = builtin_containers_for(platform_mode)
    assert all(c["kind"] != "video" for c in shipped)
    declared = [c for c in BUILTIN_TEMPLATE_CONTAINERS if seed_module.seed_matches_platform(c, platform_mode)]
    assert shipped == [c for c in declared if c["kind"] != "video"]


@requires_flag_off
def test_the_video_and_music_models_are_never_announced_while_the_studio_is_off(monkeypatch):
    """The model catalog announcements only tell users about models they can use
    (``capability_offered``, read by ``model_catalog_announcements._pinned_capabilities``)."""
    from app.llm import env_capability_defaults
    from app.llm.env_capability_defaults import ROLES, capability_offered

    assert {role.key for role in ROLES if role.feature_flag == FLAG} == {"video", "music"}
    assert not capability_offered("video") and not capability_offered("music")
    assert all(capability_offered(key) for key in ("image", "speech", "decision"))

    monkeypatch.setattr(env_capability_defaults, "is_feature_enabled", lambda flag: flag == FLAG)
    assert capability_offered("video") and capability_offered("music")


@requires_flag_off
def test_copilot_sweeps_the_objects_of_the_withheld_video_templates():
    """Copilot ships every built-in only with the Studio on; off, the hourly
    orphan sweep collects what the removed video templates left in the store."""
    from app.services.template_asset_seed import _ships_every_builtin

    assert _ships_every_builtin("copilot") is False


@pytest.mark.parametrize("platform_mode", PLATFORM_MODES)
def test_with_the_flag_on_the_template_seed_ships_the_video_templates(monkeypatch, platform_mode):
    monkeypatch.setattr(feature_flags, "is_feature_enabled", lambda _flag: True)
    assert builtin_containers_for(platform_mode) == [
        c for c in BUILTIN_TEMPLATE_CONTAINERS if seed_module.seed_matches_platform(c, platform_mode)
    ]


def test_the_frontend_offers_nothing_with_the_flag_off():
    nav = _read_frontend("src", "components", "layout", "nav-model.ts")
    assert _has(nav, 'aiLeaf("/videos", "clapperboard", "Videos", (f) => f.videoStudio)')
    assert _has(_read_frontend("src", "components", "layout", "Sidebar.tsx"), "videoStudio: videoStudioEnabled,")
    # The top bar search resolves the nav with the shared flags (#11201).
    assert "useNavFlags()" in _read_frontend("src", "components", "layout", "GlobalSearch.tsx")
    assert _has(
        _read_frontend("src", "components", "layout", "use-nav-group.ts"),
        'const videoStudio = useFeatureFlag("VIDEO_STUDIO");',
    )

    app = _read_frontend("src", "App.tsx")
    assert '<FeatureFlagGate flag="VIDEO_STUDIO">' in _line(app, 'path="/videos"')
    editor_route = app.split('path="/videos/:id"', 1)[1].split("<Route", 1)[0]
    assert '<FeatureFlagGate flag="VIDEO_STUDIO">' in editor_route
    public_route = app.split('path="/public/videos/:token"', 1)[1].split("<Route", 1)[0]
    assert "FeatureFlagGate" not in public_route, "a public link answers like an unknown one on its own"

    # The card's gate is served by the backend registry.
    from app.services.builtin_tool_groups import BUILTIN_TOOL_GROUPS

    assert next(g for g in BUILTIN_TOOL_GROUPS if g.id == "video_studio").gate == "video_studio"
    assert _has(_read_frontend("src", "types", "index.ts"), '"deep_investigation" | "video_studio"')
    assert _has(_read_frontend("src", "hooks", "useBuiltinTools.ts"), "video_studio: videoStudioEnabled,")

    assert _has(
        _read_frontend("src", "components", "chat-panel", "InlineFileContent.tsx"),
        "videoStudio: videoStudioEnabled",
    )
    assert _has(
        _read_frontend("src", "components", "chat-panel", "markers.tsx"),
        "flags.videoStudio ? <VideoCard videoId={id} /> : null",
    )

    # Each model section sits inside an open `{videoStudioEnabled && (` block;
    # Speech does not (the chat reads answers aloud with it).
    settings = _read_frontend("src", "pages", "Settings.tsx")
    for section, gated in (
        ("VideoGenerationSection", True),
        ("MusicGenerationSection", True),
        ("SpeechGenerationSection", False),
    ):
        before = settings.split(f"<{section}", 1)[0]
        start = before.rfind("{videoStudioEnabled && (")
        assert start != -1, section
        assert (")}" not in before[start:]) is gated, section

    work = _read_frontend("src", "pages", "Work.tsx")
    assert _has(work, "const videos = useVideoProjects(RECENT_VIDEOS, videoStudio);")
    # The card is the Work group's `/videos` child (#9301 N2c): `hubCards` shows a
    # card only for a visible child, and the nav leaf is gated on `f.videoStudio`.
    assert _has(work, 'hubCards(useNavGroup("work"), {')
    nav_group = _read_frontend("src", "components", "layout", "use-nav-group.ts")
    assert _has(nav_group, 'const videoStudio = useFeatureFlag("VIDEO_STUDIO");')

    kinds = _read_frontend("src", "components", "presentation", "TemplateKindSelect.tsx")
    assert _has(kinds, 'kind !== "video" || videoStudio || current === "video"')
    detail = _read_frontend("src", "pages", "TemplateDetail.tsx")
    assert detail.count('template.kind === "video" && videoStudio && (') == 2
