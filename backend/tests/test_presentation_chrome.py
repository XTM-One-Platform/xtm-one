"""The footer / header a template defines, filled once per deck.

The complaint that opened the issue: the Filigran brand deck's footer was
whatever the agent typed into ``chrome.footer_text`` when it created the deck
- it did not follow the title, and changing it meant asking the assistant to
touch every slide. These tests pin the contract that replaces that:

1. A presentation template declares ONE footer (and optional header) pattern
   in ``extra_metadata.chrome`` - ``"{{event}} - {{title}} - {{classification}}"``
   - plus the variables the pattern fills, validated like the palette.
2. Every deck the template dresses renders that pattern from its own settings
   (title, opening subtitle, event label, ``chrome.fields``) on every slide;
   an empty placeholder collapses WITH its separator.
3. The tool surface reads the footer back and edits it as one deck setting
   (``update_deck`` merges ``chrome.fields`` key by key), never as slide text.
4. The frontend renders the same rule from the same pattern: the TypeScript
   mirror is pinned against this module.

No DB required: the tool opens its own short-lived session, which is
monkeypatched with a capturing fake - the same no-DB style as
``test_presentation_patch.py``, whose fixtures this module reuses.
"""

from __future__ import annotations

import copy
import inspect
import json
import re
import uuid
from pathlib import Path

import pytest

from app.services import presentation_chrome as pc
from app.services import presentation_patch, presentation_spec
from app.services.presentation_chrome import (
    BUILTIN_PLACEHOLDERS,
    MAX_CHROME_VARIABLES,
    MAX_DECK_FIELDS,
    MAX_FIELD_VALUE_CHARS,
    MAX_PATTERN_CHARS,
    ChromeVariable,
    RenderedChrome,
    TemplateChrome,
    describe_template_chrome,
    footer_status_line,
    normalize_template_chrome,
    render_chrome,
    render_chrome_pattern,
    template_chrome,
    template_chrome_redraws,
)
from app.services.presentation_patch import LAYOUT_DECK_FIELDS, PresentationPatchError, apply_operations
from app.services.presentation_spec import MAX_LABEL_CHARS, PresentationSpecError, validate_spec
from tests.test_presentation_patch import (
    _build_tools,
    _deck,
    _deck_lookup,
    _existing_row,
    _FakeSession,
    _patch_session,
    _Result,
    _run_tool,
    _template_lookup,
    _template_row,
)

FRONTEND_ROOT = Path(__file__).resolve().parents[2] / "frontend"
PRESENTATION_DIR = FRONTEND_ROOT / "src" / "components" / "presentation"
#: The ONE table both halves of the rule are checked against - the frontend's
#: ``tests_e2e/chrome-pattern.spec.ts`` feeds the same file to ``chrome-pattern.ts``.
SHARED_CASES_PATH = FRONTEND_ROOT / "tests_e2e" / "fixtures" / "chrome-pattern-cases.json"
SHARED_CASES = json.loads(SHARED_CASES_PATH.read_text(encoding="utf-8"))

#: The frontend validator's problem CODES -> the line the backend validator
#: writes for the same problem, so one table checks the two validators refuse
#: the same declarations for the same reasons.
PROBLEM_LINES: dict[str, str] = {
    "pattern_too_long": r"\.(footer|header) is \d+ characters; the maximum is",
    "pattern_unbalanced": r"has an unbalanced placeholder",
    "pattern_malformed_slot": r"has slots that are not placeholders",
    "too_many_variables": r"declares \d+ variables; the maximum is",
    "variable_key_invalid": r"\.key .* is not a valid key",
    "variable_key_builtin": r"\.key .* is a built-in placeholder",
    "variable_key_duplicate": r"\.key .* is declared twice",
    "variable_label_missing": r"\.label is required",
    "variable_label_too_long": r"\.label is over \d+ characters",
    "variable_hint_too_long": r"\.hint is over \d+ characters",
    "variable_default_too_long": r"\.default is over \d+ characters",
    "variables_without_pattern": r"declares variables but no footer or header pattern",
    "placeholder_undeclared": r"uses placeholders no variable declares",
    "variable_unused": r"which no footer or header pattern uses",
}

#: The declaration both brand decks ship (``template_container_seed._BRAND_CHROME``,
#: pinned equal by the seed test).
BRAND_CHROME = {
    "footer": "{{event}} - {{title}} - {{classification}}",
    "header": None,
    "variables": [
        {"key": "classification", "label": "Classification", "hint": "Who may read this deck: Public, Internal, Confidential..."}
    ],
}


def _save_probes() -> list[_Result]:
    """The two results ``save_presentation`` consumes on an edit before anything
    is read AFTER the save: the history's duplicate-snapshot probe (none) and
    its retention count (zero)."""
    return [_Result(), _Result()]


def _chrome(**overrides) -> TemplateChrome:
    declared = {**BRAND_CHROME, **overrides}
    return TemplateChrome(
        footer=declared["footer"],
        header=declared["header"],
        variables=tuple(ChromeVariable(**v) for v in declared["variables"]),
    )


# ── The render rule ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("pattern", "values", "expected"),
    [
        # Every placeholder filled: the pattern as written.
        ("{{event}} - {{title}} - {{classification}}", {"event": "FIC 2026", "title": "Roadmap", "classification": "Internal"}, "FIC 2026 - Roadmap - Internal"),
        # An empty placeholder collapses WITH the separator next to it - at
        # the start, in the middle, at the end.
        ("{{a}} - {{b}} - {{c}}", {"a": "a", "b": "", "c": "c"}, "a - c"),
        ("{{a}} - {{b}} - {{c}}", {"a": "a", "b": "b", "c": ""}, "a - b"),
        ("{{a}} - {{b}} - {{c}}", {"a": "", "b": "b", "c": "c"}, "b - c"),
        ("{{a}} - {{b}} - {{c}}", {"a": "", "b": "b", "c": ""}, "b"),
        # A leading literal stays as long as SOMETHING follows it.
        ("Filigran - {{event}} - {{title}}", {"event": "", "title": "Roadmap"}, "Filigran - Roadmap"),
        ("Filigran - {{event}} - {{title}}", {"event": "", "title": ""}, ""),
        ("{{title}}: {{subtitle}}", {"title": "Roadmap", "subtitle": ""}, "Roadmap"),
        # Whitespace inside the braces is tolerated; values are trimmed.
        ("{{ title }} | {{event}}", {"title": " T ", "event": "E"}, "T | E"),
        # No placeholder at all: a fixed footer, rendered as written.
        ("Confidential", {}, "Confidential"),
        # Nothing filled: nothing drawn (never a bare separator).
        ("{{event}} - {{title}}", {}, ""),
        (None, {"title": "T"}, ""),
        ("", {"title": "T"}, ""),
    ],
)
def test_render_chrome_pattern_collapses_empty_placeholders_with_their_separators(pattern, values, expected):
    assert render_chrome_pattern(pattern, values) == expected


def test_render_chrome_reads_the_deck_settings_and_the_variable_defaults():
    spec = {
        "title": "OpenCTI Connector Ecosystem",
        "slides": [{"layout": "title", "title": "Opening", "subtitle": "Partner overview"}],
        "chrome": {"event_label": "FIC 2026"},
    }
    chrome = _chrome()
    rendered = render_chrome(spec, chrome)
    # The brand classification has no default: unset, it collapses with its
    # separator exactly like an empty event...
    assert rendered.footer == "FIC 2026 - OpenCTI Connector Ecosystem"
    assert rendered.header == ""
    # ...a variable that declares a default is filled from it...
    defaulted = _chrome(variables=[{"key": "classification", "label": "Classification", "default": "Internal"}])
    assert render_chrome(spec, defaulted).footer == "FIC 2026 - OpenCTI Connector Ecosystem - Internal"
    # ...and ``chrome.fields`` wins over both - ONE deck setting.
    spec["chrome"]["fields"] = {"classification": "Confidential"}
    assert render_chrome(spec, chrome).footer == "FIC 2026 - OpenCTI Connector Ecosystem - Confidential"
    assert render_chrome(spec, defaulted).footer == "FIC 2026 - OpenCTI Connector Ecosystem - Confidential"
    # No event label: the separator goes with it, and a rename follows.
    del spec["chrome"]["event_label"]
    spec["title"] = "Renamed"
    assert render_chrome(spec, chrome).footer == "Renamed - Confidential"
    # The subtitle is the OPENING slide's, and a header renders the same way.
    header = _chrome(header="{{title}}: {{subtitle}}")
    assert render_chrome(spec, header).header == "Renamed: Partner overview"
    spec["slides"][0]["subtitle"] = ""
    assert render_chrome(spec, header).header == "Renamed"
    # A deck without a template pattern renders nothing through this path.
    assert render_chrome(spec, None) == pc.RenderedChrome(footer="", header="")


# ── The declaration and its validation ───────────────────────────────────────


def test_normalize_accepts_the_brand_declaration_in_canonical_form():
    canonical, problems = normalize_template_chrome(
        {
            "footer": "  {{event}} - {{title}} - {{classification}}  ",
            "variables": [
                {"key": " Classification ", "label": " Classification ", "default": " Internal ", "hint": "Who may read it"}
            ],
        }
    )
    assert problems == []
    assert canonical == {
        "footer": "{{event}} - {{title}} - {{classification}}",
        "header": None,
        "variables": [{"key": "classification", "label": "Classification", "hint": "Who may read it", "default": "Internal"}],
    }
    read = template_chrome({"chrome": canonical})
    assert read is not None and read.uses("classification") and read.placeholders == ("event", "title", "classification")
    # A placeholder is spelled exactly as the key is declared: ``{{Classification}}``
    # would render as nothing on every slide, so it is reported, not stored.
    canonical, problems = normalize_template_chrome({**BRAND_CHROME, "footer": "{{event}} - {{Classification}}"})
    assert canonical is None and any("{{Classification}}" in p for p in problems), problems


def test_normalize_accepts_a_header_only_declaration_and_an_empty_one():
    canonical, problems = normalize_template_chrome({"header": "{{title}}"})
    assert problems == [] and canonical == {"footer": None, "header": "{{title}}", "variables": []}
    # Nothing declared is not a declaration: the caller drops the key.
    assert normalize_template_chrome({}) == (None, [])
    assert normalize_template_chrome({"footer": "", "header": None, "variables": []}) == (None, [])


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ("not a dict", "must be an object"),
        ({"footer": "{{title}}", "bogus": 1}, "keys the renderer does not read: bogus"),
        ({"footer": 12}, "footer must be a string"),
        ({"footer": "x" * (MAX_PATTERN_CHARS + 1)}, f"maximum is {MAX_PATTERN_CHARS}"),
        ({"footer": "{{title}} - {{event"}, "unbalanced"),
        ({"footer": "{{ti tle}}"}, "not placeholders: {{ti tle}}"),
        ({"footer": "{{title}} - {{customer}}"}, "no variable declares: {{customer}}"),
        ({"footer": "{{Title}}"}, "no variable declares: {{Title}}"),
        ({"footer": "{{title}}", "variables": [{"key": "customer", "label": "Customer"}]}, "no footer or header pattern uses"),
        ({"variables": [{"key": "customer", "label": "Customer"}]}, "no footer or header pattern to fill"),
        ({"footer": "{{x_y}}", "variables": [{"key": "x-y", "label": "X"}]}, "not a valid key"),
        ({"footer": "{{title}}", "variables": [{"key": "title", "label": "Title"}]}, "built-in placeholder"),
        ({"footer": "{{a}}", "variables": [{"key": "a", "label": "A"}, {"key": "a", "label": "A again"}]}, "declared twice"),
        ({"footer": "{{a}}", "variables": [{"key": "a"}]}, "label is required"),
        ({"footer": "{{a}}", "variables": [{"key": "a", "label": "A", "extra": 1}]}, "keys the renderer does not read: extra"),
        ({"footer": "{{a}}", "variables": "a"}, "variables must be a list"),
        ({"footer": "{{a}}", "variables": [{"key": "a", "label": "A", "hint": "h" * 200}]}, "hint is over"),
        ({"footer": "{{a}}", "variables": [{"key": "a", "label": "A", "default": 3}]}, "default must be a string"),
    ],
)
def test_normalize_reports_every_malformed_declaration_and_stores_nothing(raw, fragment):
    canonical, problems = normalize_template_chrome(raw)
    assert canonical is None
    assert problems, raw
    assert any(fragment in problem for problem in problems), problems
    assert all(problem.startswith("extra_metadata.chrome") for problem in problems), problems


def test_a_valid_key_declares_its_placeholder_even_when_the_row_is_incomplete():
    """The person adding a variable in the template editor types the key first
    and the label second: between the two the report must say ONE thing (the
    label is missing), never also "no variable declares {{client}}" for the
    very key they just typed. Mirrored by ``validateTemplateChrome`` in
    ``chrome-pattern.ts``."""
    canonical, problems = normalize_template_chrome(
        {"footer": "{{event}} | {{title}} | {{client}}", "variables": [{"key": "client"}]}
    )
    assert canonical is None
    assert len(problems) == 1, problems
    assert "label is required" in problems[0]
    assert "no variable declares" not in problems[0]


def test_normalize_caps_the_variables():
    variables = [{"key": f"v{i}", "label": f"V{i}"} for i in range(MAX_CHROME_VARIABLES + 1)]
    footer = " ".join(f"{{{{v{i}}}}}" for i in range(MAX_CHROME_VARIABLES + 1))
    canonical, problems = normalize_template_chrome({"footer": footer, "variables": variables})
    assert canonical is None and any(str(MAX_CHROME_VARIABLES) in p for p in problems)


def test_the_lenient_reader_never_raises_on_what_an_older_row_stores():
    """A row written before the validation existed, or by hand in the admin
    objects browser, must still render what it can: a footer with malformed
    variables renders with the well-formed ones; no pattern is no chrome."""
    assert template_chrome(None) is None
    assert template_chrome({"chrome": None}) is None
    assert template_chrome({"chrome": {"footer": "   "}}) is None
    assert template_chrome({"chrome": "footer"}) is None
    read = template_chrome(
        {
            "chrome": {
                "footer": "{{title}} - {{a}}",
                "variables": [
                    {"key": "a", "label": "A"},
                    "garbage",
                    {"key": "a", "label": "A twice"},
                    {"label": "no key"},
                    {"key": "B", "label": "B", "default": 3},
                ],
            }
        }
    )
    assert read is not None
    assert read.footer == "{{title}} - {{a}}" and read.header is None
    assert [v.key for v in read.variables] == ["a", "b"]
    assert read.variables[0].label == "A"  # the first declaration of a key wins
    assert read.variables[1].default is None  # a non-string default is dropped, the variable kept
    assert read.declares_pattern and read.variable_keys == ("a", "b")
    assert read.as_dict() == {
        "footer": "{{title}} - {{a}}",
        "header": None,
        "variables": [{"key": "a", "label": "A"}, {"key": "b", "label": "B"}],
    }


# ── The body-slide watermark ─────────────────────────────────────────────────


def test_the_watermark_is_read_only_from_a_literal_true():
    on = template_chrome({"chrome": {"footer": "{{title}}", "watermark": True}})
    assert on is not None and on.watermark is True
    assert on.as_dict()["watermark"] is True
    for value in ("true", 1, "yes", None, False):
        read = template_chrome({"chrome": {"footer": "{{title}}", "watermark": value}})
        assert read is not None and read.watermark is False, value
        # A declaration that never turned it on keeps the shape it always had.
        assert "watermark" not in read.as_dict(), value


def test_a_watermark_alone_is_a_declaration_that_draws_no_pattern():
    read = template_chrome({"chrome": {"watermark": True}})
    assert read is not None and read.watermark is True
    # No pattern: the deck keeps its v1 footer, nothing is redrawn through the template.
    assert not read.declares_pattern
    assert render_chrome({"title": "Deck"}, read) == RenderedChrome(footer="", header="")
    assert template_chrome({"chrome": {"watermark": False}}) is None


def test_the_watermark_validates_like_the_rest_of_the_declaration():
    canonical, problems = normalize_template_chrome({**BRAND_CHROME, "watermark": True})
    assert problems == [] and canonical == {**BRAND_CHROME, "watermark": True}
    # Off is the absence of the key, never a stored false.
    canonical, problems = normalize_template_chrome({**BRAND_CHROME, "watermark": False})
    assert problems == [] and canonical == BRAND_CHROME
    # The watermark alone is stored; the void (no pattern, no watermark) is dropped.
    assert normalize_template_chrome({"watermark": True}) == (
        {"footer": None, "header": None, "variables": [], "watermark": True},
        [],
    )
    assert normalize_template_chrome({"watermark": False}) == (None, [])
    # A non-boolean is refused by name, and nothing is stored.
    canonical, problems = normalize_template_chrome({**BRAND_CHROME, "watermark": "yes"})
    assert canonical is None
    assert any("watermark must be true or false" in problem for problem in problems)


def test_the_agent_reads_the_watermark_back():
    lines = describe_template_chrome(template_chrome({"chrome": {**BRAND_CHROME, "watermark": True}}))
    assert any("watermark" in line and "chrome.show_logo" in line for line in lines)
    assert describe_template_chrome(template_chrome({"chrome": {"watermark": True}})) == [
        line for line in lines if "watermark" in line
    ]
    assert not any("watermark" in line for line in describe_template_chrome(template_chrome({"chrome": BRAND_CHROME})))


def test_the_agent_reads_the_emblem_back_and_it_validates_like_the_watermark():
    on = template_chrome({"chrome": {"footer": "{{title}}", "emblem": True}})
    assert on is not None and on.emblem is True and on.watermark is False
    assert on.as_dict()["emblem"] is True
    for value in ("true", 1, None, False):
        read = template_chrome({"chrome": {"footer": "{{title}}", "emblem": value}})
        assert read is not None and read.emblem is False and "emblem" not in read.as_dict(), value
    # The emblem alone is a declaration; with the watermark both are stored.
    alone = template_chrome({"chrome": {"emblem": True}})
    assert alone is not None and alone.emblem and not alone.declares_pattern
    assert normalize_template_chrome({"emblem": True}) == (
        {"footer": None, "header": None, "variables": [], "emblem": True},
        [],
    )
    canonical, problems = normalize_template_chrome({**BRAND_CHROME, "watermark": True, "emblem": True})
    assert problems == [] and canonical == {**BRAND_CHROME, "watermark": True, "emblem": True}
    canonical, problems = normalize_template_chrome({**BRAND_CHROME, "emblem": "on"})
    assert canonical is None and any("emblem must be true or false" in problem for problem in problems)
    lines = describe_template_chrome(template_chrome({"chrome": {**BRAND_CHROME, "emblem": True}}))
    assert any("emblem" in line and "top-right corner" in line and "chrome.show_logo" in line for line in lines)
    assert not any("emblem" in line for line in describe_template_chrome(template_chrome({"chrome": BRAND_CHROME})))


def test_footer_right_and_the_emblem_placement_validate_and_read_back():
    """``footer_right`` may read {{page}} and {{year}} (only it may),
    ``emblem_size`` is 24..96 px, ``emblem_align`` is margin or title."""
    declared = {**BRAND_CHROME, "footer_right": "{{page}} · © {{year}} Acme", "emblem": True,
                "emblem_size": 48, "emblem_align": "title", "emblem_ink": "accent"}
    canonical, problems = normalize_template_chrome(declared)
    assert problems == [] and canonical == declared
    read = template_chrome({"chrome": declared})
    assert read is not None and read.as_dict() == declared
    lines = describe_template_chrome(read)
    assert any("48 px" in line and "top of the slide title" in line and "accent" in line for line in lines)
    assert any("footer right" in line and "{{page}}" in line for line in lines)
    # Only the right footer reads the per-slide placeholders.
    canonical, problems = normalize_template_chrome({**BRAND_CHROME, "footer": "{{title}} {{page}}"})
    assert canonical is None and any("only footer_right may use it" in p for p in problems)
    for bad in ({"emblem_size": 12}, {"emblem_size": True}, {"emblem_size": "48"}, {"emblem_align": "left"},
                {"emblem_ink": "blue"}):
        canonical, problems = normalize_template_chrome({**BRAND_CHROME, "emblem": True, **bad})
        assert canonical is None and problems, bad
        lenient = template_chrome({"chrome": {**BRAND_CHROME, "emblem": True, **bad}})
        assert lenient is not None and lenient.emblem_size is None and lenient.emblem_align is None, bad
        assert lenient.emblem_ink is None, bad
    # An unknown placeholder in the right footer is reported like any other.
    canonical, problems = normalize_template_chrome({**BRAND_CHROME, "footer_right": "{{pages}}"})
    assert canonical is None and any("{{pages}}" in p for p in problems)
    # A template without the keys keeps the shape it always had.
    assert set(template_chrome({"chrome": BRAND_CHROME}).as_dict()) == {"footer", "header", "variables"}


def test_the_thread_deck_signs_every_slide_with_its_edition_and_draws_the_emblem():
    """The THREAD deck's footer opens with the conference edition - a declared
    variable WITH a default, so a deck nobody configured still reads "THREAD
    2026" - and its mark is the corner emblem, not a faint watermark."""
    from app.services.template_container_seed import BUILTIN_TEMPLATE_CONTAINERS

    thread = next(c for c in BUILTIN_TEMPLATE_CONTAINERS if c["slug"] == "thread-brand")
    canonical, problems = normalize_template_chrome(thread["extra_metadata"]["chrome"])
    assert problems == []
    assert canonical["footer"] == "{{edition}} - {{title}} - {{classification}}"
    assert canonical.get("emblem") is True and "watermark" not in canonical
    read = template_chrome(thread["extra_metadata"])
    assert [(v.key, v.default) for v in read.variables] == [("edition", "THREAD 2026"), ("classification", None)]
    rendered = render_chrome({"title": "The Road Ahead", "slides": []}, read)
    assert rendered.footer == "THREAD 2026 - The Road Ahead"
    guidance = thread["extra_metadata"]["guidance"]
    assert "never write a footer" in guidance and "emblem" in guidance


def test_the_seeded_brand_decks_declare_the_footer_and_it_validates():
    from app.services.template_container_seed import BUILTIN_TEMPLATE_CONTAINERS

    decks = {c["slug"]: c for c in BUILTIN_TEMPLATE_CONTAINERS if c["kind"] == "presentation"}
    assert {"filigran-brand", "thread-brand", "xtm-one-brand"} <= set(decks)
    # The THREAD deck declares a footer of its own (pinned just above).
    decks.pop("thread-brand")
    # The Filigran deck (PPT Design System 3.3): title · event ·
    # classification, the corner emblem on every body slide, no watermark.
    # The XTM One deck keeps the shared declaration and plain body slides.
    # Slice 2: the right footer reads page and copyright, the emblem is
    # 48 px aligned with the title.
    filigran_chrome = {
        **BRAND_CHROME,
        "footer": "{{title}} · {{event}} · {{classification}}",
        "emblem": True,
        "footer_right": "{{page}} · © {{year}} Filigran. All rights reserved.",
        "emblem_size": 48,
        "emblem_align": "title",
        # The one-colour mark, inked with the accent on light.
        "emblem_ink": "accent",
    }
    for slug, deck in decks.items():
        declared = deck["extra_metadata"]["chrome"]
        canonical, problems = normalize_template_chrome(declared)
        assert problems == [], (slug, problems)
        expected = filigran_chrome if slug == "filigran-brand" else BRAND_CHROME
        assert canonical == expected, slug
        assert template_chrome(deck["extra_metadata"]).watermark is False, slug
        assert template_chrome(deck["extra_metadata"]).emblem is (slug == "filigran-brand"), slug
        read = template_chrome(deck["extra_metadata"])
        assert read is not None and read.uses("title") and read.uses("event"), slug
        # Optional like the event: no default, so a deck that never
        # sets a classification draws none - never an implied "Internal".
        assert [(v.key, v.default) for v in read.variables] == [("classification", None)], slug
    # And the seed's guidance tells the agent the footer is not slide text.
    assert "never write a footer into a slide" in decks["filigran-brand"]["extra_metadata"]["guidance"]


# ── validate_chrome at every template write ──────────────────────────────────


def test_validate_chrome_is_the_one_gate_every_template_write_goes_through():
    from app.services.template_containers import validate_chrome

    # A presentation template: canonical form stored, problems reported.
    stored, problems = validate_chrome({"guidance": "x", "chrome": BRAND_CHROME}, kind="presentation")
    assert problems == [] and stored["chrome"]["variables"][0]["key"] == "classification" and stored["guidance"] == "x"
    stored, problems = validate_chrome({"chrome": {"footer": "{{nope}}"}}, kind="presentation")
    assert problems and stored == {"chrome": {"footer": "{{nope}}"}}  # nothing rewritten on a refusal
    # An empty declaration is dropped rather than stored as noise.
    assert validate_chrome({"chrome": {}, "guidance": "x"}, kind="presentation") == ({"guidance": "x"}, [])
    # Any other kind has no slides to draw a footer on: the key is dropped silently.
    assert validate_chrome({"chrome": BRAND_CHROME, "brand": 1}, kind="asset-library") == ({"brand": 1}, [])
    # No chrome, or not a dict: untouched.
    assert validate_chrome({"guidance": "x"}, kind="presentation") == ({"guidance": "x"}, [])
    assert validate_chrome(None, kind="presentation") == (None, [])


def test_the_authoring_tool_and_the_rest_route_and_the_generator_all_call_validate_chrome():
    """One gate, three doors: the REST create / update / import routes,
    the agent's ``template_authoring`` tool and the AI template generator all
    validate the declaration through ``validate_chrome`` - never by hand."""
    backend = Path(__file__).resolve().parents[1] / "app"
    rest = (backend / "api" / "v1" / "template_containers.py").read_text(encoding="utf-8")
    assert rest.count("validate_chrome(") >= 3  # import, create, update
    authoring = (backend / "services" / "builtin_tools" / "template_authoring.py").read_text(encoding="utf-8")
    assert authoring.count("validate_chrome(") >= 3  # create, update, validate_presentation
    generation = (backend / "services" / "template_generation.py").read_text(encoding="utf-8")
    assert "validate_chrome(" in generation and "CHROME_KEY" in generation


# ── Chrome.fields on the deck ────────────────────────────────────────────────


def test_chrome_fields_are_cleaned_like_every_other_spec_value():
    spec = validate_spec(
        {
            **_deck(),
            "chrome": {
                "event_label": "FIC 2026",
                "fields": {" Classification ": "  Confidential ", "customer": "", "region": None, "team": "EMEA"},
            },
        }
    )
    # Keys lowercased, values trimmed, blanks and nulls dropped.
    assert spec["chrome"]["fields"] == {"classification": "Confidential", "team": "EMEA"}
    # No usable value at all: the key is absent, not an empty object.
    spec = validate_spec({**_deck(), "chrome": {"fields": {"customer": ""}}})
    assert "fields" not in spec["chrome"]


@pytest.mark.parametrize(
    ("fields", "fragment"),
    [
        ("Confidential", "must be an object"),
        ({"x-y": "v"}, "not a variable key"),
        ({"classification": 12}, "must be text"),
        ({"classification": "x" * (MAX_FIELD_VALUE_CHARS + 1)}, f"maximum is {MAX_FIELD_VALUE_CHARS}"),
        ({f"k{i}": "v" for i in range(MAX_DECK_FIELDS + 1)}, f"maximum is {MAX_DECK_FIELDS}"),
    ],
)
def test_malformed_chrome_fields_are_refused_with_the_field_named(fields, fragment):
    with pytest.raises(PresentationSpecError) as excinfo:
        validate_spec({**_deck(), "chrome": {"fields": fields}})
    assert "chrome" in str(excinfo.value) and fragment in str(excinfo.value)


def test_a_field_value_is_bounded_like_a_label():
    """A footer variable renders in the chrome bar like a label does on a
    slide; the two ceilings move together or one of them lies."""
    assert MAX_FIELD_VALUE_CHARS == MAX_LABEL_CHARS


# ── update_deck merges chrome.fields key by key ──────────────────────────────


def test_update_deck_merges_chrome_fields_key_by_key():
    spec = validate_spec(
        {**_deck(), "chrome": {"event_label": "FIC 2026", "fields": {"classification": "Internal", "customer": "ACME"}}}
    )
    out = apply_operations(
        spec, [{"op": "update_deck", "fields": {"chrome": {"fields": {"classification": "Confidential"}}}}]
    )
    patched = validate_spec(out.spec)
    # One variable changed, the other kept, the rest of the chrome kept.
    assert patched["chrome"]["fields"] == {"classification": "Confidential", "customer": "ACME"}
    assert patched["chrome"]["event_label"] == "FIC 2026"
    assert out.deck_fields == ["chrome"] and out.changed == []
    # null (or "") clears one key; clearing the last one drops the object.
    out = apply_operations(patched, [{"op": "update_deck", "fields": {"chrome": {"fields": {"customer": None}}}}])
    assert validate_spec(out.spec)["chrome"]["fields"] == {"classification": "Confidential"}
    out = apply_operations(out.spec, [{"op": "update_deck", "fields": {"chrome": {"fields": {"classification": ""}}}}])
    assert "fields" not in validate_spec(out.spec)["chrome"]
    # chrome: {fields: null} removes them all at once, like any chrome key.
    out = apply_operations(patched, [{"op": "update_deck", "fields": {"chrome": {"fields": None}}}])
    assert "fields" not in validate_spec(out.spec)["chrome"]
    # A malformed key, or a chrome that is not an object, still fails the
    # re-validation every patched spec goes through, with the field named.
    with pytest.raises(PresentationSpecError, match="chrome"):
        validate_spec(apply_operations(patched, [{"op": "update_deck", "fields": {"chrome": {"fields": {"bad key": "v"}}}}]).spec)
    with pytest.raises(PresentationSpecError, match="chrome"):
        validate_spec(apply_operations(patched, [{"op": "update_deck", "fields": {"chrome": "Confidential"}}]).spec)
    with pytest.raises(PresentationPatchError, match="fields"):
        apply_operations(patched, [{"op": "update_deck", "fields": "chrome"}])


def test_update_deck_reads_field_keys_through_the_validators_own_normaliser():
    """The merge used to pop / set the key AS SENT, so
    ``{"Classification": null}`` cleared nothing (the stored key is
    ``classification``) and ``{"Classification": "x"}`` left two spellings
    for the validator to collapse. Both maps now go through
    ``canonical_field_key`` - the function the validator itself runs."""
    spec = validate_spec({**_deck(), "chrome": {"fields": {"classification": "Internal", "customer": "ACME"}}})
    # A differently spelled key clears the stored one...
    out = apply_operations(spec, [{"op": "update_deck", "fields": {"chrome": {"fields": {"Classification": None}}}}])
    assert out.spec["chrome"]["fields"] == {"customer": "ACME"}
    # ...a blank clears it too (the validator would have dropped the value)...
    out = apply_operations(spec, [{"op": "update_deck", "fields": {"chrome": {"fields": {" CLASSIFICATION ": "   "}}}}])
    assert out.spec["chrome"]["fields"] == {"customer": "ACME"}
    # ...and a value overwrites it under ONE spelling, before any validation.
    out = apply_operations(
        spec, [{"op": "update_deck", "fields": {"chrome": {"fields": {"Classification": "Confidential"}}}}]
    )
    assert out.spec["chrome"]["fields"] == {"classification": "Confidential", "customer": "ACME"}
    assert validate_spec(out.spec)["chrome"]["fields"] == {"classification": "Confidential", "customer": "ACME"}
    # Clearing the last key under another spelling drops the object.
    out = apply_operations(
        spec, [{"op": "update_deck", "fields": {"chrome": {"fields": {"CLASSIFICATION": "", "Customer": None}}}}]
    )
    assert "fields" not in out.spec["chrome"]
    # One normaliser, three readers: the validator, the merge and the reader
    # call it by name - no second spelling of ``.strip().lower()`` anywhere.
    assert pc.canonical_field_key(" Classification ") == "classification"
    assert pc.merge_deck_fields({"Classification": "a"}, {"classification": None}) is None
    assert pc.merge_deck_fields(None, {"Team": "EMEA", "Region": ""}) == {"team": "EMEA"}
    assert pc.deck_fields({"chrome": {"fields": {"Classification": " x "}}}) == {"classification": "x"}
    validator_source = inspect.getsource(presentation_spec.Chrome._clean_fields)
    assert "canonical_field_key(" in validator_source and ".strip().lower()" not in validator_source
    merge_source = inspect.getsource(presentation_patch._op_update_deck)
    assert "merge_deck_fields(" in merge_source and ".lower()" not in merge_source


def test_the_subtitle_placeholder_reads_the_opening_subtitle_as_plain_text():
    """The opening slide's subtitle is inline MARKUP drawn by the slide; the
    footer draws text, so its markers are stripped (the way the frontend's
    ``markupToPlainText`` strips them) and the line is folded - a marker must
    never print literally in the footer, and a footer never wraps."""
    spec = {
        "title": "Roadmap",
        "slides": [{"layout": "title", "title": "Opening", "subtitle": "**Partner** [[accent:overview]]\n  H2 2026 "}],
    }
    assert pc.opening_subtitle(spec) == "Partner overview H2 2026"
    header = _chrome(header="{{title}}: {{subtitle}}")
    assert render_chrome(spec, header).header == "Roadmap: Partner overview H2 2026"
    # An unknown tone keeps its text and drops the wrapper, like the renderer.
    spec["slides"][0]["subtitle"] = "[[nope:kept]] `code`"
    assert pc.opening_subtitle(spec) == "kept code"
    # Only the OPENING slide's subtitle is read; a later title slide is not.
    spec["slides"] = [{"layout": "content", "title": "Body", "subtitle": "**x**"}, *spec["slides"]]
    assert pc.opening_subtitle(spec) == "kept code"


def _opening(subtitle: str) -> dict:
    return {"title": "Roadmap", "slides": [{"layout": "title", "title": "Opening", "subtitle": subtitle}]}


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("Q3 2026", "**Q3 2026**"),
        ("Q3 2026", "[[accent:Q3 2026]]"),
        ("Q3 2026", "_Q3_ `2026`"),
        ("Q3 2026", "Q3\n  2026 "),
    ],
)
def test_the_subtitle_redraw_compares_the_value_the_chrome_draws(before, after):
    """The coupling behind the ``{{subtitle}}`` redraw decision.

    ``subtitle_changed`` in ``builtin_tools/presentation.py`` compares
    ``opening_subtitle`` - the PLAIN-FOLDED value - before and after an edit,
    so a markup-only edit is not a redraw. That is right only because the
    chrome draws plain text: here both halves are held together. If the chrome
    starts rendering the subtitle's markup, the rendered footers below differ
    while ``opening_subtitle`` still compares equal - and this test fails,
    which is the signal to move the redraw comparison to the raw subtitle."""
    coupling = (
        "subtitle_changed in builtin_tools/presentation.py compares plain-folded "
        "values (opening_subtitle); if the chrome renders markup, compare the raw "
        "subtitle there instead"
    )
    header = _chrome(header="{{title}}: {{subtitle}}")
    a, b = _opening(before), _opening(after)
    assert pc.opening_subtitle(a) == pc.opening_subtitle(b), coupling
    assert render_chrome(a, header) == render_chrome(b, header), coupling
    # A change to the TEXT is a change on both sides.
    changed = _opening("Q4 2026")
    assert pc.opening_subtitle(a) != pc.opening_subtitle(changed)
    assert render_chrome(a, header) != render_chrome(changed, header)


def test_the_deck_chrome_draws_the_footer_and_header_as_plain_text():
    """The frontend half of the same coupling: ``DeckChrome`` draws the
    strings ``chromeText()`` hands it (the subtitle already folded by
    ``markupToPlainText``) and never runs them through the inline-markup
    renderer. A markup-rendering chrome would silently under-render the
    layout check, because the redraw decision compares plain text."""
    chrome_tsx = (PRESENTATION_DIR / "chrome.tsx").read_text(encoding="utf-8")
    mirror = (PRESENTATION_DIR / "chrome-pattern.ts").read_text(encoding="utf-8")
    coupling = (
        "the chrome must draw plain text while subtitle_changed in "
        "builtin_tools/presentation.py compares plain-folded values"
    )
    assert "InlineMarkup" not in chrome_tsx, coupling
    assert "dangerouslySetInnerHTML" not in chrome_tsx, coupling
    assert "markupToPlainText(subtitle)" in mirror, coupling


# ── The tool reads the footer back and scopes the layout check ───────────────


def test_footer_status_line_says_what_the_footer_reads_and_how_to_change_it():
    spec = {"title": "Roadmap", "slides": [], "chrome": {"fields": {"classification": "Confidential", "stray": "x"}}}
    line = footer_status_line(spec, _chrome())
    assert line.startswith('Footer: reads "Roadmap - Confidential" - template pattern "{{event}} - {{title}} - {{classification}}".')
    assert '{{event}}=empty, {{title}}="Roadmap", {{classification}}="Confidential"' in line
    assert "chrome.fields.classification (Classification)" in line and "never by editing slides" in line
    # A value the template never declared is named, so the agent stops sending it.
    assert "Ignored chrome.fields (not declared by the template): stray" in line
    # A hidden footer is said to be hidden - nothing to read.
    assert footer_status_line({**spec, "chrome": {"show_footer": False}}, _chrome()) == "Footer: hidden (chrome.show_footer is false)."
    # A free deck keeps today's footer: event label + footer text.
    free = {"title": "Roadmap", "slides": [], "chrome": {"event_label": "FIC 2026", "footer_text": "Filigran"}}
    assert footer_status_line(free, None).startswith('Footer: reads "FIC 2026 \u00b7 Filigran" - from chrome.event_label and chrome.footer_text')
    assert footer_status_line({"title": "Roadmap", "slides": []}, None).startswith("Footer: empty - set chrome.event_label")
    # A header pattern is read back next to the footer.
    both = footer_status_line(spec, _chrome(header="{{title}}"))
    assert 'Header: reads "Roadmap"' in both


def test_describe_template_chrome_names_what_fills_each_placeholder():
    lines = describe_template_chrome(_chrome(header="{{subtitle}}"))
    assert lines[0] == '  - footer on every slide: "{{event}} - {{title}} - {{classification}}"'
    assert lines[1] == '  - header on every slide: "{{subtitle}}"'
    assert lines[2].startswith("  - filled once per deck from its settings, never from slide text: ")
    assert "{{title}} = the deck title" in lines[2] and "{{event}} = chrome.event_label" in lines[2]
    assert "{{subtitle}} = the opening slide's subtitle" in lines[2]
    assert "{{classification}} = chrome.fields.classification (Classification, empty unless set)" in lines[2]
    defaulted = _chrome(variables=[{"key": "classification", "label": "Classification", "default": "Internal"}])
    assert '(Classification, default "Internal")' in describe_template_chrome(defaulted)[-1]
    assert describe_template_chrome(None) == []
    assert describe_template_chrome(TemplateChrome(footer=None, header=None)) == []


def test_template_chrome_redraws_only_when_the_pattern_reads_the_changed_field():
    assert template_chrome_redraws(_chrome(), ["title"]) is True
    assert template_chrome_redraws(_chrome(footer="{{event}} - {{classification}}"), ["title"]) is False
    assert template_chrome_redraws(_chrome(), ["transition"]) is False
    assert template_chrome_redraws(None, ["title"]) is False
    # The opening slide's subtitle is drawn only through a {{subtitle}} pattern.
    assert template_chrome_redraws(_chrome(header="{{subtitle}}"), [], subtitle_changed=True) is True
    assert template_chrome_redraws(_chrome(), [], subtitle_changed=True) is False
    # The global set is untouched: the title is a per-deck decision.
    assert "title" not in LAYOUT_DECK_FIELDS


@pytest.mark.parametrize(
    ("operation", "changes_subtitle"),
    [
        # Deleting the opening slide makes the next title slide the opening one.
        ({"op": "delete_slide", "slide": "1"}, True),
        # Moving another title slide ahead of it does the same - and writes nothing.
        ({"op": "move_slide", "slide": "3", "to": 1}, True),
        # Rewriting the opening slide without touching its subtitle changes
        # nothing the header shows, so nothing is rendered for it.
        ({"op": "update_slide", "slide": "1", "fields": {"title": "Quarterly review, board edition"}}, False),
        # A markup-only change to the opening subtitle is NOT a redraw: the
        # footer draws plain text, so "Q3 2026" and "**Q3 2026**" read the
        # same on every slide.
        pytest.param(
            {"op": "update_slide", "slide": "1", "fields": {"subtitle": "**Q3 2026**"}},
            False,
            id="markup-only subtitle edit is not a redraw - the footer draws plain text (bold)",
        ),
        pytest.param(
            {"op": "update_slide", "slide": "1", "fields": {"subtitle": "[[accent:Q3 2026]]"}},
            False,
            id="markup-only subtitle edit is not a redraw - the footer draws plain text (tone)",
        ),
    ],
)
def test_an_edit_that_changes_which_subtitle_opens_the_deck_redraws_a_subtitle_header(
    monkeypatch, operation, changes_subtitle
):
    """``{{subtitle}}`` is the FIRST title slide's subtitle, so the decision
    compares that value before and after the edit - not which slides were
    written. A delete or a move writes no slide, yet it can change what every
    header says; a rewrite of the opening slide can leave it alone."""
    from app.services import presentation_layout as layout
    from app.services.presentation_layout import LayoutReport

    user_id = uuid.uuid4()
    template_id = uuid.uuid4()
    renders: list[int] = []

    async def _fake_check(*_a, **_k):
        renders.append(1)
        return LayoutReport(slide_count=6, findings=())

    monkeypatch.setattr(layout, "check_presentation_layout", _fake_check)
    _defs, executor = _build_tools(str(user_id))

    def _deck_with_a_second_title_slide():
        # Opens on "Q3 2026"; a second title slide ("Board edition") sits third.
        spec = _deck()
        spec["slides"].insert(2, {"layout": "title", "title": "Part two", "subtitle": "Board edition"})
        return _existing_row(user_id, spec, template_container_id=template_id)

    header = {"footer": None, "header": "{{subtitle}}", "variables": []}
    existing = _deck_with_a_second_title_slide()
    row = _template_row(template_id, name="Subtitle Header", extra_metadata={"chrome": header})
    _patch_session(monkeypatch, _FakeSession(results=[*_deck_lookup(existing), *_save_probes(), *_template_lookup(row)]))
    result = _run_tool(executor, "edit_presentation", {"presentation_id": str(existing.id), "operations": [operation]})
    assert not result.is_error, result.content
    assert existing.revision == 2
    if changes_subtitle:
        # The header now reads the other title slide's subtitle on every
        # slide: the WHOLE deck is measured and the account says what it reads.
        assert renders == [1]
        assert "Layout check: every one of the 6 slides fits its canvas." in result.content, result.content
        assert 'Header: reads "Board edition"' in result.content, result.content
    else:
        # The rewritten slide is measured alone, as for any surgical edit.
        assert renders == [1]
        assert "Layout check: the slide this edit wrote fits its canvas." in result.content, result.content
        assert "Header:" not in result.content

    # The same edit on the brand template, whose pattern never reads the
    # subtitle: a delete or a move writes no slide, so nothing is rendered;
    # the rewrite is measured alone. And the footer is NOT read back - the
    # edit could not have changed what it says, so the read-back gate says
    # the same as the redraw gate (one template_chrome_redraws() reading for
    # both): the agent is never sent to look at a footer the edit did not
    # touch.
    renders.clear()
    existing = _deck_with_a_second_title_slide()
    row = _template_row(template_id, name="Filigran Brand Deck", extra_metadata={"chrome": BRAND_CHROME})
    _patch_session(monkeypatch, _FakeSession(results=[*_deck_lookup(existing), *_save_probes(), *_template_lookup(row)]))
    result = _run_tool(executor, "edit_presentation", {"presentation_id": str(existing.id), "operations": [operation]})
    assert not result.is_error, result.content
    assert "Footer:" not in result.content, result.content
    assert "Header:" not in result.content, result.content
    if operation["op"] == "update_slide":
        assert renders == [1]
        assert "Layout check: the slide this edit wrote fits its canvas." in result.content, result.content
    else:
        assert renders == []


def test_edit_presentation_sets_one_footer_variable_and_reads_the_footer_back(monkeypatch):
    """"Mark the deck Confidential" is ONE ``chrome.fields`` value on a deck
    dressed by the brand template: no slide is rewritten, the account says
    what the footer now reads, and - chrome being drawn on every slide - the
    layout check covers the whole deck."""
    from app.services import presentation_layout as layout
    from app.services.presentation_layout import LayoutReport

    user_id = uuid.uuid4()
    template_id = uuid.uuid4()
    spec = _deck()
    spec["chrome"] = {"event_label": "FIC 2026"}
    existing = _existing_row(user_id, spec, template_container_id=template_id)
    before = copy.deepcopy(existing.spec)
    renders: list[int] = []

    async def _fake_check(*_a, **_k):
        renders.append(1)
        return LayoutReport(slide_count=5, findings=())

    monkeypatch.setattr(layout, "check_presentation_layout", _fake_check)
    # The template is read AFTER the save (an untouched template the edit did
    # not need): group memberships, then the row as this user may see it.
    row = _template_row(template_id, name="Filigran Brand Deck", extra_metadata={"chrome": BRAND_CHROME})
    session = _FakeSession(results=[*_deck_lookup(existing), *_save_probes(), *_template_lookup(row)])
    _patch_session(monkeypatch, session)

    _defs, executor = _build_tools(str(user_id))
    result = _run_tool(
        executor,
        "edit_presentation",
        {
            "presentation_id": str(existing.id),
            "operations": [{"op": "update_deck", "fields": {"chrome": {"fields": {"classification": "Confidential"}}}}],
        },
    )
    assert not result.is_error, result.content
    assert existing.revision == 2
    assert existing.spec["chrome"]["fields"] == {"classification": "Confidential"}
    assert existing.spec["chrome"]["event_label"] == "FIC 2026"
    for i, slide in enumerate(before["slides"]):
        assert slide == existing.spec["slides"][i], i
    assert "Deck fields changed: chrome" in result.content
    assert 'Footer: reads "FIC 2026 - Quarterly review - Confidential"' in result.content
    assert "Template: Filigran Brand Deck" in result.content
    assert renders == [1]


def test_a_rename_redraws_the_deck_only_when_the_template_footer_shows_the_title(monkeypatch):
    """The title is not in ``LAYOUT_DECK_FIELDS`` - a rename on a free
    deck renders nothing. On a deck whose template footer reads ``{{title}}``
    the title IS on every slide, so the same rename renders the whole deck and
    the account reads the new footer back. The read-back follows the SAME
    per-deck reading as the redraw: a footer that never shows the title did
    not change, so it is not read back either - not on a template with a
    fixed pattern, not on a free deck's v1 footer."""
    from app.services import presentation_layout as layout
    from app.services.presentation_layout import LayoutReport

    user_id = uuid.uuid4()
    template_id = uuid.uuid4()
    renders: list[int] = []

    async def _fake_check(*_a, **_k):
        renders.append(1)
        return LayoutReport(slide_count=5, findings=())

    monkeypatch.setattr(layout, "check_presentation_layout", _fake_check)
    _defs, executor = _build_tools(str(user_id))
    rename = {"op": "update_deck", "fields": {"title": "Quarterly review, board edition"}}

    # The brand footer reads the title: the rename is drawn on every slide.
    existing = _existing_row(user_id, template_container_id=template_id)
    row = _template_row(template_id, name="Filigran Brand Deck", extra_metadata={"chrome": BRAND_CHROME})
    _patch_session(monkeypatch, _FakeSession(results=[*_deck_lookup(existing), *_save_probes(), *_template_lookup(row)]))
    result = _run_tool(executor, "edit_presentation", {"presentation_id": str(existing.id), "operations": [rename]})
    assert not result.is_error, result.content
    assert renders == [1]
    assert 'Footer: reads "Quarterly review, board edition"' in result.content
    assert "{{classification}}=empty" in result.content

    # A template whose footer never shows the title: the rename draws nothing,
    # and a footer the rename could not have changed is not read back.
    renders.clear()
    existing = _existing_row(user_id, template_container_id=template_id)
    fixed = {"footer": "{{event}} - {{classification}}", "variables": BRAND_CHROME["variables"]}
    row = _template_row(template_id, name="Fixed Footer", extra_metadata={"chrome": fixed})
    _patch_session(monkeypatch, _FakeSession(results=[*_deck_lookup(existing), *_save_probes(), *_template_lookup(row)]))
    result = _run_tool(executor, "edit_presentation", {"presentation_id": str(existing.id), "operations": [rename]})
    assert not result.is_error, result.content
    assert renders == []
    assert "Deck fields changed: title" in result.content
    assert "Footer:" not in result.content, result.content

    # A free deck: its v1 footer (event label + footer text) never shows the
    # title, so the rename neither renders nor reads the footer back.
    renders.clear()
    spec = _deck()
    spec["chrome"] = {"event_label": "FIC 2026", "footer_text": "Filigran"}
    existing = _existing_row(user_id, spec)
    _patch_session(monkeypatch, _FakeSession(results=[*_deck_lookup(existing), *_save_probes()]))
    result = _run_tool(executor, "edit_presentation", {"presentation_id": str(existing.id), "operations": [rename]})
    assert not result.is_error, result.content
    assert existing.revision == 2
    assert renders == []
    assert "Footer:" not in result.content, result.content


def test_get_presentation_reads_the_footer_back_with_the_template(monkeypatch):
    user_id = uuid.uuid4()
    template_id = uuid.uuid4()
    spec = _deck()
    spec["chrome"] = {"event_label": "FIC 2026", "fields": {"classification": "Confidential"}}
    existing = _existing_row(user_id, spec, template_container_id=template_id)
    row = _template_row(template_id, name="Filigran Brand Deck", extra_metadata={"chrome": BRAND_CHROME})
    # The deck lookup, the open comments (none), then the template facts.
    _patch_session(monkeypatch, _FakeSession(results=[*_deck_lookup(existing), _Result(rows=[]), *_template_lookup(row)]))
    _defs, executor = _build_tools(str(user_id))
    text = _run_tool(executor, "get_presentation", {"presentation_id": str(existing.id)}).content
    assert 'Footer: reads "FIC 2026 - Quarterly review - Confidential"' in text
    assert "chrome.fields.classification (Classification)" in text
    assert "Template: Filigran Brand Deck" in text


def test_the_tool_schema_tells_the_agent_the_footer_is_a_deck_setting():
    from app.services.presentation_patch import OPERATION_CONTRACTS

    defs, _executor = _build_tools(str(uuid.uuid4()))
    generate = next(d for d in defs if d.name == "generate_presentation")
    chrome_schema = generate.input_schema["properties"]["chrome"]["description"]
    assert "fields" in chrome_schema and "ONE deck setting" in chrome_schema and "footer pattern" in chrome_schema
    assert "chrome.fields" in OPERATION_CONTRACTS["update_deck"]


# ── The REST response and the palette carry the pattern ──────────────────────


def test_serialize_presentation_carries_the_template_chrome_live():
    from app.services.presentations import TemplatePalette, serialize_presentation

    user_id = uuid.uuid4()
    existing = _existing_row(user_id)
    payload = serialize_presentation(existing, include_spec=True, template_chrome=_chrome())
    assert payload["template_chrome"] == BRAND_CHROME
    assert serialize_presentation(existing, include_spec=True)["template_chrome"] is None
    # The palette a deck read resolves carries it from the same container load.
    assert TemplatePalette(themes={}).chrome is None
    rest = (Path(__file__).resolve().parents[1] / "app" / "api" / "v1" / "presentations.py").read_text(encoding="utf-8")
    assert "template_chrome=palette.chrome" in rest


def test_the_response_model_lets_the_template_chrome_through():
    """The detail routes declare ``response_model=PresentationResponse``, and
    FastAPI drops every key that model does not declare - so a serializer that
    emits ``template_chrome`` is not enough on its own. The pattern must
    survive the round trip through the model, or the editor panel silently
    falls back to the free footer for every deck (found live: the seeded
    brand deck served no ``template_chrome`` at all)."""
    from app.api.v1.schemas.presentation import PresentationResponse
    from app.services.presentations import serialize_presentation

    user_id = uuid.uuid4()
    existing = _existing_row(user_id)
    payload = serialize_presentation(existing, include_spec=True, template_chrome=_chrome())
    through = PresentationResponse.model_validate(payload).model_dump()
    assert through["template_chrome"] == payload["template_chrome"]
    assert through["template_chrome"]["footer"] == "{{event}} - {{title}} - {{classification}}"
    # A free deck says so explicitly rather than by omission - the frontend
    # keys the panel on ``template_chrome`` being null, never on the key missing.
    free = PresentationResponse.model_validate(serialize_presentation(existing, include_spec=True)).model_dump()
    assert "template_chrome" in free and free["template_chrome"] is None


def test_the_template_chrome_follows_the_readers_visibility_of_the_template():
    """``template_chrome`` rides ``TemplatePalette.chrome`` out of the SAME
    visibility-checked container load as the palette: a template the reader
    can see contributes its declaration; one they cannot see (un-shared since,
    a collaborator's private template) contributes ``None`` and the deck reads
    as a free deck - footer included, exactly like its palette and name. There
    is no second load that could fall behind that gate."""
    import asyncio

    from app.services.presentations import resolve_template_palette, serialize_presentation

    owner = uuid.uuid4()
    template_id = uuid.uuid4()
    row = _template_row(template_id, extra_metadata={"chrome": BRAND_CHROME})
    visible = asyncio.run(
        resolve_template_palette(_FakeSession(results=_template_lookup(row)), template_id, user_id=owner)
    )
    assert visible.chrome == _chrome()
    assert visible.name == "XTM One Brand Deck"
    hidden = asyncio.run(
        resolve_template_palette(_FakeSession(results=_template_lookup(None)), template_id, user_id=owner)
    )
    assert hidden.chrome is None and hidden.name is None
    deck = _existing_row(owner, template_container_id=template_id)
    payload = serialize_presentation(deck, include_spec=True, template_name=hidden.name, template_chrome=hidden.chrome)
    assert payload["template_chrome"] is None and payload["template_name"] is None
    # A template with no declaration is a free deck too - never an empty pattern.
    plain = asyncio.run(
        resolve_template_palette(
            _FakeSession(results=_template_lookup(_template_row(template_id))), template_id, user_id=owner
        )
    )
    assert plain.chrome is None


def test_the_rest_patch_replaces_the_chrome_wholesale_while_update_deck_merges_it():
    """Two write paths, two contracts, both pinned so neither drifts: the
    editor's PATCH sends the WHOLE ``chrome`` object (``fields`` included) and
    the route stores it as sent - a body that omits a key drops the stored
    one, ``null`` resets the chrome - while the agent's ``update_deck`` merges
    key by key, because the agent sets ONE variable at a time and must not
    have to re-send the others. A stale ``expected_revision`` writes nothing
    on either path."""
    import asyncio

    from fastapi import HTTPException

    from app.api.v1 import presentations as api
    from app.api.v1.schemas.presentation import PresentationUpdate
    from tests.test_presentations import _noop_refresh, _rest_user

    owner = uuid.uuid4()
    spec = validate_spec({**_deck(), "chrome": {"event_label": "FIC 2026", "fields": {"classification": "Confidential"}}})
    deck = _existing_row(owner, copy.deepcopy(spec))
    user = _rest_user(owner)

    async def _patch(body, session):
        return await api.update_presentation(str(deck.id), body, request=None, user=user, db=session)

    # Stale revision: 409, the chrome untouched.
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_patch(PresentationUpdate(chrome={"fields": {"customer": "ACME"}}, expected_revision=7), _FakeSession(results=_deck_lookup(deck))))
    assert exc.value.status_code == 409
    assert deck.spec["chrome"]["fields"] == {"classification": "Confidential"}
    assert deck.spec["chrome"]["event_label"] == "FIC 2026"

    # The editor sends the whole object: what it omits is gone.
    session = _FakeSession(results=_deck_lookup(deck))
    session.refresh = _noop_refresh
    asyncio.run(_patch(PresentationUpdate(chrome={"fields": {"Customer": " ACME "}}, expected_revision=1), session))
    assert deck.revision == 2 and session.committed
    assert deck.spec["chrome"].get("fields") == {"customer": "ACME"}
    assert not deck.spec["chrome"].get("event_label")

    # The agent merges: one variable set, the rest kept.
    out = apply_operations(spec, [{"op": "update_deck", "fields": {"chrome": {"fields": {"customer": "ACME"}}}}])
    assert out.spec["chrome"]["event_label"] == "FIC 2026"
    assert out.spec["chrome"]["fields"] == {"classification": "Confidential", "customer": "ACME"}


# ── The frontend renders the same rule ───────────────────────────────────────


def _case_id(case: dict) -> str:
    return case["name"]


@pytest.mark.parametrize("case", SHARED_CASES["render"], ids=_case_id)
def test_the_shared_table_renders_the_same_on_the_backend(case):
    """The same table ``tests_e2e/chrome-pattern.spec.ts`` feeds to
    ``renderChromePattern``: every collapse edge case (all empty, leading /
    inner / trailing separators, mixed separators, adjacent placeholders,
    a repeated placeholder, a literal-only pattern) and the value that is
    inserted verbatim - never expanded a second time."""
    assert render_chrome_pattern(case["pattern"], case["values"]) == case["expected"]


@pytest.mark.parametrize("case", SHARED_CASES["declarations"], ids=_case_id)
def test_the_shared_table_validates_the_same_on_the_backend(case):
    """The same declarations ``validateTemplateChrome`` accepts or refuses:
    accepted ones store the same canonical object, refused ones are refused
    for the same reasons (the frontend's codes mapped onto the backend's
    lines), and neither side reports a problem the other does not."""
    canonical, problems = normalize_template_chrome(case["declaration"])
    assert canonical == case["canonical"]
    assert len(problems) == len(case["problems"]), problems
    for code in case["problems"]:
        assert any(re.search(PROBLEM_LINES[code], line) for line in problems), (code, problems)


@pytest.mark.parametrize("case", SHARED_CASES["decks"], ids=_case_id)
def test_the_shared_table_fills_a_deck_the_same_on_the_backend(case):
    """A deck plus a stored declaration read leniently, on both sides: the
    settings fill the pattern, a field key is read in its one spelling, the
    subtitle's markup is stripped, an undeclared placeholder of an older row
    renders empty (never literally), a pattern-less declaration is none."""
    chrome = template_chrome({"chrome": case["declaration"]})
    if case.get("reads") is False:
        assert chrome is None
        assert render_chrome(case["spec"], chrome) == pc.RenderedChrome(footer="", header="")
        return
    assert chrome is not None
    rendered = render_chrome(case["spec"], chrome)
    assert (rendered.footer, rendered.header) == (case["footer"], case["header"])


def test_the_shared_table_covers_every_problem_both_validators_can_report():
    """Every code the frontend validator can emit is in the table at least
    once, and the table names no code the frontend does not have - so a new
    rule added on one side fails here until the other side and the table know it."""
    mirror = (PRESENTATION_DIR / "chrome-pattern.ts").read_text(encoding="utf-8")
    union = re.search(r"export interface ChromeProblem \{\s*code:(.*?);", mirror, re.S)
    assert union is not None
    frontend_codes = set(re.findall(r'"([a-z_]+)"', union.group(1)))
    assert frontend_codes == set(PROBLEM_LINES)
    reported = {code for case in SHARED_CASES["declarations"] for code in case["problems"]}
    assert reported == frontend_codes
    # The spec that feeds the frontend half exists and reads the same file.
    spec = (FRONTEND_ROOT / "tests_e2e" / "chrome-pattern.spec.ts").read_text(encoding="utf-8")
    assert "chrome-pattern-cases.json" in spec and "validateTemplateChrome" in spec
    assert "renderChromePattern" in spec and "chromeText" in spec


def test_the_frontend_mirror_renders_the_same_rule_from_the_same_pattern():
    """``chrome-pattern.ts`` is the TypeScript half of :mod:`presentation_chrome`:
    the same placeholder syntax, the same built-ins, the same collapse rule.
    Pinned by grep - a divergence would make the tool result and the screen
    disagree about what the footer reads."""
    mirror = (PRESENTATION_DIR / "chrome-pattern.ts").read_text(encoding="utf-8")
    # The placeholder syntax, verbatim on both sides.
    assert pc.PLACEHOLDER_RE.pattern == r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}"
    assert "/\\{\\{\\s*([A-Za-z0-9_]+)\\s*\\}\\}/g" in mirror, "placeholder regex differs"
    # The built-ins, in the same order.
    builtins = ", ".join(f'"{key}"' for key in BUILTIN_PLACEHOLDERS)
    assert f"BUILTIN_PLACEHOLDERS = [{builtins}]" in mirror
    # The key grammar and every bound, value for value.
    assert f"VARIABLE_KEY_RE = /{pc.VARIABLE_KEY_RE.pattern}/;" in mirror
    for name in (
        "MAX_CHROME_VARIABLES",
        "MAX_PATTERN_CHARS",
        "MAX_VARIABLE_LABEL_CHARS",
        "MAX_VARIABLE_HINT_CHARS",
        "MAX_FIELD_VALUE_CHARS",
    ):
        assert f"export const {name} = {getattr(pc, name)};" in mirror, name
    # The subtitle is read as plain text on both sides, through each side's
    # inline-markup tokenizer (the two mirror each other already).
    assert "markupToPlainText(subtitle)" in mirror
    assert "markup_to_plain_text_lenient(subtitle)" in inspect.getsource(pc.opening_subtitle)
    # One spelling of a field key on both sides.
    assert "export function canonicalFieldKey" in mirror and "canonicalFieldKey(key)" in mirror
    # The four entry points the components and the template preview call.
    for marker in (
        "export function renderChromePattern",
        "export function chromeValues",
        "export function chromeText",
        "export function templateChromeOf",
    ):
        assert marker in mirror, marker
    # The collapse rule is the one the module docstring states: a separator
    # needs a value on its left AND one somewhere on its right.
    assert re.search(r"before\b.*\bafter|left\b.*\bright", mirror, re.S), "collapse rule not documented"
    # The component draws what the mirror renders and reads no title itself.
    chrome_tsx = (PRESENTATION_DIR / "chrome.tsx").read_text(encoding="utf-8")
    assert "text.footer" in chrome_tsx and "text.header" in chrome_tsx
    assert 'className="deck-chrome-header"' in chrome_tsx
    css = (PRESENTATION_DIR / "presentation.css").read_text(encoding="utf-8")
    assert ".deck-chrome-header" in css
