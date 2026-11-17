"""The `quote` block's variants and the `plain` metric.

A customer quote is the strongest proof a deck carries, and the one look the
block had - a boxed italic line - could not carry a statement slide. These pin
the contract: every new field is optional and a quote without them is the
historical card (in the stored spec, the renderer and the stylesheet), an
omitted look field follows the variant the same way on both sides, the credit
is one thing or the other, the pictures are deck pictures, the layout check
reads a statement's line count, the layout switch reads a customer quote, and
the example the tool description teaches is the statement slide itself.
"""

from __future__ import annotations

import json
import re
import typing
import uuid
from pathlib import Path

import pytest

from app.api.v1.schemas.presentation import PresentationLayoutFinding, PresentationResponse
from app.services.presentation_export import EXTRACT_PRIMITIVES_JS, LAYOUT_AUDIT_JS
from app.services.presentation_guidance import BLOCK_EXAMPLES, build_generate_presentation_description
from app.services.presentation_layout import _SMALLER_QUOTE_SIZE, FindingKind, findings_from_observations
from app.services.presentation_layout_switch import switch_slide_layout
from app.services.presentation_patch import PresentationPatchError, apply_operations
from app.services.presentation_pictures import picture_placements, quote_picture_ids, slide_file_references
from app.services.presentation_spec import (
    HERO_QUOTE_MAX_LINES,
    MAX_QUOTE_FIGURES,
    QUOTE_VARIANT_DEFAULTS,
    QUOTE_VARIANTS,
    STATEMENT_MAX_LINES,
    PresentationSpecError,
    quote_credit,
    spec_reference,
    validate_spec,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PRESENTATION_DIR = BACKEND_ROOT.parent / "frontend" / "src" / "components" / "presentation"
SKILL = BACKEND_ROOT / "app" / "agents" / "seed_data" / "skill_content" / "presentation-design.md"

PHOTO = str(uuid.uuid4())
LOGO = str(uuid.uuid4())
OTHER = str(uuid.uuid4())
_TEXT = {"type": "text", "content": "Context."}


def _read(*parts: str) -> str:
    return PRESENTATION_DIR.joinpath(*parts).read_text(encoding="utf-8")


def _deck(*blocks: dict, title: str | None = None) -> dict:
    slide: dict = {"layout": "content", "blocks": list(blocks)}
    if title:
        slide["title"] = title
    return {"title": "Customer proof", "slides": [slide]}


def _stored(block: dict) -> dict:
    return validate_spec(_deck(block))["slides"][0]["blocks"][0]


def _statement(**overrides) -> dict:
    return {**BLOCK_EXAMPLES["quote"], **overrides}


# ── The spec ─────────────────────────────────────────────────────────────────


def test_a_quote_with_no_new_field_is_stored_as_the_card_it_always_was():
    # Only the variant is spelled out: every other look field reads as the
    # variant's, so a re-saved deck gains ONE key per quote, never a size or
    # an italic flag that would pin the card's look on a later variant change.
    assert _stored({"type": "quote", "content": "q", "attribution": "a"}) == {
        "type": "quote",
        "content": "q",
        "variant": "card",
        "attribution": "a",
    }


def test_the_statement_example_is_the_one_the_issue_asked_for_and_validates_alone_on_a_slide():
    assert BLOCK_EXAMPLES["quote"] == {
        "type": "quote",
        "variant": "statement",
        "size": "hero",
        "content": "If I took OpenCTI away from my team, they would walk out.",
        "author": "Dana Whitlock",
        "role": "VP Security Operations",
        "organization": "Contoso Financial",
        "figures": [{"value": "80K", "label": "Employees"}, {"value": "$30B", "label": "Revenue"}],
    }
    # Alone on an untitled content slide: the customer statement slide.
    block = _stored(BLOCK_EXAMPLES["quote"])
    assert block["variant"] == "statement" and block["size"] == "hero"
    assert [figure["value"] for figure in block["figures"]] == ["80K", "$30B"]
    # The catalogue get_presentation(reference=...) serves teaches it as written (one-line JSON, #8812).
    from app.services.presentation_guidance import build_presentation_reference

    assert build_generate_presentation_description()
    description = build_presentation_reference("quote")
    assert json.dumps(BLOCK_EXAMPLES["quote"], ensure_ascii=False, separators=(",", ":")) in description


@pytest.mark.parametrize(
    ("field", "value"),
    [("variant", "poster"), ("size", "xxl"), ("align", "end"), ("quote_marks", "fancy"), ("italic", "sometimes")],
)
def test_a_look_value_the_renderer_does_not_draw_is_refused(field, value):
    with pytest.raises(PresentationSpecError, match=rf"slides\.0\.blocks\.0.*{field}"):
        validate_spec(_deck({"type": "quote", "content": "q", field: value}))


def test_the_credit_is_the_legacy_line_or_the_split_fields_never_both():
    for split in ({"author": "A"}, {"role": "CISO"}, {"organization": "Acme"}):
        assert _stored({"type": "quote", "content": "q", **split})["variant"] == "card"
    with pytest.raises(PresentationSpecError) as caught:
        validate_spec(_deck({"type": "quote", "content": "q", "attribution": "A, CISO", "role": "CISO"}))
    message = str(caught.value)
    assert "slides.0.blocks.0 (layout=content, block=quote)" in message
    assert "attribution OR as author / role / organization" in message
    assert "this quote sets attribution and role" in message
    # The same refusal as a person reads it behind the editor's Save (a 422).
    assert caught.value.human_details().startswith(
        "- Slide 1 (content): blocks.0 in quote - Value error, give the credit as attribution OR as author"
    )


def test_the_credit_reads_as_one_line_in_the_order_it_is_drawn():
    assert quote_credit({"attribution": "Bruce Schneier"}) == "Bruce Schneier"
    assert quote_credit(BLOCK_EXAMPLES["quote"]) == (
        "Dana Whitlock, VP Security Operations, Contoso Financial"
    )
    assert quote_credit({"organization": "Pilot team", "author": "SOC lead"}) == "SOC lead, Pilot team"
    assert quote_credit({"author": "  "}) is None
    assert quote_credit({}) is None


def test_a_quote_carries_at_most_four_figures_and_each_is_a_value_and_a_label():
    figure = {"value": "1", "label": "One"}
    assert _stored({"type": "quote", "content": "q", "figures": []})["figures"] == []
    assert len(_stored({"type": "quote", "content": "q", "figures": [figure] * MAX_QUOTE_FIGURES})["figures"]) == 4
    with pytest.raises(PresentationSpecError, match="figures"):
        validate_spec(_deck({"type": "quote", "content": "q", "figures": [figure] * (MAX_QUOTE_FIGURES + 1)}))
    with pytest.raises(PresentationSpecError, match=r"figures\.0\.label"):
        validate_spec(_deck({"type": "quote", "content": "q", "figures": [{"value": "1"}]}))
    # A figure is drawn as a plain tile - the value in its tone, the label - so
    # it has no field a card would draw: one would validate and show nothing.
    assert _stored({"type": "quote", "content": "q", "figures": [{**figure, "tone": "success"}]})["figures"] == [
        {**figure, "tone": "success"}
    ]
    for extra in ("sublabel", "icon", "delta", "trend", "sparkline"):
        value = {"sublabel": "since 2019", "icon": "users", "delta": "+4%", "trend": "up", "sparkline": [1, 2]}[extra]
        with pytest.raises(PresentationSpecError, match=rf"figures\.0\.{extra}"):
            validate_spec(_deck({"type": "quote", "content": "q", "figures": [{**figure, extra: value}]}))


def test_the_quote_pictures_are_deck_pictures_the_file_check_and_the_grant_both_see():
    spec = validate_spec(_deck(_statement(photo_file_id=PHOTO, logo_file_id=LOGO)))
    references = {(path, value) for path, _layout, value in slide_file_references(spec)}
    assert ("slides.0.blocks.0.photo_file_id", PHOTO) in references
    assert ("slides.0.blocks.0.logo_file_id", LOGO) in references


def test_a_quote_picture_needs_no_credit_to_be_saved():
    # "Name, Role" beside a company logo, a portrait with no author: every
    # picture saves whatever its credit - the renderer names an unnamed one
    # (`quotePictureAlt`), the spec never refuses it.
    for block in (
        {"photo_file_id": PHOTO},
        {"role": "CISO", "organization": "Acme", "photo_file_id": PHOTO},
        {"author": "Dana Whitlock", "role": "VP", "logo_file_id": LOGO},
        {"role": "CISO", "photo_file_id": PHOTO, "logo_file_id": LOGO},
        {"attribution": "A, CISO, Acme", "photo_file_id": PHOTO, "logo_file_id": LOGO},
    ):
        stored = _stored({"type": "quote", "content": "q", **block})
        assert {k: stored.get(k) for k in ("photo_file_id", "logo_file_id")} == {
            k: block.get(k) for k in ("photo_file_id", "logo_file_id")
        }


def test_the_deck_names_only_the_quote_pictures_its_spec_references():
    spec = validate_spec(
        {
            "title": "Deck",
            "slides": [
                {"layout": "content", "blocks": [_statement(photo_file_id=PHOTO), {"type": "image", "file_id": OTHER}]},
                {
                    "layout": "content",
                    "columns": [[{"type": "quote", "content": "q", "author": "B", "logo_file_id": LOGO}], [_TEXT]],
                },
            ],
        }
    )
    assert quote_picture_ids(spec) == [PHOTO, LOGO]
    assert quote_picture_ids({"title": "Deck", "slides": []}) == []
    assert quote_picture_ids(None) == []


@pytest.mark.asyncio
async def test_the_picture_labels_are_the_readers_facts_of_the_referenced_quote_pictures(monkeypatch):
    import app.services.presentation_pictures as pictures

    asked: list[list[str]] = []

    async def fake_facts(db, file_ids, *, user_id, group_ids=None, is_admin=False):
        asked.append(list(file_ids))

        def fact(file_id, filename, description):
            return pictures.PictureFacts(
                file_id=file_id, filename=filename, content_type="image/png", size=1, origin="template asset",
                description=description,
            )

        return {
            PHOTO: fact(PHOTO, "dana-whitlock.png", None),
            LOGO: fact(LOGO, "contoso.svg", "Contoso Financial logo"),
            # Withheld from this reader: nothing to name it by, left out.
            OTHER: fact(OTHER, None, None),
        }

    monkeypatch.setattr(pictures, "load_picture_facts", fake_facts)
    spec = {"slides": [{"layout": "content", "blocks": [_statement(photo_file_id=PHOTO, logo_file_id=LOGO)]}]}
    labels = await pictures.load_quote_picture_labels(None, spec, user_id=uuid.uuid4())
    assert asked == [[PHOTO, LOGO]]
    assert labels == {
        PHOTO: {"description": None, "filename": "dana-whitlock.png"},
        LOGO: {"description": "Contoso Financial logo", "filename": "contoso.svg"},
    }


def test_the_detail_payload_carries_the_picture_labels_and_the_list_shape_does_not():
    from types import SimpleNamespace

    from app.services.presentations import serialize_presentation

    deck = SimpleNamespace(
        id=uuid.uuid4(), title="Deck", theme="dark", slide_count=1, revision=1, user_id=None,
        conversation_id=None, assignment_run_id=None, template_container_id=None, is_company_managed=False,
        shared_group_ids=None, shared_group_permissions=None, shared_user_ids=None, shared_user_permissions=None,
        access_restricted=False, updated_by_user_id=None, created_at=None, updated_at=None,
        spec={"title": "Deck", "slides": []},
    )
    labels = {LOGO: {"description": "Contoso Financial logo", "filename": "contoso.svg"}}
    assert serialize_presentation(deck, picture_labels=labels)["picture_labels"] == labels
    assert serialize_presentation(deck)["picture_labels"] == {}
    assert "picture_labels" not in serialize_presentation(deck, picture_labels=labels, include_spec=False)
    # The response model keeps the field (a response_model drops unknown keys).
    assert "picture_labels" in PresentationResponse.model_fields


def test_a_picture_the_credit_does_not_name_is_never_announced_as_nothing():
    view = _read("blocks", "quote.tsx")
    assert 'quotePictureAlt(block, "photo_file_id", useDeckPictureLabel(photo), t_i18n("Portrait"))' in view
    assert 'quotePictureAlt(block, "logo_file_id", useDeckPictureLabel(logo), t_i18n("Logo"))' in view
    assert 'alt=""' not in view
    # Every slide host provides the labels, so the viewer and the export page agree.
    for host in ("PresentationDeck.tsx", "StaticSlide.tsx"):
        assert "<DeckPictureLabelProvider value={presentation.picture_labels ?? NO_PICTURE_LABELS}>" in _read(host), host
    # The PPTX carries the alt the page announced.
    assert 'item.alt = el.getAttribute("alt").trim()' in EXTRACT_PRIMITIVES_JS


def test_the_editable_pptx_picture_carries_the_alt_the_page_announced():
    import io

    from PIL import Image
    from pptx import Presentation as PptxDeck

    from app.services.pptx_writer import _add_image

    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (15, 188, 255)).save(buffer, format="PNG")
    deck = PptxDeck()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    geometry = (1920, 1080, 6350, 0.75)
    captures = {"x0-0": buffer.getvalue(), "x0-1": buffer.getvalue()}
    _add_image(slide, {"id": "x0-0", "x": 0, "y": 0, "w": 40, "h": 40, "alt": "Contoso Financial logo"}, geometry, captures)
    _add_image(slide, {"id": "x0-1", "x": 50, "y": 0, "w": 40, "h": 40}, geometry, captures)
    named, unnamed = (shape._element.nvPicPr.cNvPr for shape in slide.shapes)
    assert named.get("descr") == "Contoso Financial logo"
    # A picture the page announced as nothing keeps what python-pptx always wrote.
    assert unnamed.get("descr") == "image.png"


def test_the_picture_list_names_a_quotes_pictures_after_its_credit():
    # `replace_image` and `get_presentation` list every picture a slide shows:
    # a quote's portrait is its author, its logo the organization.
    spec = validate_spec(_deck(_statement(photo_file_id=PHOTO, logo_file_id=LOGO)))
    places = {place.file_id: place for place in picture_placements(spec)}
    assert places[PHOTO].where == "block 1 (quote) photo_file_id" and places[PHOTO].context == "Dana Whitlock"
    assert places[LOGO].where == "block 1 (quote) logo_file_id" and places[LOGO].context == "Contoso Financial"
    # The legacy one-line credit names both.
    legacy = validate_spec(
        _deck({"type": "quote", "content": "q", "attribution": "SOC lead, Acme", "photo_file_id": PHOTO})
    )
    assert picture_placements(legacy)[0].context == "SOC lead, Acme"


def test_the_metric_blocks_take_the_plain_variant_and_the_alignment():
    metric = {"type": "metric", "value": "80K", "label": "Employees"}
    grid = {"type": "metrics_grid", "items": [{"value": "1", "label": "One"}]}
    assert {k: _stored(metric)[k] for k in ("variant", "align")} == {"variant": "card", "align": "start"}
    assert _stored({**metric, "variant": "plain", "align": "center"})["variant"] == "plain"
    assert _stored({**grid, "variant": "plain", "align": "center"})["align"] == "center"
    with pytest.raises(PresentationSpecError, match="variant"):
        validate_spec(_deck({**metric, "variant": "ghost"}))


# ── The contract is one table, read the same way on both sides ───────────────


def test_an_omitted_look_field_reads_the_same_in_the_renderer():
    source = _read("blocks", "quote-style.ts")
    rows = dict(
        (name, {"size": size, "align": align, "italic": italic == "true", "quote_marks": marks})
        for name, size, align, italic, marks in re.findall(
            r'^\s+(\w+): \{ size: "(\w+)", align: "(\w+)", italic: (true|false), quote_marks: "(\w+)" \},$',
            source,
            re.MULTILINE,
        )
    )
    assert rows == QUOTE_VARIANT_DEFAULTS
    assert tuple(rows) == QUOTE_VARIANTS
    assert f"export const HERO_QUOTE_MAX_LINES = {HERO_QUOTE_MAX_LINES};" in source
    # The editor offers the same values the validator takes, in catalog order.
    schema = _read("block-schema.ts")
    assert 'export const QUOTE_VARIANTS = ["card", "statement", "pull", "testimonial"] as const;' in schema
    assert f"export const MAX_QUOTE_FIGURES = {MAX_QUOTE_FIGURES};" in schema


def test_the_reference_says_what_an_omitted_look_field_reads_as():
    reference = spec_reference()
    (line,) = [row for row in reference.splitlines() if row.startswith("- quote:")]
    for expected in (
        'variant?: QuoteVariant = "card"',
        "default: statement xl, pull lg, otherwise md",
        "default: statement center, otherwise start",
        "default: card true, otherwise false",
        "default: statement decorative, otherwise none",
        "attribution?: markup<=120 (legacy, never with author / role / organization)",
        "figures?: QuoteFigure[] (0-4)",
        "photo_file_id?: file_id",
        "logo_file_id?: file_id",
    ):
        assert expected in line, expected
    assert '- QuoteSize: "md"|"lg"|"xl"|"hero"' in reference
    assert '- MetricVariant: "card"|"plain"' in reference
    (figure,) = [row for row in reference.splitlines() if row.startswith("- QuoteFigure:")]
    assert figure == '- QuoteFigure: {value: text<=40, label: markup<=120, tone?: Tone = "accent"}'
    # A plain tile's look, worded once for the metric and the grid.
    for block in ("metric", "metrics_grid"):
        (row,) = [row for row in reference.splitlines() if row.startswith(f"- {block}:")]
        assert "plain = unboxed, value + label only" in row, block


def test_replace_text_never_rewrites_a_quote_look():
    # `quote_marks` is a choice, like `variant` or `size`: "line" found in
    # "inline" would write "inrow" and fail the whole edit at validation.
    deck = validate_spec(_deck({"type": "quote", "content": "Words.", "quote_marks": "inline", "variant": "pull"}))
    for find in ("line", "none", "pull"):
        with pytest.raises(PresentationPatchError):
            apply_operations(deck, [{"op": "replace_text", "find": find, "replace": "row"}])


# ── The layout check ─────────────────────────────────────────────────────────


def _lines(lines: int, size: str = "hero") -> dict:
    return {"kind": "lines", "lines": lines, "size": size, "block": "quote", "text": "If I took OpenCTI away"}


def _findings(*observations: dict) -> list:
    spec = {"title": "Deck", "slides": [{"layout": "content"}]}
    return findings_from_observations(
        [{"index": 0, "slide_id": "s0", "layout": "content", "findings": list(observations)}], spec
    )


def test_a_statement_within_four_lines_is_no_finding():
    assert _findings(_lines(STATEMENT_MAX_LINES)) == []
    assert _findings(_lines(1)) == []


def test_a_statement_past_four_lines_is_a_finding_with_its_remedy():
    (finding,) = _findings(_lines(6))
    assert finding.kind == "lines" and finding.severity == "dense" and finding.amount == 6
    assert 'the statement quote "If I took OpenCTI away" runs to 6 lines at size hero' in finding.message
    assert f"in at most {STATEMENT_MAX_LINES}" in finding.message
    assert "Cut it to its strongest sentence, set size xl, or make it a pull quote" in finding.message
    # The smallest step has no smaller size to point at.
    (smallest,) = _findings(_lines(5, "md"))
    assert "set size" not in smallest.message


def test_a_hero_that_stepped_down_is_reported_at_the_size_it_is_drawn():
    # Past three lines a hero is drawn one step down, at the h1 size: its
    # lines are counted there, so "at size hero" would name a size not drawn.
    (finding,) = _findings({**_lines(5), "stepped": True})
    assert "runs to 5 lines at the h1 size a hero steps down to" in finding.message
    assert "at size hero" not in finding.message
    assert "set size xl" in finding.message
    # The page says whether it stepped, read off the attribute `fitHeroQuote` writes.
    assert "stepped: quote.dataset.quoteFit === \"step\"" in LAYOUT_AUDIT_JS


def test_the_api_contract_names_every_finding_kind_and_what_a_lines_amount_counts():
    fields = PresentationLayoutFinding.model_fields
    assert fields["kind"].description == " | ".join(typing.get_args(FindingKind))
    assert "lines: the statement's line count" in (fields["amount"].description or "")
    # The client's reading of the same contract.
    types = (PRESENTATION_DIR.parents[1] / "types" / "index.ts").read_text(encoding="utf-8")
    kind_doc = types.split("export type PresentationLayoutFindingKind", 1)[0].rsplit("/**", 1)[1]
    for kind in typing.get_args(FindingKind):
        assert f"(`{kind}`" in kind_doc, kind
    assert "`lines`: the statement's line count" in types


def test_a_long_statement_is_never_also_told_the_slide_is_sparse():
    sparse = {"kind": "sparse", "unused": 0.6, "empty": 500, "slide": 1080}
    assert [finding.kind for finding in _findings(_lines(6), sparse)] == ["lines"]


def test_the_audit_counts_every_statement_and_names_the_quote():
    assert "slide.querySelectorAll(\".deck-quote[data-variant='statement']\")" in LAYOUT_AUDIT_JS
    assert 'kind: "lines", lines: Math.round(text.clientHeight / lineHeight)' in LAYOUT_AUDIT_JS
    structural = re.search(r"const STRUCTURAL = /(.+)/;", LAYOUT_AUDIT_JS).group(1)
    assert "quote-(rule|glyph|open|close|credit|author|role|org|photo|portrait|logo|logo-image|figures)" in structural
    # Every class the quote renders is scaffolding: a finding in it names the quote.
    view = _read("blocks", "quote.tsx")
    rendered = set(re.findall(r'"deck-quote-([a-z-]+)"', view))
    named = set(re.search(r"quote-\(([^)]+)\)", structural).group(1).split("|"))
    assert rendered <= named, sorted(rendered - named)
    # The remedy's smaller size is one step down the quote scale, read off it.
    assert _SMALLER_QUOTE_SIZE == {"hero": "xl", "xl": "lg", "lg": "md"}


# ── The layout switch ────────────────────────────────────────────────────────


def test_the_layout_switch_reads_a_customer_quote_and_names_its_pictures():
    slide = _deck(_statement(photo_file_id=PHOTO, logo_file_id=LOGO), title="Contoso")["slides"][0]
    switched = switch_slide_layout(slide, "takeaways")
    items = switched.slide["items"]
    assert items[0]["title"].startswith("If I took OpenCTI away from my team")
    assert items[0]["description"] == "Dana Whitlock, VP Security Operations, Contoso Financial"
    assert [item["title"] for item in items[1:]] == ["Employees", "Revenue"]
    assert "The quote's portrait and logo - a takeaways slide lists words" in switched.dropped


def test_standalone_metrics_still_become_dashboard_tiles_and_a_lost_look_is_said():
    # The stored `metric` spells its look (`variant`, `align`); a dashboard tile
    # is a `MetricItem`, which has neither - carried over, the move was refused.
    slide = {
        "id": "s-kpis",
        "layout": "content",
        "title": "Q4 in figures",
        "blocks": [
            {"type": "metric", "value": "42%", "label": "Fewer alerts", "variant": "plain", "align": "center"},
            {"type": "metric", "value": "11 min", "label": "Triage", "delta": "-4 min"},
            {"type": "chart", "kind": "bar", "categories": ["W1", "W2"], "series": [{"name": "A", "values": [1, 2]}]},
        ],
    }
    stored = validate_spec({"title": "Deck", "slides": [slide]})["slides"][0]
    outcome = switch_slide_layout(stored, "dashboard")
    assert outcome.slide["tiles"] == [
        {"value": "42%", "label": "Fewer alerts", "tone": "accent"},
        {"value": "11 min", "label": "Triage", "tone": "accent", "delta": "-4 min"},
    ]
    validate_spec({"title": "Deck", "slides": [outcome.slide]})
    assert "The plain or centred look of 1 metric - a dashboard slide draws its tiles as cards" in outcome.dropped
    # A card metric has no look to lose: nothing is said about it.
    stored["blocks"][0] = {**stored["blocks"][0], "variant": "card", "align": "start"}
    quiet = switch_slide_layout(stored, "dashboard")
    assert not any("look of" in note for note in quiet.dropped), quiet.dropped


def test_the_pictures_of_several_quotes_are_counted_by_role():
    slide = _deck(
        _statement(photo_file_id=PHOTO, logo_file_id=LOGO),
        {"type": "quote", "content": "Second.", "author": "B", "photo_file_id": str(uuid.uuid4())},
        title="Proof",
    )["slides"][0]
    switched = switch_slide_layout(slide, "takeaways")
    assert "The quotes' 2 portraits and 1 logo - a takeaways slide lists words" in switched.dropped, switched.dropped


# ── The renderer and the stylesheet ──────────────────────────────────────────


def test_the_card_rules_of_the_stylesheet_are_pinned():
    """A quote at the card's values carries no look attribute, so it renders
    through these rules alone - pinned verbatim (the root only gains the custom
    property the other looks scale from, which paints nothing). The bar is the
    straight 4px rule of the Filigran design language: the two corners it
    runs into stay 4px, the other two keep the card radius."""
    css = _read("presentation.css")
    for rule in (
        ".deck-root .deck-quote {\n"
        "  /* The text size the variants scale from: the mark, the credit and\n"
        "     the figures follow it. The card's own rules below never read it. */\n"
        "  --deck-quote-fs: var(--deck-fs-lg);\n"
        "  margin: 0;\n"
        "  padding: var(--deck-space-4) var(--deck-space-5);\n"
        "  border-left: 4px solid var(--deck-accent);\n"
        "  background: var(--deck-surface);\n"
        "  border-radius: 4px var(--deck-radius) var(--deck-radius) 4px;\n}",
        ".deck-root .deck-quote blockquote {\n  margin: 0;\n  padding: 0;\n  border: 0;\n  background: none;\n"
        "  box-shadow: none;\n  width: auto;\n  font-family: var(--deck-heading-font);\n"
        "  font-size: var(--deck-fs-lg);\n  font-weight: 400;\n  line-height: 1.35;\n  font-style: italic;\n}",
        ".deck-root .deck-quote figcaption {\n  margin-top: var(--deck-space-2);\n  color: var(--deck-muted);\n"
        "  font-size: var(--deck-fs-sm);\n}",
        '.deck-root .deck-quote figcaption::before {\n  content: "\u2014 ";\n}',
    ):
        assert rule in css, rule
    # Every other look is keyed by an attribute a card never carries.
    style = _read("blocks", "quote-style.ts")
    assert '"data-variant": look.variant !== "card" ? look.variant : undefined,' in style
    assert '"data-size": look.size !== card.size ? look.size : undefined,' in style


def test_the_marks_are_real_glyphs_the_export_reads_as_text():
    view = _read("blocks", "quote.tsx")
    assert '<span className="deck-quote-glyph" aria-hidden="true">' in view
    assert '<span className="deck-quote-open">{OPEN_MARK}</span>' in view
    assert '<span className="deck-quote-close">{CLOSE_MARK}</span>' in view
    css = _read("presentation.css")
    assert not re.search(r"\.deck-quote-(glyph|open|close)::(before|after)", css)
    # The accent colour, in the heading font: a text run the PPTX writer keeps.
    glyph = re.search(r"\.deck-root \.deck-quote-glyph \{(.*?)\}", css, re.S).group(1)
    assert "color: var(--deck-accent);" in glyph and "font-family: var(--deck-heading-font);" in glyph
    # Being aria-hidden, the glyph is left out of the layout audit's glyph
    # measure like every decoration. That is only safe while it is an IN-FLOW
    # box cut to its ink (the upper half of its em square, line height 1): the
    # fit host then shrinks the body for it, and nothing can cut or push it off
    # the slide without cutting or pushing the quote's measured text first. A
    # glyph taken out of the flow could overflow unseen - revisit the audit.
    for declaration in ("display: block;", "height: 0.5em;", "line-height: 1;"):
        assert declaration in glyph, declaration
    assert not re.search(r"\b(position|transform|translate|margin)\s*:", glyph)
    assert not re.search(r"\.deck-quote-glyph[^{]*\{[^}]*\b(position|transform|translate)\s*:", css)
    assert "if (node.parentElement.closest(\"[aria-hidden='true']\")) continue;" in LAYOUT_AUDIT_JS


def test_the_hero_step_is_never_written_inside_its_own_resize_observer():
    # The step resizes the box the observer watches: written in the delivery,
    # it raises "loop completed with undelivered notifications". Deferred to a
    # frame, like `fit.ts` and `spread.ts`, the second pass changes nothing.
    view = _read("blocks", "quote.tsx")
    hook = view.split("function useFitHeroQuote", 1)[1].split("\nexport function", 1)[0]
    assert "new ResizeObserver(schedule)" in hook
    assert "frame = window.requestAnimationFrame(() => {" in hook
    assert "if (frame) window.cancelAnimationFrame(frame);" in hook
    assert "new ResizeObserver(fit)" not in hook


def test_the_picture_picker_never_throws_to_the_viewer_nor_takes_the_decks_undo():
    field = _read("TemplateAssetField.tsx")
    # The app throws every query error to the route's ErrorBoundary: a 404 on a
    # template the reader cannot see would drop the unsaved draft with the viewer.
    assert "useTemplateAssets(open && templateId ? templateId : undefined, {\n    throwOnError: false,\n  })" in field
    hooks = (PRESENTATION_DIR.parents[1] / "hooks" / "api" / "useTemplateContainerApi.ts").read_text(encoding="utf-8")
    assert "...(options.throwOnError === false ? { throwOnError: false } : {})" in hooks
    # Only an image is offered, whatever its role says, and the grid draws
    # each one from its bounded thumbnail, else from the file itself
    # up to 2 MB as before.
    assert '(asset.content_type ?? "").startsWith("image/")' in field
    assert "templateAssetThumbnailUrl(templateId, asset)" in field
    assert "asset.size <= MAX_PREVIEW_BYTES ? fileUrl(asset.id) : null" in field
    # Its search box writes nothing to the draft: Ctrl+Z there is the browser's.
    assert "{...OWN_UNDO_ATTR}" in field
    layer = _read("SlideEditLayer.tsx")
    assert "!target?.closest(OWN_UNDO_SELECTOR)" in layer.split("const routesUndo", 1)[1].split("};", 1)[0]
    # The legacy attribution emptied to be retyped keeps its control, decided
    # once per selected block (the control is keyed by it - the inspector is
    # not, or a Move button pressed in it would lose the focus as it moves).
    controls = _read("SchemaFieldControls.tsx")
    assert "if (field.whenSet && blank && !shownWhenSet) return null;" in controls
    inspector = _read("SlideInspector.tsx")
    assert "key={field.whenSet ? `${field.key}@${slideIndex}:${blockPath}` : field.key}" in inspector
    assert "key={`${slideIndex}:${selection.block}`}" not in inspector


def test_a_lone_statement_is_centred_by_the_layout_not_by_spacers():
    body = _read("layouts", "body.tsx")
    assert "verticalAlign={bodyVerticalAlign(slide.blocks, slide.vertical_align)}" in body
    assert "distribute={isLoneStatement(slide.blocks) ? undefined : slide.distribute}" in body
    # The design skill's quote slide is the statement alone, no spacer.
    recipe = SKILL.read_text(encoding="utf-8").split("### 3.11", 1)[1].split("### 3.12", 1)[0]
    block = json.loads(re.search(r"```json\n(.*?)\n```", recipe, re.S).group(1).replace("\n", " "))
    assert [b["type"] for b in block["blocks"]] == ["quote"]
    assert block["blocks"][0]["variant"] == "statement"
    validate_spec({"title": "Skill", "slides": [block]})


def test_the_plain_metric_drops_the_card():
    css = _read("presentation.css")
    plain = re.search(
        r'\.deck-root \.deck-metric\[data-variant="plain"\],\n'
        r'\.deck-root \.deck-metrics\[data-density\] \.deck-metric\[data-variant="plain"\] \{(.*?)\}',
        css,
        re.S,
    ).group(1)
    for declaration in ("padding: 0;", "border: 0;", "border-radius: 0;", "background: none;"):
        assert declaration in plain, declaration
    # The card light (the tone beam and bloom, `::after`) belongs to the card.
    assert '.deck-root .deck-metric[data-variant="plain"]::after { content: none; }' in css
    tile = _read("blocks", "metrics.tsx")
    assert 'data-variant={variant === "plain" ? "plain" : undefined}' in tile
    # ...and draws its value and label only: every adornment goes through the
    # one rule (`blocks/metric-look.ts`, unit-tested), so the stage, the PPTX
    # extractor and the layout check read the same tile.
    assert "const drawn = tileAdornments(metric, variant);" in tile
    for adornment in ("icon", "sparkline", "sublabel", "delta"):
        assert f"{{drawn.{adornment} ? (" in tile or f"{{drawn.{adornment} ? <" in tile, adornment
        assert f"{{metric.{adornment} ?" not in tile, adornment
