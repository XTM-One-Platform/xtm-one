"""The fleet's one-page datasheet (#9768 S4), without a database or a browser.

Pinned here: the status reads as the About hero says it; every list is capped
with "+N more"; a part the documentation does not hold reads as missing, never
guessed; every value is escaped; the mini graph is fixed and drops what it
cannot show; the renderer prints page 1 only, JavaScript off; the route is
licence-gated, not AI-gated, and its filename is safe.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.services import fleet_datasheet as sheet
from app.services.fleet_datasheet import GraphNode, PrerequisiteLine


def _doc(**overrides):
    base = {
        "fleet_name": "Alert Desk",
        "product_name": "XTM One",
        "sections": {"purpose": "Triage **alerts**."},
        "structured": {
            "purpose_line": "Triages every new alert.",
            "outcome_line": "Analysts see only what matters.",
            "serves": ["SOC analysts"],
            "members": [{"name": "Triage agent", "role": "Ranks each alert by impact"}],
            "triggers": [{"module": "opencti", "name": "New incident", "detail": "every created incident"}],
            "steps": [{"title": "Enrich", "detail": "Look up the observables"}, {"title": "Close", "gate": True}],
            "integrations": [{"member": "Triage agent", "integrations": ["OpenCTI"], "tool_groups": ["Fleets"]}],
            "objects": [{"module": "opencti", "objects": ["Note", "Report"]}],
            "quota": [{"trigger": "New incident", "runs_min": 10, "runs_max": 40}],
        },
        "version": 3,
        "status": "on",
        "automations_on": 2,
        "automations_total": 2,
        "nodes": [],
        "prerequisites": [PrerequisiteLine(label="OpenCTI connected", met=True)],
        "generated_on": datetime(2026, 11, 7, tzinfo=UTC),
    }
    return sheet.build_datasheet_html(**{**base, **overrides})


@pytest.mark.parametrize(
    ("total", "enabled", "paused", "expected"),
    [(0, 0, 0, "not_set_up"), (3, 0, 0, "not_set_up"), (3, 0, 2, "paused"), (3, 1, 0, "partially_on"), (3, 3, 0, "on")],
)
def test_the_status_reads_as_the_about_hero(total, enabled, paused, expected):
    assert sheet.datasheet_status(total, enabled, paused) == expected


def test_the_sheet_carries_every_part_of_the_documentation():
    page = _doc()
    for text in (
        "Alert Desk",
        "XTM One fleet datasheet",
        "Triages every new alert.",
        "SOC analysts",
        "Ranks each alert by impact",
        "New incident",
        "Approval gate",
        "Triage agent",
        "OpenCTI",
        "Note, Report",
        "10 to 40 runs a month (estimate)",
        "1 of 1</b>prerequisites met",
        "2 of 2",
        "Generated on 07 Nov 2026, version 3",
        "@page { size: A4; margin: 0; }",
    ):
        assert text in page, text


def test_no_prose_section_the_about_page_retired_is_printed():
    """#10123: the outcome and unit-of-work prose live in the ROI tab, not on the sheet."""
    page = _doc()
    assert "Analysts see only what matters." not in page
    assert "unit of work" not in page and "Outcome." not in page


def test_a_one_value_quota_reads_about_n():
    """#10123: never "1,238 to 1,238"."""
    one = sheet.quota_text({"quota": [{"runs_min": 1238, "runs_max": 1238}]})
    assert one == "about 1.2K runs a month (estimate)"
    assert sheet.quota_text({}) == sheet.MISSING


def test_member_cards_cap_the_tools_with_a_count():
    live = [{"member": "Hunter", "integrations": ["opencti", "openaev", "opencrq"], "tool_groups": ["web", "a_b", "c_d"]}]
    page = _doc(tool_uses=live)
    cards = page.split("Member cards</h2>")[1].split("</section>")[0]
    assert cards.count('class="card"') == 1
    assert f"+{6 - sheet.CAPS['card_tools']}" in cards


def test_no_work_item_key_or_cron_is_printed():
    triggers = [
        {"module": "work_item", "name": "Re-test due", "detail": "Work item key ctem:f1:retest:"},
        {"module": "schedule", "name": "Weekly sweep", "detail": "0 7 * * 1"},
        {"module": "opencti", "name": "New report", "detail": "Opened once the fix is deployed"},
    ]
    page = _doc(structured={"triggers": triggers})
    assert "ctem:" not in page
    assert "0 7 * * 1" not in page
    assert "Re-test due" in page and "Weekly sweep" in page
    assert "Opened once the fix is deployed" in page


def test_no_shipped_fleet_doc_prints_a_work_item_key():
    from app.agents import builtin_fleet_docs

    shipped = repr(vars(builtin_fleet_docs))
    assert "ctem:f1:" not in shipped and "ctem:f2:" not in shipped and "ctem:f3:" not in shipped


def test_a_part_the_documentation_does_not_hold_reads_as_missing():
    page = _doc(sections={}, structured={})
    assert page.count(sheet.MISSING) >= 7
    assert "runs a month" not in page


def test_every_list_is_capped_with_the_count_of_what_was_cut():
    triggers = [{"module": "platform", "name": f"Trigger {i}"} for i in range(9)]
    page = _doc(structured={"triggers": triggers})
    cap = sheet.CAPS["triggers"]
    assert f"Trigger {cap - 1}" in page and f"Trigger {cap}" not in page
    assert f"+{9 - sheet.CAPS['triggers']} more" in page


def test_every_shipped_fleet_names_all_its_triggers_on_the_sheet():
    # #1319: Platform Health ships 6 triggers, vulnerability prioritization 7; none may fold into "+N more".
    # The CTEM loop (one trigger per phase and module) stays folded: the sheet is one page, always.
    from app.agents import builtin_fleet_docs

    folded = {"CTEM_LOOP_STRUCTURED"}
    shipped = {
        name: value
        for name, value in vars(builtin_fleet_docs).items()
        if name.endswith("_STRUCTURED") and name not in folded and isinstance(value, dict) and value.get("triggers")
    }
    assert "PLATFORM_HEALTH_STRUCTURED" in shipped
    for name, structured in shipped.items():
        page = _doc(structured={"triggers": structured["triggers"]})
        assert "more</li>" not in page, name
    health = _doc(structured={"triggers": builtin_fleet_docs.PLATFORM_HEALTH_STRUCTURED["triggers"]})
    assert "Module checks" in health and "Memory review" in health


def test_every_value_is_escaped():
    page = _doc(fleet_name="<script>alert(1)</script>", structured={"purpose_line": "<img src=x onerror=1>"})
    assert "<script>alert" not in page and "<img src=x" not in page
    assert "&lt;script&gt;" in page


def test_a_long_text_is_cut_with_an_ellipsis():
    assert sheet.plain_text("word " * 200, 50).endswith("\u2026")
    assert len(sheet.plain_text("word " * 200, 50)) == 50
    assert sheet.plain_text("## Title\n- **bold** [link](http://x)") == "Title bold link"


def test_the_mini_graph_is_fixed_and_drops_what_it_cannot_show():
    root = uuid.uuid4()
    nodes = [GraphNode(agent_id=root, parent_agent_id=None, depth=0, name="Orchestrator", is_orchestrator=True)]
    nodes += [GraphNode(agent_id=uuid.uuid4(), parent_agent_id=root, depth=1, name=f"Member {i}") for i in range(12)]
    first = sheet.mini_graph_svg(nodes)
    assert first == sheet.mini_graph_svg(nodes), "same input, same picture"
    assert first.count("<rect") == sheet.CAPS["graph"]
    assert f"+{13 - sheet.CAPS['graph']} more" in first
    assert first.count("<path") == sheet.CAPS["graph"] - 1
    assert sheet.mini_graph_svg([]) == f'<p class="missing">{sheet.MISSING}</p>'


def test_every_graph_name_is_whole_and_inside_the_viewbox():
    """UX round 1 B2: no truncated label, no text past the svg edge."""
    import re

    root = uuid.uuid4()
    long_name = "Threat-informed Exposure Response Orchestrator"
    nodes = [GraphNode(agent_id=root, parent_agent_id=None, depth=0, name=long_name, is_orchestrator=True)]
    nodes += [
        GraphNode(
            agent_id=uuid.uuid4(), parent_agent_id=root, depth=1, name=f"Vulnerability Prioritization Analyst {i}"
        )
        for i in range(6)
    ]
    svg = sheet.mini_graph_svg(nodes)
    assert "\u2026" not in svg
    words = " ".join(re.findall(r"<tspan[^>]*>([^<]*)</tspan>", svg))
    for word in long_name.split():
        assert word in words, word
    width, height = (float(v) for v in re.search(r'viewBox="0 0 (\S+) (\S+)"', svg).groups())
    for x, y, w, h in re.findall(r'<rect x="([\d.]+)" y="([\d.]+)" width="([\d.]+)" height="([\d.]+)"', svg):
        assert float(x) >= 0 and float(x) + float(w) <= width
        assert float(y) >= 0 and float(y) + float(h) <= height
    assert all(len(line) <= sheet.NODE_LINE_CHARS for line in sheet.name_lines(long_name))
    assert " ".join(sheet.name_lines(long_name)) == long_name


def _cards(page: str) -> str:
    return page.split("Member cards</h2>")[1].split("</section>")[0]


def test_integrations_come_from_the_live_bindings():
    """UX round 1 B3: the members' live tool use, not only the documentation's list."""
    live = [{"member": "Hunter", "integrations": ["opencti", "openaev"], "tool_groups": ["web"]}]
    section = _cards(_doc(structured={}, tool_uses=live))
    assert "Hunter" in section and "OpenCTI" in section and "OpenAEV" in section and "web" in section
    assert sheet.MISSING not in section
    empty = _cards(_doc(structured={}, tool_uses=[{"member": "Idle", "integrations": []}]))
    assert "Idle" in empty and "no tools" in empty
    assert sheet.MISSING in _cards(_doc(structured={}, tool_uses=[]))


RAW_KEYS = ("generate_file", "shared_state", "work_items", "web_fetch", "mitre_atlas", "opencti")


def test_no_tool_group_or_provider_key_is_printed():
    """UX round 2 B1: catalog display names, never a raw key, never a key with its underscores eaten."""
    from app.api.v1.fleets import _datasheet_tool_labels

    live = [
        {"member": "Hunter", "integrations": ["mitre_atlas", "opencti"], "tool_groups": list(RAW_KEYS[:4])},
        {"member": "Scribe", "tool_groups": ["some_future_group"]},
    ]
    page = _doc(structured={}, tool_uses=live, tool_labels=_datasheet_tool_labels())
    section = _cards(page)
    for key in (*RAW_KEYS[:5], "some_future_group"):
        assert key not in section
        assert key.replace("_", "") not in section.lower()
    # The card shows CAPS["card_tools"] names, the rest as +N.
    for label in ("MITRE ATLAS", "OpenCTI", "Generate file", "Shared state"):
        assert label in section
    assert "Some future group" in section


def test_a_key_without_labels_is_humanized():
    assert sheet.tool_label("web_fetch") == "Web fetch"
    assert sheet.tool_label("openaev") == "OpenAEV"
    assert sheet.tool_label("Threat Intel MCP") == "Threat Intel MCP"
    assert sheet.tool_label("generate_file", {"generate_file": "Generate file"}) == "Generate file"


@pytest.mark.parametrize(
    ("updated_by", "restored", "expected"),
    [
        ("seed", None, "Written by Filigran, shipped with the platform"),
        ("fork", None, "Copied from the original fleet when it was duplicated"),
        ("3f1c", None, "Generated by AI from the fleet's configuration"),
        (None, None, "Generated by AI from the fleet's configuration"),
        ("3f1c", {"version": None, "author": "seed"}, "Restored from the shipped version, written by Filigran"),
        ("3f1c", {"version": 2, "author": "ai"}, "Restored from version 2, generated by AI"),
    ],
)
def test_the_footer_names_the_author_as_the_about_page(updated_by, restored, expected):
    """UX round 1 B4: a shipped seed never reads as generated by AI."""
    author = sheet.datasheet_author(updated_by, restored)
    assert author == expected
    assert f"<span>{expected.replace(chr(39), '&#x27;')}</span></footer>" in _doc(author=author)


def test_the_filename_is_safe():
    assert sheet.datasheet_filename("AI security / CTEM: v2") == "ai-security-ctem-v2-datasheet.pdf"
    assert sheet.datasheet_filename("") == "fleet-datasheet.pdf"


@pytest.mark.asyncio
async def test_the_renderer_prints_page_one_only_with_javascript_off(monkeypatch):
    import playwright.async_api as pw_api

    from app.services import pdf_render

    seen: dict = {}

    class _Page:
        async def set_content(self, html, wait_until=None):
            seen["html"] = html

        async def pdf(self, **kwargs):
            seen["pdf"] = kwargs
            return b"%PDF-1.4\n%%EOF"

    class _Context:
        async def route(self, _pattern, _handler):
            return None

        async def new_page(self):
            return _Page()

        async def close(self):
            return None

    class _Browser:
        async def new_context(self, **kwargs):
            seen["context"] = kwargs
            return _Context()

        async def close(self):
            return None

    class _Chromium:
        async def launch(self, **kwargs):
            return _Browser()

    class _Playwright:
        chromium = _Chromium()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(pw_api, "async_playwright", lambda: _Playwright())
    document = _doc()
    pdf = await pdf_render.render_document_to_pdf(document, first_page_only=True)
    assert pdf.startswith(b"%PDF-")
    assert seen["html"] == document, "a whole document is not wrapped"
    assert seen["pdf"]["page_ranges"] == "1"
    assert seen["pdf"]["margin"]["top"] == "0"
    assert seen["context"]["java_script_enabled"] is False

    await pdf_render.render_text_to_pdf("# Report", title="doc")
    assert seen["pdf"]["page_ranges"] == "", "the text renderer still prints every page"
    assert seen["pdf"]["margin"]["top"] == pdf_render.PDF_RENDER_MARGIN


def test_the_route_is_licence_gated_not_ai_gated():
    import inspect

    from app.api.v1 import fleets

    source = inspect.getsource(fleets.export_fleet_datasheet_endpoint)
    assert "require_agentic_ai_licensed" in source
    assert "require_ai_enabled" not in source
    assert "render_document_to_pdf(document, first_page_only=True)" in source
