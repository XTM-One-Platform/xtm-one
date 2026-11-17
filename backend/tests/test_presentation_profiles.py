"""The `profiles` block and `image.padding`.

The founders slide of 25 Sep 2026 was a browser-rendered PNG placed as an
`image`, so one logo or one padding meant a full rebuild. These pin the native
form: the spec accepts it, the tool describes it, the layout audit names a
truncated line, the layout switch reads it, and the padding is a field.
"""

from __future__ import annotations

import re
import uuid

import pytest

from app.services.presentation_export import LAYOUT_AUDIT_JS
from app.services.presentation_guidance import BLOCK_EXAMPLES, build_generate_presentation_description
from app.services.presentation_layout import findings_from_observations
from app.services.presentation_layout_switch import switch_slide_layout
from app.services.presentation_spec import (
    BLOCK_TYPES,
    MAX_PROFILE_LINE_CHARS,
    MAX_PROFILE_LINES,
    MAX_PROFILES,
    PresentationSpecError,
    spec_reference,
    validate_spec,
)

PHOTO = str(uuid.uuid4())
LOGO = str(uuid.uuid4())


def _deck(*blocks: dict, layout: str = "content") -> dict:
    return {"title": "People", "slides": [{"layout": layout, "title": "Founders", "blocks": list(blocks)}]}


def _profiles(**overrides) -> dict:
    block = {
        "type": "profiles",
        "items": [
            {
                "name": "Samuel Hassine",
                "title": "Co-founder & CEO",
                "photo_file_id": PHOTO,
                "lines": [
                    {"text": "Created OpenCTI at ANSSI", "logo_file_id": LOGO},
                    {"text": "Former head of a CERT", "icon": "shield"},
                    "Plain fact",
                ],
            },
            {"name": "Julien Richard", "title": "Co-founder & CTO", "lines": ["Created OpenAEV"]},
        ],
    }
    block.update(overrides)
    return block


# ── The spec ─────────────────────────────────────────────────────────────────


def test_profiles_is_a_catalog_block_the_reference_and_the_examples_describe():
    assert "profiles" in BLOCK_TYPES
    assert "profiles" in BLOCK_EXAMPLES
    reference = spec_reference()
    assert "profiles" in reference
    for field in ("photo_file_id", "logo_file_id", "photo_size", "lines"):
        assert field in reference, field
    description = build_generate_presentation_description()
    assert "`profiles` block" in description
    assert "people_photo" in description


def test_a_profiles_block_validates_and_a_bare_string_line_is_coerced():
    spec = validate_spec(_deck(_profiles()))
    block = spec["slides"][0]["blocks"][0]
    assert block["type"] == "profiles"
    first, second = block["items"]
    assert first["photo_file_id"] == PHOTO
    assert first["lines"][0]["logo_file_id"] == LOGO
    assert first["lines"][1]["icon"] == "shield"
    assert first["lines"][2] == {"text": "Plain fact"}
    assert second["lines"] == [{"text": "Created OpenAEV"}]
    # The defaults an agent may leave out.
    assert block.get("photo_size", "md") == "md"
    assert first.get("tone", "accent") == "accent"


def test_a_profile_needs_a_name_and_at_least_one_line():
    with pytest.raises(PresentationSpecError, match=r"items\.0.*name"):
        validate_spec(_deck({"type": "profiles", "items": [{"lines": ["x"]}]}))
    with pytest.raises(PresentationSpecError, match=r"items\.0.*lines"):
        validate_spec(_deck({"type": "profiles", "items": [{"name": "A", "lines": []}]}))


def test_a_team_of_nine_is_one_block_and_twelve_is_the_cap():
    """A 3 x 3 team is ONE block (it used to take three stacked blocks of
    three); twelve is three rows of four, the widest grid a slide reads."""
    assert MAX_PROFILES == 12
    nine = [{"name": f"Person {i}", "title": "Role", "lines": ["fact"]} for i in range(9)]
    spec = validate_spec(_deck({"type": "profiles", "items": nine}))
    assert len(spec["slides"][0]["blocks"][0]["items"]) == 9


def test_the_bounds_are_the_constants():
    too_many_people = [{"name": f"P{i}", "lines": ["x"]} for i in range(MAX_PROFILES + 1)]
    with pytest.raises(PresentationSpecError, match="items"):
        validate_spec(_deck({"type": "profiles", "items": too_many_people}))
    too_many_lines = [{"name": "A", "lines": [f"fact {i}" for i in range(MAX_PROFILE_LINES + 1)]}]
    with pytest.raises(PresentationSpecError, match="lines"):
        validate_spec(_deck({"type": "profiles", "items": too_many_lines}))
    too_long = [{"name": "A", "lines": ["w" * (MAX_PROFILE_LINE_CHARS + 1)]}]
    with pytest.raises(PresentationSpecError, match="text"):
        validate_spec(_deck({"type": "profiles", "items": too_long}))


def test_a_profile_line_is_one_line_by_contract():
    """The renderer draws a fact ``nowrap`` + ellipsis and the fit pass / the
    audit measure ONE run; the inline markup renders a line break as ``<br>``,
    which breaks a nowrap row in two - so a break is refused, field-addressed,
    with the fix in the message. Markup on the line stays welcome."""
    validate_spec(_deck({"type": "profiles", "items": [{"name": "A", "lines": ["**Bold** fact with `code`"]}]}))
    for broken in ("Created OpenCTI\nat ANSSI", "Created OpenCTI\r\nat ANSSI", {"text": "two\nlines", "icon": "shield"}):
        with pytest.raises(PresentationSpecError, match=r"lines\.0.*text.*single line") as info:
            validate_spec(_deck({"type": "profiles", "items": [{"name": "A", "lines": [broken]}]}))
        assert "split it into two lines[] entries" in str(info.value)


def test_photo_size_and_columns_are_closed_choices():
    validate_spec(_deck(_profiles(photo_size="lg", columns=2, density="tight")))
    with pytest.raises(PresentationSpecError, match="photo_size"):
        validate_spec(_deck(_profiles(photo_size="xl")))
    with pytest.raises(PresentationSpecError, match="columns"):
        validate_spec(_deck(_profiles(columns=5)))


def test_profiles_nests_where_atomic_blocks_do():
    callout = {"type": "callout", "title": "Team", "children": [_profiles()]}
    validate_spec(_deck(callout))
    columns = {"layout": "content", "title": "Cols", "columns": [[_profiles()], [{"type": "text", "content": "x"}]]}
    validate_spec({"title": "People", "slides": [columns]})


def test_image_padding_is_a_closed_choice_defaulting_to_none():
    image = {"type": "image", "file_id": PHOTO, "alt": "team"}
    spec = validate_spec(_deck(image))
    assert spec["slides"][0]["blocks"][0].get("padding", "none") == "none"
    for padding in ("sm", "md", "lg"):
        padded = validate_spec(_deck({**image, "padding": padding}))
        assert padded["slides"][0]["blocks"][0]["padding"] == padding
    with pytest.raises(PresentationSpecError, match="padding"):
        validate_spec(_deck({**image, "padding": "huge"}))


# ── The layout audit ─────────────────────────────────────────────────────────


def test_the_audit_measures_every_profile_line_and_names_the_line():
    # The explicit pass: scrollWidth against clientWidth, like the chrome bars.
    assert 'const PROFILE_LINE_TEXT = "deck-profile-line-text"' in LAYOUT_AUDIT_JS
    assert '[PROFILE_LINE_TEXT, "profile-line"]' in LAYOUT_AUDIT_JS
    assert "run.scrollWidth - run.clientWidth" in LAYOUT_AUDIT_JS
    # The card's scaffolding is structural: a finding names the line, the
    # name, the title or the card - never the grid or the tile.
    structural = re.search(r"const STRUCTURAL = /(.+)/;", LAYOUT_AUDIT_JS).group(1)
    for part in ("head", "identity", "photo", "portrait", "initials", "lines", "line-text", "tile", "logo"):
        assert f"{part}|" in structural or f"{part})" in structural, part


def test_the_audit_measures_the_name_and_the_title_as_one_line_each():
    assert '["deck-profile-name", "profile-name"]' in LAYOUT_AUDIT_JS
    assert '["deck-profile-title", "profile-title"]' in LAYOUT_AUDIT_JS
    # The generic clip pass skips every measured run, or a cut would be reported twice.
    assert "profileOneLine(el)) continue" in LAYOUT_AUDIT_JS


@pytest.mark.parametrize("block,field", [("profile-name", "name"), ("profile-title", "title")])
def test_a_cut_name_or_title_is_a_clipped_finding_naming_the_field(block, field):
    observation = {"kind": "clipped", "side": "right", "amount": 40, "block": block, "text": "A long role"}
    spec = {"title": "Deck", "slides": [{"layout": "content", "title": "Team"}]}
    (finding,) = findings_from_observations(
        [{"index": 0, "slide_id": "s0", "layout": "content", "findings": [observation]}], spec
    )
    assert finding.kind == "clipped"
    assert f'the profile {field} "A long role" is cut off at its right edge' in finding.message
    assert f"profiles.items[].{field} is one line" in finding.message
    assert "fewer columns" in finding.message and "photo_size: sm" in finding.message


def test_a_truncated_profile_line_is_a_clipped_finding_with_its_own_remedy():
    observation = {
        "kind": "clipped",
        "side": "right",
        "amount": 640,
        "block": "profile-line",
        "text": "A deliberately very long fact that",
    }
    spec = {"title": "Deck", "slides": [{"layout": "content", "title": "Founders"}]}
    (finding,) = findings_from_observations(
        [{"index": 0, "slide_id": "s0", "layout": "content", "findings": [observation]}], spec
    )
    assert finding.kind == "clipped"
    assert finding.severity == "overloaded"
    assert 'the profile line "A deliberately very long fact that" is cut off at its right edge' in finding.message
    assert "lines[].text is one line" in finding.message
    # The cut is horizontal: the remedies widen the row (fewer columns) or
    # shrink the type (density: tight); the portrait sits ABOVE the lines and
    # changes nothing about their width, so it is never named.
    assert "fewer columns" in finding.message and "density: tight" in finding.message
    assert "photo_size" not in finding.message


# ── In a real browser: twelve people at four columns ────────────────────────


def _twelve_people_page(p):
    """A 1920x1080 content slide holding one 12-person block on the real
    stylesheet, in the markup `profiles.tsx` renders: names of 16 characters
    and titles of 24, the ordinary identity of a leadership team."""
    from tests.test_presentation_architecture_bare import _chromium
    from tests.test_presentation_export import _deck_stylesheet

    css = _deck_stylesheet()
    _sync, playwright_error = _chromium()
    try:
        browser = p.chromium.launch()
    except playwright_error as exc:
        pytest.skip(f"no Chromium for Playwright here: {exc}")
    card = (
        '<div class="deck-profile" data-tone="accent"><div class="deck-profile-head">'
        '<span class="deck-profile-photo" data-kind="initials"><span class="deck-profile-initials">AB</span></span>'
        '<div class="deck-profile-identity"><div class="deck-profile-name">Abcdefgh Ijklmno</div>'
        '<div class="deck-profile-title">Chief Operating Officer.</div></div></div>'
        '<ul class="deck-profile-lines"><li class="deck-profile-line"><span class="deck-profile-tile" data-kind="dot"></span>'
        '<span class="deck-profile-line-text">One fact</span></li></ul></div>'
    )
    page = browser.new_page(viewport={"width": 1920, "height": 1080})
    page.set_content(
        '<div class="deck-root" data-scheme="dark"><div class="deck-canvas deck-static-slide" data-scheme="dark" '
        'style="position:relative;width:1920px;height:1080px;overflow:hidden">'
        '<div class="deck-slide" data-layout="content" style="padding:80px 40px;box-sizing:border-box;width:1920px;height:1080px">'
        '<div class="deck-blocks"><div class="deck-profiles" data-columns="4" data-density="normal" data-photo-size="md">'
        f"{card * 12}</div></div></div></div></div>"
    )
    page.add_style_tag(content=f"body {{ margin: 0; }}\n{css}")
    page.wait_for_timeout(100)
    return browser, page


def test_twelve_ordinary_identities_fit_on_one_line_at_four_columns():
    """UX round 1 of the grid: at four columns the portrait beside the name
    left about 216 px, so most names of a 12-person team were cut. The
    portrait now sits above the identity at four columns, which takes the
    card's width: a 16-character name and a 24-character title fit with no
    ellipsis, and the layout check reports no `clipped` finding."""
    from tests.test_presentation_architecture_bare import _chromium

    assert len("Abcdefgh Ijklmno") == 16 and len("Chief Operating Officer.") == 24
    sync_playwright, _ = _chromium()
    with sync_playwright() as p:
        browser, page = _twelve_people_page(p)
        cuts = page.evaluate(
            """() => Array.from(document.querySelectorAll('.deck-profile-name, .deck-profile-title'))
                  .map((el) => el.scrollWidth - el.clientWidth)"""
        )
        (slide,) = page.evaluate(LAYOUT_AUDIT_JS)
        browser.close()
    assert len(cuts) == 24
    assert max(cuts) <= 1, cuts
    assert [f for f in slide["findings"] if f["kind"] == "clipped"] == []


# ── The layout switch ────────────────────────────────────────────────────────


def test_the_layout_switch_reads_a_person_as_one_point():
    slide = _deck(_profiles())["slides"][0]
    switched = switch_slide_layout(slide, "takeaways")
    titles = [item.get("title") for item in switched.slide.get("items", [])]
    assert titles[:2] == ["Samuel Hassine", "Julien Richard"]
    first = switched.slide["items"][0]
    assert "Co-founder & CEO" in (first.get("description") or "")
    assert "Created OpenCTI at ANSSI" in (first.get("description") or "")


def test_the_layout_switch_names_the_portraits_and_logos_it_drops():
    """The contract: every dropped thing is named in ``dropped``.

    The fixture has one portrait (Samuel) and one line logo (the OpenCTI
    line); Julien has neither. A target that lists words keeps the people's
    words and loses both pictures - and says so, with the counts.
    """
    slide = _deck(_profiles())["slides"][0]
    for target in ("takeaways", "agenda"):
        switched = switch_slide_layout(slide, target)
        dropped = " | ".join(switched.dropped)
        assert "1 portrait" in dropped, (target, dropped)
        assert "1 line logo" in dropped, (target, dropped)
        assert "profiles block" in dropped, (target, dropped)
    # No picture, nothing said about pictures.
    words_only = _deck({"type": "profiles", "items": [{"name": "A", "lines": ["x"]}, {"name": "B", "lines": ["y"]}]})
    assert not any("portrait" in d or "logo" in d for d in switch_slide_layout(words_only["slides"][0], "takeaways").dropped)
    # A content target keeps the block whole, pictures included - nothing dropped.
    cols = _deck(_profiles(), layout="content")["slides"][0]
    cols["title"] = "Team"
    kept_whole = switch_slide_layout({**cols, "layout": "content", "blocks": [_profiles(), {"type": "text", "content": "x"}]}, "content")
    assert not any("portrait" in d for d in kept_whole.dropped)
