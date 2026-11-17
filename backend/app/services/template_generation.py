# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""AI-assisted template container authoring.

The "Generate with AI" action of the Templates UI turns a plain-language brief
("a light corporate deck for quarterly business reviews, navy and gold") into a
complete ``TemplateContainer`` candidate: name, description, searchable labels,
a ``theme_config`` palette, the ``layouts`` catalog and the agent-facing
``extra_metadata`` (deck defaults + composition guidance).

Two rules shape this module:

* **The LLM proposes, the validators decide.** Whatever the model returns goes
  through the SAME guardrails an agent authoring a template hits
  (``template_containers.validate_theme_config`` / ``validate_layouts``), plus
  the deck-defaults enums, so the candidate handed to the UI is one the renderer
  will honour. Rejected entries come back as ``warnings`` rather than being
  silently dropped — the user sees what was pruned and why.
* **The contract is derived, never re-spelled.** The meta-prompt lists the
  theme keys, the layout ids and their fields, and the deck enums straight from
  the modules that enforce them, so the model can never be told about a key or
  layout the validator would then reject.

Nothing is persisted here: the endpoint returns a candidate the form applies.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
from collections import Counter
from typing import Any

from app.core.utils import format_exception_message
from app.llm.base import ChatMessage
from app.llm.cut_answers import chat_retrying_a_cut
from app.services.presentation_chrome import CHROME_KEY
from app.services.presentation_spec import (
    PRESENTATION_ASPECT_RATIOS,
    PRESENTATION_THEMES,
    PRESENTATION_TRANSITIONS,
)
from app.services.presentations import (
    BRAND_THEME_CONFIG_KEYS,
    DECK_DEFAULT_KEYS,
    DECK_SLIDE_DEFAULT_KEYS,
    DECK_SLIDE_DEFAULT_LAYOUTS,
    DECK_SLIDE_DEFAULT_VALUES,
    SUPPORTED_SCHEMES_KEY,
    THEME_FILE_ID_KEYS,
    THEME_SCHEMES,
    derived_supported_schemes,
    template_deck_defaults,
)
from app.services.template_containers import (
    PRESENTATION_KIND,
    REPORT_KIND,
    VIDEO_KIND,
    normalize_kind,
    normalize_tags,
    theme_config_keys_for_kind,
    validate_chrome,
    validate_layouts,
    validate_theme_config,
    validate_theme_variants,
)

logger = logging.getLogger(__name__)

# Output budget of one template proposal; a proposal cut at it is retried once
# with twice the room, never JSON-repaired into a template.
_TEMPLATE_MAX_TOKENS = 4096

#: Bounds on what the endpoint accepts / returns. Generous for a brief, tight
#: for the free-text fields that end up in a system prompt (``guidance``).
MAX_BRIEF_CHARS = 4000
MAX_PALETTE_HINTS = 8
MAX_HINT_CHARS = 60
MAX_NAME_CHARS = 200
MAX_DESCRIPTION_CHARS = 2000
MAX_GUIDANCE_CHARS = 2000
MAX_TAGS = 10
MAX_LOGO_BYTES = 5 * 1024 * 1024
#: Colours reported from a logo. Six is enough to seed background / accent /
#: gradient; more only dilutes the signal with anti-aliasing shades.
MAX_LOGO_COLORS = 6

#: ``extra_metadata`` keys the generator is allowed to fill. ``deck_defaults``
#: and ``guidance`` are what the presentation tooling reads; ``brand`` is the
#: free-form provenance block the seed also uses. Anything else the model
#: invents is dropped with a warning — ``extra_metadata`` is agent-visible, so
#: it must not become a dumping ground for hallucinated structure. The
#: ``supported_schemes`` declaration is NOT asked of the model: it is
#: derived from the palette it drew (:func:`derived_supported_schemes`) and
#: written by :func:`normalize_template_candidate`, so a generated template
#: always carries one and the form's Dark / Light switches start from it. A
#: ``theme_variants`` palette (the other scheme's colours) is kept when the
#: model returns one - a refinement round echoes the template's own - and held
#: to the palette contract like ``theme_config``. The ``chrome`` footer /
#: header declaration is likewise not asked of the model but kept when
#: echoed, validated like a human-authored one.
EXTRA_METADATA_KEYS: tuple[str, ...] = (
    "deck_defaults",
    "guidance",
    "brand",
    "theme_variants",
    CHROME_KEY,
)

_HEX_COLOR_RE = re.compile(r"#(?:[0-9a-fA-F]{3}){1,2}\b")


class TemplateGenerationError(Exception):
    """The model did not produce a usable candidate (after a retry)."""


# ── Logo palette extraction ──────────────────────────────────────────────────


def _normalize_hex(token: str) -> str:
    token = token.lower()
    if len(token) == 4:  # #abc -> #aabbcc
        token = "#" + "".join(ch * 2 for ch in token[1:])
    return token


def _svg_palette(raw: bytes, max_colors: int) -> list[str]:
    """Most frequent hex colours named in an SVG's attributes / inline styles."""
    text = raw.decode("utf-8", errors="ignore")
    counts = Counter(_normalize_hex(m.group(0)) for m in _HEX_COLOR_RE.finditer(text))
    return [color for color, _ in counts.most_common(max_colors)]


def _raster_palette(raw: bytes, max_colors: int) -> list[str]:
    """Dominant opaque colours of a raster image, most frequent first.

    Median-cut quantisation on a thumbnail: fast, deterministic, and good enough
    to seed a palette — this is a hint for the model, not a colour-science tool.
    Transparent pixels are dropped first so a logo on a transparent canvas does
    not report black (the RGBA->RGB default) as its main colour.
    """
    from PIL import Image

    with Image.open(io.BytesIO(raw)) as img:
        img = img.convert("RGBA")
        img.thumbnail((128, 128))
        data = img.tobytes()
    opaque = bytearray()
    for offset in range(0, len(data) - 3, 4):
        if data[offset + 3] > 128:
            opaque += data[offset : offset + 3]
    if not opaque:
        return []
    strip = Image.frombytes("RGB", (len(opaque) // 3, 1), bytes(opaque))
    quantized = strip.quantize(colors=max_colors, method=Image.Quantize.MEDIANCUT)
    palette = quantized.getpalette() or []
    counted = quantized.getcolors() or []
    counted.sort(key=lambda item: item[0], reverse=True)
    colors: list[str] = []
    for _count, index in counted:
        offset = index * 3
        if offset + 2 >= len(palette):
            continue
        r, g, b = palette[offset : offset + 3]
        colors.append(f"#{r:02x}{g:02x}{b:02x}")
    return colors[:max_colors]


def extract_palette(raw: bytes, content_type: str | None, *, max_colors: int = MAX_LOGO_COLORS) -> list[str]:
    """Hex colours that dominate an uploaded logo, or ``[]`` when unreadable.

    Never raises: the palette is a convenience hint and a corrupt or exotic file
    must not fail the generation it decorates.
    """
    if not raw:
        return []
    is_svg = (content_type or "").lower() == "image/svg+xml" or b"<svg" in raw[:4096].lower()
    try:
        if is_svg:
            return _svg_palette(raw, max_colors)
        return _raster_palette(raw, max_colors)
    except Exception as exc:
        logger.debug("Logo palette extraction skipped: %s", format_exception_message(exc))
        return []


async def extract_palette_async(raw: bytes, content_type: str | None) -> list[str]:
    """Thread-offloaded :func:`extract_palette` (Pillow decoding is CPU-bound)."""
    return await asyncio.to_thread(extract_palette, raw, content_type)


# ── Meta-prompt ──────────────────────────────────────────────────────────────


def _layout_entries(kind: str) -> list[dict[str, Any]]:
    """The layout / block catalog the model may advertise, with its labels."""
    if kind == PRESENTATION_KIND:
        from app.services.template_container_seed import default_layout_entries

        return default_layout_entries()
    if kind == REPORT_KIND:
        from app.services.report_spec import block_catalog

        return [{"id": block, "fields": fields} for block, fields in block_catalog().items()]
    return []


def _format_layout_lines(entries: list[dict[str, Any]]) -> str:
    lines = []
    for entry in entries:
        label = entry.get("name") or entry["id"]
        use_for = entry.get("use_for") or ""
        fields = ", ".join(entry.get("fields") or [])
        line = f"- `{entry['id']}` ({label})"
        if use_for:
            line += f": {use_for}"
        if fields:
            line += f" Fields: {fields}."
        lines.append(line)
    return "\n".join(lines)


def build_template_system_prompt(kind: str) -> str:
    """The generator's contract for *kind*, derived from the enforcing modules."""
    keys = [key for key in theme_config_keys_for_kind(kind) if key not in THEME_FILE_ID_KEYS]
    brand_keys = ", ".join(BRAND_THEME_CONFIG_KEYS)
    entries = _layout_entries(kind)

    parts = [
        "You are a senior brand and presentation designer producing a design "
        "TEMPLATE for an AI presentation platform. The template is metadata an AI "
        "agent later reads to build slides in the requested style: a palette, "
        "typefaces, a catalog of slide layouts and short composition guidance.\n",
        "Return ONE JSON object and nothing else (no prose, no markdown fences) with "
        "exactly these keys:\n"
        '- "name": short, specific template name (max 60 chars).\n'
        '- "description": 1-2 sentences on the style and when to use it.\n'
        '- "tags": 3-6 lowercase tags agents can filter on (audience, tone, industry).\n'
        '- "theme_config": object of CSS colour / font tokens (see keys below).\n'
        '- "layouts": array of {"id", "name", "use_for"} entries (ids from the '
        "catalog below).\n"
        '- "extra_metadata": object with "deck_defaults", "guidance" and "brand".\n',
        "theme_config rules:\n"
        f"- Allowed keys: {', '.join(keys)}. Always set the six brand keys "
        f"({brand_keys}) and, for a deck, surface, line, the four semantic tones "
        "(info, success, warning, error), gradient_from and gradient_to, and the "
        "six ordered chart series colours chart_1..chart_6 (chart_1 = the accent, "
        "each distinguishable from the next, none reading as a feedback tone). A "
        "deck's brand_1..brand_4 are optional: brand hues such as product colours "
        "a diagram box can wear - set them only when the brief names them.\n"
        "- Every value is ONE plain CSS token: a colour like '#0b1020', "
        "'hsl(220 54% 8%)' or 'rgb(11 16 32)', or a font stack like "
        "\"'Inter', system-ui, sans-serif\". No url(), braces, semicolons, "
        "comments or gradients as values.\n"
        "- Contrast is mandatory: foreground on background >= 7:1, muted on "
        "background >= 4.5:1, accent legible on both background and surface. "
        "Never use the same colour for background and surface.\n"
        "- Fonts: name real, widely available families and end each stack with a "
        "generic family. Prefer one display face for headings and one text face "
        "for body.\n",
    ]

    if kind == PRESENTATION_KIND:
        parts.append(
            "Deck-specific rules:\n"
            f"- extra_metadata.deck_defaults is an object with theme in "
            f"[{', '.join(PRESENTATION_THEMES)}] (dark = dark surfaces, light = "
            "white paper; the brand comes from this template, not the theme), transition in "
            f"[{', '.join(PRESENTATION_TRANSITIONS)}] and aspect_ratio in "
            f"[{', '.join(PRESENTATION_ASPECT_RATIOS)}]. Choose the deck theme that "
            "matches the palette's background luminance: a light background needs "
            "theme 'light'.\n"
            "- layouts: advertise EVERY layout below that suits the style (a "
            "complete deck needs at least title, agenda, section, content, "
            "metrics_grid, roadmap, takeaways and closing). Write use_for as one "
            "sentence telling an agent when to use the layout in THIS style.\n"
            "- extra_metadata.guidance: 3-5 sentences of composition guidance for the "
            "agent (opening, pacing, density, when to use which layout, tone of "
            "voice). Reference layout ids in backticks.\n"
            '- extra_metadata.brand: {"source": "SOURCE", "audience": "AUDIENCE"} - where the '
            "palette comes from and who the deck is for.\n"
            f"Slide layout catalog:\n{_format_layout_lines(entries)}\n"
        )
    elif kind == REPORT_KIND:
        parts.append(
            "Report-specific rules:\n"
            "- layouts: advertise the report blocks below that suit the style, each "
            "with a one-sentence use_for.\n"
            "- extra_metadata.guidance: 3-5 sentences of composition guidance for the "
            "agent. extra_metadata.deck_defaults must be an empty object.\n"
            f"Report block catalog:\n{_format_layout_lines(entries)}\n"
        )
    elif kind == VIDEO_KIND:
        parts.append(
            "Video-specific rules:\n"
            "- layouts: an empty array (a video has scenes, not layouts).\n"
            "- Set the chart_1..chart_6 series too: a mosaic backdrop draws its tiles in them.\n"
            "- extra_metadata.guidance: 3-5 sentences for the agent producing videos in this "
            "style (opening, pacing, narration tone, what to show on screen). "
            "extra_metadata.deck_defaults must be an empty object; the video rules "
            "(extra_metadata.video) are set in the template's editor, not generated.\n"
        )
    return "".join(parts)


def build_template_messages(
    *,
    brief: str,
    kind: str,
    palette_hints: list[str],
    logo_palette: list[str],
    current: dict[str, Any] | None,
) -> list[ChatMessage]:
    """System + user turn for one generation (or refinement) round."""
    user_parts = [f"Design brief:\n{brief.strip()}"]
    if palette_hints:
        user_parts.append("Palette hints from the user (honour them): " + ", ".join(palette_hints))
    if logo_palette:
        user_parts.append(
            "Dominant colours of the uploaded logo, most frequent first: "
            + ", ".join(logo_palette)
            + ". Build the palette so the logo sits naturally on the background; "
            "ignore neutral whites/blacks that are just the logo's own canvas."
        )
    if current:
        user_parts.append(
            "The user is refining THIS existing template. Keep everything that the "
            "brief does not ask to change (same keys, same structure) and apply only "
            "the requested changes:\n" + json.dumps(current, ensure_ascii=False, indent=2)
        )
    user_parts.append("Answer with the JSON object only.")
    return [
        ChatMessage(role="system", content=build_template_system_prompt(kind)),
        ChatMessage(role="user", content="\n\n".join(user_parts)),
    ]


# ── Parsing + normalisation ──────────────────────────────────────────────────


def extract_json_object(text: str) -> dict[str, Any]:
    """The first JSON object in an LLM reply, fences and chatter tolerated."""
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```\s*$", "", cleaned)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in the model reply")
    parsed = json.loads(cleaned[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("the model reply is not a JSON object")
    return parsed


def validation_problem(exc: Any) -> str:
    """A Pydantic refusal of a model's JSON, worded back to the model: one
    ``field: reason`` per error, the first twelve."""
    return "; ".join(
        f"{'.'.join(str(p) for p in err.get('loc', ()))}: {str(err.get('msg', '')).removeprefix('Value error, ')}"
        for err in exc.errors()[:12]
    )


def _clean_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned[:limit] or None


def _normalize_deck_defaults(raw: object, *, aspect_ratio: str | None, warnings: list[str]) -> dict[str, Any]:
    allowed = {
        "theme": PRESENTATION_THEMES,
        "transition": PRESENTATION_TRANSITIONS,
        "aspect_ratio": PRESENTATION_ASPECT_RATIOS,
    }
    defaults: dict[str, Any] = {}
    if isinstance(raw, dict):
        # The per-cover objects (`hero_defaults` / `hero_tones` /
        # `slide_themes`) are read through the ONE whitelist the renderer
        # applies (`template_deck_defaults`), never a second table here - so a
        # generated template may carry them, and what it carries is exactly
        # what a deck made from it will honour.
        slide_level = template_deck_defaults(
            {"deck_defaults": {key: raw[key] for key in DECK_SLIDE_DEFAULT_KEYS if key in raw}}
        )
        for key, value in raw.items():
            if key in DECK_SLIDE_DEFAULT_KEYS:
                if key not in slide_level:
                    warnings.append(
                        f"deck_defaults.{key} carried no usable title / closing value and was dropped."
                    )
                    continue
                # A kept object may still have lost an entry - a body layout,
                # a value outside the catalog - and the agent must be told
                # which, or it believes the closing's cover landed too.
                if isinstance(value, dict):
                    for layout, entry in value.items():
                        if layout in slide_level[key]:
                            continue
                        if layout not in DECK_SLIDE_DEFAULT_LAYOUTS:
                            warnings.append(
                                f"deck_defaults.{key}.{layout} is not a cover layout "
                                f"({' / '.join(DECK_SLIDE_DEFAULT_LAYOUTS)} only); it was dropped."
                            )
                        else:
                            warnings.append(
                                f"deck_defaults.{key}.{layout} = {entry!r} is not one of "
                                f"{', '.join(DECK_SLIDE_DEFAULT_VALUES[key])}; it was dropped."
                            )
                continue
            if key not in DECK_DEFAULT_KEYS:
                warnings.append(f"deck_defaults.{key} is not a deck default and was dropped.")
                continue
            if not isinstance(value, str) or value.strip().lower() not in allowed[key]:
                warnings.append(
                    f"deck_defaults.{key} = {value!r} is not one of {', '.join(allowed[key])}; it was dropped."
                )
                continue
            defaults[key] = value.strip().lower()
        defaults.update(slide_level)
    elif raw not in (None, {}):
        warnings.append("deck_defaults must be an object; it was dropped.")
    if "aspect_ratio" not in defaults and aspect_ratio:
        defaults["aspect_ratio"] = aspect_ratio
    return defaults


def _normalize_brand(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    brand: dict[str, Any] = {}
    for key, value in list(raw.items())[:8]:
        if not isinstance(key, str):
            continue
        if isinstance(value, str):
            cleaned = _clean_text(value, 200)
            if cleaned:
                brand[key[:40]] = cleaned
        elif isinstance(value, list):
            items = [_clean_text(item, 80) for item in value[:12] if isinstance(item, str)]
            brand[key[:40]] = [item for item in items if item]
    return brand


def normalize_template_candidate(
    raw: dict[str, Any],
    *,
    kind: str,
    fallback_name: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Turn a model reply into a storable candidate plus the list of prunings.

    Everything the renderer would ignore is removed and REPORTED: an unknown
    theme key, an unsafe token, an invented layout id, an out-of-enum deck
    default. The candidate that comes out is exactly what ``POST
    /template-containers`` accepts and ``buildTemplatePreview`` renders.
    """
    warnings: list[str] = []
    # A closed choice: the route refused anything else before the model
    # ran, so an unknown value here is a programming error, not user input -
    # fall back to the default kind rather than emit a candidate no route accepts.
    kind = normalize_kind(kind) or PRESENTATION_KIND

    name = _clean_text(raw.get("name"), MAX_NAME_CHARS) or (fallback_name or "").strip()[:MAX_NAME_CHARS]
    if not name:
        name = "Generated template"
        warnings.append("The model returned no name; a placeholder was used.")

    theme_config, problems = validate_theme_config(raw.get("theme_config"), kind=kind)
    warnings.extend(problems)
    missing = [key for key in BRAND_THEME_CONFIG_KEYS if key not in theme_config]
    if missing:
        warnings.append(
            "The palette is missing " + ", ".join(missing) + "; the renderer will use its theme defaults for them."
        )

    layouts, problems = validate_layouts(raw.get("layouts"), kind=kind)
    warnings.extend(problems)
    if not layouts and kind in (PRESENTATION_KIND, REPORT_KIND):
        # An empty catalog is still a working template (the preview falls back to
        # the full deck), but an agent reads the catalog to know what it may use,
        # so advertise everything rather than nothing.
        layouts = [
            {key: value for key, value in entry.items() if key in ("id", "name", "use_for", "fields")}
            for entry in _layout_entries(kind)
        ]
        warnings.append("The model advertised no usable layout; the full catalog was used instead.")

    aspect_ratio = _clean_text(raw.get("aspect_ratio"), 30)
    if kind == PRESENTATION_KIND:
        if aspect_ratio and aspect_ratio not in PRESENTATION_ASPECT_RATIOS:
            warnings.append(
                f"aspect_ratio {aspect_ratio!r} is not one of {', '.join(PRESENTATION_ASPECT_RATIOS)}; 16:9 was used."
            )
            aspect_ratio = "16:9"
        aspect_ratio = aspect_ratio or "16:9"

    extra_raw = raw.get("extra_metadata")
    extra: dict[str, Any] = {}
    if isinstance(extra_raw, dict):
        for key in extra_raw:
            if key not in EXTRA_METADATA_KEYS:
                warnings.append(f"extra_metadata.{key} is not read by the platform and was dropped.")
        if kind == PRESENTATION_KIND:
            extra["deck_defaults"] = _normalize_deck_defaults(
                extra_raw.get("deck_defaults"), aspect_ratio=aspect_ratio, warnings=warnings
            )
        guidance = _clean_text(extra_raw.get("guidance"), MAX_GUIDANCE_CHARS)
        if guidance:
            extra["guidance"] = guidance
        brand = _normalize_brand(extra_raw.get("brand"))
        if brand:
            extra["brand"] = brand
        if kind == PRESENTATION_KIND and extra_raw.get("theme_variants") not in (None, {}):
            accepted, problems = validate_theme_variants(
                {"theme_variants": extra_raw.get("theme_variants")}, kind=kind
            )
            warnings.extend(problems)
            variants = accepted.get("theme_variants") if isinstance(accepted, dict) else None
            if isinstance(variants, dict) and any(variants.values()):
                extra["theme_variants"] = {k: v for k, v in variants.items() if v}
        if kind == PRESENTATION_KIND and extra_raw.get(CHROME_KEY) not in (None, {}):
            # A footer / header pattern the model chose to echo: held to
            # the same contract as a human-authored one - a problem drops it
            # with a warning, never stores a slot that would render literally.
            accepted, problems = validate_chrome({CHROME_KEY: extra_raw.get(CHROME_KEY)}, kind=kind)
            if problems:
                warnings.extend(problems)
            elif isinstance(accepted, dict) and accepted.get(CHROME_KEY):
                extra[CHROME_KEY] = accepted[CHROME_KEY]
    elif kind == PRESENTATION_KIND:
        extra["deck_defaults"] = _normalize_deck_defaults(None, aspect_ratio=aspect_ratio, warnings=warnings)

    if kind == PRESENTATION_KIND:
        # The schemes this design supports are DECLARED on the template.
        # The generator draws ONE palette, so the declaration is the scheme that
        # palette's background reads as; a palette with no background yet
        # supports both until the author says otherwise. The deck default theme
        # has to be one of them, or every deck generated from the template would
        # be refused by its own template.
        supported = derived_supported_schemes(theme_config, extra) or list(THEME_SCHEMES)
        extra[SUPPORTED_SCHEMES_KEY] = supported
        defaults = extra.get("deck_defaults") or {}
        default_theme = defaults.get("theme")
        if default_theme and default_theme not in supported:
            warnings.append(
                f"deck_defaults.theme = {default_theme!r} is not a scheme this palette supports "
                f"({', '.join(supported)}); {supported[0]!r} was used."
            )
            defaults["theme"] = supported[0]
        elif not default_theme:
            defaults["theme"] = supported[0]
        extra["deck_defaults"] = defaults

    tags_raw = raw.get("tags")
    tags = normalize_tags([t for t in tags_raw if isinstance(t, str)] if isinstance(tags_raw, list) else None)
    tags = tags[:MAX_TAGS]

    candidate = {
        "name": name,
        "description": _clean_text(raw.get("description"), MAX_DESCRIPTION_CHARS),
        "kind": kind,
        # The legacy free-text ``theme`` label is no longer asked for nor kept:
        # the deck themes a template supports are derived from its
        # palette, and a label the model invented rendered nothing.
        "theme": None,
        "aspect_ratio": aspect_ratio,
        "tags": tags,
        "theme_config": theme_config,
        "layouts": layouts,
        "extra_metadata": extra,
    }
    return candidate, warnings


# ── Orchestration ────────────────────────────────────────────────────────────


def clean_palette_hints(hints: list[str] | None) -> list[str]:
    cleaned: list[str] = []
    for hint in hints or []:
        if not isinstance(hint, str):
            continue
        text = " ".join(hint.split())[:MAX_HINT_CHARS]
        if text and text not in cleaned:
            cleaned.append(text)
        if len(cleaned) >= MAX_PALETTE_HINTS:
            break
    return cleaned


def current_template_summary(current: dict[str, Any] | None) -> dict[str, Any] | None:
    """The subset of an existing template worth showing the model on refine.

    Sharing / ownership / ids are stripped: they are irrelevant to the design
    and would only invite the model to echo them back.
    """
    if not isinstance(current, dict):
        return None
    keep = ("name", "description", "aspect_ratio", "tags", "theme_config", "layouts", "extra_metadata")
    summary = {key: current[key] for key in keep if current.get(key) not in (None, "", [], {})}
    return summary or None


async def generate_template_candidate(
    llm: Any,
    model: str | None,
    *,
    brief: str,
    kind: str,
    palette_hints: list[str],
    logo_palette: list[str],
    current: dict[str, Any] | None,
    fallback_name: str | None,
) -> tuple[dict[str, Any], list[str]]:
    """One generation round with a single JSON-repair retry.

    Returns ``(candidate, warnings)``; raises :class:`TemplateGenerationError`
    when the model could not be coaxed into valid JSON twice in a row.
    """
    messages = build_template_messages(
        brief=brief,
        kind=kind,
        palette_hints=palette_hints,
        logo_palette=logo_palette,
        current=current,
    )
    last_error: Exception | None = None
    for attempt in range(2):

        async def _generate(max_tokens: int, _messages=messages):
            return await llm.chat(
                _messages, model=model, temperature=0.6, max_tokens=max_tokens
            )

        response, cut = await chat_retrying_a_cut(
            llm, model, _TEMPLATE_MAX_TOKENS, _generate, label="Template generation"
        )
        if cut:
            # Never repaired into a template: the failed-generation path.
            raise TemplateGenerationError(
                "The model did not return a valid template. Its reply was cut "
                "at the output limit (max_tokens)."
            )
        try:
            raw = extract_json_object(response.content or "")
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            logger.info(
                "Template generation reply was not JSON (attempt %s): %s", attempt + 1, format_exception_message(exc)
            )
            messages = [
                *messages,
                ChatMessage(role="assistant", content=response.content or ""),
                ChatMessage(
                    role="user",
                    content="That was not valid JSON. Reply again with ONLY the JSON object, no fences, no prose.",
                ),
            ]
            continue
        return normalize_template_candidate(raw, kind=kind, fallback_name=fallback_name)
    raise TemplateGenerationError(
        "The model did not return a valid template. " + (format_exception_message(last_error) if last_error else "")
    )
