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

"""Authoring guidance for agent-generated decks.

The contract an agent writes against is ``presentation_spec``; this module is
what teaches the model to use it WELL, and it is the single source of the
``generate_presentation`` tool description. It holds:

- one compact example per block type and per v2 layout, every one of them
  VALIDATED by ``tests/test_presentation_guidance.py`` through the real
  ``validate_spec`` (an example the validator would refuse teaches the model a
  field that does not exist, which is worse than no example at all);
- what the six tones mean, so ``success`` / ``error`` are used for meaning and
  not for decoration - the table must cover every ``Tone`` member;
- the soft budgets that separate a slide from a page, quoting the ``MAX_*``
  constants rather than re-spelling numbers that would drift.

The description is derived from the models (``spec_reference()``) plus these
sections, so a new layout, block or field is documented to the agent the moment
it is added to the spec. The longer composition recipes (a QBR, a roadmap
review, a post-incident deck...) are NOT here: they live in the opt-in
``presentation-design`` built-in skill (``agents/seed_data/skill_content``) so
the per-turn tool description stays as short as a complete contract allows.
"""

from __future__ import annotations

import functools
import json
import typing
from typing import Any

from app.services.presentation_markup import MARKUP_SUMMARY
from app.services.presentation_notes import WORDS_PER_MINUTE
from app.services.presentation_spec import (
    BLOCK_TYPES,
    DASHBOARD_GRID_ROWS,
    DEFAULT_SLIDE_CAP,
    MAX_ARCHITECTURE_GRID,
    MAX_ARCHITECTURE_NODES,
    MAX_BLOCKS_PER_SLIDE,
    MAX_CHART_CATEGORIES,
    MAX_CHART_SERIES,
    MAX_DASHBOARD_PANELS,
    MAX_DASHBOARD_TILES,
    MAX_LIST_ITEMS,
    MAX_MAP_CATEGORIES,
    MAX_SLIDE_CAP,
    MAX_TABLE_COLUMNS,
    MAX_TABLE_ROWS,
    ORG_CHART_HINT,
    ORG_CHART_READABLE_NODES,
    SLIDE_LAYOUTS,
    Tone,
    spec_reference,
)

#: What each tone MEANS. Colour is the renderer's business; meaning is the
#: model's, and a deck where ``error`` decorates a heading because red "pops"
#: reads as a deck full of problems. Keyed by the ``Tone`` literal members and
#: pinned to them by the test suite.
TONE_SEMANTICS: dict[str, str] = {
    "accent": (
        "the brand colour - the ONE thing on the slide that matters (the key number, the decision, the current phase)"
    ),
    "info": "neutral context and definitions - to know, not to act on",
    "success": ('what works, is done or improved - positive numbers, resolved items, the "after" side'),
    "warning": "caution - moderate risk, open questions, a deadline at risk",
    "error": ('problems - blockers, critical findings, negative numbers, the "before" side of a fix'),
    "muted": ("de-emphasis - background context, superseded options, the baseline being compared against"),
}

#: A delta is coloured by its direction unless ``delta_tone`` names the news:
#: where lower is better, the direction alone reads a good quarter red.
DELTA_TONE_RULE = (
    "A metric's delta is coloured by its trend (up green, down red); for costs, loss, churn and burn, "
    'a fall uses delta_tone "success" and a rise "error" - the arrow keeps the trend.'
)


def soft_budgets() -> tuple[str, ...]:
    """Density rules for a slide that reads from the back of the room.

    The validator's ``MAX_*`` caps are safety bounds (a runaway generation must
    not produce an unusable artifact); these are the numbers a good deck
    actually uses. Derived from the caps where a cap exists so the two never
    disagree.
    """
    return (
        "one idea per slide, and the title states it in <= 8 words "
        "(a claim, not a topic: 'Alerts fell 42% after consolidation', not 'Results')",
        f"a content slide carries 3-5 blocks or 2-3 columns, not the {MAX_BLOCKS_PER_SLIDE} the validator allows",
        "blocks are sized to their content; place a short body "
        "(vertical_align center or stretch), takeaway callout last",
        f"lists: <= 6 items at normal density, <= 8 compact, <= {MAX_LIST_ITEMS} tight - "
        "beyond that split the slide or use columns",
        "callout / quote / text body: <= 40 words; the rest goes in the speaker notes",
        "metrics: 3-4 tiles per row, value <= 6 characters, label <= 3 words; "
        "a KPI row that recurs across the deck uses one size and one density on every slide",
        f"tables: <= 6 rows x 4 columns for a readable slide ({MAX_TABLE_ROWS} x {MAX_TABLE_COLUMNS} is the hard cap); "
        "a table that carries the slide alone takes fill, and size lg when it has the room",
        f"charts: <= 4 series and <= 8 categories read from the back of the room ({MAX_CHART_SERIES} x "
        f"{MAX_CHART_CATEGORIES} is the hard cap; a map takes one value per country, up to {MAX_MAP_CATEGORIES}); "
        "one message per chart, said in its title (drawn in its frame "
        "as a panel's is - not a callout around the chart); the caption is the note under the plot",
        f"dashboard: <= 6 tiles and <= 4 panels fit a 16:9 slide without shrinking ({MAX_DASHBOARD_TILES} tiles "
        f"and {MAX_DASHBOARD_PANELS} panels on {DASHBOARD_GRID_ROWS} rows is the hard cap); a chart that carries "
        "the slide gets span 2",
        "portrait pages (A4 / letter) are read at arm's length: 4-7 blocks per page, "
        "tables may run to 8-10 rows, 1-4 pages per document",
        "diagrams: <= 10 nodes, labels <= 3 words, one direction (LR or TD); "
        f"architecture: <= 8 nodes on a grid of at most 4 x 4 (cap {MAX_ARCHITECTURE_NODES} on "
        f"{MAX_ARCHITECTURE_GRID} x {MAX_ARCHITECTURE_GRID}), edge labels <= 3 words, one core",
        "deck: title -> agenda -> (section -> 2-5 content) x N -> takeaways -> closing; "
        f"about one slide per minute of talk (cap {DEFAULT_SLIDE_CAP} slides, or the template's "
        f"max_slides up to {MAX_SLIDE_CAP})",
        "speaker notes on every content slide, 40-120 words (about "
        f"{WORDS_PER_MINUTE} words a minute spoken): the opening line, the one number or "
        "example to land, the transition to the next slide - never the bullets read back",
        "one tone per meaning across the whole deck (accent = key, success = done, "
        "error = problem); never a tone for decoration",
    )


# ── Examples ─────────────────────────────────────────────────────────────────
#
# Minimal on purpose: the required fields plus the one or two optional fields
# that make the block worth using. Every one is validated by the test suite,
# wrapped in a ``content`` slide of a one-slide deck.

BLOCK_EXAMPLES: dict[str, dict[str, Any]] = {
    "text": {
        "type": "text",
        "content": "Consolidating on **one platform** cut triage time by _a third_.",
    },
    "heading": {"type": "heading", "content": "What changed", "level": 3, "icon": "git-branch"},
    "quote": {
        "type": "quote",
        "variant": "statement",
        "size": "hero",
        "content": "If I took OpenCTI away from my team, they would walk out.",
        "author": "Dana Whitlock",
        "role": "VP Security Operations",
        "organization": "Contoso Financial",
        "figures": [{"value": "80K", "label": "Employees"}, {"value": "$30B", "label": "Revenue"}],
    },
    "list": {
        "type": "list",
        "style": "check",
        "items": ["Fewer tools", "One workflow", {"text": "Faster response", "tone": "success"}],
    },
    "callout": {
        "type": "callout",
        "tone": "accent",
        "icon": "lightbulb",
        "title": "Decision",
        "content": "Consolidate on one platform by Q2.",
    },
    "metric": {
        "type": "metric",
        "value": "42%",
        "label": "Fewer alerts",
        "delta": "-12 pts",
        "trend": "down",
        "delta_tone": "success",
        "tone": "success",
        "sparkline": [61, 58, 54, 49, 42],
    },
    "metrics_grid": {
        "type": "metrics_grid",
        "items": [
            {"value": "3", "label": "Products"},
            {"value": "1", "label": "Platform"},
            {"value": "0", "label": "Silos", "tone": "success"},
        ],
    },
    "table": {
        "type": "table",
        "headers": ["Capability", "Community", {"text": "Enterprise", "tone": "accent"}],
        "rows": [
            ["Knowledge graph", True, True],
            ["AI assistant", False, True],
            ["Support", "Community", {"text": "24/7", "tone": "success"}],
        ],
        "footnote": "Enterprise features need a Filigran license.",
    },
    "chart": {
        "type": "chart",
        "kind": "line",
        "categories": ["Q1", "Q2", "Q3", "Q4"],
        "series": [{"name": "ARR", "values": [11200000, 12900000, 14600000, 16500000]}],
        "format": "currency",
        "target": 18000000,
        "goal_label": "Plan",
        "title": "ARR by quarter",
        "caption": "Contracted ARR at quarter end, EUR",
    },
    "key_value": {
        "type": "key_value",
        "columns": 2,
        "items": [
            {"label": "Deployment", "value": "SaaS (EU / US) or on-premises"},
            {"label": "Support", "value": "24/7, 1h P1 response"},
            {"label": "Term", "value": "Annual"},
            {"label": "Users", "value": "Unlimited"},
        ],
    },
    "badges": {
        "type": "badges",
        "items": [
            {"text": "SOC 2 Type II", "icon": "shield-check"},
            {"text": "ISO 27001", "icon": "badge-check"},
            {"text": "SaaS or on-prem", "tone": "info"},
        ],
    },
    "feature_list": {
        "type": "feature_list",
        "columns": 2,
        "items": [
            {"icon": "shield-check", "title": "Hardened", "description": "CIS level 2"},
            {"icon": "zap", "title": "Fast", "description": "Sub-second detections"},
        ],
    },
    "phase_card": {
        "type": "phase_card",
        "icon": "rocket",
        "title": "Phase 1",
        "subtitle": "Q1 2026",
        "items": ["Discovery", "Pilot with 3 teams"],
        "goal": {"label": "Goal", "value": "3 design partners live"},
    },
    "numbered_cards": {
        "type": "numbered_cards",
        "items": [
            {"title": "Start with the threat", "description": "Not with the tool."},
            {"title": "Measure what matters", "description": "Time to respond, not alert count."},
        ],
    },
    "pricing_tiers": {
        "type": "pricing_tiers",
        "tiers": [
            {
                "name": "Community",
                "price": "Free",
                "description": "Open source, self-hosted",
                "features": ["Core platform", "Community support"],
                "cta": "Get started",
            },
            {
                "name": "Enterprise",
                "price": "Contact us",
                "period": "per year",
                "features": ["Everything in Community", "AI assistant", "Premium support"],
                "highlight": True,
                "badge": "Most popular",
                "cta": "Talk to sales",
            },
        ],
    },
    "personas": {
        "type": "personas",
        "items": [
            {
                "name": "CISO",
                "tagline": "Owns the risk narrative",
                "icon": "shield",
                "pains": ["No single number for the board", "Controls assumed, not proven"],
                "gains": ["Prevention + detection score per business unit"],
            },
            {
                "name": "SOC analyst",
                "tagline": "Lives in the detection stack",
                "icon": "radar",
                "pains": ["Rules never tested against live TTPs"],
                "gains": ["Scenarios generated from the intel feed", "Gaps with a fix attached"],
                "tone": "info",
            },
        ],
    },
    "profiles": {
        "type": "profiles",
        "items": [
            {
                "name": "Samuel Hassine",
                "title": "Chief Executive Officer",
                "photo_file_id": "PEOPLE_PHOTO_FILE_ID",
                "lines": [
                    {"text": "Director, Security Strategy and Operations, Tanium", "icon": "building-2"},
                    {"text": "Director, Cyber Threat Intelligence, ANSSI", "logo_file_id": "LOGO_FILE_ID"},
                    {"text": "15+ years in cybersecurity operations and risk management", "icon": "users"},
                ],
            },
            {
                "name": "Julien Richard",
                "title": "Chief Technology Officer",
                "photo_file_id": "PEOPLE_PHOTO_FILE_ID",
                "lines": [
                    {"text": "Vice President, Engineering, YOOI", "icon": "building-2"},
                    {"text": "Director, Engineering Management, Axway", "icon": "building-2"},
                    {"text": "15+ years in engineering leadership and software development", "icon": "users"},
                ],
            },
        ],
    },
    "timeline": {
        "type": "timeline",
        "style": "phases",
        "items": [
            {"label": "Q1", "title": "Pilot", "tone": "success"},
            {"label": "Q2", "title": "Rollout", "tone": "accent"},
            {"label": "Q3", "title": "Scale"},
        ],
    },
    "process_flow": {
        "type": "process_flow",
        "steps": [
            {"icon": "radar", "title": "Detect"},
            {"icon": "search", "title": "Triage"},
            {"icon": "shield-check", "title": "Respond"},
        ],
        "loop": True,
    },
    "comparison": {
        "type": "comparison",
        "left": {
            "label": "Before",
            "blocks": [{"type": "list", "style": "cross", "items": ["5 consoles", "Manual triage"]}],
        },
        "right": {
            "label": "After",
            "blocks": [{"type": "list", "style": "check", "items": ["1 console", "Auto-triage"]}],
        },
    },
    "section_header": {"type": "section_header", "title": "What we learned", "icon": "target"},
    "banner": {
        "type": "banner",
        "eyebrow": "Product brief",
        "title": "OpenAEV Enterprise Edition",
        "subtitle": "Breach and attack simulation, continuously validated",
        "icon": "shield-check",
        "actions": [
            {"label": "filigran.io", "url": "https://filigran.io", "primary": True},
            {"label": "sales@filigran.io", "url": "mailto:sales@filigran.io"},
        ],
    },
    "challenge_answer": {
        "type": "challenge_answer",
        "icon": "layers",
        "challenge": "Five consoles and five queues - every incident is correlated **by hand**.",
        "answer": "One case model - every source lands in the same investigation.",
    },
    "image": {
        "type": "image",
        "file_id": "PLATFORM_FILE_ID",
        "alt": "Reference architecture",
        "size": "lg",
    },
    # A video block plays one source: video_id (a Video Studio video), file_id
    # (an MP4 / WebM platform file) or url (a YouTube / Vimeo link, or a public
    # MP4 / WebM link imported on save).
    "video": {
        "type": "video",
        "video_id": "VIDEO_STUDIO_VIDEO_ID",
        "alt": "Product tour",
        "frame": "browser",
        "fill": True,
    },
    "logo_grid": {
        "type": "logo_grid",
        "items": [
            {"file_id": "PLATFORM_FILE_ID", "label": "Customer A"},
            {"file_id": "PLATFORM_FILE_ID", "label": "Customer B"},
            {"file_id": "PLATFORM_FILE_ID", "label": "Customer C"},
        ],
        "columns": 3,
    },
    "logo_wall": {
        "type": "logo_wall",
        "columns": 2,
        "per_row": 4,
        "logo_height": "md",
        "tone": "original",
        "groups": [
            {"title": title, "icon": icon, "logos": ["LOGO_FILE_ID"] * 8}
            for title, icon in (
                ("Government", "landmark"),
                ("Finance", "banknote"),
                ("Energy", "zap"),
                ("Industry", "factory"),
            )
        ],
    },
    "formula": {
        "type": "formula",
        "terms": [
            {"title": "Core platform", "subtitle": "Always included", "icon": "sparkles"},
            {"title": "Modules used", "subtitle": "Pick what you need", "logos": ["LOGO_FILE_ID"] * 3, "tone": "info"},
            {"title": "Agentic capacity", "subtitle": "Sized to your usage", "icon": "cpu", "tone": "success"},
            {"title": "One platform license", "icon": "badge-check", "variant": "result"},
        ],
    },
    "config_cards": {
        "type": "config_cards",
        "heading": "Example configurations",
        "cards": [
            {
                "title": "Threat intelligence",
                "subtitle": "Example configuration",
                "rows": [
                    {"logo_file_id": "LOGO_FILE_ID", "label": "Module A", "chip": "Enterprise Edition"},
                    {"logo_file_id": "LOGO_FILE_ID", "label": "Module B", "chip": "Included", "chip_tone": "success"},
                    {
                        "logo_file_id": "LOGO_FILE_ID",
                        "label": "Module C",
                        "chip": "Available, not used",
                        "state": "dimmed",
                    },
                ],
                "footer": "Agentic capacity",
                "footer_value": "Included allowance",
            },
            {
                "title": "Full platform",
                "subtitle": "Example configuration",
                "rows": [
                    {"logo_file_id": "LOGO_FILE_ID", "label": "Module A", "chip": "Enterprise Edition"},
                    {"logo_file_id": "LOGO_FILE_ID", "label": "Module B", "chip": "Enterprise Edition"},
                    {"logo_file_id": "LOGO_FILE_ID", "label": "Module C", "chip": "Enterprise Edition"},
                ],
                "footer": "Agentic capacity",
                "footer_value": "+ packs",
            },
        ],
    },
    "diagram": {
        "type": "diagram",
        "source": "flowchart LR\n  Detect --> Triage --> Respond --> Learn",
        "caption": "The response loop",
    },
    "architecture": {
        "type": "architecture",
        "nodes": [
            {
                "id": "hub",
                "label": "XTM Hub",
                "icon": "globe",
                "chips": ["Trials", "Community"],
                "row": 1,
                "col": 1,
                "col_span": 3,
            },
            {"id": "cti", "label": "OpenCTI", "icon": "brain", "color": "brand_2", "row": 2, "col": 2},
            {
                "id": "core",
                "label": "XTM One",
                "sublabel": "Agentic core",
                "icon": "sparkles",
                "shape": "circle",
                "emphasis": "core",
                "color": "brand_1",
                "row": 3,
                "col": 2,
            },
            {"id": "crq", "label": "OpenCRQ", "icon": "chart-line", "color": "brand_4", "row": 4, "col": 1},
            {"id": "aev", "label": "OpenAEV", "icon": "crosshair", "color": "brand_3", "row": 4, "col": 3},
        ],
        "edges": [
            {"source": "hub", "target": "cti", "label": "Onboarding"},
            {"source": "cti", "target": "crq", "label": "Threat landscape"},
            {"source": "cti", "target": "aev", "label": "Threat data"},
            {"source": "aev", "target": "cti", "label": "Security posture"},
            {"source": "core", "target": "cti", "arrows": "none", "tone": "muted"},
            {"source": "core", "target": "crq", "arrows": "none", "tone": "muted"},
            {"source": "core", "target": "aev", "arrows": "none", "tone": "muted"},
        ],
        "zones": [{"label": "XTM Platform", "icon": "layers", "row": 2, "col": 1, "row_span": 3, "col_span": 3}],
        "legend": [
            {"label": "Data flow", "marker": "arrow"},
            {"label": "Runs on the core", "marker": "line", "tone": "muted"},
        ],
        "caption": "Every module runs on the core; the flows are what the modules exchange",
    },
    "org_chart": {
        "type": "org_chart",
        "nodes": [
            {"id": "ceo", "kind": "person", "name": "Alice Martin", "role": "Chief Executive Officer"},
            {"id": "cto", "kind": "person", "name": "Bob Stone", "role": "Chief Technology Officer", "manager": "ceo"},
            {"id": "cfo", "kind": "person", "name": "Carol Diaz", "role": "Chief Financial Officer", "manager": "ceo"},
            {"id": "cro", "kind": "vacant", "role": "Chief Revenue Officer", "period": "Q3", "manager": "ceo"},
        ],
    },
    "positioning_map": {
        "type": "positioning_map",
        "x_axis": {"title": "Coverage", "low_label": "Single stage", "high_label": "Whole loop"},
        "y_axis": {"title": "Openness", "low_label": "Closed", "high_label": "Open"},
        "quadrants": {
            "top_left": {"label": "Open tools"},
            "top_right": {"label": "Open platform", "tone": "accent"},
            "bottom_left": {"label": "Closed tools"},
            "bottom_right": {"label": "Closed suites"},
        },
        "items": [
            {"label": "Acme", "x": 90, "y": 92, "emphasis": "highlight", "logo_file_id": "LOGO_FILE_ID"},
            {"label": "Globex", "x": 70, "y": 25, "note": "Broad suite, closed API"},
            {"label": "Initech", "x": 20, "y": 70},
        ],
        "callouts": [{"title": "Whole loop", "text": "One platform from intel to validation"}],
    },
    "poll": {
        "type": "poll",
        "id": "p1",
        "questions": [
            {"id": "q1", "kind": "quiz", "prompt": "2+2?", "options": ["3", "4"], "correct": 1},
        ],
    },
    "columns": {
        "type": "columns",
        "ratio": "wide_left",
        "columns": [
            [{"type": "list", "items": ["Fewer tools", "One workflow"]}],
            [{"type": "callout", "tone": "warning", "title": "Risk", "content": "Migration window"}],
        ],
    },
    "spacer": {"type": "spacer", "grow": True},
    "divider": {"type": "divider", "label": "Then"},
}
assert tuple(BLOCK_EXAMPLES) == BLOCK_TYPES, (  # noqa: S101 - import-time contract
    set(BLOCK_EXAMPLES) ^ set(BLOCK_TYPES)
)

#: Two units on one figure: a count against the left-hand axis, a
#: price against the right-hand one. Validated like the block examples, and
#: rendered by the frontend's chart catalog (``blocks/chart-catalog.json``).
DUAL_AXIS_CHART_EXAMPLE: dict[str, Any] = {
    "type": "chart",
    "kind": "combo",
    "categories": ["Q4 25", "Q1 26", "Q2 26", "Q3 26"],
    "series": [
        {"name": "New logos", "kind": "bar", "values": [24, 34, 42, 56]},
        {"name": "ASP", "kind": "line", "axis": "secondary", "values": [240000, 330000, 420000, 540000]},
    ],
    "secondary_format": "currency",
    "secondary_unit": "EUR",
    "caption": "New logos and average sales price",
}

LAYOUT_EXAMPLES: dict[str, dict[str, Any]] = {
    "title": {
        "layout": "title",
        "title": "One platform, **zero** silos",
        "subtitle": "How we consolidated detection and response",
        "event_label": "All Hands - Sept 2026",
        "speakers": [{"name": "Ada Lovelace", "title": "Head of Security Engineering"}],
        "icon_strip": ["shield-check", "radar", "zap"],
    },
    "section": {
        "layout": "section",
        "number": 2,
        "label": "Part 2",
        "title": "What changed",
        "icon": "git-branch",
    },
    "agenda": {
        "layout": "agenda",
        "items": ["Where we were", "What changed", "What is next"],
        "current": 2,
    },
    "content": {
        "layout": "content",
        "title": "Alerts fell 42% after consolidation",
        "columns": [
            [{"type": "metric", "value": "42%", "label": "Fewer alerts", "trend": "down"}],
            [
                {
                    "type": "list",
                    "style": "check",
                    "items": ["One console", "Auto-triage", "Shared context"],
                }
            ],
        ],
        "ratio": "wide_right",
        "vertical_align": "center",
        "notes": "Pause on the number; the list is the how.",
    },
    "roadmap": {
        "layout": "roadmap",
        "title": "Three phases to full coverage",
        "phases": [
            {"title": "Pilot", "subtitle": "Q1", "items": ["3 teams"], "tone": "success"},
            {"title": "Rollout", "subtitle": "Q2", "items": ["All SOC tiers"], "tone": "accent"},
            {"title": "Scale", "subtitle": "Q3", "items": ["Partners"], "tone": "muted"},
        ],
    },
    "takeaways": {
        "layout": "takeaways",
        "items": [
            {"title": "Start with the threat", "description": "Not with the tool."},
            {"title": "One platform", "description": "Fewer consoles, faster response."},
            {"title": "Measure time to respond", "description": "Not alert volume."},
        ],
    },
    "dashboard": {
        "layout": "dashboard",
        "title": "OpenCTI adoption keeps compounding",
        "tiles": [
            {"value": "6,250", "label": "Active instances", "delta": "+9%", "trend": "up", "sparkline": [4100, 4800, 5300, 5700, 6250]},
            {"value": "38%", "label": "On 6.x", "trend": "up"},
            {"value": "112", "label": "EE trials", "delta": "+21", "trend": "up", "tone": "success"},
        ],
        "panels": [
            {
                "title": "Active instances per quarter",
                "span": 2,
                "block": {
                    "type": "chart",
                    "kind": "line",
                    "categories": ["Q1", "Q2", "Q3", "Q4"],
                    "series": [{"name": "Instances", "values": [4800, 5300, 5700, 6250]}],
                },
            },
            {
                "title": "EE trials",
                "block": {
                    "type": "chart",
                    "kind": "bar",
                    "categories": ["Q1", "Q2", "Q3", "Q4"],
                    "series": [{"name": "Trials", "values": [64, 78, 91, 112]}],
                    "show_legend": False,
                },
            },
        ],
        "notes": "The tiles are the headline; the panels are the evidence.",
    },
    "closing": {
        "layout": "closing",
        "title": "Thank you",
        "speaker": {"name": "Ada Lovelace", "title": "Head of Security Engineering"},
        "contacts": [
            {"kind": "email", "value": "ada@example.com"},
            {"kind": "web", "value": "example.com"},
        ],
        "tagline": "Questions?",
    },
}
assert tuple(LAYOUT_EXAMPLES) == SLIDE_LAYOUTS, (  # noqa: S101 - import-time contract
    set(LAYOUT_EXAMPLES) ^ set(SLIDE_LAYOUTS)
)

#: The product showcase of a keynote: a screenshot in a browser window
#: on a brand-gradient glow, taking the body height of the wide column at the
#: picture's ratio. Validated like the block examples; the composition the
#: design skill teaches as 3.21.
SHOWCASE_IMAGE_EXAMPLE: dict[str, Any] = {
    "type": "image",
    "file_id": "SCREENSHOT_FILE_ID",
    "alt": "The investigation workspace",
    "frame": "browser",
    "fill": True,
}

#: The bridge of a board revenue slide: opening ARR, the changes, the
#: ending ARR - each total the sum of the steps before it, which the validator
#: checks. Validated by the test suite like the block examples.
WATERFALL_CHART_EXAMPLE: dict[str, Any] = {
    "type": "chart",
    "kind": "waterfall",
    "categories": ["Opening ARR", "New logos", "Expansion", "Churn", "Closing ARR"],
    "series": [{"name": "ARR", "values": [16500000, 1800000, 1300000, -400000, 19200000]}],
    "totals": [0, 4],
    "format": "currency",
    "caption": "ARR bridge, quarter over quarter",
}

#: Two populations rung by rung: individual contributors against
#: managers per seniority level, the first drawn leftwards from the spine, the
#: second rightwards, both written as positive sizes. Validated by the test
#: suite like the block examples.
BUTTERFLY_CHART_EXAMPLE: dict[str, Any] = {
    "type": "chart",
    "kind": "butterfly",
    "categories": ["Executive", "Director", "Manager", "Lead", "Senior", "Mid", "Entry"],
    "series": [
        {"name": "Contributors", "values": [0, 2, 10, 40, 80, 30, 15]},
        {"name": "Managers", "values": [5, 20, 18, 6, 0, 0, 0]},
    ],
    "show_values": True,
}

#: A figure by country: a world choropleth of one series, its
#: categories ISO 3166-1 alpha-2 codes. Validated by the test suite like the
#: block examples, and drawn by the vitest chart catalog.
MAP_CHART_EXAMPLE: dict[str, Any] = {
    "type": "chart",
    "kind": "map",
    "categories": ["US", "FR", "DE", "GB", "JP", "BR", "IN", "AU"],
    "series": [{"name": "Customers", "values": [42, 31, 18, 15, 9, 7, 6, 5]}],
    "caption": "Customers by country, end of quarter",
}


#: A leadership team: one person over their direct reports, an open seat among
#: them. The minimal org chart, and the block example above.
ORG_CHART_LEADERSHIP_EXAMPLE: dict[str, Any] = BLOCK_EXAMPLES["org_chart"]

#: A headcount plan: the leads as cards over their teams as bubbles (each
#: circle's area its headcount), the planned hires dated, the roll-up written
#: as today against the target. Validated by the test suite like the block
#: examples.
ORG_CHART_HEADCOUNT_EXAMPLE: dict[str, Any] = {
    "type": "org_chart",
    "style": "hybrid",
    "show_counts": True,
    "count_mode": "both",
    "nodes": [
        {"id": "vp", "kind": "person", "name": "Alice Martin", "role": "VP Engineering"},
        {"id": "plat", "kind": "person", "name": "Bob Stone", "role": "Platform lead", "manager": "vp"},
        {"id": "apps", "kind": "person", "name": "Carol Diaz", "role": "Apps lead", "manager": "vp"},
        {"id": "plat-team", "kind": "bubble", "role": "Platform", "count": 9, "manager": "plat"},
        {"id": "plat-hires", "kind": "planned", "role": "Platform engineers", "count": 4, "period": "H2 FY28", "manager": "plat"},
        {"id": "apps-team", "kind": "bubble", "role": "Apps", "count": 14, "manager": "apps"},
        {"id": "apps-open", "kind": "vacant", "role": "Mobile engineer", "manager": "apps"},
    ],
}


#: The architecture's bare forms, quoted by both deck tools: a zone headed by
#: the platform's mark and its row of chips, and a node drawn without its card.
ARCHITECTURE_BARE_HINT = (
    "An architecture zone takes a logo_file_id - the platform's brand mark in its top-left corner, "
    "its label then optional and drawn after the mark - and up to 6 chips, the platform's capabilities "
    "as one row of pills on that same row, in the zone's top-right corner (chips_align start puts them "
    "after the logo / label), taking no grid cell; a node takes frame: false to draw its "
    "logo or icon, label and chips bare, with no border, surface or glow (ghost only dims it), its "
    "edges meeting the content."
)

#: How an architecture edge is steered, quoted by both deck tools: the
#: sides it leaves and enters, the lanes of a pair, and a zone's border as an end.
ARCHITECTURE_EDGE_HINT = (
    "An architecture edge takes source_side / target_side (top, right, bottom, left) to leave and enter "
    "by that side, honoured even facing away, the route bending as needed; the edges of one pair of nodes "
    "take lane 1, 2 to run as parallel arrows at one height on both nodes (an exchange is level without "
    "lanes too); and an edge ends on a zone's border with target (or source) zone:ID, ID being the zone's "
    "id (set one on the zone), its label or its index - never an invisible anchor node."
)


def _compact(value: Any) -> str:
    """One-line JSON, the way the model should write it in a tool call."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def format_org_chart_guidance() -> str:
    """The ORG CHARTS paragraph both deck tools carry: the rule, the kinds and the two minimal examples."""
    return (
        f"ORG CHARTS: {ORG_CHART_HINT}. Each seat has a kind with one meaning: person (a real seat: "
        "name, role, photo_file_id or initials), vacant (open today, role only, period = target date), "
        "planned (a future hire: role, period, count 3 = one card, '+3' before its role), group (a team as one card: "
        "role = the team, count, name = its leader), bubble (a team as a circle whose AREA is its count), "
        "external (contractor, partner, agency); dotted_to draws a secondary manager dashed. The deck lays "
        "the tree out itself (one bus per manager, equal cards per level, leaves folded into a column when "
        "a level is too wide) and refuses to shrink it under a readable size: past "
        f"{ORG_CHART_READABLE_NODES} seats fold with collapse_below / depth_limit or draw the teams as "
        "bubbles. show_counts writes each "
        'manager\'s roll-up, count_mode "both" today against the target. A leadership team: '
        f"{_compact(ORG_CHART_LEADERSHIP_EXAMPLE)}. A headcount plan, leads over their teams as bubbles "
        f"and the planned hires: {_compact(ORG_CHART_HEADCOUNT_EXAMPLE)}."
    )


def format_block_examples() -> str:
    return "\n".join(f"- {block}: {_compact(example)}" for block, example in BLOCK_EXAMPLES.items())


def format_layout_examples() -> str:
    return "\n".join(f"- {layout}: {_compact(example)}" for layout, example in LAYOUT_EXAMPLES.items())


def format_tone_semantics() -> str:
    return "\n".join(f"- {tone}: {meaning}" for tone, meaning in TONE_SEMANTICS.items())


def format_soft_budgets() -> str:
    return "\n".join(f"- {rule}" for rule in soft_budgets())


#: The ``reference`` values ``get_presentation`` serves besides a layout,
#: block or type name.
REFERENCE_SECTIONS: tuple[str, ...] = ("all", "layouts", "blocks", "types", "examples")


@functools.cache
def build_presentation_reference_text() -> str:
    """The whole authoring catalogue: the model-derived reference plus the examples.

    It used to ride in the ``generate_presentation`` description and was sent
    on every generation of every turn (about 49k characters); it is now served
    on demand by ``get_presentation(reference=...)``.
    """
    return (
        f"{spec_reference()}\n\n"
        "LAYOUT EXAMPLES (one slide each, minimal):\n"
        f"{format_layout_examples()}\n\n"
        "BLOCK EXAMPLES (minimal; every field not shown is optional):\n"
        f"{format_block_examples()}"
    )


def build_presentation_reference(reference: str) -> str:
    """The part of the authoring catalogue *reference* names.

    ``all`` is the whole catalogue; ``layouts`` / ``blocks`` / ``types`` one
    section of the spec reference; ``examples`` the layout and block examples;
    any other value is a comma-separated list of layout, block or type names,
    each answered with its reference line and its example when it has one.
    """
    wanted = (reference or "").strip()
    key = wanted.lower()
    reference_text = spec_reference()
    layouts, blocks, types = reference_text.split("\n\n", 2)
    if key in ("", "all"):
        return build_presentation_reference_text()
    if key == "layouts":
        return layouts
    if key == "blocks":
        return blocks
    if key == "types":
        return types
    if key == "examples":
        return (
            "LAYOUT EXAMPLES (one slide each, minimal):\n"
            f"{format_layout_examples()}\n\n"
            "BLOCK EXAMPLES (minimal; every field not shown is optional):\n"
            f"{format_block_examples()}"
        )
    lines = {
        line[2:].split(":", 1)[0]: line
        for line in reference_text.splitlines()
        if line.startswith("- ")
    }
    found: list[str] = []
    unknown: list[str] = []
    for name in (part.strip() for part in wanted.split(",")):
        if not name:
            continue
        line = lines.get(name)
        if line is None:
            unknown.append(name)
            continue
        found.append(line)
        example = LAYOUT_EXAMPLES.get(name) if name in LAYOUT_EXAMPLES else BLOCK_EXAMPLES.get(name)
        if example is not None:
            found.append(f"  example: {_compact(example)}")
    if unknown:
        found.append(
            f"Not a layout, block or type name: {', '.join(unknown)}. Pass one of "
            f"{', '.join(REFERENCE_SECTIONS)}, or names from: layouts "
            f"{', '.join(SLIDE_LAYOUTS)}; blocks {', '.join(BLOCK_TYPES)}."
        )
    return "\n".join(found)


@functools.cache
def build_generate_presentation_description() -> str:
    """The ``generate_presentation`` tool description, derived from the models.

    Order matters: what the tool is for and when NOT to use it first (the
    routing decision), then the contract (reference + examples), then how to
    write well (tones, budgets), then how to edit in place. A model that reads
    only the first paragraph still routes correctly.

    Built once per process: it depends on the models and constants alone,
    and the deck tools are built on every turn.
    """
    return (
        "Create an interactive slide deck, or revise one in place, from a structured "
        "spec: title, theme, chrome and ordered slides, each with one layout from the "
        "catalog below. The deck renders inline in the chat and at /presentations/ID, "
        "exports to PDF / PPTX (export_presentation) and can be shared. Use it whenever "
        "the user asks for slides, a deck, a presentation, a pitch or a keynote - never "
        "generate_file (a .pptx from code is not interactive and cannot be revised) "
        "and never a wall of markdown.\n\n"
        "The result is a [[PRESENTATION:ID]] marker: put it on its own line in your "
        "reply exactly as returned; the chat draws the deck from it.\n\n"
        f"CATALOG: layouts {', '.join(SLIDE_LAYOUTS)}; blocks {', '.join(BLOCK_TYPES)}. "
        "Fields, bounds and a minimal example of each come on demand: "
        'get_presentation(reference="content,kpi_row,chart") answers the lines of the '
        'layouts, blocks or types you name, reference="all" the whole catalogue, '
        '"examples" every example. Read the ones you will write before the first '
        "generate_presentation; a validation error names the slide, "
        "field and allowed keys.\n\n"
        "LAYOUT EXAMPLES (one slide each, minimal):\n"
        f"{format_layout_examples()}\n\n"
        f"INLINE MARKUP in every markup field: {MARKUP_SUMMARY}. No HTML and no markdown "
        "headings / lists inside a string - use a heading / list block.\n\n"
        "TEMPLATE AND THEME: the template is the deck's DESIGN (palette, fonts, logos, "
        "predefined assets) and decides which themes it allows; the theme is only the "
        "colour scheme, dark or light. The template is the user's choice, settled "
        "BEFORE the slides are written: a template they name is found with "
        "search_templates(kind=\"presentation\", name=...) and passed as template_id; "
        "when they name none and can pick from several, the tool creates nothing and "
        "returns the list - ask them which one (or none) in one short question, then "
        "generate. Say which template and theme the deck got; both can be changed "
        "later (edit_presentation template_id, or the deck's Design panel).\n\n"
        "PREDEFINED ASSETS: a brand template ships the real architecture / product-"
        "flow diagrams, product screenshots, illustrations, certification badges, "
        "product logos and team photos as assets - get_template (the deck's "
        "template_id; no id = the platform default) lists them by role with what "
        "each depicts and its file_id; place one with an `image` block rather than "
        "drawing an approximation. The partner, integration and customer logos are "
        "assets of the same template (get_template role=\"partner_logo\" / "
        "\"integration_logo\" / \"customer_logo\", or query set to the vendor name) - place "
        "several at once with a `logo_grid` block, or a `logo_wall` when they are grouped "
        "by sector or category. A file_id is checked on save: an invented one is refused.\n\n"
        "SHOWCASE: a product screenshot that carries the slide is an `image` with frame "
        '"browser" (window bar), "browser_slim" (thin bar, for shots side by side) or '
        '"device" (bezel), on a glow in the brand '
        "gradient, hugging the picture at its ratio; in a wide_left slide's wide "
        "column, fill: true makes it the largest the body holds, e.g. "
        f"{_compact(SHOWCASE_IMAGE_EXAMPLE)} - never a drawn window placed "
        "as a picture. A problem and what resolves it is a `challenge_answer` block: two "
        "stacked cards of one width joined by an arrow (error over success), never two "
        "callouts and a spacer.\n\n"
        "PEOPLE: founders, speakers or a leadership team are a `profiles` block - a "
        "portrait (photo_file_id, a `people_photo` asset of the template), name, title "
        "and 1-5 one-line facts, each with a logo_file_id or an icon tile; one logo or "
        "one padding is then a one-field edit, never a card drawn in a browser and "
        "placed as an `image`.\n\n"
        "DIAGRAMS: a platform, ecosystem or data-flow figure is an `architecture` block "
        "(nodes on a small grid, a product logo via logo_file_id, labeled edges the deck "
        "routes itself, a zone per boundary, one core), never Mermaid: `diagram` is for a "
        f"flowchart or sequence only, and no diagram is ever rendered to an image. {ARCHITECTURE_BARE_HINT} "
        f"{ARCHITECTURE_EDGE_HINT}\n\n"
        f"{format_org_chart_guidance()}\n\n"
        "CHARTS: numbers over categories are a `chart` block (bar / stacked_bar / line / "
        "area / pie / donut / gauge / combo / waterfall / butterfly / map) - native, in the template's "
        "palette, with tooltips on screen and an editable chart in the PPTX - never a picture drawn "
        "with code and placed as an `image`. A figure by country is a `map`: its categories are "
        "ISO 3166-1 alpha-2 codes (FR, US, GB), one series, region europe / americas / apac to zoom; "
        "a code that is not one is refused with its index named, and the PPTX carries the map as a "
        "picture with its title, caption and legend as text - not an editable chart: "
        f"{_compact(MAP_CHART_EXAMPLE)}. legend_style classes trades the ramp for at most five flat "
        "classes listed beside the map (classes 2-7 forces the count), and markers false drops the dot "
        "over a country too small to see. Values are raw numbers; `format` says how "
        "they read (currency: 16500000 -> EUR 16.5M; percent: 86 -> 86%). A bridge - a "
        "level, what changed it, the level reached - is a `waterfall`: one series of "
        "changes, `totals` the levels drawn from zero; a total the steps do not add up "
        f"to is refused with the gap named: {_compact(WATERFALL_CHART_EXAMPLE)}. Two populations "
        "compared rung by rung (contributors and managers per level, a population pyramid) are a "
        "`butterfly`: exactly two series, the first drawn leftwards from a central spine and the "
        "second rightwards, every value a positive size (never signed - a negative is refused; a 0 "
        "is a rung that side lacks, drawn with no bar and no value), the categories the rungs top to "
        "bottom on the spine (spine left puts them on the left axis), show_totals each side's total "
        "under the legend; the PPTX is an editable clustered bar chart whose left series is stored "
        "negative under a 0;0; number format (no sign, no label on a 0), its categories on the left "
        f"axis: {_compact(BUTTERFLY_CHART_EXAMPLE)}. Two units on "
        "one figure (a count and a price, millions and a ratio): the second unit's "
        'series take axis "secondary" and secondary_format / secondary_unit say how the '
        "right-hand axis reads (secondary_target for a reference line on it), e.g. "
        f"{_compact(DUAL_AXIS_CHART_EXAMPLE)}. A KPI page - "
        "a tile row of metrics over a grid of charts and tables - is a `dashboard` slide, "
        "not a content slide of stacked charts; a tile's `sparkline` carries its trend.\n\n"
        'PORTRAIT PAGES: aspect_ratio "A4" or "letter" makes a printable DOCUMENT, not '
        "a slide show - a product brief, one-pager, datasheet or battle card - with the "
        "same layouts and blocks on portrait paper; the chat and the viewer show it as "
        "stacked pages to scroll, zoom and print (no slide transitions, no fullscreen "
        "presenting), so call it a page or a brief when you talk about it, never a slide. "
        "1-4 pages, each a `content` slide that "
        "opens with a `banner` (eyebrow + title + subtitle), stacks 4-7 blocks "
        "(`badges` for editions / certifications, `key_value` for a spec sheet, a "
        "`table` feature matrix with true / false cells, `feature_list`, "
        "`personas` for who it is for, `metrics_grid`, `pricing_tiers` for offers) "
        "and closes with a `banner` "
        'variant "solid" or a `callout` carrying the call to action; the contact '
        "line goes in the footer (chrome.footer_text, or a template footer variable in "
        'chrome.fields). Prefer theme "light" for a page that will be printed.\n\n'
        "TONES mean something; pick by meaning, never by colour:\n"
        f"{format_tone_semantics()}\n"
        f"{DELTA_TONE_RULE}\n\n"
        "DENSITY - a slide is read from the back of the room:\n"
        f"{format_soft_budgets()}\n\n"
        "EDITING an existing deck: use edit_presentation - it changes one slide, block "
        "or word in place and leaves every other slide byte-identical; read the slide "
        'you change with get_presentation(slides="12"). Pass presentation_id HERE only to '
        "rewrite a deck wholesale, with the COMPLETE spec (every slide, keeping each "
        "slide's id); without presentation_id a new deck is created. A validation error "
        "names the slide and field (slides.3 (layout=content): ...): fix that slide and "
        "resubmit instead of regenerating. "
        "Every save is followed by a LAYOUT CHECK of the rendered slides in the result "
        "(a body shrunk to fit, text cut off, a badge running past its box): fix each "
        "slide it names by cutting content (edit_presentation on that slide), save "
        "again, and only then answer."
    )


__all__ = [
    "BLOCK_EXAMPLES",
    "BUTTERFLY_CHART_EXAMPLE",
    "DELTA_TONE_RULE",
    "DUAL_AXIS_CHART_EXAMPLE",
    "LAYOUT_EXAMPLES",
    "ORG_CHART_HEADCOUNT_EXAMPLE",
    "ORG_CHART_LEADERSHIP_EXAMPLE",
    "REFERENCE_SECTIONS",
    "SHOWCASE_IMAGE_EXAMPLE",
    "TONE_SEMANTICS",
    "WATERFALL_CHART_EXAMPLE",
    "build_generate_presentation_description",
    "build_presentation_reference",
    "build_presentation_reference_text",
    "format_block_examples",
    "format_layout_examples",
    "format_org_chart_guidance",
    "format_soft_budgets",
    "format_tone_semantics",
    "soft_budgets",
]

# Import-time contract: the tone table covers exactly the Tone literal.
assert set(TONE_SEMANTICS) == set(typing.get_args(Tone)), (  # noqa: S101
    set(TONE_SEMANTICS) ^ set(typing.get_args(Tone))
)
