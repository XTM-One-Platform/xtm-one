"""Tests for the "Generate with AI" template generator.

The generator is a thin orchestration around three promises:

* the model's proposal is normalised by the SAME validators an agent authoring
  a template hits, and whatever they prune is REPORTED, never silently dropped;
* the meta-prompt is derived from the enforcing modules, so it can never
  document a theme key or a layout id the validator would then reject;
* a logo's dominant colours are a best-effort hint that never fails the brief.
"""

from __future__ import annotations

import asyncio
import io
import json
from types import SimpleNamespace

import pytest

from app.services import template_generation as gen
from app.services.presentation_spec import (
    PRESENTATION_ASPECT_RATIOS,
    PRESENTATION_THEMES,
    PRESENTATION_TRANSITIONS,
    SLIDE_LAYOUTS,
    layout_catalog,
)
from app.services.presentations import THEME_CONFIG_KEYS, THEME_FILE_ID_KEYS
from app.services.reports import REPORT_THEME_CONFIG_KEYS


def _proposal(**overrides):
    base = {
        "name": "Corporate Navy",
        "description": "A calm corporate deck for quarterly reviews.",
        "theme": "Corporate Navy!",
        "tags": ["Corporate", " finance ", "corporate", "builtin"],
        "aspect_ratio": "16:9",
        "theme_config": {
            "background": "#0b1020",
            "surface": "#141c33",
            "line": "#2a3558",
            "foreground": "#f5f7fb",
            "muted": "#aeb6cc",
            "accent": "#d4af37",
            "info": "#4f8cff",
            "success": "#3ccf6a",
            "warning": "#f0a93a",
            "error": "#f05a5a",
            "gradient_from": "#d4af37",
            "gradient_to": "#4f8cff",
            "heading_font": "'Playfair Display', Georgia, serif",
            "body_font": "'Inter', system-ui, sans-serif",
        },
        "layouts": [
            {"id": "title", "name": "Cover", "use_for": "Open the deck."},
            {"id": "agenda", "use_for": "Announce the chapters."},
            {"id": "content", "use_for": "Tell the story."},
            {"id": "closing", "use_for": "Close with a call to action."},
        ],
        "extra_metadata": {
            "deck_defaults": {"theme": "dark", "transition": "fade", "aspect_ratio": "16:9"},
            "guidance": "Open with a `title` slide, keep one idea per `content` slide.",
            "brand": {"source": "brief", "audience": "executives"},
        },
    }
    base.update(overrides)
    return base


# ── Normalisation ────────────────────────────────────────────────────────────


def test_clean_proposal_passes_through_untouched():
    candidate, warnings = gen.normalize_template_candidate(_proposal(), kind="presentation")

    assert warnings == []
    assert candidate["name"] == "Corporate Navy"
    assert candidate["kind"] == "presentation"
    # The legacy free-text ``theme`` label is neither asked for nor kept:
    # a label the model invents renders nothing, and the deck themes
    # a template supports are derived from its palette instead.
    assert candidate["theme"] is None
    assert candidate["aspect_ratio"] == "16:9"
    # Tags go through ``normalize_tags``: trimmed, lower-cased, de-duplicated,
    # and the seed's identity tag is refused so a generated template can never
    # be mistaken for a built-in.
    assert candidate["tags"] == ["corporate", "finance"]
    assert candidate["theme_config"]["accent"] == "#d4af37"
    assert [layout["id"] for layout in candidate["layouts"]] == ["title", "agenda", "content", "closing"]
    # ``fields`` is re-derived from the spec models, never taken from the model.
    assert candidate["layouts"][0]["fields"] == layout_catalog()["title"]
    assert candidate["layouts"][0]["name"] == "Cover"
    assert candidate["extra_metadata"]["deck_defaults"] == {
        "theme": "dark",
        "transition": "fade",
        "aspect_ratio": "16:9",
    }
    assert candidate["extra_metadata"]["guidance"].startswith("Open with a `title`")
    assert candidate["extra_metadata"]["brand"] == {"source": "brief", "audience": "executives"}


def test_unknown_theme_keys_and_unsafe_values_are_pruned_and_reported():
    proposal = _proposal()
    proposal["theme_config"]["shadow"] = "0 0 10px black"
    proposal["theme_config"]["accent"] = "url(https://evil/x.png)"
    proposal["theme_config"]["logo_file_id"] = "not-a-uuid"

    candidate, warnings = gen.normalize_template_candidate(proposal, kind="presentation")

    assert "shadow" not in candidate["theme_config"]
    assert "accent" not in candidate["theme_config"]
    assert "logo_file_id" not in candidate["theme_config"]
    joined = "\n".join(warnings)
    assert "'shadow' is not honoured" in joined
    assert "theme_config['accent']" in joined
    assert "theme_config['logo_file_id']" in joined
    # The accent was pruned, so the "missing brand key" warning names it too.
    assert "missing accent" in joined


def test_invented_layout_ids_are_dropped_and_an_empty_catalog_falls_back_to_everything():
    proposal = _proposal(layouts=[{"id": "hero_banner"}, {"id": "title"}, "title"])
    candidate, warnings = gen.normalize_template_candidate(proposal, kind="presentation")
    assert [layout["id"] for layout in candidate["layouts"]] == ["title"]
    joined = "\n".join(warnings)
    assert "'hero_banner' is not a slide layout" in joined
    assert "listed twice" in joined

    proposal = _proposal(layouts=[{"id": "nope"}])
    candidate, warnings = gen.normalize_template_candidate(proposal, kind="presentation")
    assert [layout["id"] for layout in candidate["layouts"]] == list(SLIDE_LAYOUTS)
    assert any("full catalog was used" in w for w in warnings)
    # The fallback carries the same labels the built-in seed advertises.
    assert all(layout.get("fields") for layout in candidate["layouts"])


def test_deck_defaults_are_checked_against_the_spec_enums():
    proposal = _proposal(
        extra_metadata={
            "deck_defaults": {"theme": "neon", "transition": "Slide", "aspect_ratio": "21:9", "speed": "fast"},
            "guidance": "x",
            "author": "someone",
        }
    )
    candidate, warnings = gen.normalize_template_candidate(proposal, kind="presentation")

    defaults = candidate["extra_metadata"]["deck_defaults"]
    # Case-insensitive match keeps a valid value; invalid ones are dropped, the
    # aspect ratio is back-filled from the candidate so a preview renders in the
    # right geometry, and the dropped theme is back-filled from the schemes the
    # palette supports - a template's default deck theme is always one
    # its own declaration allows.
    assert defaults == {"theme": "dark", "transition": "slide", "aspect_ratio": "16:9"}
    joined = "\n".join(warnings)
    assert "deck_defaults.theme = 'neon'" in joined
    assert "deck_defaults.aspect_ratio = '21:9'" in joined
    assert "deck_defaults.speed is not a deck default" in joined
    assert "extra_metadata.author is not read by the platform" in joined
    assert "author" not in candidate["extra_metadata"]


def test_deck_defaults_keep_the_per_cover_objects_through_the_one_whitelist():
    """The per-cover defaults (`hero_defaults` / `hero_tones` /
    `slide_themes`) a generated template carries go through the renderer's own
    whitelist (`template_deck_defaults`) - kept when usable, dropped with a
    warning when nothing in them is - never a second table that disagrees."""
    proposal = _proposal(
        extra_metadata={
            "deck_defaults": {
                "theme": "dark",
                "hero_defaults": {"title": "Gradient", "closing": "neon", "content": "gradient"},
                "hero_tones": {"title": "accent"},
                "slide_themes": {"closing": "sepia"},
            },
            "guidance": "x",
        }
    )
    candidate, warnings = gen.normalize_template_candidate(proposal, kind="presentation")

    defaults = candidate["extra_metadata"]["deck_defaults"]
    assert defaults["hero_defaults"] == {"title": "gradient"}
    assert defaults["hero_tones"] == {"title": "accent"}
    assert "slide_themes" not in defaults
    joined = "\n".join(warnings)
    assert "deck_defaults.slide_themes carried no usable title / closing value" in joined
    assert "hero_defaults is not a deck default" not in joined
    # An entry dropped INSIDE a kept object is named too, with its reason: the
    # agent must not believe the closing's cover landed with the title's.
    assert (
        "deck_defaults.hero_defaults.closing = 'neon' is not one of plain, gradient, split, spotlight; "
        "it was dropped."
    ) in joined
    assert "deck_defaults.hero_defaults.content is not a cover layout (title / closing only); it was dropped." in joined
    assert "hero_defaults.title" not in joined and "hero_tones.title" not in joined


# ── The scheme declaration a generated template carries ──────────────


def test_a_generated_deck_template_declares_the_scheme_its_palette_reads_as():
    candidate, warnings = gen.normalize_template_candidate(_proposal(), kind="presentation")
    # A navy background is a dark palette: the template is declared dark-only,
    # which is what the New Template form's Dark / Light switches start from.
    assert candidate["extra_metadata"]["supported_schemes"] == ["dark"]
    assert warnings == []

    light = _proposal()
    light["theme_config"] = {**light["theme_config"], "background": "#ffffff", "foreground": "#111111"}
    light["extra_metadata"] = {**light["extra_metadata"], "deck_defaults": {"theme": "light"}}
    candidate, warnings = gen.normalize_template_candidate(light, kind="presentation")
    assert candidate["extra_metadata"]["supported_schemes"] == ["light"]
    assert candidate["extra_metadata"]["deck_defaults"]["theme"] == "light"


def test_a_deck_default_theme_outside_the_declaration_is_snapped_and_reported():
    proposal = _proposal(extra_metadata={"deck_defaults": {"theme": "light"}, "guidance": "x"})
    candidate, warnings = gen.normalize_template_candidate(proposal, kind="presentation")
    # The palette is dark and ships no light variant: a light default would
    # make every deck generated from the template refused by its own template.
    assert candidate["extra_metadata"]["supported_schemes"] == ["dark"]
    assert candidate["extra_metadata"]["deck_defaults"]["theme"] == "dark"
    assert any("deck_defaults.theme = 'light' is not a scheme this palette supports" in w for w in warnings)


def test_a_second_palette_is_kept_and_widens_the_declaration():
    proposal = _proposal()
    proposal["extra_metadata"] = {
        **proposal["extra_metadata"],
        "theme_variants": {
            "light": {"background": "#ffffff", "foreground": "#111111", "shadow": "0 0 1px"},
            "sepia": {"background": "#f4ecd8"},
        },
    }
    candidate, warnings = gen.normalize_template_candidate(proposal, kind="presentation")
    # A refinement round echoes the template's own light palette: it is kept,
    # held to the palette contract, and the template now supports both schemes.
    assert candidate["extra_metadata"]["theme_variants"] == {
        "light": {"background": "#ffffff", "foreground": "#111111"}
    }
    assert candidate["extra_metadata"]["supported_schemes"] == ["dark", "light"]
    joined = "\n".join(warnings)
    assert "theme_variants.sepia is not a scheme" in joined
    assert "'shadow' is not honoured" in joined


def test_a_palette_with_no_readable_background_supports_both_schemes():
    proposal = _proposal()
    proposal["theme_config"] = {k: v for k, v in proposal["theme_config"].items() if k != "background"}
    candidate, _warnings = gen.normalize_template_candidate(proposal, kind="presentation")
    assert candidate["extra_metadata"]["supported_schemes"] == ["dark", "light"]


def test_a_report_candidate_carries_no_scheme_declaration():
    candidate, _warnings = gen.normalize_template_candidate(_proposal(), kind="report")
    assert "supported_schemes" not in candidate["extra_metadata"]


def test_an_unknown_kind_falls_back_to_presentation():
    # The route refuses it before the model runs; the normaliser must
    # still never emit a candidate no create route accepts.
    candidate, _warnings = gen.normalize_template_candidate(_proposal(), kind="dashboard")
    assert candidate["kind"] == "presentation"


def test_unknown_aspect_ratio_falls_back_to_16_9_for_decks():
    candidate, warnings = gen.normalize_template_candidate(_proposal(aspect_ratio="21:9"), kind="presentation")
    assert candidate["aspect_ratio"] == "16:9"
    assert any("aspect_ratio '21:9'" in w for w in warnings)


def test_portrait_page_formats_are_valid_deck_aspect_ratios():
    # A product brief / one-pager template is an A4 or Letter PAGE, not a
    # slide — the collateral formats are first-class, not a fallback to 16:9.
    for ratio in ("A4", "letter"):
        candidate, warnings = gen.normalize_template_candidate(_proposal(aspect_ratio=ratio), kind="presentation")
        assert candidate["aspect_ratio"] == ratio
        assert not any("aspect_ratio" in w for w in warnings)


def test_missing_name_uses_the_form_name_then_a_placeholder():
    candidate, warnings = gen.normalize_template_candidate(
        _proposal(name=""), kind="presentation", fallback_name="  My deck  "
    )
    assert candidate["name"] == "My deck"
    assert warnings == []

    candidate, warnings = gen.normalize_template_candidate(_proposal(name=None), kind="presentation")
    assert candidate["name"] == "Generated template"
    assert any("no name" in w for w in warnings)


def test_report_kind_uses_the_report_whitelist_and_block_catalog():
    proposal = _proposal(
        theme_config={
            "background": "#ffffff",
            "foreground": "#111111",
            "accent": "#0015a8",
            "muted": "#555555",
            "heading_font": "Georgia, serif",
            "body_font": "Inter, sans-serif",
            "critical": "#b8180a",
            "gradient_from": "#000",  # deck-only key: not honoured by a report
        },
        layouts=[{"id": "kpi_row"}, {"id": "title"}],
        extra_metadata={"deck_defaults": {"theme": "dark"}, "guidance": "Lead with the verdict."},
    )
    candidate, warnings = gen.normalize_template_candidate(proposal, kind="report")

    assert candidate["kind"] == "report"
    assert "critical" in candidate["theme_config"]
    assert "gradient_from" not in candidate["theme_config"]
    assert [layout["id"] for layout in candidate["layouts"]] == ["kpi_row"]
    joined = "\n".join(warnings)
    assert "'title' is not a report block" in joined
    # A report has no deck defaults: none are emitted, none are validated.
    assert "deck_defaults" not in candidate["extra_metadata"]
    assert candidate["extra_metadata"]["guidance"] == "Lead with the verdict."
    # A report's aspect ratio is free-form (paper sizes), so it is kept as given.
    assert candidate["aspect_ratio"] == "16:9"


def test_guidance_and_description_are_bounded():
    proposal = _proposal(description="x" * 5000)
    proposal["extra_metadata"]["guidance"] = "g" * 5000
    candidate, _ = gen.normalize_template_candidate(proposal, kind="presentation")
    assert len(candidate["description"]) == gen.MAX_DESCRIPTION_CHARS
    assert len(candidate["extra_metadata"]["guidance"]) == gen.MAX_GUIDANCE_CHARS


# ── Meta-prompt derivation ───────────────────────────────────────────────────


def test_system_prompt_names_only_keys_and_layouts_the_validators_accept():
    prompt = gen.build_template_system_prompt("presentation")

    for key in THEME_CONFIG_KEYS:
        if key in THEME_FILE_ID_KEYS:
            # File ids are attached, not generated: the model is never told to
            # invent one.
            assert key not in prompt
        else:
            assert key in prompt
    for layout in SLIDE_LAYOUTS:
        assert f"`{layout}`" in prompt
    for value in (*PRESENTATION_THEMES, *PRESENTATION_TRANSITIONS, *PRESENTATION_ASPECT_RATIOS):
        assert value in prompt

    report_prompt = gen.build_template_system_prompt("report")
    for key in REPORT_THEME_CONFIG_KEYS:
        if key not in THEME_FILE_ID_KEYS:
            assert key in report_prompt
    assert "`kpi_row`" in report_prompt
    assert "`agenda`" not in report_prompt


def test_user_turn_carries_hints_logo_palette_and_the_template_being_refined():
    messages = gen.build_template_messages(
        brief="Warm editorial deck",
        kind="presentation",
        palette_hints=["terracotta", "#f4ede4"],
        logo_palette=["#aa3311", "#ffffff"],
        current={"name": "Old", "theme_config": {"accent": "#000000"}},
    )
    assert [m.role for m in messages] == ["system", "user"]
    user = messages[1].content
    assert "Warm editorial deck" in user
    assert "terracotta, #f4ede4" in user
    assert "#aa3311, #ffffff" in user
    assert '"name": "Old"' in user
    assert "refining THIS existing template" in user


def test_current_template_summary_strips_everything_but_design_fields():
    summary = gen.current_template_summary(
        {
            "id": "abc",
            "user_id": "u1",
            "shared_group_ids": ["g"],
            "name": "N",
            "description": "",
            "tags": [],
            "theme_config": {"accent": "#fff"},
            "layouts": [{"id": "title"}],
        }
    )
    assert summary == {"name": "N", "theme_config": {"accent": "#fff"}, "layouts": [{"id": "title"}]}
    assert gen.current_template_summary(None) is None
    assert gen.current_template_summary({"id": "x"}) is None


def test_palette_hints_are_trimmed_deduped_and_capped():
    hints = gen.clean_palette_hints(["  navy ", "navy", 12, "x" * 200] + [f"c{i}" for i in range(20)])
    assert hints[0] == "navy"
    assert hints[1] == "x" * gen.MAX_HINT_CHARS
    assert len(hints) == gen.MAX_PALETTE_HINTS


# ── JSON extraction + retry ──────────────────────────────────────────────────


def test_extract_json_object_tolerates_fences_and_chatter():
    payload = {"name": "x", "theme_config": {}}
    assert gen.extract_json_object("```json\n" + json.dumps(payload) + "\n```") == payload
    assert gen.extract_json_object("Here you go:\n" + json.dumps(payload) + "\nEnjoy!") == payload
    with pytest.raises(ValueError):
        gen.extract_json_object("no json here")
    with pytest.raises(ValueError):
        gen.extract_json_object("[1, 2, 3]")


class _FakeLLM:
    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.calls: list[list] = []

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, **_):
        self.calls.append(list(messages))
        return SimpleNamespace(content=self.replies.pop(0))


def test_generation_retries_once_on_invalid_json_then_normalises():
    llm = _FakeLLM(["Sure! Here is a template:", json.dumps(_proposal())])
    candidate, warnings = asyncio.run(
        gen.generate_template_candidate(
            llm,
            "m",
            brief="Corporate navy deck",
            kind="presentation",
            palette_hints=[],
            logo_palette=[],
            current=None,
            fallback_name=None,
        )
    )
    assert candidate["name"] == "Corporate Navy"
    assert warnings == []
    assert len(llm.calls) == 2
    # The repair turn keeps the failed reply in context and asks for JSON only.
    assert llm.calls[1][-2].role == "assistant"
    assert "ONLY the JSON object" in llm.calls[1][-1].content


def test_generation_gives_up_after_two_bad_replies():
    llm = _FakeLLM(["nope", "still nope"])
    with pytest.raises(gen.TemplateGenerationError):
        asyncio.run(
            gen.generate_template_candidate(
                llm,
                "m",
                brief="x",
                kind="presentation",
                palette_hints=[],
                logo_palette=[],
                current=None,
                fallback_name=None,
            )
        )


# ── Logo palette ─────────────────────────────────────────────────────────────


def test_svg_palette_counts_hex_colours_and_expands_shorthand():
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><rect fill="#ABC"/><path fill="#abc" stroke="#123456"/></svg>'
    assert gen.extract_palette(svg, "image/svg+xml") == ["#aabbcc", "#123456"]


def test_raster_palette_reports_dominant_opaque_colours():
    from PIL import Image

    img = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    for x in range(40):
        for y in range(40):
            if x < 30:
                img.putpixel((x, y), (16, 32, 64, 255))  # dominant navy
            elif y < 20:
                img.putpixel((x, y), (212, 175, 55, 255))  # gold
            # remaining quadrant stays transparent -> must be ignored
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    palette = gen.extract_palette(buf.getvalue(), "image/png")
    assert palette[0] == "#102040"
    assert "#d4af37" in palette
    assert "#000000" not in palette


def test_palette_extraction_never_raises():
    assert gen.extract_palette(b"not an image", "image/png") == []
    assert gen.extract_palette(b"", "image/png") == []
    assert gen.extract_palette(b"<svg></svg>", None) == []


# ── The route holds the kind to the closed choice ────────────────────


def _generate_client(monkeypatch, *, llm_calls: list):
    import uuid

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import deps
    from app.api.v1 import ai as ai_mod
    from app.core.database import get_db

    user = SimpleNamespace(id=uuid.uuid4(), is_admin=False, role="user", full_name="Dana")

    class _Session:
        async def execute(self, *a, **k):
            raise AssertionError("no database work is expected on a refused brief")

    app = FastAPI()
    app.include_router(ai_mod.router, prefix="/api/v1/ai")
    app.dependency_overrides[deps.get_current_user] = lambda: user
    app.dependency_overrides[deps.require_ai_enabled] = lambda: None
    app.dependency_overrides[get_db] = lambda: _Session()

    async def _fake_llm(db, user_id):
        llm_calls.append(user_id)
        raise AssertionError("the model must not be reached for a refused kind")

    monkeypatch.setattr(ai_mod, "get_llm_for_user", _fake_llm)
    return TestClient(app)


def test_generate_route_refuses_a_kind_no_template_can_be_created_with(monkeypatch):
    """The Templates form offers Presentation / Report / Video; a hand-written
    brief for anything else is refused BEFORE a model is paid for, with the
    same sentence the create route uses."""
    calls: list = []
    client = _generate_client(monkeypatch, llm_calls=calls)
    resp = client.post("/api/v1/ai/generate-template", json={"brief": "A calm navy deck", "kind": "dashboard"})
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"].startswith("kind must be one of presentation, report, video (got 'dashboard')")
    assert calls == []
