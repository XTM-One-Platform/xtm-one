"""Video templates: the seeded brands, what a template presets, adds
and locks, how its rules are validated, and the palette a video reads from it."""

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.services import template_containers
from app.services.template_container_seed import BUILTIN_TEMPLATE_CONTAINERS, builtin_containers_for
from app.services.video_studio import templates, themes
from app.services.video_studio.spec import VideoSpecError, authored_spec, validate_video_spec

pytestmark = pytest.mark.usefixtures("video_studio_enabled")

TAKE_ID = "00000000-0000-0000-0000-000000000001"
#: Every platform's video templates, and the Filigran-branded ones Copilot adds.
_GLOBAL_VIDEO_SLUGS = {"xtm-one-video", "xtm-one-brand-film", "threat-briefing-video", "social-teaser-video"}
_VIDEO_SLUGS = _GLOBAL_VIDEO_SLUGS | {"filigran-video", "thread-video", "filigran-brand-film", "filigran-event-loop"}


def _entry(slug: str) -> dict:
    return next(entry for entry in BUILTIN_TEMPLATE_CONTAINERS if entry["slug"] == slug)


def _template(
    style: dict | None = None, *, kind: str = "video", schemes=None, name: str = "Brand"
) -> templates.VideoTemplate:
    row = SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        kind=kind,
        description=None,
        tags=[],
        theme_config={"background": "#000000", "foreground": "#ffffff", "accent": "#00e0ff"},
        extra_metadata={"video": style or {}, "supported_schemes": schemes or ["dark", "light"]},
    )
    return templates.VideoTemplate.from_container(row)


def _authored(**overrides) -> dict:
    raw = {
        "title": "Hunting with OpenCTI",
        "scenes": [{"type": "recording", "take_id": TAKE_ID}],
    }
    raw.update(overrides)
    return authored_spec(raw)


# ── The seeded brands ─────────────────────────────────────────────────────


def test_the_seed_ships_the_video_templates():
    videos = {entry["slug"] for entry in BUILTIN_TEMPLATE_CONTAINERS if entry["kind"] == "video"}
    assert videos == _VIDEO_SLUGS


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("copilot", _VIDEO_SLUGS), ("xtm_one", _GLOBAL_VIDEO_SLUGS), ("dev", _GLOBAL_VIDEO_SLUGS)],
)
def test_the_filigran_and_thread_videos_are_copilot_only(mode, expected):
    shipped = {entry["slug"] for entry in builtin_containers_for(mode) if entry["kind"] == "video"}
    assert shipped == expected


def test_every_template_says_what_it_is_made_for_and_not_only_product_demos():
    """The "New video" dialog suggests a template per kind of video: every kind has one, everywhere."""
    for mode in ("copilot", "xtm_one"):
        purposes: set[str] = set()
        for entry in builtin_containers_for(mode):
            if entry["kind"] != "video":
                continue
            declared = entry["extra_metadata"]["video"]["purposes"]
            assert declared and set(declared) <= set(templates.TEMPLATE_PURPOSES), entry["slug"]
            purposes.update(declared)
        assert purposes == set(templates.TEMPLATE_PURPOSES), mode


def test_the_brand_films_open_on_statements_and_close_on_the_tagline():
    film = _entry("filigran-brand-film")["extra_metadata"]["video"]
    assert film["scene_defaults"]["statement"]["reveal"] == "lines"
    assert "*Take action.*" in film["outro"]["scene"]["title"]
    loop = _entry("filigran-event-loop")["extra_metadata"]["video"]
    # A booth loop is silent: no voice, no captions, no clip sound.
    assert loop["defaults"]["voiceover"] == {"enabled": False}
    assert loop["defaults"]["captions"] == {"enabled": False}
    assert loop["scene_defaults"]["clip"]["audio"] is False
    applied = templates.apply_template(
        authored_spec({"title": "Booth", "scenes": [{"type": "statement", "lines": ["Too much *noise*."]}]}),
        _template(loop),
    )
    statement, outro = applied.spec["scenes"]
    assert statement["duration"] == 3.5 and statement["dim"] == 0.6
    assert outro["type"] == "outro" and applied.spec["voiceover"]["enabled"] is False


@pytest.mark.parametrize("slug", sorted(_VIDEO_SLUGS))
def test_a_brand_video_ships_every_asset_of_its_deck(slug):
    """A video places what its deck places: the same files - the
    screenshots, diagrams and photos of an image scene, the logos of a
    `logo_grid` figure - and the same marks on its cards and watermark."""
    from app.services.template_asset_seed import load_brand_manifests, merge_manifests
    from app.services.video_studio.spec import BRAND_ASSETS

    merged = merge_manifests(load_brand_manifests())
    deck = _entry(slug)["extra_metadata"]["brand"]["deck"]

    def marks(container: str) -> dict[str, str]:
        return {asset.role: asset.filename for asset in merged[container] if asset.role in BRAND_ASSETS}

    assert {asset.filename for asset in merged[slug]} == {asset.filename for asset in merged[deck]}
    assert marks(slug) == marks(deck)


def test_the_filigran_brand_film_carries_the_brand_pictures():
    """A brand film needs the brand's own material: the team, events and
    products (its customer and partner logos are private libraries, included
    like every Copilot template's: test_template_asset_seed)."""
    from app.services.template_asset_seed import load_brand_manifests, merge_manifests

    merged = merge_manifests(load_brand_manifests())
    roles = {asset.role for asset in merged["filigran-brand-film"]}
    assert {"logo", "logo_mark", "image", "screenshot", "product_logo"} <= roles


@pytest.mark.parametrize("slug", sorted(_VIDEO_SLUGS))
def test_every_seeded_style_is_valid_and_canonical(slug):
    style = _entry(slug)["extra_metadata"]["video"]
    canonical, problems = templates.validate_style(style)
    assert problems == []
    assert templates.VideoTemplateStyle.model_validate(canonical)


@pytest.mark.parametrize("slug", sorted(_VIDEO_SLUGS))
def test_a_seeded_templates_cards_are_the_videos_to_customize(slug):
    """A template's opening and closing cards are a starting point, never locked: a video's own replace them."""
    style = _entry(slug)["extra_metadata"]["video"]
    assert not [path for path in style.get("locked", []) if path.startswith(("scenes.title.", "scenes.outro."))]
    authored = _authored(
        scenes=[
            {"type": "title", "title": "Our own\n*opening*", "style": "minimal", "duration": 6},
            {"type": "recording", "take_id": TAKE_ID},
            {"type": "outro", "title": "Our own closing", "cta": "Talk to us", "style": "spotlight"},
        ]
    )
    applied = templates.apply_template(authored, _template(style))
    first, *_, last = applied.spec["scenes"]
    assert applied.added_scenes == []
    assert (first["title"], first["style"], first["duration"]) == ("Our own\n*opening*", "minimal", 6)
    assert (last["title"], last["cta"], last["style"]) == ("Our own closing", "Talk to us", "spotlight")


def test_the_filigran_tagline_closes_on_two_lines():
    for slug in ("filigran-brand-film", "filigran-event-loop"):
        title = _entry(slug)["extra_metadata"]["video"]["outro"]["scene"]["title"]
        assert title == "Uncover threats.\n*Take action.*", slug


@pytest.mark.parametrize(
    ("video", "deck"),
    [("thread-video", "thread-brand"), ("xtm-one-video", "xtm-one-brand")],
)
def test_a_brand_video_reads_its_decks_palette(video, deck):
    assert _entry(video)["theme_config"] == _entry(deck)["theme_config"]


def test_the_filigran_videos_keep_their_palette_through_the_deck_redesign():
    """The Filigran Brand Deck moved to the PPT Design System tokens;
    the video templates are out of that scope and keep the palette they had,
    declared once for every Filigran video."""
    palette = _entry("filigran-video")["theme_config"]
    assert palette["background"] == "#070d18" and palette["accent"] == "#42caff"
    assert palette != _entry("filigran-brand")["theme_config"]
    for slug in ("filigran-brand-film",):
        assert _entry(slug)["theme_config"] == palette, slug


def test_the_default_video_template_follows_the_platform():
    assert templates.DEFAULT_VIDEO_TEMPLATE_SLUGS[0] == "filigran-video"
    assert "xtm-one-video" in templates.DEFAULT_VIDEO_TEMPLATE_SLUGS
    assert all(_entry(slug)["kind"] == "video" for slug in templates.DEFAULT_VIDEO_TEMPLATE_SLUGS)


def test_the_thread_video_draws_on_the_mosaic_dark_only():
    metadata = _entry("thread-video")["extra_metadata"]
    assert metadata["supported_schemes"] == ["dark"]
    assert metadata["video"]["defaults"]["theme"]["background"] == "mosaic"
    assert "theme.background" in metadata["video"]["locked"]


# ── What a template does to a spec ────────────────────────────────────────


_STYLE = {
    "aspect_ratios": ["16:9", "9:16"],
    "defaults": {
        "theme": {"background": "mosaic", "watermark": "top_right"},
        "captions": {"style": "bar"},
        "voiceover": {"style": "Bright and crisp", "speed": 1.05},
    },
    "scene_defaults": {
        "title": {"style": "aurora", "align": "left"},
        "recording": {"frame": {"style": "window", "radius": 14}, "cursor": {"style": "halo"}},
    },
    "overlay_defaults": {"callout": {"style": "dark"}},
    "intro": {"required": True, "scene": {"kicker": "THREAD 2026", "duration": 3.4}},
    "outro": {"required": True, "scene": {"title": "See you at {title}", "url": "filigran.io/thread"}},
    "locked": ["theme.palette", "theme.background", "scenes.recording.frame"],
}


def test_defaults_fill_only_what_the_author_left_unset():
    applied = templates.apply_template(
        _authored(captions={"style": "outline"}, voiceover={"voice": "nova"}), _template(_STYLE)
    )
    spec = applied.spec
    assert spec["captions"]["style"] == "outline", "an unlocked choice of the author is kept"
    assert spec["voiceover"] == {**spec["voiceover"], "voice": "nova", "style": "Bright and crisp", "speed": 1.05}
    assert spec["theme"]["watermark"] == "top_right"


def test_the_intro_and_outro_are_added_with_the_videos_title():
    applied = templates.apply_template(_authored(), _template(_STYLE))
    scenes = applied.spec["scenes"]
    assert [scene["type"] for scene in scenes] == ["title", "recording", "outro"]
    assert applied.added_scenes == ["intro", "outro"]
    assert scenes[0]["title"] == "Hunting with OpenCTI"
    assert scenes[0]["kicker"] == "THREAD 2026"
    assert scenes[0]["style"] == "aurora", "the intro takes the title card defaults"
    assert scenes[-1]["title"] == "See you at Hunting with OpenCTI"


def test_an_authored_opening_card_keeps_its_words_and_takes_the_look():
    authored = _authored(
        scenes=[{"type": "title", "title": "Mine", "kicker": "WORKSHOP"}, {"type": "recording", "take_id": TAKE_ID}]
    )
    applied = templates.apply_template(authored, _template(_STYLE))
    first = applied.spec["scenes"][0]
    assert applied.added_scenes == ["outro"]
    assert (first["title"], first["kicker"]) == ("Mine", "WORKSHOP")
    assert first["duration"] == 3.4 and first["align"] == "left"


def test_a_template_scene_id_never_collides_with_the_authors():
    authored = _authored(scenes=[{"id": "intro", "type": "recording", "take_id": TAKE_ID}])
    applied = templates.apply_template(authored, _template(_STYLE))
    ids = [scene["id"] for scene in applied.spec["scenes"]]
    assert len(ids) == len(set(ids))
    assert applied.added_scenes[0] == "intro-2"


def test_locks_win_and_say_so():
    authored = _authored(
        theme={"background": "grid", "accent": "#ff0000"},
        scenes=[
            {
                "type": "recording",
                "take_id": TAKE_ID,
                "frame": {"style": "none"},
                "overlays": [{"type": "callout", "text": "Here"}],
            }
        ],
    )
    applied = templates.apply_template(authored, _template(_STYLE, name="THREAD"))
    spec = applied.spec
    recording = next(scene for scene in spec["scenes"] if scene["type"] == "recording")
    assert spec["theme"]["background"] == "mosaic"
    assert "accent" not in spec["theme"] or spec["theme"].get("accent") is None
    assert recording["frame"]["style"] == "window" and recording["frame"]["radius"] == 14
    assert recording["cursor"]["style"] == "halo"
    assert recording["overlays"][0]["style"] == "dark"
    text = " ".join(applied.notes)
    assert "theme.accent not kept" in text
    assert "theme.background is set by the THREAD template" in text
    assert "scenes.recording.frame is set by the THREAD template" in text


def test_a_lock_the_author_agrees_with_says_nothing():
    applied = templates.apply_template(_authored(theme={"background": "mosaic"}), _template(_STYLE))
    assert not any("theme.background" in note for note in applied.notes)


def test_a_format_the_brand_does_not_ship_falls_back_to_its_first():
    applied = templates.apply_template(_authored(format={"aspect_ratio": "1:1"}), _template(_STYLE))
    assert applied.spec["format"]["aspect_ratio"] == "16:9"
    assert any("16:9, 9:16" in note for note in applied.notes)


def test_a_scheme_the_template_does_not_dress_falls_back():
    applied = templates.apply_template(_authored(theme={"mode": "light"}), _template(_STYLE, schemes=["dark"]))
    assert applied.spec["theme"]["mode"] == "dark"
    assert any("dark only" in note for note in applied.notes)


def test_without_a_template_the_spec_is_the_authors():
    authored = _authored(freestyle=True, theme={"background": "grid"})
    applied = templates.apply_template(authored, None)
    assert applied.spec == validate_video_spec(authored)
    assert applied.notes == [] and applied.added_scenes == []


def test_a_presentation_template_lends_its_palette_only():
    applied = templates.apply_template(_authored(), _template(kind="presentation"))
    assert [scene["type"] for scene in applied.spec["scenes"]] == ["recording"]
    assert applied.template is not None and applied.template.style is None


def test_freestyle_and_a_template_exclude_each_other():
    with pytest.raises(VideoSpecError, match="freestyle"):
        authored_spec(
            {
                "title": "x",
                "freestyle": True,
                "template_id": str(uuid.uuid4()),
                "scenes": [{"type": "title", "title": "x"}],
            }
        )


def test_the_stored_spec_is_what_the_author_wrote():
    stored = _authored()
    assert set(stored) == {"title", "scenes"}
    assert stored["scenes"][0] == {"id": "recording-1", "type": "recording", "take_id": TAKE_ID}


def test_a_whole_new_spec_keeps_a_free_style_video_in_free_style(monkeypatch):
    """A rewrite naming no look keeps free style, as one keeps its template."""
    platform_default = SimpleNamespace(id=uuid.uuid4(), kind="video")

    async def default_template(_db):
        return platform_default

    monkeypatch.setattr(templates, "default_template", default_template)

    def resolved(authored: dict):
        return asyncio.run(templates.resolve_for_spec(None, None, authored, current_id=None))

    free = _authored(freestyle=True)
    rewrite = _authored(theme={"background": "grid"})
    kept = templates.keep_look(rewrite, free)
    assert kept["freestyle"] is True and resolved(kept) is None
    assert resolved(rewrite) is platform_default
    # A look the new spec names wins: free style turned off, or a template.
    assert resolved(templates.keep_look(_authored(freestyle=False), free)) is platform_default
    chosen = _authored(template_id=str(uuid.uuid4()))
    assert templates.keep_look(chosen, free) is chosen
    # A video that wore a template, or a new one, is left to resolve_for_spec.
    assert templates.keep_look(rewrite, _authored()) is rewrite
    assert templates.keep_look(rewrite, None) is rewrite


# ── Validating the rules ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("style", "fragment"),
    [
        ({"defaults": {"scenes": []}}, "defaults.scenes is not something a template presets"),
        ({"defaults": {"theme": {"accent": "#fff000"}}}, "defaults.theme.accent"),
        ({"scene_defaults": {"recording": {"take_id": TAKE_ID}}}, "the author writes it"),
        ({"scene_defaults": {"recording": {"camera": {"keyframes": []}}}}, "keyframes belong to one recording"),
        ({"overlay_defaults": {"callout": {"text": "Hi"}}}, "overlay_defaults.callout.text"),
        ({"intro": {"scene": {"take_id": TAKE_ID}}}, "intro.scene.take_id"),
        ({"locked": ["captions.style"]}, "names no value"),
        ({"locked": ["format.aspect_ratio"]}, "aspect_ratios"),
        ({"scene_defaults": {"recording": {"camera": {"max_zoom": 9}}}}, "do not make a valid video"),
    ],
)
def test_a_style_that_cannot_dress_a_video_is_refused(style, fragment):
    canonical, problems = templates.validate_style(style)
    assert canonical is None
    assert any(fragment in problem for problem in problems), problems


def test_an_empty_style_is_dropped():
    assert templates.validate_style({}) == (None, [])
    assert template_containers.validate_video_style({"video": {}}, kind="video") == ({}, [])


def test_the_rules_belong_to_video_templates_only():
    metadata = {"video": {"locked": ["nonsense"]}, "guidance": "x"}
    assert template_containers.validate_video_style(metadata, kind="presentation") == ({"guidance": "x"}, [])
    _, problems = template_containers.validate_video_style(metadata, kind="video")
    assert problems


def test_video_is_a_template_kind_with_schemes_and_no_layouts():
    assert template_containers.normalize_kind("Video") == "video"
    assert template_containers.has_color_schemes("video")
    assert not template_containers.has_color_schemes("report")
    assert template_containers.validate_layouts([{"id": "title"}], kind="video") == ([], [])
    kept, problems = template_containers.validate_supported_schemes({"supported_schemes": ["dark"]}, kind="video")
    assert kept == {"supported_schemes": ["dark"]} and problems == []


# ── The palette a video reads ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "rgb"),
    [
        ("#ff0000", (1.0, 0.0, 0.0)),
        ("#0f0", (0.0, 1.0, 0.0)),
        ("rgb(0 0 255)", (0.0, 0.0, 1.0)),
        ("rgb(0, 0, 255)", (0.0, 0.0, 1.0)),
        ("hsl(0 100% 50%)", (1.0, 0.0, 0.0)),
        ("hsl(120, 100%, 50%)", (0.0, 1.0, 0.0)),
        ("hsl(270 100% 65%)", None),
    ],
)
def test_css_colours_are_read(value, rgb):
    parsed = themes.parse_color(value)
    assert parsed is not None
    if rgb is not None:
        assert parsed == pytest.approx(rgb, abs=0.01)


def test_unknown_colours_are_ignored():
    assert themes.parse_color("var(--x)") is None
    assert themes.parse_color("red") is None


def test_the_xtm_one_palette_reaches_the_video():
    palette = _entry("xtm-one-video")["theme_config"]
    theme = themes.build_theme(mode="dark", background_style="mesh", template_palette=palette)
    assert theme["accent"] == "hsl(270 100% 65%)"
    assert theme["gradient_from"] == "hsl(270 100% 65%)" and theme["gradient_to"] == "hsl(197 100% 53%)"
    assert len(theme["series"]) == 6


def test_the_thread_palette_brings_its_fonts_and_gradient():
    palette = _entry("thread-video")["theme_config"]
    theme = themes.build_theme(mode="dark", background_style="mosaic", template_palette=palette, has_mark=True)
    assert theme["heading_font"].startswith("'Elms Sans'")
    assert theme["accent"] == palette["accent"]
    assert (theme["gradient_from"], theme["gradient_to"]) == (palette["gradient_from"], palette["gradient_to"])
    assert theme["series"] == [palette[f"chart_{i}"] for i in range(1, 7)]
    assert theme["on_accent"] == "#0b0f19", "THREAD cyan is light: dark text on it"
    assert theme["mark"] is True


def test_the_authors_accent_wins_when_the_palette_is_not_locked():
    theme = themes.build_theme(
        mode="dark", background_style="aurora", template_palette={"accent": "#00e0ff"}, accent="#ff0000"
    )
    assert theme["accent"] == "#ff0000"
    assert theme["gradient_from"] == "#ff0000"
