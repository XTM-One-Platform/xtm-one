"""A card never writes the brand name its logo already spells.

A video on the Filigran Brand Film template opened on its logo (the Filigran
wordmark), a kicker "FILIGRAN" and the title: the brand twice on one card,
and nothing in the spec could take the kicker off. What is proven:

* every built-in video template's cards, rendered, show the brand once - its
  logo - in both colour schemes it ships;
* a template kicker naming the brand the card's logo spells is left off, as
  on a template duplicated before the built-ins dropped theirs; the symbol
  alone (a ``logo_mark``) and a logo the template does not caption spell no
  name, so their kicker stays;
* ``template_scenes.<card>.kicker`` sets the kicker of a card the template
  adds - kept even when it names the brand - and ``null`` removes it; the
  spec keeps the ``null`` and bounds the text;
* the quality check reads "brand name drawn twice on the same card" on the
  template's cards and on the author's own cards and statements drawing a
  logo or a logo overlay, as a finding the audience would see, naming the
  field that fixes it;
* the marks are read from the template's assets: the file the palette names,
  else the oldest asset of the role.
"""

from __future__ import annotations

import asyncio
import copy
import os
import uuid
from types import SimpleNamespace

import pytest

from app.services.template_asset_seed import load_brand_manifests, merge_manifests
from app.services.template_container_seed import BUILTIN_TEMPLATE_CONTAINERS
from app.services.video_studio import quality, templates, timeline
from app.services.video_studio.render_checks import BLOCKING_SEVERITIES
from app.services.video_studio.spec import (
    BRAND_ASSETS,
    VideoSpecError,
    authored_spec,
    brand_marks,
    parse_video_spec,
    validate_video_spec,
)
from app.services.video_studio.themes import build_theme

_SEEDED = [entry for entry in BUILTIN_TEMPLATE_CONTAINERS if entry["kind"] == "video"]
#: The intro kickers the built-ins shipped earlier: a duplicate made then still carries one.
_KICKERS_AS_SHIPPED = {
    "filigran-video": "Filigran",
    "filigran-brand-film": "Filigran",
    "thread-video": "THREAD 2026",
    "xtm-one-video": "XTM One",
    "xtm-one-brand-film": "XTM One",
}


def _as_shipped(entry: dict, kicker: str) -> dict:
    duplicate = copy.deepcopy(entry)
    duplicate["extra_metadata"]["video"]["intro"]["scene"]["kicker"] = kicker
    return {**duplicate, "case": f"{entry['slug']}-as-shipped"}


_VIDEO_TEMPLATES = [
    *_SEEDED,
    *(_as_shipped(entry, _KICKERS_AS_SHIPPED[entry["slug"]]) for entry in _SEEDED if entry["slug"] in _KICKERS_AS_SHIPPED),
]
_MANIFESTS = merge_manifests(load_brand_manifests())
LOGO, LOGO_LIGHT, MARK = (str(uuid.uuid5(uuid.NAMESPACE_URL, role)) for role in BRAND_ASSETS)


def _seeded_marks(slug: str) -> dict[str, templates.BrandMark]:
    """The marks a built-in template draws: its manifest's brand assets and their captions."""
    marks: dict[str, templates.BrandMark] = {}
    for asset in _MANIFESTS[slug]:
        if asset.role in BRAND_ASSETS and asset.role not in marks:
            marks[asset.role] = templates.BrandMark(str(uuid.uuid5(uuid.NAMESPACE_URL, asset.filename)), asset.role, asset.description)
    return marks


def _template(name: str, style: dict, marks: dict[str, templates.BrandMark], *, schemes=None) -> templates.VideoTemplate:
    row = SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        kind="video",
        description=None,
        tags=[],
        theme_config={"background": "#000000", "foreground": "#ffffff", "accent": "#00e0ff"},
        extra_metadata={"video": style, "supported_schemes": schemes or ["dark", "light"]},
    )
    template = templates.VideoTemplate.from_container(row)
    template.marks = dict(marks)
    return template


def _built_in(entry: dict) -> templates.VideoTemplate:
    row = SimpleNamespace(
        id=uuid.uuid4(),
        name=entry["name"],
        kind="video",
        description=None,
        tags=entry["tags"],
        theme_config=entry["theme_config"],
        extra_metadata=entry["extra_metadata"],
    )
    template = templates.VideoTemplate.from_container(row)
    template.marks = _seeded_marks(entry["slug"])
    return template


def _brand(template: templates.VideoTemplate) -> str:
    return templates.brand_names(template)[0]


def _xtm_blast(**top) -> dict:
    """The video of the report: "XTM Blast", one statement, the template's cards around it."""
    return authored_spec({"title": "XTM Blast", "scenes": [{"id": "s1", "type": "statement", "lines": ["One"]}], **top})


#: The Filigran Brand Film as it shipped earlier: a kicker naming the brand its logo spells.
_OLD_FILM = {
    "intro": {"required": True, "scene": {"kicker": "Filigran", "duration": 3.6}},
    "outro": {"required": True, "scene": {"title": "Uncover threats.\n*Take action.*", "url": "filigran.io"}},
    "brand_name": "Filigran",
}
_FILIGRAN_MARKS = {
    "logo": templates.BrandMark(LOGO, "logo", "Filigran wordmark (cyan mark + white wordmark), light-on-dark"),
    "logo_light": templates.BrandMark(LOGO_LIGHT, "logo_light", "Filigran wordmark in dark ink"),
    "logo_mark": templates.BrandMark(MARK, "logo_mark", "Filigran symbol alone"),
}


def _cards(entry: dict, scheme: str) -> list[dict]:
    template = _built_in(entry)
    applied = templates.apply_template(_xtm_blast(theme={"mode": scheme}), template)
    return [scene for scene in applied.spec["scenes"] if scene["id"] in applied.added_scenes]


def _occurrences(scene: dict, template: templates.VideoTemplate, theme: dict) -> int:
    """How often *scene* shows its template's brand: once per logo spelling it, once per text writing it."""
    brand = [_brand(template)]
    marks = brand_marks(template.marks, light=theme.get("mode") == "light")
    logo = marks["logo"] if scene.get("show_logo") else None
    spelled = 1 if logo is not None and logo.role != "logo_mark" and templates.names_in(logo.caption, brand) else 0
    words = [scene.get(key) for key in ("kicker", "title", "subtitle", "cta", "url")]
    return spelled + sum(len(templates.names_in(text, brand)) for text in words if text)


# ── Every built-in template ───────────────────────────────────────────────

def _case(entry: dict) -> str:
    return entry.get("case") or entry["slug"]


_CASES = [
    pytest.param(entry, scheme, id=f"{_case(entry)}-{scheme}")
    for entry in _VIDEO_TEMPLATES
    for scheme in entry["extra_metadata"].get("supported_schemes", ["dark"])
    if entry["extra_metadata"]["video"].get("intro") or entry["extra_metadata"]["video"].get("outro")
]


@pytest.mark.parametrize(("entry", "scheme"), _CASES)
def test_every_built_in_templates_cards_show_the_brand_once(entry, scheme):
    """Its logo spells the brand; no word of the card spells it again, and the quality check agrees."""
    template = _built_in(entry)
    applied = templates.apply_template(_xtm_blast(theme={"mode": scheme}), template)
    theme = applied.spec["theme"]
    cards = [scene for scene in applied.spec["scenes"] if scene["id"] in applied.added_scenes]
    assert cards, entry["slug"]
    for card in cards:
        if card.get("show_logo"):
            assert _occurrences(card, template, theme) == 1, (entry["slug"], card)
        assert templates.brand_repeats(card, template, theme) == [], (entry["slug"], card)
    spec = parse_video_spec(applied.spec)
    facts = quality.ContentFacts(template=template)
    found = quality.content_findings(spec, timeline.estimate_plan(spec, {}).document, facts, _xtm_blast())
    assert [f.message for f in found if f.kind == "brand_repeated"] == []


def test_the_built_ins_declare_the_name_their_logo_spells():
    for entry in _SEEDED:
        template = _built_in(entry)
        logo = template.marks["logo"]
        assert templates.names_in(logo.caption, [_brand(template)]), entry["slug"]


# ── The rule ──────────────────────────────────────────────────────────────


def test_a_template_kicker_naming_the_brand_its_logo_spells_is_left_off():
    """The Brand Film as it shipped: "XTM Blast" under the Filigran wordmark, with no FILIGRAN between them."""
    template = _template("Filigran Brand Film", _OLD_FILM, _FILIGRAN_MARKS)
    intro = templates.apply_template(_xtm_blast(), template).spec["scenes"][0]
    assert intro["id"] == "intro" and intro["title"] == "XTM Blast" and "kicker" not in intro
    assert _occurrences(intro, template, {"mode": "dark"}) == 1
    # A light video draws the light logo, which spells it too.
    light = templates.apply_template(_xtm_blast(theme={"mode": "light"}), template).spec["scenes"][0]
    assert "kicker" not in light


def test_a_kicker_the_name_appears_in_is_left_off_whatever_its_case_and_its_other_words():
    template = _template("Conference", {**_OLD_FILM, "intro": {"scene": {"kicker": "FILIGRAN 2026"}}}, _FILIGRAN_MARKS)
    assert "kicker" not in templates.apply_template(_xtm_blast(), template).spec["scenes"][0]


def test_a_kicker_that_is_not_the_brand_stays():
    template = _template("Filigran Brand Film", {**_OLD_FILM, "intro": {"scene": {"kicker": "Threat briefing"}}}, _FILIGRAN_MARKS)
    assert templates.apply_template(_xtm_blast(), template).spec["scenes"][0]["kicker"] == "Threat briefing"


def test_the_symbol_alone_and_an_uncaptioned_logo_spell_no_name():
    """No wordmark on the card, nothing repeated: the kicker is the only place the name is written."""
    no_logo = {**_OLD_FILM, "intro": {"scene": {"kicker": "Filigran", "show_logo": False}}}
    with_mark = {
        **no_logo,
        "scene_defaults": {"title": {"overlays": [{"type": "image", "asset": "logo_mark", "position": "top_left"}]}},
    }
    assert templates.apply_template(_xtm_blast(), _template("Filigran", with_mark, _FILIGRAN_MARKS)).spec["scenes"][0][
        "kicker"
    ] == "Filigran"
    uncaptioned = {role: templates.BrandMark(mark.file_id, role) for role, mark in _FILIGRAN_MARKS.items()}
    intro = templates.apply_template(_xtm_blast(), _template("Filigran", _OLD_FILM, uncaptioned)).spec["scenes"][0]
    assert intro["kicker"] == "Filigran"
    # A template without a mark of its own draws its logo for the mark: that one spells the name.
    logo_only = {"logo": _FILIGRAN_MARKS["logo"]}
    intro = templates.apply_template(_xtm_blast(), _template("Filigran", with_mark, logo_only)).spec["scenes"][0]
    assert "kicker" not in intro


def test_the_videos_own_logo_replaces_the_templates_and_spells_nothing_known():
    template = _template("Filigran Brand Film", _OLD_FILM, _FILIGRAN_MARKS)
    own = templates.apply_template(_xtm_blast(theme={"logo_file_id": str(uuid.uuid4())}), template).spec["scenes"][0]
    assert own["kicker"] == "Filigran"
    # The template's own logo named as the video's: still the wordmark.
    named = templates.apply_template(_xtm_blast(theme={"logo_file_id": LOGO}), template).spec["scenes"][0]
    assert "kicker" not in named


def test_the_template_name_is_the_brand_when_it_declares_none():
    style = {key: value for key, value in _OLD_FILM.items() if key != "brand_name"}
    acme = {"logo": templates.BrandMark(LOGO, "logo", "The Acme wordmark")}
    template = _template("Acme", {**style, "intro": {"scene": {"kicker": "Acme"}}}, acme)
    assert "kicker" not in templates.apply_template(_xtm_blast(), template).spec["scenes"][0]
    assert templates.brand_names(_template("Filigran Brand Film", _OLD_FILM, {})) == ["Filigran", "Filigran Brand Film"]


def test_an_address_is_one_word_never_the_brand():
    assert templates.names_in("filigran.io/thread", ["THREAD", "Filigran"]) == []
    assert templates.names_in("See you at THREAD", ["thread"]) == ["thread"]
    assert templates.names_in("Filigran's platform", ["Filigran"]) == ["Filigran"]
    assert templates.names_in("XTM One automates", ["XTM One", "XTM"]) == ["XTM One", "XTM"]
    assert templates.names_in("XTM Blast", ["XTM One"]) == []


# ── template_scenes.<card>.kicker ─────────────────────────────────────────


def test_the_spec_sets_the_kicker_of_the_cards_the_template_adds():
    template = _template("Filigran Brand Film", _OLD_FILM, _FILIGRAN_MARKS)
    keynote = templates.apply_template(
        _xtm_blast(template_scenes={"intro": {"kicker": "Keynote"}, "outro": {"kicker": "Thank you"}}), template
    )
    intro, outro = keynote.spec["scenes"][0], keynote.spec["scenes"][-1]
    assert (intro["kicker"], outro["kicker"]) == ("Keynote", "Thank you")
    planned = timeline.estimate_plan(parse_video_spec(keynote.spec), {}).document["scenes"]
    assert (planned[0]["kicker"], planned[-1]["kicker"]) == ("Keynote", "Thank you")
    # Set by the spec, the brand name stays: the author asked for it, the quality check says what it costs.
    asked = templates.apply_template(_xtm_blast(template_scenes={"intro": {"kicker": "Filigran"}}), template)
    assert asked.spec["scenes"][0]["kicker"] == "Filigran"


def test_a_null_kicker_is_no_kicker_and_the_spec_keeps_it():
    style = {**_OLD_FILM, "intro": {"scene": {"kicker": "Threat briefing"}}}
    template = _template("Filigran Brand Film", style, _FILIGRAN_MARKS)
    authored = _xtm_blast(template_scenes={"intro": {"kicker": None, "duration": 5}})
    assert authored["template_scenes"] == {"intro": {"kicker": None, "duration": 5.0}}
    assert validate_video_spec(authored)["template_scenes"]["intro"] == {"duration": 5.0, "kicker": None}
    intro = templates.apply_template(authored, template).spec["scenes"][0]
    assert "kicker" not in intro and intro["duration"] == 5.0
    # Blank is not a choice: the template's kicker stays.
    blank = templates.apply_template(_xtm_blast(template_scenes={"intro": {"kicker": "  "}}), template)
    assert blank.spec["scenes"][0]["kicker"] == "Threat briefing"


def test_the_spec_bounds_a_cards_kicker():
    with pytest.raises(VideoSpecError, match=r"template_scenes\.intro\.kicker"):
        _xtm_blast(template_scenes={"intro": {"kicker": "x" * 81}})
    with pytest.raises(VideoSpecError, match=r"template_scenes\.outro\.kicker"):
        _xtm_blast(template_scenes={"outro": {"kicker": 7}})


def test_a_template_may_preset_its_closing_cards_kicker():
    style = {**_OLD_FILM, "outro": {"required": True, "scene": {"title": "Thanks", "kicker": "THREAD 2026"}}}
    canonical, problems = templates.validate_style(style)
    assert problems == [] and canonical["outro"]["scene"]["kicker"] == "THREAD 2026"


# ── The quality check ─────────────────────────────────────────────────────


def _findings(raw: dict, template: templates.VideoTemplate) -> list[quality.QualityFinding]:
    authored = authored_spec(raw)
    spec = parse_video_spec(templates.apply_template(authored, template).spec)
    document = timeline.estimate_plan(spec, {}).document
    found = quality.content_findings(spec, document, quality.ContentFacts(template=template), authored)
    return [finding for finding in found if finding.kind == "brand_repeated"]


def test_a_kicker_the_spec_sets_to_the_brand_is_a_finding_naming_its_fix():
    template = _template("Filigran Brand Film", _OLD_FILM, _FILIGRAN_MARKS)
    raw = {"title": "XTM Blast", "scenes": [{"type": "statement", "lines": ["One"]}], "template_scenes": {"intro": {"kicker": "FILIGRAN"}}}
    [finding] = _findings(raw, template)
    assert (finding.scene_id, finding.scene_index, finding.severity) == ("intro", None, "dense")
    # What the audience would see: the render waits for it.
    assert finding.severity in BLOCKING_SEVERITIES
    assert 'its logo already spells "Filigran" and its kicker "FILIGRAN" writes it again' in finding.message
    assert "template_scenes.intro.kicker: null" in finding.message
    raw["template_scenes"] = {"intro": {"kicker": None}}
    assert _findings(raw, template) == []


def test_a_video_titled_with_the_brand_is_told_to_retitle_or_open_on_its_own_card():
    template = _template("Filigran Brand Film", _OLD_FILM, _FILIGRAN_MARKS)
    [finding] = _findings({"title": "Filigran in 2026", "scenes": [{"type": "statement", "lines": ["One"]}]}, template)
    assert finding.scene_id == "intro" and "retitle the video" in finding.message


def test_the_authors_own_card_and_statement_drawing_a_logo_are_checked_too():
    template = _template("Filigran Brand Film", _OLD_FILM, _FILIGRAN_MARKS)
    raw = {
        "title": "XTM Blast",
        "scenes": [
            {"id": "open", "type": "title", "title": "Filigran XTM Blast"},
            {
                "id": "proof",
                "type": "statement",
                "lines": ["Built by *Filigran*"],
                "overlays": [{"type": "image", "asset": "logo", "position": "top_left"}],
            },
            {"id": "quiet", "type": "statement", "lines": ["Built by Filigran"]},
            {"id": "marked", "type": "statement", "lines": ["Built by Filigran"], "overlays": [{"type": "image", "asset": "logo_mark"}]},
        ],
    }
    found = {finding.scene_id: finding for finding in _findings(raw, template)}
    assert set(found) == {"open", "proof"}
    assert found["open"].scene_index == 0 and "hide its logo (show_logo: false)" in found["open"].message
    assert "its logo overlay (overlays.0)" in found["proof"].message and 'its lines.0 "Built by Filigran"' in found["proof"].message
    assert "drop the logo overlay (overlays.0)" in found["proof"].message


def test_a_statement_s_logo_the_watermark_hides_spells_no_brand():
    """A statement draws no logo while the watermark is on - nothing to read twice on it."""
    template = _template("Filigran Brand Film", _OLD_FILM, _FILIGRAN_MARKS)
    statement = {"id": "proof", "type": "statement", "lines": ["Built by Filigran"], "show_logo": True}
    raw = {"title": "XTM Blast", "scenes": [statement]}
    assert [f.scene_id for f in _findings({**raw, "theme": {"watermark": "none"}}, template)] == ["proof"]
    assert _findings({**raw, "theme": {"watermark": "bottom_right"}}, template) == []


def test_a_free_style_video_and_a_template_without_marks_have_no_brand_finding():
    raw = {"title": "Filigran", "scenes": [{"type": "title", "title": "Filigran", "kicker": "Filigran"}]}
    assert _findings({**raw, "freestyle": True}, None) == []
    assert _findings(raw, _template("Filigran Brand Film", _OLD_FILM, {})) == []


# ── Reading the marks ─────────────────────────────────────────────────────


class _Rows:
    def __init__(self, rows):
        self.rows = rows
        self.queries = 0

    async def execute(self, _statement):
        self.queries += 1
        return SimpleNamespace(all=lambda: list(self.rows))


def test_the_marks_are_the_palettes_files_else_the_oldest_asset_of_each_role():
    other_logo = str(uuid.uuid4())
    rows = [
        (uuid.UUID(LOGO), "logo", "Filigran wordmark"),
        (uuid.UUID(other_logo), "logo", "Filigran wordmark, the 2025 one"),
        (uuid.UUID(MARK), "logo_mark", "Filigran symbol alone"),
    ]
    container = SimpleNamespace(
        id=uuid.uuid4(),
        name="Filigran Brand Film",
        kind="video",
        description=None,
        tags=[],
        theme_config={"background": "#000000"},
        extra_metadata={"video": {"brand_name": "Filigran"}},
    )
    template = asyncio.run(templates.load_video_template(_Rows(rows), container))
    assert {role: mark.caption for role, mark in template.marks.items()} == {
        "logo": "Filigran wordmark",
        "logo_mark": "Filigran symbol alone",
    }
    container.theme_config = {"background": "#000000", "logo_file_id": other_logo}
    named = asyncio.run(templates.load_video_template(_Rows(rows), container))
    assert named.marks["logo"].file_id == other_logo
    no_container = _Rows(rows)
    assert asyncio.run(templates.load_video_template(no_container, None)) is None and no_container.queries == 0


# ── In the compositor ─────────────────────────────────────────────────────


async def _drawn_card(document: dict, frame: int) -> dict:
    """The words and the logo of the card on screen at *frame*, as the compositor draws it."""
    from playwright.async_api import async_playwright

    from app.services.builtin_tools.html_render import font_face_css
    from app.services.headless_render import RENDER_CHROMIUM_ARGS, RENDER_SERVICE_WORKERS
    from app.services.video_studio.compositor_page import ORIGIN, page_html

    html = page_html(font_face_css()).encode("utf-8")
    index = f"{ORIGIN}/index.html"

    async def handle(route) -> None:
        if route.request.url == index:
            await route.fulfill(status=200, body=html, content_type="text/html; charset=utf-8")
        else:
            await route.abort("blockedbyclient")

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=list(RENDER_CHROMIUM_ARGS))
            try:
                context = await browser.new_context(
                    viewport={"width": int(document["width"]), "height": int(document["height"])},
                    service_workers=RENDER_SERVICE_WORKERS,
                )
                await context.route("**/*", handle)
                page = await context.new_page()
                await page.goto(index, wait_until="load")
                await page.evaluate("doc => window.__xtm.load(doc)", {**document, "preview_media": {}})
                await page.evaluate("frame => window.__xtm.render(frame)", frame)
                # A title card is a `.card`, a statement's a `.card.stmt`: the one on screen is the intro.
                return await page.evaluate(
                    """() => {
                      const card = [...document.querySelectorAll('.card:not(.stmt)')].find(c => c.getClientRects().length);
                      return card ? { text: card.innerText, logo: !!card.querySelector('img.logo') } : null;
                    }"""
                )
            finally:
                await browser.close()
    except Exception as exc:
        text = str(exc)
        if "Executable doesn't exist" not in text and "playwright install" not in text.lower():
            raise
        if os.environ.get("REQUIRE_CHROMIUM") == "1":
            pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
        pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")


_INTROS = [entry for entry in _VIDEO_TEMPLATES if (entry["extra_metadata"]["video"].get("intro") or {}).get("required")]


@pytest.mark.chromium
@pytest.mark.parametrize("entry", _INTROS, ids=_case)
async def test_the_intro_card_of_every_built_in_template_renders_the_brand_once(entry):
    """Rendered: the logo spells the brand, and the brand appears nowhere in the card's drawn words."""
    template = _built_in(entry)
    spec = parse_video_spec(templates.apply_template(_xtm_blast(), template).spec)
    theme = build_theme(
        mode=spec.theme.mode,
        background_style=spec.theme.background,
        template_palette=entry["theme_config"],
        has_logo=True,
        has_mark=True,
    )
    document = timeline.plan(spec, timeline.PlanMedia(), fps=30, theme=theme).document
    intro, following = document["scenes"][:2]
    assert intro["id"] == "intro"
    # Every block of the card in, before the next scene comes over it.
    drawn = await _drawn_card(document, int(min(2.0, float(following["start"]) - 0.1) * 30))
    assert drawn is not None and drawn["logo"], entry["slug"]
    assert templates.names_in(drawn["text"], ["XTM Blast"]) == ["XTM Blast"], drawn["text"]
    brand = _brand(template)
    assert templates.names_in(template.marks["logo"].caption, [brand])
    assert templates.names_in(drawn["text"], [brand]) == [], (entry["slug"], drawn["text"])
