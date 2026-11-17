"""The two keynote compositions: the showcase frame of an ``image``
block and the ``challenge_answer`` block.

A keynote deck showed the product through a window chrome drawn by hand
(``render_html_to_image``) and placed as a picture, and named a problem and
its answer with two callouts and a spacer. Both are native now: ``image.frame``
(``browser`` / ``device`` on a brand-gradient glow, hugging the picture at its
ratio; with ``fill`` the largest the room holds, the column stretching for it
like a chart's) and ``challenge_answer`` (two stacked cards of one width and
one left edge joined by an arrow, error over success). Pinned here: the
schema and its defaults (an existing deck renders byte-identical), the
reference and the tool description, the layout switch reading the new
block's words, and the frontend halves - the component per block, the CSS
hooks, the types and the inspector catalog - so a half added on one side only
fails the build.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.services import presentation_guidance as guidance
from app.services.presentation_layout_switch import switch_slide_layout
from app.services.presentation_spec import (
    BLOCK_TYPES,
    CONTAINER_BLOCK_TYPES,
    FILL_BLOCK_TYPES,
    IMAGE_FRAMES,
    ChallengeAnswerBlock,
    ImageBlock,
    PresentationSpecError,
    block_catalog,
    spec_reference,
    validate_spec,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = BACKEND_ROOT.parent / "frontend"
PRESENTATION_DIR = FRONTEND_ROOT / "src" / "components" / "presentation"
SKILL_PATH = BACKEND_ROOT / "app/agents/seed_data/skill_content/presentation-design.md"


def _read(*parts: str) -> str:
    return PRESENTATION_DIR.joinpath(*parts).read_text(encoding="utf-8")


def _deck(*slides: dict) -> dict:
    return {"title": "Keynote", "slides": list(slides)}


def _content(*blocks: dict, **extra) -> dict:
    return {"layout": "content", "title": "Every alert lands in one investigation", "blocks": list(blocks), **extra}


def _image(**extra) -> dict:
    return {"type": "image", "file_id": "SCREENSHOT_FILE_ID", "alt": "The workspace", **extra}


def _challenge(**extra) -> dict:
    return {
        "type": "challenge_answer",
        "challenge": "Every incident is correlated **by hand**.",
        "answer": "One case model for every source.",
        **extra,
    }


def _stored_block(block: dict) -> dict:
    return validate_spec(_deck(_content(block)))["slides"][0]["blocks"][0]


# ── The image frame ──────────────────────────────────────────────────────────


def test_the_frame_is_off_by_default_and_the_only_difference_a_framed_image_writes():
    plain = _stored_block(_image())
    assert plain["frame"] == "none"
    for frame in ("browser", "browser_slim", "device"):
        framed = _stored_block(_image(frame=frame))
        assert framed["frame"] == frame
        assert {**framed, "frame": "none"} == plain
    # Spelling the default is the default.
    assert _stored_block(_image(frame="none")) == plain


def test_the_frame_values_are_the_ones_the_renderer_draws():
    assert IMAGE_FRAMES == ("none", "browser", "browser_slim", "device")
    assert ImageBlock.model_fields["frame"].default == "none"
    with pytest.raises(PresentationSpecError) as refused:
        validate_spec(_deck(_content(_image(frame="window"))))
    assert "frame" in str(refused.value)
    # A bool is not a frame: the chrome is a look, not a switch.
    with pytest.raises(PresentationSpecError):
        validate_spec(_deck(_content(_image(frame=True))))


def test_the_showcase_composition_validates_in_the_wide_column():
    """The slide of the issue: the framed, filling screenshot in the wide column of a wide_left slide."""
    slide = {
        "layout": "content",
        "title": "Every alert lands in one investigation",
        "columns": [
            [_image(frame="browser", fill=True)],
            [{"type": "list", "style": "arrow", "items": ["The timeline", "The graph"]}],
        ],
        "ratio": "wide_left",
        "vertical_align": "center",
    }
    stored = validate_spec(_deck(slide))["slides"][0]
    picture = stored["columns"][0][0]
    assert picture["frame"] == "browser" and picture["fill"] is True
    assert stored["ratio"] == "wide_left"


def test_the_reference_lists_the_frame_with_its_alias_and_its_meaning():
    reference = spec_reference()
    image_line = next(line for line in reference.splitlines() if line.startswith("- image:"))
    assert 'frame?: ImageFrame = "none"' in image_line
    assert "browser = window bar, browser_slim = thin bar" in image_line
    assert "device = bezel" in image_line
    assert "hugging the picture at its ratio" in image_line
    assert '- ImageFrame: "none"|"browser"|"browser_slim"|"device"' in reference
    assert "frame" in block_catalog()["image"]


# ── The challenge_answer block ───────────────────────────────────────────────


def test_challenge_answer_is_an_atomic_block_of_the_catalog():
    assert "challenge_answer" in BLOCK_TYPES
    assert "challenge_answer" not in CONTAINER_BLOCK_TYPES
    assert block_catalog()["challenge_answer"] == [
        "challenge",
        "answer",
        "icon",
        "challenge_label",
        "answer_label",
        "fill",
    ]
    # Atomic: it goes where a callout's children and a comparison side go, and in a column.
    callout = {"type": "callout", "title": "Why", "children": [_challenge()]}
    comparison = {
        "type": "comparison",
        "left": {"label": "Before", "blocks": [_challenge()]},
        "right": {"label": "After", "blocks": [{"type": "text", "content": "x"}]},
    }
    columns = {"type": "columns", "columns": [[_challenge()], [{"type": "text", "content": "x"}]]}
    stored = validate_spec(_deck(_content(callout, comparison, columns)))["slides"][0]["blocks"]
    assert stored[0]["children"][0]["type"] == "challenge_answer"
    assert stored[1]["left"]["blocks"][0]["type"] == "challenge_answer"
    assert stored[2]["columns"][0][0]["type"] == "challenge_answer"


def test_challenge_answer_stores_its_defaults_and_refuses_an_empty_side():
    stored = _stored_block(_challenge())
    assert stored == {
        "type": "challenge_answer",
        "challenge": "Every incident is correlated **by hand**.",
        "answer": "One case model for every source.",
        "challenge_label": "Challenge",
        "answer_label": "Answer",
        "fill": False,
    }
    assert ChallengeAnswerBlock.model_fields["fill"].default is False
    # Not a grid block: its `fill` stretches the two cards, no row spread to cap.
    assert "challenge_answer" not in FILL_BLOCK_TYPES
    relabelled = _stored_block(
        _challenge(challenge_label="The problem", answer_label="Our answer", icon="layers", fill=True)
    )
    assert relabelled["challenge_label"] == "The problem"
    assert relabelled["answer_label"] == "Our answer"
    assert relabelled["icon"] == "layers" and relabelled["fill"] is True
    for missing in ("challenge", "answer"):
        block = _challenge()
        del block[missing]
        with pytest.raises(PresentationSpecError) as refused:
            validate_spec(_deck(_content(block)))
        assert missing in str(refused.value)
    with pytest.raises(PresentationSpecError):
        validate_spec(_deck(_content(_challenge(answer=""))))
    # Markup, never HTML; the tones are the block's own, not a field.
    with pytest.raises(PresentationSpecError):
        validate_spec(_deck(_content(_challenge(challenge="<b>Five</b> consoles"))))
    with pytest.raises(PresentationSpecError):
        validate_spec(_deck(_content(_challenge(tone="warning"))))


def test_the_reference_describes_the_block_and_its_fill_as_a_card_stretch():
    lines = {line[2:].split(":", 1)[0]: line for line in spec_reference().splitlines() if line.startswith("- ")}
    line = lines["challenge_answer"]
    assert "challenge: markup<=1500" in line and "answer: markup<=1500" in line
    assert 'challenge_label?: markup<=120 = "Challenge"' in line
    assert 'answer_label?: markup<=120 = "Answer"' in line
    assert "fill?: bool = false (stretch the two cards to share the available height)" in line
    assert "icon?: icon (leads the challenge card" in line


# ── The guidance ─────────────────────────────────────────────────────────────


def test_the_tool_description_teaches_both_compositions_once():
    description = guidance.build_generate_presentation_description()
    showcase = next(part for part in description.split("\n\n") if part.startswith("SHOWCASE:"))
    assert json.dumps(guidance.SHOWCASE_IMAGE_EXAMPLE, ensure_ascii=False, separators=(",", ":")) in showcase
    assert '"browser"' in showcase and '"device"' in showcase
    assert "`challenge_answer`" in showcase and "error over success" in showcase
    assert "never two callouts and a spacer" in showcase
    assert description.count("SHOWCASE:") == 1
    # The example is a placeholder file id, a frame and fill - the slide of the issue.
    example = guidance.SHOWCASE_IMAGE_EXAMPLE
    assert example["frame"] == "browser" and example["fill"] is True
    assert re.fullmatch(r"[A-Z][A-Z_]*FILE_ID", example["file_id"])
    stored = _stored_block(example)
    assert stored["frame"] == "browser"
    challenge = guidance.BLOCK_EXAMPLES["challenge_answer"]
    assert challenge["icon"] and "**" in challenge["challenge"]


def test_the_skill_teaches_the_showcase_in_the_wide_column_and_the_challenge_alone():
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert "### 3.21 Product showcase (framed screenshot)" in text
    assert "### 3.22 Challenge and answer" in text
    blocks = [json.loads(raw) for raw in re.findall(r"```json\n(.*?)\n```", text, flags=re.DOTALL)]
    showcase = next(s for s in blocks if any(b.get("frame") for column in s.get("columns", []) for b in column))
    picture = showcase["columns"][0][0]
    assert picture == {**picture, "type": "image", "frame": "browser", "fill": True}
    assert showcase["ratio"] == "wide_left" and showcase["vertical_align"] == "center"
    challenge = next(s for s in blocks if any(b.get("type") == "challenge_answer" for b in s.get("blocks", [])))
    assert challenge["blocks"][0]["fill"] is True and challenge["blocks"][0]["icon"]
    assert challenge["footer_block"]["type"] == "callout"
    # The review pass names both, and the "what not to do" names the hand-drawn window.
    assert '`frame: "browser"` or `"device"` (3.21)' in text
    assert "one `challenge_answer` block (3.22)" in text
    assert "which is the `image` block's own `frame`" in text


# ── The layout switch ────────────────────────────────────────────────────────


def test_the_layout_switch_reads_the_two_cards_as_two_points():
    slide = validate_spec(_deck(_content(_challenge(icon="layers"))))["slides"][0]
    switched = switch_slide_layout(slide, "takeaways")
    items = switched.slide["items"]
    assert [item["title"] for item in items] == ["Challenge", "Answer"]
    assert items[0]["description"] == "Every incident is correlated **by hand**."
    assert items[1]["description"] == "One case model for every source."
    assert items[0].get("icon") == "layers"
    assert items[1].get("icon") == "circle-check"
    # Nothing is dropped: the block carries words only.
    assert not any("challenge" in dropped for dropped in switched.dropped)
    relabelled = validate_spec(_deck(_content(_challenge(challenge_label="The problem", answer_label="Our answer"))))[
        "slides"
    ][0]
    assert [item["title"] for item in switch_slide_layout(relabelled, "agenda").slide["items"]] == [
        "The problem",
        "Our answer",
    ]


# ── The frontend halves ──────────────────────────────────────────────────────


def test_frontend_media_renders_the_frame_from_the_field_and_the_css_draws_it():
    media = _read("blocks", "media.tsx")
    assert "data-frame={framed ? frame : undefined}" in media
    assert 'className="deck-showcase-glow" aria-hidden="true" data-export-backdrop="true"' in media
    assert "useShowcaseRoom(roomRef, framed && !!block.fill)" in media
    # The picture's ratio reaches the box around it through the shared image.
    shared = _read("blocks", "shared.tsx")
    assert "onRatio?: (ratio: number) => void" in shared
    assert "onRatio?.(measured)" in shared
    css = _read("presentation.css")
    for frame in ("browser", "browser_slim", "device"):
        assert f'.deck-root .deck-figure[data-frame="{frame}"] {{' in css, frame
    # The slim bar scales with its room, a third of the browser bar at most, and feeds the chrome maths.
    slim = css[css.index('.deck-root .deck-figure[data-frame="browser_slim"] {') :]
    slim = slim[: slim.index("}")]
    assert "--deck-showcase-bar: clamp(8px, 2cqw, 14px);" in slim
    assert "--deck-showcase-extra-h: calc(var(--deck-showcase-bar) + 2 * var(--deck-hairline));" in slim
    assert (
        '.deck-root .deck-figure[data-frame="browser_slim"] .deck-showcase-room { container-type: inline-size; }' in css
    )
    assert 'frame === "browser_slim" ?' in media
    # The window takes the picture's shape: the bucket rule and the room rule, each reading the ratio.
    assert (
        '.deck-root .deck-figure[data-frame]:not([data-size="full"]):not([data-fill="true"]) .deck-showcase[data-sized="true"]'
        in css
    )
    assert '.deck-root .deck-figure[data-frame][data-fill="true"] .deck-showcase[data-sized="true"]' in css
    assert "var(--deck-room-w, 100%)" in css and "var(--deck-room-h, var(--deck-image-h, 520px))" in css
    # The glow is a background (a picture in the PPTX), in the brand gradient, per scheme.
    glow = css[css.index(".deck-root .deck-showcase-glow {") :]
    glow = glow[: glow.index("}")]
    assert "background-image: radial-gradient(" in glow and "box-shadow" not in glow
    assert "var(--deck-gradient-from)" in glow and "var(--deck-gradient-to)" in glow
    # One glow and one bezel per scheme; the template design tokens add one glow override, nothing else.
    schemes = css[: css.index("/* \u2500\u2500 Template design tokens")]
    assert schemes.count("--deck-showcase-glow:") == 2 and schemes.count("--deck-showcase-bezel:") == 2
    flat = css[css.index('.deck-root [data-design-glow="false"] {') :]
    flat = flat[: flat.index("}")]
    assert "--deck-showcase-glow: 0%;" in flat
    assert css.count("--deck-showcase-glow:") == 3 and css.count("--deck-showcase-bezel:") == 2
    # A fill image is a filler its column stretches for, in a columns block and in the slide's columns.
    assert (
        '.deck-root .deck-blocks > .deck-columns:has(> .deck-column > .deck-blocks > .deck-figure[data-fill="true"])'
        in css
    )
    assert '.deck-root .deck-columns > .deck-column:has(> .deck-blocks > .deck-figure[data-fill="true"])' in css


def test_frontend_challenge_block_mirrors_the_model_and_the_css_stacks_it():
    view = _read("blocks", "challenge.tsx")
    assert "export function ChallengeAnswerBlockView(" in view
    assert 'const CHALLENGE_DEFAULT_LABELS = { challenge: "Challenge", answer: "Answer" } as const;' in view
    defaults = ChallengeAnswerBlock.model_fields
    assert defaults["challenge_label"].default == "Challenge" and defaults["answer_label"].default == "Answer"
    assert 'role="challenge"' in view and 'tone="error"' in view
    assert 'role="answer"' in view and 'tone="success"' in view
    # The connector: one SVG between the cards, decoration, captured alone for the PPTX.
    arrow = view.split("function ChallengeArrow() {", 1)[1].split("\n}\n", 1)[0]
    assert (
        '<svg\n      className="deck-challenge-arrow"\n      aria-hidden="true"\n      data-export-alone="true"'
        in arrow
    )
    assert "strokeWidth={DECK_ICON_STROKE_WIDTH}" in arrow
    assert "<ChallengeArrow />" in view.split("export function ChallengeAnswerBlockView(", 1)[1]
    assert 'case "challenge_answer":' in _read("blocks", "index.tsx")
    css = _read("presentation.css")
    assert ".deck-root .deck-challenge {" in css
    assert "grid-template-columns: minmax(0, 1fr);" in css[css.index(".deck-root .deck-challenge {") :]
    assert '.deck-root .deck-challenge[data-fill="true"] {' in css
    # Lit like every card of the catalog.
    assert ".deck-challenge-card\n)" in css or ".deck-challenge-card\n) {" in css


def test_frontend_types_and_inspector_carry_the_new_fields():
    types = (FRONTEND_ROOT / "src" / "types" / "index.ts").read_text(encoding="utf-8")
    assert 'export type DeckImageFrame = "none" | "browser" | "browser_slim" | "device";' in types
    image = types.split("export interface DeckImageBlock ", 1)[1].split("\n}\n", 1)[0]
    assert "frame?: DeckImageFrame;" in image
    block = types.split("export interface DeckChallengeAnswerBlock ", 1)[1].split("\n}\n", 1)[0]
    for field in (
        "challenge: DeckMarkup;",
        "answer: DeckMarkup;",
        "icon?: DeckIconName;",
        "challenge_label?: DeckMarkup;",
        "answer_label?: DeckMarkup;",
        "fill?: boolean;",
    ):
        assert field in block, field
    assert "| DeckChallengeAnswerBlock" in types.split("export type DeckAtomicBlock =", 1)[1].split(";", 1)[0]
    schema = _read("block-schema.ts")
    assert 'export const IMAGE_FRAMES = ["none", "browser", "browser_slim", "device"] as const;' in schema
    assert 'en("frame", "Frame", IMAGE_FRAMES' in schema
    assert "  challenge_answer: {" in schema
    icons = _read("block-icons.ts")
    assert re.search(r"^  challenge_answer: \w+,$", icons, re.MULTILINE)
    # The option labels of the frame picker, and every inspector string, in the four catalogs.
    controls = _read("SchemaFieldControls.tsx")
    assert 'browser: "Browser window"' in controls and 'device: "Device"' in controls
    assert 'browser_slim: "Slim browser window"' in controls
    for lang in ("en", "fr", "es", "ja"):
        catalog = json.loads((FRONTEND_ROOT / "src" / "i18n" / "lang" / f"{lang}.json").read_text(encoding="utf-8"))
        for key in (
            "Challenge and answer",
            "The challenge",
            "The answer",
            "Frame",
            "Browser window",
            "Slim browser window",
            "Device",
        ):
            assert key in catalog, (lang, key)
