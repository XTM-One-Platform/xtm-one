"""A template theme value never makes a viewer's browser load anything.

A template's palette lands in the CSS custom properties the deck and the report
paint with (``background: var(--deck-bg)`` and friends), so a value that loads an
image loads it in the browser of EVERY person who opens a deck wearing the
template - their address and the time they looked leak to whoever hosts the
image. A browser only loads a resource through a CSS function, never through a
keyword or a quoted string, so the guard is an allow-list of the functions that
cannot load anything (``SAFE_THEME_FUNCTIONS``), read through the escapes and
the case a browser reads them through. These tests pin both halves: every
fetching form is refused on write and dropped on read, every legitimate colour,
gradient and font stack survives.

The cases live in ``frontend/tests_e2e/fixtures/theme-value-cases.json``, the
ONE table the frontend mirror (``theme-value.ts``, what the template preview
filters through) is checked against too.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.presentations import (
    MAX_THEME_VALUE_CHARS,
    THEME_CONFIG_KEYS,
    THEME_FILE_ID_KEYS,
    is_safe_theme_value,
    sanitize_theme_config,
)
from app.services.reports import REPORT_THEME_CONFIG_KEYS

FRONTEND_ROOT = Path(__file__).resolve().parents[2] / "frontend"
SHARED_CASES = json.loads(
    (FRONTEND_ROOT / "tests_e2e" / "fixtures" / "theme-value-cases.json").read_text(encoding="utf-8")
)

#: Every key whose value is a CSS token, deck and report alike (the file-id keys
#: hold asset UUIDs and have a rule of their own).
CSS_KEYS: tuple[str, ...] = tuple(
    dict.fromkeys(key for key in (*THEME_CONFIG_KEYS, *REPORT_THEME_CONFIG_KEYS) if key not in THEME_FILE_ID_KEYS)
)

#: Values that load (or could load) a resource, in every spelling a browser
#: reads as the same function: quotes, vendor prefixes, case, CSS escapes, a
#: space before the parenthesis, a nested call, a second background layer, a
#: legacy ``-webkit-gradient()`` argument outside it, an unknown function.
FETCHING_VALUES: list[str] = SHARED_CASES["fetching"]

#: What a template legitimately carries: every colour syntax, keywords, var(),
#: the math functions, every gradient form (legacy -webkit-gradient included)
#: and quoted font families.
ACCEPTED_VALUES: list[str] = SHARED_CASES["accepted"]

#: The refusals the guard always had, kept: what would leave the declaration,
#: open a comment or an at-rule, escape a character, hide in a control or
#: non-ASCII character, leave a parenthesis open, or run too long.
STRUCTURAL_REFUSALS: list[str] = [*SHARED_CASES["structural"], "x" * (MAX_THEME_VALUE_CHARS + 1)]

#: What both sides strip from the ends of a value before judging and serving it.
TRIM_CHARACTERS: list[str] = SHARED_CASES["trim"]


@pytest.mark.parametrize("value", FETCHING_VALUES)
def test_a_fetching_css_function_is_refused_for_every_theme_key(value):
    """The leak: accepted for every key before, refused for every key now."""
    accepted = [key for key in CSS_KEYS if is_safe_theme_value(key, value)]
    assert accepted == [], f"{value!r} accepted for {accepted}"
    assert sanitize_theme_config({"background": value, "accent": "#42caff"}) == {"accent": "#42caff"}
    assert sanitize_theme_config({"card": value}, keys=REPORT_THEME_CONFIG_KEYS) == {}


@pytest.mark.parametrize("value", ACCEPTED_VALUES)
def test_a_legitimate_theme_value_is_kept_as_written(value):
    refused = [key for key in CSS_KEYS if not is_safe_theme_value(key, value)]
    assert refused == [], f"{value!r} refused for {refused}"
    # Served exactly as written (trimmed), never re-spelled.
    assert sanitize_theme_config({"background": f"  {value} "}) == {"background": value}


@pytest.mark.parametrize("value", STRUCTURAL_REFUSALS)
def test_the_structural_refusals_are_kept(value):
    assert not any(is_safe_theme_value(key, value) for key in CSS_KEYS), repr(value)


def test_a_value_at_the_length_limit_is_kept():
    assert is_safe_theme_value("background", "x" * MAX_THEME_VALUE_CHARS)
    # The limit is read on the trimmed value, like every other rule.
    assert is_safe_theme_value("background", f"  {'x' * MAX_THEME_VALUE_CHARS}\n")


def test_both_sides_trim_the_same_characters():
    """Python's ``str.strip()`` and JavaScript's ``trim()`` strip different sets
    (U+001C-U+001F and U+0085 only in Python, U+FEFF only in JavaScript), so
    each side trims the union, and the preview serves what a deck serves."""
    from app.services.presentations import THEME_VALUE_TRIM_CHARS

    python_whitespace = {chr(code) for code in range(0x110000) if chr(code).isspace()}
    assert set(TRIM_CHARACTERS) == python_whitespace | {"\ufeff"}
    assert set(THEME_VALUE_TRIM_CHARS) == set(TRIM_CHARACTERS)
    for char in TRIM_CHARACTERS:
        padded = f"{char}#0b1020{char}"
        assert is_safe_theme_value("background", padded), repr(char)
        assert sanitize_theme_config({"background": padded}) == {"background": "#0b1020"}, repr(char)
        if char != " ":
            # Inside a value only the ASCII space is allowed.
            assert not is_safe_theme_value("background", f"red{char}blue"), repr(char)


@pytest.mark.parametrize("key", THEME_FILE_ID_KEYS)
def test_a_file_id_key_takes_a_canonical_uuid_and_nothing_else(key):
    """The frontend builds a download path from the value, so it is the
    canonical 8-4-4-4-12 spelling or nothing - not every string
    ``uuid.UUID()`` parses (braces, ``urn:uuid:``, no dashes)."""
    assert is_safe_theme_value(key, str(uuid.uuid4()))
    for value in SHARED_CASES["file_id"]["accepted"]:
        assert is_safe_theme_value(key, value), value
        assert sanitize_theme_config({key: f" {value}\n"}) == {key: value}
    for value in SHARED_CASES["file_id"]["refused"]:
        assert not is_safe_theme_value(key, value), value


def test_the_allow_list_is_the_one_the_module_documents():
    """The functions a value may call compute a colour, a gradient of colours or
    a length - and none of those can load anything. A new entry is a decision
    about fetching, so it is pinned here rather than slipping in."""
    from app.services.presentations import SAFE_THEME_FUNCTIONS

    gradients = {
        f"{prefix}{repeating}{shape}-gradient"
        for prefix in ("", "-webkit-", "-moz-")
        for repeating in ("", "repeating-")
        for shape in ("linear", "radial", "conic")
    }
    assert set(SAFE_THEME_FUNCTIONS) == {
        "rgb",
        "rgba",
        "hsl",
        "hsla",
        "hwb",
        "lab",
        "lch",
        "oklab",
        "oklch",
        "color",
        "color-mix",
        "light-dark",
        "calc",
        "min",
        "max",
        "clamp",
        "var",
        "-webkit-gradient",
        *gradients,
    }
    for fetching in ("url", "image", "image-set", "-webkit-image-set", "cross-fade", "element", "paint", "src"):
        assert fetching not in SAFE_THEME_FUNCTIONS


def test_the_frontend_mirror_calls_the_same_functions():
    """The template preview builds its palette on the client from the template as
    stored or as edited - data no backend read has filtered - so its mirror of
    the guard must allow exactly what the backend allows, no more."""
    from app.services.presentations import MAX_THEME_VALUE_CHARS, SAFE_THEME_FUNCTIONS

    source = (FRONTEND_ROOT / "src" / "components" / "presentation" / "theme-value.ts").read_text(encoding="utf-8")
    match = re.search(r"export const SAFE_THEME_FUNCTIONS[^=]*=\s*new Set\(\[(.*?)\]\)", source, re.DOTALL)
    assert match, "theme-value.ts must export SAFE_THEME_FUNCTIONS as a Set literal"
    assert set(re.findall(r'"([a-z-]+)"', match.group(1))) == set(SAFE_THEME_FUNCTIONS)
    assert f"export const MAX_THEME_VALUE_CHARS = {MAX_THEME_VALUE_CHARS};" in source
    files = re.search(r"export const THEME_FILE_ID_KEYS[^=]*=\s*\[(.*?)\]", source, re.DOTALL)
    assert files and tuple(re.findall(r'"([a-z_]+)"', files.group(1))) == THEME_FILE_ID_KEYS
    # The mirror's unit test reads the same table as this module.
    unit = (FRONTEND_ROOT / "src" / "components" / "presentation" / "theme-value.test.ts").read_text(encoding="utf-8")
    assert "theme-value-cases.json" in unit and "isSafeThemeValue(" in unit
    # ... and it is what the preview filters every palette through, serving the
    # value trimmed the way the backend serves it.
    preview = (FRONTEND_ROOT / "src" / "components" / "presentation" / "previewSpec.ts").read_text(encoding="utf-8")
    assert 'import { isSafeThemeValue, trimThemeValue } from "./theme-value";' in preview
    assert "isSafeThemeValue(key, value)" in preview
    assert "config[key] = trimThemeValue(value);" in preview
    assert "UNSAFE_THEME_VALUE" not in preview, "the deny-list mirror is gone"
    # The sinks re-apply it, so a palette that reaches them by another road
    # (a regressed read path) is still filtered: the deck's inline variables,
    # reveal's slide paper and the report's variables.
    # Each serves the value trimmed with the shared set, as the backend does.
    deck = (FRONTEND_ROOT / "src" / "components" / "presentation" / "theme.ts").read_text(encoding="utf-8")
    assert 'import { isSafeThemeValue, trimThemeValue } from "./theme-value.ts";' in deck
    assert "if (value && isSafeThemeValue(key, value)) style[cssVar] = trimThemeValue(value);" in deck
    assert 'const own = trimThemeValue(resolution.tokens?.background ?? "");' in deck
    assert 'if (!own || !isSafeThemeValue("background", own)) return { color: scheme };' in deck
    report = (FRONTEND_ROOT / "src" / "components" / "report" / "ReportView.tsx").read_text(encoding="utf-8")
    assert "if (value && isSafeThemeValue(key, value)) style[variable] = trimThemeValue(value);" in report


# -- the write boundary --------------------------------------------------------


def test_the_authoring_validator_names_the_key_and_the_function():
    from app.services.template_containers import validate_theme_config, validate_theme_variants

    accepted, problems = validate_theme_config(
        {"accent": "#42caff", "background": 'image-set("//example.com/x.png" 1x)'}
    )
    assert accepted == {"accent": "#42caff"}
    assert len(problems) == 1
    assert problems[0].startswith("theme_config['background'] = ")
    assert "image-set()" in problems[0]
    # The retry can succeed: the refusal says what IS allowed.
    assert "var()" in problems[0] and "gradient" in problems[0]

    # An escaped spelling is named by what a browser would read.
    _accepted, problems = validate_theme_config({"surface": "\\69 mage-set('//example.com/x.png' 1x)"})
    assert "image-set()" in problems[0]

    # A report key is held to the same rule.
    _accepted, problems = validate_theme_config(
        {"card": "cross-fade('//example.com/a.png', '//example.com/b.png', 50%)"}, kind="report"
    )
    assert problems and "theme_config['card']" in problems[0] and "cross-fade()" in problems[0]

    # A variant palette is the base's contract, named by where it lives.
    _metadata, problems = validate_theme_variants(
        {"theme_variants": {"light": {"background": "#f2f2f3", "accent": "image('//example.com/x.png')"}}}
    )
    assert len(problems) == 1
    assert problems[0].startswith("extra_metadata.theme_variants.light['accent']")
    assert "image()" in problems[0]


def test_every_write_path_goes_through_the_validators():
    """REST create / update, the package import (and the duplicate built on it),
    the authoring tool and the generator all hold a palette to the ONE value
    guard, so no write path is a back door around it."""
    import inspect

    from app.api.v1 import template_containers as api
    from app.services import template_generation
    from app.services.builtin_tools import template_authoring

    create = inspect.getsource(api.create_template_container)
    update = inspect.getsource(api.update_template_container)
    for route in (create, update):
        assert "unsafe_theme_value_problems(" in route
    assert "validate_theme_config(" in inspect.getsource(api._persist_imported_package)
    assert "validate_theme_variants(" in inspect.getsource(api._persist_imported_package)
    tool_source = inspect.getsource(template_authoring)
    assert tool_source.count("validate_theme_config(") >= 2
    assert "validate_theme_config(" in inspect.getsource(template_generation.normalize_template_candidate)


def test_the_rest_guard_refuses_harm_and_keeps_trusting_the_rest():
    """The REST surface trusts its caller about what merely does nothing (an
    unknown key, a blank value) but not about a value that does harm."""
    from app.services.template_containers import unsafe_theme_value_problems

    assert (
        unsafe_theme_value_problems(
            {"background": "#0b1020", "palette": {"accent": "#fff"}, "accent": "", "muted": 12, "surface": "\ufeff "},
            {"theme_variants": {"light": {"background": "#fff"}, "sepia": "x"}, "guidance": "x"},
            kind="presentation",
        )
        == []
    )
    problems = unsafe_theme_value_problems(
        {"background": 'image-set("//example.com/x.png" 1x)', "logo_file_id": "../etc/passwd"},
        {"theme_variants": {"light": {"surface": "url(//example.com/x.png)"}}},
        kind="presentation",
    )
    assert len(problems) == 3
    assert any(p.startswith("theme_config['background']") and "image-set()" in p for p in problems)
    assert any(p.startswith("theme_config['logo_file_id']") and "UUID" in p for p in problems)
    assert any(p.startswith("extra_metadata.theme_variants.light['surface']") and "url()" in p for p in problems)
    # A key the kind does not honour is not the REST guard's business (it is
    # stored and never forwarded), whatever it holds.
    assert unsafe_theme_value_problems({"critical": "url(//example.com/x.png)"}, None, kind="presentation") == []
    assert unsafe_theme_value_problems({"critical": "url(//example.com/x.png)"}, None, kind="report")


# -- the read boundary ---------------------------------------------------------


async def test_a_stored_template_stops_serving_a_fetching_value(monkeypatch):
    """No data migration: a template stored before the guard keeps every safe
    value and stops serving the fetching ones on the next read, so a deck
    wearing it paints those keys from its scheme's own tokens."""
    from app.services import presentations

    container = SimpleNamespace(
        id=uuid.uuid4(),
        name="Tracker",
        layouts=[],
        theme_config={
            "background": 'image-set("//example.com/x.png" 1x)',
            "surface": "-webkit-cross-fade('//example.com/a.png', '//example.com/b.png', 50%)",
            "foreground": "#f2f2f3",
            "accent": "#42caff",
            "heading_font": "'Geologica', sans-serif",
        },
        extra_metadata={
            "theme_variants": {
                "light": {"background": "#f2f2f3", "accent": "IMAGE('//example.com/x.png')"},
            }
        },
    )

    async def fake_load(db, container_id, *, user_id, is_admin=False):
        return container

    async def fake_fill(db, loaded, themes, *, keys):
        return None

    monkeypatch.setattr(presentations, "_load_visible_template", fake_load)
    monkeypatch.setattr(presentations, "_fill_role_logos", fake_fill)

    palette = await presentations.resolve_template_palette(None, container.id, user_id=uuid.uuid4())
    for scheme, theme in palette.themes.items():
        leaked = {key: value for key, value in theme.items() if "//example.com" in value}
        assert leaked == {}, (scheme, leaked)
    assert "background" not in palette.themes["dark"] and "surface" not in palette.themes["dark"]
    assert palette.themes["dark"]["accent"] == "#42caff"
    assert palette.themes["dark"]["heading_font"] == "'Geologica', sans-serif"
    # The variant's safe background lands; its fetching accent does not, so the
    # base accent shows through.
    assert palette.themes["light"]["background"] == "#f2f2f3"
    assert palette.themes["light"]["accent"] == "#42caff"

    # The one-scheme resolver the report path shares drops them too.
    report = await presentations.resolve_template_theme(
        None, container.id, user_id=uuid.uuid4(), keys=REPORT_THEME_CONFIG_KEYS
    )
    assert "background" not in report
    assert report["accent"] == "#42caff"
