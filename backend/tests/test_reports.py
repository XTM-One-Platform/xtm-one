"""Tests for agent-generated executive reports.

These lock in the contract:

1. A valid ``ReportSpec`` is persisted and the tool returns a ``[[REPORT:<id>]]``
   marker the UI renders as a printable document.
2. Markup never escapes ``rich_text``: a fragment carrying scripts / event
   handlers / iframes / ``<style>`` never reaches storage (DOMPurify is the
   render-time layer; this is the generation-time one), and no other field can
   carry a document.
3. Revising a report rewrites the SAME row and bumps ``revision`` — the marker
   already in the conversation stays valid, so the frontend reconciles the block
   list instead of mounting a second document.
4. A malformed spec fails with a field-addressed error and writes nothing.
5. A template can restyle a report through a WIDER palette whitelist than a deck
   without gaining any way to author CSS — and widening it must not widen the
   deck's.

No DB required: the tool opens its own short-lived session, which is
monkeypatched with a capturing fake — same no-DB style as
``test_presentations.py``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.services.report_spec import (
    MAX_BLOCKS,
    REPORT_BLOCKS,
    REPORT_THEMES,
    ReportSpecError,
    block_catalog,
    validate_spec,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = BACKEND_ROOT.parent / "frontend"


# ── Fakes ────────────────────────────────────────────────────────────────────


class _Result:
    def __init__(self, one=None, rows=None):
        self._one = one
        self._rows = rows or []

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._one


class _FakeSession:
    """Async-context session capturing added rows and executed statements."""

    def __init__(self, results=None):
        self._results = list(results or [])
        self.statements: list = []
        self.added: list = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement, *a, **k):
        self.statements.append(statement)
        return self._results.pop(0) if self._results else _Result()

    async def commit(self):
        self.committed = True

    async def flush(self):
        return None

    def add(self, obj):
        self.added.append(obj)


def _session_factory(session: _FakeSession):
    def _factory():
        return session

    return _factory


def _header(title: str = "Brief") -> dict:
    return {
        "title": title,
        "eyebrow": "Cyber Threat Intelligence",
        "subtitle": "What happened and what to decide.",
        "tags": ["As of 1 August 2026", "TLP:CLEAR"],
        "verdict": {"grade": "HIGH THREAT", "label": "Verdict"},
    }


def _blocks(count: int = 2) -> list[dict]:
    return [
        {"block": "prose", "title": f"Section {i}", "lead": f"<p>Body {i}</p>"}
        for i in range(count)
    ]


#: One minimal-but-valid instance of every block in the catalog. Parametrized
#: below, so a new block added to ``REPORT_BLOCKS`` without an entry here fails
#: the coverage assertion rather than shipping untested.
BLOCK_SAMPLES: dict[str, dict] = {
    "kpi_row": {
        "block": "kpi_row",
        "items": [{"value": "≈ 74k", "label": "devices", "note": "counts vary"}],
    },
    "prose": {
        "block": "prose",
        "lead": "<p>The lead.</p>",
        "rich_text": "<p>The body.</p>",
        "alert": {"label": "Governance:", "rich_text": "<p>Patching is not enough.</p>"},
    },
    "decisions": {
        "block": "decisions",
        "items": [{"title": "Trigger an audit", "text": "Inventory everything."}],
    },
    "chain": {
        "block": "chain",
        "steps": [{"title": "Exposed surfaces", "text": "Admin on the internet."}],
        "note": "Aggregated from public reporting.",
    },
    "assessment_table": {
        "block": "assessment_table",
        "rows": [
            {
                "statement": "The campaign happened.",
                "confidence": "high",
                "confidence_label": "HIGH",
            }
        ],
    },
    "scenarios": {
        "block": "scenarios",
        "items": [
            {"title": "Defence control", "text": "Rules changed.", "impact": "Detection"}
        ],
    },
    "risk_table": {
        "block": "risk_table",
        "rows": [
            {
                "situation": "Admin exposed, no MFA.",
                "level": "critical",
                "posture": "Assume compromise.",
                "level_label": "CRITICAL",
            }
        ],
    },
    "matrix": {
        "block": "matrix",
        "axis_label": "Profile / device",
        "column_labels": ["Cleaned personal", "Travel phone"],
        "rows": [
            {
                "label": "Entry to Russia",
                "cells": [
                    {"label": "Moderate", "level": "moderate"},
                    {"label": "Manageable", "level": "controlled"},
                ],
            },
            {
                "label": "Occupied territory",
                "cells": [
                    {"label": "High", "level": "elevated"},
                    {"label": "Unacceptable", "level": "critical"},
                ],
            },
        ],
        "note": "The level depends on the data carried, not the job title.",
    },
    "callout": {
        "block": "callout",
        "title": "Recommended position",
        "rich_text": "<p>Treat as an identity incident.</p>",
        "score": "4/5",
        "score_label": "Conditional generic risk",
    },
    "timeline": {
        "block": "timeline",
        "phases": [
            {
                "label": "0-4 hours",
                "headline": "Close the window.",
                "items": ["Rotate every secret."],
            }
        ],
    },
    "checklist": {
        "block": "checklist",
        "items": ["How many devices are operated?"],
        "columns": 2,
    },
    "fill_in_box": {
        "block": "fill_in_box",
        "title": "To complete",
        # No ``value``: the blank rule is the point of the block.
        "fields": [{"label": "Number of devices"}, {"label": "MFA", "value": "100%"}],
        "notes": ["Status: NOT ASSESSED"],
    },
    "annex": {
        "block": "annex",
        "summary": "Indicators to hunt for",
        "items": ["Admin login from an unusual ASN."],
        "table": {
            "headers": ["Technique", "Use"],
            "rows": [["T1110 — Brute Force", "Password spraying."]],
        },
        "note": "There is no stable universal IOC.",
        "open": True,
    },
    "sources": {
        "block": "sources",
        "items": [
            {
                "label": "Fortinet PSIRT advisory",
                "url": "https://www.fortinet.com/blog/psirt-blogs/advisory",
                "note": "19 June 2026",
            },
            {"label": "Internal telemetry review"},
        ],
        "intro": "Cross-checked public sources.",
        "limits": "<p><strong>Limits:</strong> no internal data.</p>",
    },
}


def _build_tools(user_id: str | None = None, conversation_id: str | None = None):
    from app.services.builtin_tools import build_report_tools

    return build_report_tools(
        db=None,  # type: ignore[arg-type]
        user_id=user_id or str(uuid.uuid4()),
        conversation_id=conversation_id,
    )


def _run_tool(executor, name: str, arguments: dict):
    from app.tools.base import ToolCall

    return asyncio.run(
        executor.execute_tool(ToolCall(id="call-1", name=name, arguments=arguments))
    )


def _compiled(query) -> str:
    return str(
        query.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


# ── The spec contract ────────────────────────────────────────────────────────


def test_valid_spec_normalizes_and_fills_defaults():
    spec = validate_spec(
        {"title": "Brief", "header": _header(), "blocks": _blocks(2)}
    )
    # ``light`` by default: a report's primary output is a printed document.
    assert spec["theme"] == "light"
    assert spec["title"] == "Brief"
    assert len(spec["blocks"]) == 2
    assert spec["header"]["title"] == "Brief"
    # ``exclude_none`` keeps the stored spec free of null noise.
    assert "footer" not in spec


def test_block_samples_cover_the_whole_catalog():
    """A block added to the catalog without a sample here would otherwise be
    silently exempt from every test below."""
    assert set(BLOCK_SAMPLES) == set(REPORT_BLOCKS)


@pytest.mark.parametrize("block_id", REPORT_BLOCKS)
def test_every_catalog_block_validates(block_id):
    spec = validate_spec(
        {"title": "B", "header": _header(), "blocks": [BLOCK_SAMPLES[block_id]]}
    )
    assert spec["blocks"][0]["block"] == block_id


def test_block_catalog_matches_the_documented_set():
    """``block_catalog()`` builds the tool description, so it must describe every
    block and nothing but."""
    assert set(block_catalog()) == set(REPORT_BLOCKS)
    # Every advertised field must really exist on the model — a field documented
    # but rejected is the failure mode this guards.
    for block_id, fields in block_catalog().items():
        assert "title" in fields, block_id
        assert "block" not in fields


@pytest.mark.parametrize(
    ("payload", "expected_fragment"),
    [
        ({"header": _header(), "blocks": _blocks(1)}, "title"),
        ({"title": "B", "blocks": _blocks(1)}, "header"),
        ({"title": "B", "header": _header()}, "blocks"),
        ({"title": "B", "header": _header(), "blocks": []}, "blocks"),
        (
            {"title": "B", "header": _header(), "blocks": [{"block": "nope"}]},
            "block",
        ),
        (
            {
                "title": "B",
                "header": _header(),
                "blocks": [{"block": "prose", "invented": "x"}],
            },
            "invented",
        ),
        (
            {
                "title": "B",
                "header": _header(),
                "blocks": [
                    {
                        "block": "assessment_table",
                        "rows": [{"statement": "s", "confidence": "certain"}],
                    }
                ],
            },
            "confidence",
        ),
        (
            {
                "title": "B",
                "header": _header(),
                "blocks": [
                    {
                        "block": "risk_table",
                        "rows": [{"situation": "s", "level": "very bad"}],
                    }
                ],
            },
            "level",
        ),
    ],
)
def test_invalid_spec_reports_the_offending_field(payload, expected_fragment):
    with pytest.raises(ReportSpecError) as exc:
        validate_spec(payload)
    assert expected_fragment in str(exc.value)
    # The message must be actionable, not a JSON dump of Pydantic error objects.
    assert "call the tool again" in str(exc.value)


def test_block_cap_is_enforced():
    with pytest.raises(ReportSpecError) as exc:
        validate_spec(
            {
                "title": "B",
                "header": _header(),
                "blocks": _blocks(MAX_BLOCKS + 1),
            }
        )
    assert "blocks" in str(exc.value)


def test_error_message_names_the_block_of_the_failing_block():
    """Pydantic puts the resolved union member in the path
    (``blocks.1.timeline.phases``), which reads like a field called "timeline".
    It is moved into an explicit ``(block=…)`` note instead."""
    with pytest.raises(ReportSpecError) as exc:
        validate_spec(
            {
                "title": "B",
                "header": _header(),
                "blocks": [
                    BLOCK_SAMPLES["prose"],
                    {"block": "timeline", "phases": []},
                ],
            }
        )
    message = str(exc.value)
    assert "(block=timeline)" in message
    assert "blocks.1.phases" in message


def test_non_dict_payload_is_refused():
    with pytest.raises(ReportSpecError):
        validate_spec(["not", "a", "spec"])


def test_every_theme_in_the_catalog_is_accepted():
    for theme in REPORT_THEMES:
        spec = validate_spec(
            {
                "title": "B",
                "theme": theme,
                "header": _header(),
                "blocks": _blocks(1),
            }
        )
        assert spec["theme"] == theme


# ── Markup containment ───────────────────────────────────────────────────────


DANGEROUS = [
    "<script>alert(1)</script>",
    '<p onclick="steal()">x</p>',
    '<img src=x onerror="steal()">',
    '<iframe src="https://evil.example"></iframe>',
    '<form action="https://evil.example"><input name="p"></form>',
    '<a href="javascript:alert(1)">x</a>',
    '<object data="x"></object>',
    '<p srcdoc="<script>x</script>">y</p>',
    "<style>body{display:none}</style>",
]


@pytest.mark.parametrize("rich_text", DANGEROUS)
def test_dangerous_rich_text_is_rejected(rich_text):
    with pytest.raises(ReportSpecError) as exc:
        validate_spec(
            {
                "title": "B",
                "header": _header(),
                "blocks": [{"block": "prose", "rich_text": rich_text}],
            }
        )
    assert "rich_text" in str(exc.value)


@pytest.mark.parametrize(
    "payload_builder",
    [
        # Every rich_text-bearing position in the spec, not just the obvious one:
        # a guard applied to some fields and not others is the classic gap.
        lambda bad: {"block": "prose", "lead": bad},
        lambda bad: {"block": "prose", "rich_text": bad},
        lambda bad: {"block": "prose", "alert": {"rich_text": bad}},
        lambda bad: {"block": "callout", "rich_text": bad},
        lambda bad: {
            "block": "decisions",
            "items": [{"title": "t", "rich_text": bad}],
        },
        lambda bad: {"block": "annex", "summary": "s", "rich_text": bad},
        lambda bad: {
            "block": "sources",
            "items": [{"label": "l"}],
            "limits": bad,
        },
    ],
)
def test_every_rich_text_position_is_guarded(payload_builder):
    with pytest.raises(ReportSpecError):
        validate_spec(
            {
                "title": "B",
                "header": _header(),
                "blocks": [payload_builder("<script>alert(1)</script>")],
            }
        )


def test_header_verdict_rich_text_is_guarded():
    header = _header()
    header["verdict"]["rich_text"] = "<script>alert(1)</script>"
    with pytest.raises(ReportSpecError):
        validate_spec({"title": "B", "header": header, "blocks": _blocks(1)})


def test_safe_paragraph_markup_is_accepted():
    html = (
        "<h3>Findings</h3><p>Two <strong>critical</strong> issues, see "
        '<a href="https://example.org">the report</a>.</p>'
        "<ul><li>one</li><li>two</li></ul>"
    )
    spec = validate_spec(
        {
            "title": "B",
            "header": _header(),
            "blocks": [{"block": "prose", "rich_text": html}],
        }
    )
    assert spec["blocks"][0]["rich_text"] == html


def test_rich_text_is_the_only_html_field_in_the_spec():
    """No field may carry a whole HTML document, a stylesheet or an iframe. If a
    future change adds one, this test is the tripwire.

    ``SourceItem.url`` is the one deliberate exception and is asserted
    explicitly: it is a bibliography link, validated to be plain http(s) by
    ``test_source_url_must_be_http``."""
    from app.services import report_spec as mod

    models = [
        getattr(mod, name)
        for name in dir(mod)
        if isinstance(getattr(mod, name), type)
        and issubclass(getattr(mod, name), mod.BaseModel)
        and getattr(mod, name) is not mod.BaseModel
    ]
    suspicious = {
        f"{model.__name__}.{name}"
        for model in models
        for name in model.model_fields
        if any(bad in name for bad in ("html", "iframe", "script", "style", "css"))
    }
    assert suspicious == set()

    url_fields = {
        f"{model.__name__}.{name}"
        for model in models
        for name in model.model_fields
        if "url" in name
    }
    assert url_fields == {"SourceItem.url"}


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,<script>x</script>",
        "/relative/path",
        "ftp://example.org/file",
        "not a url",
    ],
)
def test_source_url_must_be_http(url):
    with pytest.raises(ReportSpecError) as exc:
        validate_spec(
            {
                "title": "B",
                "header": _header(),
                "blocks": [
                    {"block": "sources", "items": [{"label": "l", "url": url}]}
                ],
            }
        )
    assert "url" in str(exc.value)


def test_source_without_a_url_is_allowed():
    """An unlinked reference (an internal review, a paper) is a normal citation."""
    spec = validate_spec(
        {
            "title": "B",
            "header": _header(),
            "blocks": [{"block": "sources", "items": [{"label": "Internal review"}]}],
        }
    )
    assert spec["blocks"][0]["items"][0]["label"] == "Internal review"


def test_ragged_annex_table_is_refused():
    """A row that does not match the header width renders as a broken table — an
    error the agent can fix beats a visual defect the reader has to interpret."""
    with pytest.raises(ReportSpecError) as exc:
        validate_spec(
            {
                "title": "B",
                "header": _header(),
                "blocks": [
                    {
                        "block": "annex",
                        "summary": "s",
                        "table": {
                            "headers": ["A", "B"],
                            "rows": [["only one cell"]],
                        },
                    }
                ],
            }
        )
    assert "header" in str(exc.value)


def test_annex_table_accepts_blank_cells():
    spec = validate_spec(
        {
            "title": "B",
            "header": _header(),
            "blocks": [
                {
                    "block": "annex",
                    "summary": "s",
                    "table": {"headers": ["A", "B"], "rows": [["x", ""]]},
                }
            ],
        }
    )
    assert spec["blocks"][0]["table"]["rows"] == [["x", ""]]


def test_the_rich_text_guard_is_the_reports_and_decks_carry_no_rich_text():
    """One definition of "unsafe markup" platform-wide, and one consumer left:
    the report's paragraph-level ``rich_text`` block reads the shared guard, and
    the deck spec no longer has a ``rich_text`` field at all - a second
    HTML field on a deck would be a second HTML sink to keep in step."""
    from app.services import presentation_spec, report_spec, rich_text_guard

    assert report_spec.check_rich_text is rich_text_guard.check_rich_text
    assert not hasattr(presentation_spec, "_check_rich_text")
    assert "rich_text" not in json.dumps(presentation_spec.PresentationSpec.model_json_schema())


# ── The tool: create ─────────────────────────────────────────────────────────


def test_generate_report_persists_and_returns_a_marker(monkeypatch):
    import app.core.database as database

    session = _FakeSession()
    monkeypatch.setattr(database, "async_session", _session_factory(session))

    user_id = str(uuid.uuid4())
    conversation_id = str(uuid.uuid4())
    _defs, executor = _build_tools(user_id, conversation_id)
    result = _run_tool(
        executor,
        "generate_report",
        {
            "title": "Brief",
            "header": _header(),
            "blocks": _blocks(3),
            "theme": "dark",
            "footer": "TLP:CLEAR",
        },
    )

    assert not result.is_error, result.content
    assert len(session.added) == 1
    row = session.added[0]
    assert row.title == "Brief"
    assert row.theme == "dark"
    assert row.block_count == 3
    assert row.revision == 1
    assert row.spec["footer"] == "TLP:CLEAR"
    assert str(row.user_id) == user_id
    assert str(row.conversation_id) == conversation_id
    assert session.committed
    assert f"[[REPORT:{row.id}]]" in result.content
    # The id is echoed as text too, so the model can revise the report later.
    assert str(row.id) in result.content


def test_marker_format_matches_the_channel_and_frontend_regexes(monkeypatch):
    """One marker shape, three consumers (web renderer, channel rewriter, tool
    output). A divergence leaks raw ``[[REPORT:…]]`` text to users."""
    import app.core.database as database
    from app.channels.base import _REPORT_MARKER_RE

    session = _FakeSession()
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    _defs, executor = _build_tools()
    content = _run_tool(
        executor,
        "generate_report",
        {"title": "B", "header": _header(), "blocks": _blocks(1)},
    ).content

    assert _REPORT_MARKER_RE.search(content)

    # The web app reads markers with the chat panel's grammar, tested on the
    # same samples (frontend/src/pages/chat/answer-markers.contract.json).
    contract = json.loads(
        (FRONTEND_ROOT / "src" / "pages" / "chat" / "answer-markers.contract.json").read_text(encoding="utf-8")
    )["markers"]
    samples = [m for m in contract if m["kind"] == "REPORT"]
    assert samples
    for sample in samples:
        match = _REPORT_MARKER_RE.fullmatch(sample["marker"])
        assert match, sample["marker"]
        assert match.group(1).lower() == sample["id"]


@pytest.mark.parametrize("field", ["header", "blocks"])
def test_json_string_arguments_are_parsed(monkeypatch, field):
    """Providers occasionally stringify object/array arguments; a guaranteed
    failure would otherwise be indistinguishable from a bad spec."""
    import app.core.database as database

    session = _FakeSession()
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    _defs, executor = _build_tools()
    arguments = {"title": "B", "header": _header(), "blocks": _blocks(2)}
    arguments[field] = json.dumps(arguments[field])
    result = _run_tool(executor, "generate_report", arguments)
    assert not result.is_error, result.content
    assert len(session.added) == 1


@pytest.mark.parametrize("field", ["header", "blocks"])
def test_non_json_string_arguments_are_refused(monkeypatch, field):
    import app.core.database as database

    session = _FakeSession()
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    _defs, executor = _build_tools()
    arguments = {"title": "B", "header": _header(), "blocks": _blocks(1)}
    arguments[field] = "definitely not json"
    result = _run_tool(executor, "generate_report", arguments)
    assert result.is_error
    assert field in result.content
    assert session.added == []


def test_invalid_spec_writes_nothing_and_returns_a_tool_error(monkeypatch):
    import app.core.database as database

    session = _FakeSession()
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    _defs, executor = _build_tools()
    result = _run_tool(
        executor,
        "generate_report",
        {"title": "B", "header": _header(), "blocks": [{"block": "invented"}]},
    )
    assert result.is_error
    assert session.added == []
    assert not session.committed
    assert "Allowed blocks" in result.content


def test_anonymous_caller_cannot_generate_a_report(monkeypatch):
    """A report is only reachable through the authenticated read endpoint, so an
    anonymous external-channel caller would have nothing to render."""
    import app.core.database as database

    session = _FakeSession()
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    _defs, executor = _build_tools(user_id="anonymous")
    result = _run_tool(
        executor,
        "generate_report",
        {"title": "B", "header": _header(), "blocks": _blocks(1)},
    )
    assert result.is_error
    assert "signed-in" in result.content
    assert "generate_file" in result.content
    assert session.added == []


# ── Revising rewrites the same report in place ───────────────────────────────


def _existing_row(user_id: uuid.UUID, blocks: int = 3):
    from app.models.report import Report

    return Report(
        id=uuid.uuid4(),
        user_id=user_id,
        title="Brief",
        theme="light",
        block_count=blocks,
        revision=1,
        spec={"title": "Brief", "header": _header(), "blocks": _blocks(blocks)},
    )


def test_revising_reuses_the_row_and_bumps_the_revision(monkeypatch):
    import app.core.database as database

    user_id = uuid.uuid4()
    existing = _existing_row(user_id, blocks=3)
    session = _FakeSession(results=[_Result(one=existing)])
    monkeypatch.setattr(database, "async_session", _session_factory(session))

    _defs, executor = _build_tools(str(user_id))
    revised = [BLOCK_SAMPLES["kpi_row"], BLOCK_SAMPLES["callout"]]
    result = _run_tool(
        executor,
        "generate_report",
        {
            "title": "Brief v2",
            "header": _header(),
            "blocks": revised,
            "report_id": str(existing.id),
        },
    )

    assert not result.is_error, result.content
    # No second row: the SAME report was rewritten, so the marker already sitting
    # in the conversation stays valid and the document updates in place.
    assert session.added == []
    assert existing.revision == 2
    assert existing.title == "Brief v2"
    assert existing.block_count == 2
    assert [b["block"] for b in existing.spec["blocks"]] == ["kpi_row", "callout"]
    assert f"[[REPORT:{existing.id}]]" in result.content
    assert "Updated report" in result.content


def test_revising_a_report_that_is_not_yours_is_refused(monkeypatch):
    import app.core.database as database

    session = _FakeSession(results=[_Result(one=None)])
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    _defs, executor = _build_tools()
    result = _run_tool(
        executor,
        "generate_report",
        {
            "title": "B",
            "header": _header(),
            "blocks": _blocks(1),
            "report_id": str(uuid.uuid4()),
        },
    )
    assert result.is_error
    assert "was found" in result.content
    assert session.added == []


def test_edit_lookup_is_scoped_to_the_owner(monkeypatch):
    """Owner-only, not owner-or-admin: an agent must never be able to rewrite
    somebody else's report from a chat, whatever the operator's role."""
    import app.core.database as database

    user_id = uuid.uuid4()
    session = _FakeSession(results=[_Result(one=None)])
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    _defs, executor = _build_tools(str(user_id))
    _run_tool(
        executor,
        "generate_report",
        {
            "title": "B",
            "header": _header(),
            "blocks": _blocks(1),
            "report_id": str(uuid.uuid4()),
        },
    )
    assert session.statements
    sql = _compiled(session.statements[0])
    assert "user_id" in sql
    assert str(user_id) in sql


def test_non_uuid_report_id_is_rejected_without_touching_the_db(monkeypatch):
    import app.core.database as database

    session = _FakeSession()
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    _defs, executor = _build_tools()
    result = _run_tool(
        executor,
        "generate_report",
        {
            "title": "B",
            "header": _header(),
            "blocks": _blocks(1),
            "report_id": "not-a-uuid",
        },
    )
    assert result.is_error
    assert session.statements == []
    assert session.added == []


# ── The tool: read back ──────────────────────────────────────────────────────


def test_get_report_returns_the_current_spec(monkeypatch):
    import app.core.database as database

    user_id = uuid.uuid4()
    existing = _existing_row(user_id, blocks=2)
    session = _FakeSession(results=[_Result(one=existing)])
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    _defs, executor = _build_tools(str(user_id))
    result = _run_tool(executor, "get_report", {"report_id": str(existing.id)})

    content = result.content if hasattr(result, "content") else str(result)
    assert "Brief" in content
    assert "revision 1" in content
    # The block map is what lets the model address a section by index next turn.
    assert "[prose]" in content


def test_get_report_rejects_a_non_uuid(monkeypatch):
    import app.core.database as database

    session = _FakeSession()
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    _defs, executor = _build_tools()
    result = _run_tool(executor, "get_report", {"report_id": "nope"})
    content = result.content if hasattr(result, "content") else str(result)
    assert "valid UUID" in content
    assert session.statements == []


# ── Template linkage + palette ───────────────────────────────────────────────


def test_template_id_is_linked_to_the_report(monkeypatch):
    import app.core.database as database

    session = _FakeSession()
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    template_id = str(uuid.uuid4())
    _defs, executor = _build_tools()
    result = _run_tool(
        executor,
        "generate_report",
        {
            "title": "B",
            "header": _header(),
            "blocks": _blocks(1),
            "template_id": template_id,
        },
    )
    assert not result.is_error, result.content
    assert str(session.added[0].template_container_id) == template_id


def test_invalid_template_id_is_refused(monkeypatch):
    import app.core.database as database

    session = _FakeSession()
    monkeypatch.setattr(database, "async_session", _session_factory(session))
    _defs, executor = _build_tools()
    result = _run_tool(
        executor,
        "generate_report",
        {
            "title": "B",
            "header": _header(),
            "blocks": _blocks(1),
            "template_id": "not-a-uuid",
        },
    )
    assert result.is_error
    assert "search_templates" in result.content
    assert session.added == []


def test_both_palettes_extend_the_shared_brand_base():
    """A report needs document surfaces and semantic risk colours a slide does
    not; a slide needs gradients, semantic tones and a logo a report
    does not. Each EXTENDS the shared brand base — the six keys a brand template
    sets once and both artifacts honour — and never replaces it, so one
    company-managed brand container styles decks AND reports."""
    from app.services.presentations import BRAND_THEME_CONFIG_KEYS, THEME_CONFIG_KEYS
    from app.services.reports import REPORT_THEME_CONFIG_KEYS

    brand = set(BRAND_THEME_CONFIG_KEYS)
    assert brand == {"background", "foreground", "accent", "muted", "heading_font", "body_font"}
    assert brand < set(REPORT_THEME_CONFIG_KEYS)
    assert brand < set(THEME_CONFIG_KEYS)
    assert {"paper", "card", "line", "critical", "elevated", "moderate", "controlled"} <= set(
        REPORT_THEME_CONFIG_KEYS
    )
    # Report-only keys never leak into the deck whitelist and vice versa: the
    # renderer maps each list to its own CSS variables, an unmapped key is stored
    # and silently ignored — exactly the failure the whitelists exist to prevent.
    assert {"paper", "card", "critical"}.isdisjoint(THEME_CONFIG_KEYS)
    assert {"gradient_from", "logo_file_id"}.isdisjoint(REPORT_THEME_CONFIG_KEYS)


def test_widening_the_report_palette_did_not_widen_the_decks():
    """The regression this guards: ``sanitize_theme_config`` grew a ``keys``
    parameter for reports, and a wrong default would silently let report-only
    keys through to decks, where the renderer maps six and drops the rest."""
    from app.services.presentations import sanitize_theme_config

    raw = {"accent": "#ff4700", "critical": "#c62828", "paper": "#f4f6f8"}
    assert sanitize_theme_config(raw) == {"accent": "#ff4700"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"critical": "#c62828"}, {"critical": "#c62828"}),
        ({"paper": "hsl(210 20% 96%)"}, {"paper": "hsl(210 20% 96%)"}),
        # CSS-escaping values are refused for report keys exactly as for deck ones:
        # the value guard is a property of the renderer, not of the consumer.
        ({"critical": "red;}body{display:none"}, {}),
        ({"card": "url(https://evil.example/x.png)"}, {}),
        ({"line": "/* comment */ red"}, {}),
        ({"elevated": "x" * 200}, {}),
        ({"moderate": 12}, {}),
        ({"invented_key": "#000"}, {}),
    ],
)
def test_report_theme_values_are_css_safe(raw, expected):
    from app.services.presentations import sanitize_theme_config
    from app.services.reports import REPORT_THEME_CONFIG_KEYS

    assert sanitize_theme_config(raw, keys=REPORT_THEME_CONFIG_KEYS) == expected


def _resolver_session(found: dict[str, uuid.UUID]):
    """A session answering the default resolver's slug lookups from *found*."""
    asked: list[str] = []

    class _Result:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    class _Session:
        async def execute(self, stmt):
            sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
            for slug, container_id in found.items():
                if f"slug:{slug}" in sql:
                    asked.append(slug)
                    return _Result(container_id)
            asked.append("?")
            return _Result(None)

    return _Session(), asked


@pytest.mark.parametrize(
    ("seeded", "expected"),
    [
        (("filigran-report", "xtm-one-report"), "filigran-report"),
        (("xtm-one-report",), "xtm-one-report"),
        ((), None),
    ],
)
def test_a_report_with_no_template_wears_the_platform_brand(seeded, expected):
    """Copilot mode seeds both brand reports and picks Filigran; an XTM One
    platform seeds only its own; a platform before the seed keeps the built-in."""
    from app.services.reports import DEFAULT_REPORT_TEMPLATE_SLUGS, effective_report_template

    assert DEFAULT_REPORT_TEMPLATE_SLUGS == ("filigran-report", "xtm-one-report")
    ids = {slug: uuid.uuid4() for slug in seeded}
    session, _asked = _resolver_session(ids)
    template_id, scheme = asyncio.run(effective_report_template(session, None, None))
    assert template_id == (ids[expected] if expected else None)
    assert scheme == "light"


class _MetadataSession:
    """A session answering the linked template's ``extra_metadata`` read."""

    def __init__(self, extra_metadata):
        self.extra_metadata = extra_metadata
        self.statements: list[str] = []

    async def execute(self, stmt):
        self.statements.append(str(stmt))
        value = self.extra_metadata

        class _Result:
            def scalar_one_or_none(self):
                return value

        return _Result()


@pytest.mark.parametrize("extra_metadata", [None, {}, {"theme_variants": {}}, {"guidance": "x"}])
def test_a_linked_template_without_variants_is_applied_as_authored(extra_metadata):
    """An explicit template is never replaced by the brand, and one with a
    single palette is read with no scheme, exactly as before the default existed."""
    from app.services.reports import effective_report_template

    linked = uuid.uuid4()
    session = _MetadataSession(extra_metadata)
    assert asyncio.run(effective_report_template(session, linked, "dark")) == (linked, None)
    assert all("slug:" not in sql for sql in session.statements), "the brand is never looked up"


@pytest.mark.parametrize(("theme", "scheme"), [("light", "light"), ("dark", "dark"), (None, "light")])
def test_a_linked_template_with_variants_is_read_for_the_report_s_theme(theme, scheme):
    """Naming the brand template explicitly gives the look of not naming it:
    a template with ``theme_variants`` is read for the report's own scheme
    (UX round 1 on #492, B2: a linked brand report printed its dark palette on
    a light report)."""
    from app.services.reports import effective_report_template

    linked = uuid.uuid4()
    session = _MetadataSession({"theme_variants": {"light": {"paper": "#ffffff"}}})
    assert asyncio.run(effective_report_template(session, linked, theme)) == (linked, scheme)


def _css_rgb(value: str) -> tuple[float, float, float]:
    """``#rrggbb`` or space-syntax ``hsl()`` (what the report seeds use) as sRGB fractions."""
    import re

    from app.services.presentations import _hsl_to_rgb

    text = value.strip().lower()
    if text.startswith("#"):
        return tuple(int(text[i : i + 2], 16) / 255.0 for i in (1, 3, 5))  # type: ignore[return-value]
    match = re.fullmatch(r"hsl\(([\d.]+)\s+([\d.]+)%\s+([\d.]+)%\)", text)
    assert match, value
    return _hsl_to_rgb(float(match[1]), float(match[2]) / 100, float(match[3]) / 100)


def _mix_with_white(rgb: tuple[float, float, float], share: float) -> str:
    """CSS ``color-mix(in srgb, COLOUR share, white)`` as ``#rrggbb``."""
    return "#" + "".join(f"{round((c * share + (1 - share)) * 255):02x}" for c in rgb)


def test_the_accent_ink_on_the_masthead_and_callout_is_readable():
    """The masthead eyebrow, the verdict label, the callout number and score are
    drawn in ``--report-accent-on-bg`` on ``--report-bg`` (the callout) and on
    the masthead gradient from ``background`` to 82% background + white. In both
    schemes of every brand report that ink reaches 4.5:1 on both stops (UX
    round 1 on #492, B1: the light accent, tuned for paper, was drawn on the
    dark masthead at 1.56:1)."""
    import re

    from app.services.presentations import (
        css_color_luminance,
        sanitize_theme_config,
        scheme_palette,
        template_theme_variant,
    )
    from app.services.reports import REPORT_THEME_CONFIG_KEYS
    from app.services.template_container_seed import BUILTIN_TEMPLATE_CONTAINERS

    css = (FRONTEND_ROOT / "src" / "components" / "report" / "report.css").read_text(encoding="utf-8")
    match = re.search(r"--report-accent-on-bg:\s*color-mix\(in srgb, var\(--report-accent\) (\d+)%, white\);", css)
    assert match, "report.css defines the accent ink for the dark surfaces"
    share = int(match[1]) / 100
    for selector in (".report-eyebrow", ".report-verdict-label", ".report-callout .report-secno", ".report-score"):
        rule = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
        assert rule and "color: var(--report-accent-on-bg)" in rule[1], selector

    def ratio(a: str, b: str) -> float:
        la, lb = css_color_luminance(a), css_color_luminance(b)
        assert la is not None and lb is not None, (a, b)
        return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)

    for seed in (c for c in BUILTIN_TEMPLATE_CONTAINERS if c["kind"] == "report"):
        base = sanitize_theme_config(seed["theme_config"], keys=REPORT_THEME_CONFIG_KEYS)
        for scheme in ("dark", "light"):
            variant = template_theme_variant(seed["extra_metadata"], scheme, keys=REPORT_THEME_CONFIG_KEYS)
            palette = scheme_palette(base, variant, scheme)
            ink = _mix_with_white(_css_rgb(palette["accent"]), share)
            dark_stop = _mix_with_white(_css_rgb(palette["background"]), 1.0)
            light_stop = _mix_with_white(_css_rgb(palette["background"]), 0.82)
            for stop in (dark_stop, light_stop):
                assert ratio(ink, stop) >= 4.5, (seed["slug"], scheme, ink, stop)


def test_a_dark_report_reads_the_brand_s_dark_palette():
    from app.services.reports import effective_report_template

    brand = uuid.uuid4()
    session, _asked = _resolver_session({"xtm-one-report": brand})
    assert asyncio.run(effective_report_template(session, None, "dark")) == (brand, "dark")


def test_the_seeded_brand_reports_are_complete_and_readable():
    """Each brand report carries every report key in both schemes, survives the
    sanitizer whole, and draws every ink at 4.5:1 or more on its page and card."""
    from app.services.presentations import (
        css_color_luminance,
        sanitize_theme_config,
        scheme_palette,
        template_theme_variant,
    )
    from app.services.reports import DEFAULT_REPORT_TEMPLATE_SLUGS, REPORT_THEME_CONFIG_KEYS
    from app.services.template_container_seed import BUILTIN_TEMPLATE_CONTAINERS

    def ratio(a: str, b: str) -> float:
        la, lb = css_color_luminance(a), css_color_luminance(b)
        assert la is not None and lb is not None, (a, b)
        return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)

    seeds = {c["slug"]: c for c in BUILTIN_TEMPLATE_CONTAINERS if c["kind"] == "report"}
    assert set(seeds) == set(DEFAULT_REPORT_TEMPLATE_SLUGS)
    for slug, seed in seeds.items():
        base = sanitize_theme_config(seed["theme_config"], keys=REPORT_THEME_CONFIG_KEYS)
        assert set(base) == set(REPORT_THEME_CONFIG_KEYS), slug
        for scheme in ("dark", "light"):
            variant = template_theme_variant(seed["extra_metadata"], scheme, keys=REPORT_THEME_CONFIG_KEYS)
            palette = scheme_palette(base, variant, scheme)
            assert set(palette) == set(REPORT_THEME_CONFIG_KEYS), (slug, scheme)
            assert ratio(palette["background"], "#ffffff") >= 7, (slug, scheme, "masthead")
            for ink in ("foreground", "muted", "accent", "critical", "elevated", "moderate", "controlled"):
                for surface in ("paper", "card"):
                    assert ratio(palette[ink], palette[surface]) >= 4.5, (slug, scheme, ink, surface)


def test_the_default_report_skin_has_no_coral_accent():
    """The renderer's own fallback accent follows the product theme: the coral
    of the old built-in skin is what a report with no brand looked like."""
    css = (FRONTEND_ROOT / "src" / "components" / "report" / "report.css").read_text(encoding="utf-8")
    assert "--report-accent: hsl(17 100% 50%)" not in css


def test_report_theme_resolution_is_visibility_checked():
    """A container un-shared since the report was generated must contribute
    nothing rather than leak its brand config."""
    import inspect

    from app.services import reports

    source = inspect.getsource(reports.resolve_report_theme)
    # Delegates to the deck resolver instead of re-spelling the visibility query —
    # a second copy could fall behind and start leaking.
    assert "resolve_template_theme" in source
    assert "REPORT_THEME_CONFIG_KEYS" in source

    # ... and the deck resolver itself loads the container through the ONE
    # visibility-checked loader (split out so the per-scheme resolver
    # shares it rather than re-spelling the query).
    from app.services import presentations

    assert "_load_visible_template(" in inspect.getsource(presentations.resolve_template_theme)
    assert "caller_visible_filter" in inspect.getsource(presentations._load_visible_template)


def test_template_container_validates_report_block_ids():
    """A report template advertising a block that does not exist would render
    nothing for it, so authoring refuses the id up front."""
    from app.services.template_containers import validate_layouts

    good, problems = validate_layouts(
        [{"id": "risk_table", "name": "Risk matrix"}], kind="report"
    )
    assert not problems
    # ``fields`` is derived from the Pydantic model, never taken from the caller.
    assert good[0]["fields"] == block_catalog()["risk_table"]

    _bad, bad_problems = validate_layouts([{"id": "content"}], kind="report")
    assert bad_problems
    assert "not a report block" in bad_problems[0]


def test_report_template_theme_keys_are_accepted_by_authoring():
    """Without threading ``kind`` through, authoring would tell a report template
    author that ``critical`` "is not honoured by the renderer" — while the report
    renderer honours it perfectly well."""
    from app.services.template_containers import validate_theme_config

    accepted, problems = validate_theme_config(
        {"accent": "#ff4700", "critical": "#c62828"}, kind="report"
    )
    assert accepted == {"accent": "#ff4700", "critical": "#c62828"}
    assert problems == []

    # Same payload on a deck: the report-only key is refused, loudly.
    _accepted, deck_problems = validate_theme_config(
        {"accent": "#ff4700", "critical": "#c62828"}, kind="presentation"
    )
    assert any("critical" in p for p in deck_problems)


# ── Wiring ───────────────────────────────────────────────────────────────────


def test_generate_report_is_a_recognized_builtin_tool_id():
    from app.services.builtin_tools.agent_builder import _VALID_BUILTIN_TOOL_IDS

    assert "generate_report" in _VALID_BUILTIN_TOOL_IDS


def test_builtin_catalog_advertises_every_sub_tool():
    """The catalog (no request text) lists the export too; a turn offers it
    only when its request asks for a file (``tests/test_report_export.py``)."""
    definitions, _executor = _build_tools()
    assert {d.name for d in definitions} == {"generate_report", "get_report", "export_report"}
    assert all(d.source == "builtin:generate_report" for d in definitions)


def test_tool_schema_documents_the_block_catalog():
    definitions, _executor = _build_tools()
    generate = next(d for d in definitions if d.name == "generate_report")
    block_enum = generate.input_schema["properties"]["blocks"]["items"]["properties"][
        "block"
    ]["enum"]
    assert set(block_enum) == set(REPORT_BLOCKS)
    # Every block is named in the prose description too, so the model learns the
    # nested shapes and not just the ids.
    for block_id in REPORT_BLOCKS:
        assert block_id in generate.description
    assert generate.input_schema["required"] == ["title", "header", "blocks"]


def test_tool_description_steers_away_from_the_neighbouring_tools():
    """Three tools produce "a document"; without this the model reaches for the
    wrong one and the user gets a static file instead of a rendered report."""
    definitions, _executor = _build_tools()
    description = next(
        d for d in definitions if d.name == "generate_report"
    ).description
    assert "generate_file" in description
    assert "generate_presentation" in description
    # The editing contract and the marker are the two things a caller must know.
    assert "report_id" in description
    assert "[[REPORT:" in description
    # And the one non-obvious print trap.
    assert "PDF" in description


def test_tool_description_states_the_authoring_boundary():
    """The model must be told it authors values, not markup — otherwise it tries
    to emit a styled page and every call fails validation."""
    definitions, _executor = _build_tools()
    description = next(
        d for d in definitions if d.name == "generate_report"
    ).description
    assert "NOT HTML" in description
    assert "rich_text" in description


@pytest.mark.parametrize(
    ("module_path", "function_name"),
    [
        ("app.services.agent_chat", "run_agent_chat"),
        ("app.services.general_chat", None),
        ("app.services.assignment_engine", "execute_assignment_run"),
    ],
)
def test_every_agentic_entry_point_wires_the_tool(module_path, function_name):
    """Chat, the general assistant and assignment runs must all build the tool
    when the agent opts in — a missing branch silently drops the capability on
    that path only, which is the classic way a builtin half-ships."""
    import importlib
    import inspect

    module = importlib.import_module(module_path)
    source = (
        inspect.getsource(getattr(module, function_name))
        if function_name
        else inspect.getsource(module)
    )
    assert '"generate_report" in' in source
    assert "build_report_tools(" in source


def test_assignment_runs_stamp_the_report_with_their_run():
    """Without this the report of a scheduled brief is orphaned (neither
    conversation- nor run-scoped), exactly the ``generate_file`` bug."""
    import ast
    import inspect
    import textwrap

    from app.services import assignment_engine

    tree = ast.parse(
        textwrap.dedent(inspect.getsource(assignment_engine.execute_assignment_run))
    )
    wired = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = (
            node.func.id
            if isinstance(node.func, ast.Name)
            else getattr(node.func, "attr", None)
        )
        if name != "build_report_tools":
            continue
        wired = any(kw.arg == "assignment_run_id" for kw in node.keywords)
    assert wired, (
        "execute_assignment_run must call build_report_tools with "
        "assignment_run_id=... so run-generated reports stay attributable"
    )


def test_tool_never_writes_on_the_callers_session():
    """Tool closures run mid agentic loop after the caller released its pooled
    connection; flushing on ``db`` would pin a connection for minutes."""
    import inspect

    from app.services.builtin_tools import report as mod

    source = inspect.getsource(mod.build_report_tools)
    assert "del db" in source
    assert "async_session()" in source


def test_settings_catalog_exposes_the_tool():
    """The Settings page builds its built-in tool catalog by calling every
    builder; a missing entry hides the tool from the agent configuration UI."""
    import inspect

    from app.services import builtin_tool_catalog

    source = inspect.getsource(builtin_tool_catalog)
    assert 'catalog["generate_report"]' in source


# ── REST surface ─────────────────────────────────────────────────────────────


def test_read_endpoint_is_owner_or_admin():
    from fastapi import HTTPException

    from app.api.v1.reports import _assert_readable

    owner = uuid.uuid4()
    report = SimpleNamespace(user_id=owner)

    _assert_readable(report, SimpleNamespace(id=owner, role="user"))
    _assert_readable(report, SimpleNamespace(id=uuid.uuid4(), role="admin"))
    with pytest.raises(HTTPException) as exc:
        _assert_readable(report, SimpleNamespace(id=uuid.uuid4(), role="user"))
    assert exc.value.status_code == 403
    # Orphaned report (author deleted) stays readable for auditing, admins only.
    with pytest.raises(HTTPException):
        _assert_readable(
            SimpleNamespace(user_id=None), SimpleNamespace(id=owner, role="user")
        )


def test_report_api_exposes_no_write_route():
    """Specs are only ever written through the validated tool path, so the REST
    surface must stay read-only — a POST/PUT here would bypass validation. The
    one POST renders a PDF and writes nothing."""
    from app.api.v1 import reports as api

    methods = {
        (method, route.path)
        for route in api.router.routes
        for method in getattr(route, "methods", set())
        if method != "GET"
    }
    assert methods == {("POST", "/{report_id}/export")}


def test_serializer_exposes_the_spec_and_resolved_theme():
    from app.services.reports import serialize_report

    report = _existing_row(uuid.uuid4(), blocks=2)
    payload = serialize_report(report, theme_config={"critical": "#c62828"})
    assert payload["spec"]["title"] == "Brief"
    assert payload["block_count"] == 2
    assert payload["revision"] == 1
    assert payload["theme_config"] == {"critical": "#c62828"}


def test_router_is_registered():
    from app.main import application

    paths = {getattr(route, "path", "") for route in application.routes}
    assert "/api/v1/reports/{report_id}" in paths


# ── External channels ────────────────────────────────────────────────────────


def test_channel_markers_become_web_app_links():
    """A report has no channel representation, so Slack / Teams get a link to the
    viewer rather than the raw marker."""
    from app.channels.base import resolve_channel_report_markers

    report_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    row = SimpleNamespace(id=report_id, title="CTI Brief")
    session = _FakeSession(results=[_Result(rows=[row])])

    out = asyncio.run(
        resolve_channel_report_markers(
            session,
            f"Here it is: [[REPORT:{report_id}]]",
            "https://app.example.org/",
            conversation_id=conversation_id,
        )
    )
    assert f"[CTI Brief](https://app.example.org/reports/{report_id})" in out
    assert "[[REPORT:" not in out


def test_channel_markers_for_unknown_reports_are_stripped():
    """Fail closed: a marker the scope query did not match is removed, never
    linked — an LLM-emitted UUID must not mint a link to somebody else's report."""
    from app.channels.base import resolve_channel_report_markers

    session = _FakeSession(results=[_Result(rows=[])])
    out = asyncio.run(
        resolve_channel_report_markers(
            session,
            f"See [[REPORT:{uuid.uuid4()}]]",
            "https://app.example.org",
            conversation_id=uuid.uuid4(),
        )
    )
    assert "[[REPORT:" not in out
    assert "app.example.org/reports" not in out


def test_channel_markers_are_stripped_when_there_is_no_scope():
    from app.channels.base import resolve_channel_report_markers

    session = _FakeSession()
    out = asyncio.run(
        resolve_channel_report_markers(
            session,
            f"See [[REPORT:{uuid.uuid4()}]]",
            "https://app.example.org",
        )
    )
    assert "[[REPORT:" not in out
    assert session.statements == []


def test_channel_router_resolves_report_markers():
    """The resolver existing is not enough — the outbound path has to call it."""
    import inspect

    from app.channels import router

    source = inspect.getsource(router)
    assert "resolve_channel_report_markers" in source


def test_marker_resolution_is_shared_with_decks():
    """The scoping contract (fail closed, strip the unmatched) has exactly one
    implementation; a per-artifact copy could fall behind and start linking
    across conversations."""
    import inspect

    from app.channels import base

    for name in (
        "resolve_channel_report_markers",
        "resolve_channel_presentation_markers",
    ):
        source = inspect.getsource(getattr(base, name))
        assert "_resolve_channel_artifact_markers(" in source


# ── Frontend contract ────────────────────────────────────────────────────────


def test_frontend_renders_every_block_in_the_catalog():
    """A block in the backend catalog with no component renders nothing, which is
    the report equivalent of a deck layout rendering a blank slide. Same
    tripwire, one layer up: the switch in ``blocks.tsx`` must name every block
    id."""
    source = (
        FRONTEND_ROOT / "src" / "components" / "report" / "blocks.tsx"
    ).read_text(encoding="utf-8")
    missing = [b for b in REPORT_BLOCKS if f'case "{b}":' not in source]
    assert missing == [], f"blocks.tsx has no case for: {', '.join(missing)}"


def test_frontend_maps_every_whitelisted_theme_key():
    """A whitelisted key with no CSS variable is stored, served and silently
    ignored — the palette half-applies and nobody can tell why."""
    from app.services.reports import REPORT_THEME_CONFIG_KEYS

    source = (
        FRONTEND_ROOT / "src" / "components" / "report" / "ReportView.tsx"
    ).read_text(encoding="utf-8")
    missing = [k for k in REPORT_THEME_CONFIG_KEYS if f"{k}:" not in source]
    assert missing == [], f"ReportView.tsx maps no CSS variable for: {missing}"


def test_frontend_sanitizes_rich_text_before_rendering_it():
    """``dangerouslySetInnerHTML`` is only ever reached through the shared
    DOMPurify strict profile — the enforcing layer, since the backend check could
    be bypassed by an older row."""
    source = (
        FRONTEND_ROOT / "src" / "components" / "report" / "blocks.tsx"
    ).read_text(encoding="utf-8")
    assert "sanitizeRichText" in source
    # One call site, one guard: every dangerouslySetInnerHTML in the file must be
    # the sanitized one.
    assert source.count("dangerouslySetInnerHTML") == 1
    assert "__html: clean" in source


def test_print_stylesheet_exists_and_forces_annexes_open():
    """A collapsed <details> prints as its summary line alone, so annex content
    would vanish from an exported PDF. CSS cannot reopen one — the ``open``
    attribute is driven from React state instead."""
    css = (FRONTEND_ROOT / "src" / "components" / "report" / "report.css").read_text(encoding="utf-8")
    assert "@media print" in css
    assert "@page" in css

    view = (
        FRONTEND_ROOT / "src" / "components" / "report" / "ReportView.tsx"
    ).read_text(encoding="utf-8")
    assert "beforeprint" in view
    assert "afterprint" in view
    assert "forceOpen" in view


# ── Generalisation beyond the first reference document ────────────────


def test_matrix_grades_every_intersection_not_one_per_row():
    """The whole reason this block exists: a table grades one situation per row, a
    matrix grades every combination of two variables. Flattening a 4x4 matrix into
    a table would keep 4 of its 16 judgements."""
    spec = validate_spec(
        {
            "title": "B",
            "header": _header(),
            "blocks": [
                {
                    "block": "matrix",
                    "column_labels": ["A", "B", "C", "D"],
                    "rows": [
                        {
                            "label": f"context {r}",
                            "cells": [
                                {"label": f"{r}{c}", "level": "moderate"}
                                for c in range(4)
                            ],
                        }
                        for r in range(4)
                    ],
                }
            ],
        }
    )
    cells = [c for row in spec["blocks"][0]["rows"] for c in row["cells"]]
    assert len(cells) == 16


def test_ragged_matrix_row_is_refused():
    """A row that does not match the column count renders as a broken grid — an
    error the agent can fix beats a visual defect the reader must interpret."""
    with pytest.raises(ReportSpecError) as exc:
        validate_spec(
            {
                "title": "B",
                "header": _header(),
                "blocks": [
                    {
                        "block": "matrix",
                        "column_labels": ["A", "B", "C"],
                        "rows": [
                            {
                                "label": "short row",
                                "cells": [{"label": "x", "level": "moderate"}],
                            }
                        ],
                    }
                ],
            }
        )
    message = str(exc.value)
    assert "column" in message
    # The message must name WHICH row, or a 8-row matrix is a guessing game.
    assert "short row" in message


def test_matrix_and_risk_table_share_one_level_scale():
    """Two grading scales in one report would read as two different meanings of
    'critical'."""
    from app.services.report_spec import MatrixCell, RiskRow

    def _levels(model, field):
        return set(model.model_fields[field].annotation.__args__)

    assert _levels(MatrixCell, "level") == _levels(RiskRow, "level")


def test_section_number_is_a_string_not_an_integer():
    """Real briefs number an annex "R" or "A.2"; an int field would refuse both."""
    spec = validate_spec(
        {
            "title": "B",
            "header": _header(),
            "blocks": [
                {"block": "prose", "title": "Judgment", "number": "01"},
                {"block": "prose", "title": "Matrix", "number": "R"},
                {"block": "prose", "title": "Annex", "number": "A.2"},
            ],
        }
    )
    assert [b["number"] for b in spec["blocks"]] == ["01", "R", "A.2"]


def test_number_is_never_invented_by_the_validator():
    """A report that does not number its sections must not acquire numbering."""
    spec = validate_spec(
        {"title": "B", "header": _header(), "blocks": [BLOCK_SAMPLES["prose"]]}
    )
    assert "number" not in spec["blocks"][0]


def test_every_block_accepts_a_number_and_a_title():
    """The table of contents is derived from these two, so a block that refused
    either would be invisible to navigation."""
    for block_id in REPORT_BLOCKS:
        payload = {**BLOCK_SAMPLES[block_id], "title": "T", "number": "07"}
        spec = validate_spec(
            {"title": "B", "header": _header(), "blocks": [payload]}
        )
        assert spec["blocks"][0]["number"] == "07", block_id


def test_evidence_nature_is_free_text_not_an_enum():
    """The taxonomy is analyst vocabulary and differs per shop — an enum would be
    refused by the first brief that says 'vendor advisory'."""
    for nature in ("public decision", "vendor advisory", "sigint", "рапорт"):
        spec = validate_spec(
            {
                "title": "B",
                "header": _header(),
                "blocks": [
                    {
                        "block": "assessment_table",
                        "rows": [
                            {
                                "statement": "s",
                                "confidence": "high",
                                "nature": nature,
                                "appraisal": "Corroborated.",
                            }
                        ],
                    }
                ],
            }
        )
        assert spec["blocks"][0]["rows"][0]["nature"] == nature


def test_decision_items_carry_their_own_confidence():
    """A `decisions` block is also how a brief lays out analytic items, each with
    its own confidence footer."""
    spec = validate_spec(
        {
            "title": "B",
            "header": _header(),
            "blocks": [
                {
                    "block": "decisions",
                    "items": [
                        {
                            "title": "Search",
                            "text": "t",
                            "confidence": "high",
                            "confidence_label": "high on the power, variable on use",
                        }
                    ],
                }
            ],
        }
    )
    item = spec["blocks"][0]["items"][0]
    assert item["confidence"] == "high"
    assert "variable" in item["confidence_label"]


def test_source_ref_is_carried_for_citation():
    spec = validate_spec(
        {
            "title": "B",
            "header": _header(),
            "blocks": [
                {
                    "block": "sources",
                    "items": [{"label": "Reuters", "ref": "S01 · INTERNATIONAL MEDIA"}],
                }
            ],
        }
    )
    assert spec["blocks"][0]["items"][0]["ref"].startswith("S01")


def test_table_of_contents_is_derived_never_authored():
    """Anything an agent could write in a TOC is already in the spec, and a
    hand-authored one goes stale on the next in-place revision. So no field may
    exist for it, and the renderer must compute it."""
    from app.services import report_spec as mod

    models = [
        getattr(mod, name)
        for name in dir(mod)
        if isinstance(getattr(mod, name), type)
        and issubclass(getattr(mod, name), mod.BaseModel)
        and getattr(mod, name) is not mod.BaseModel
    ]
    toc_fields = {
        f"{m.__name__}.{n}"
        for m in models
        for n in m.model_fields
        if any(bad in n for bad in ("toc", "contents", "nav", "anchor"))
    }
    assert toc_fields == set()

    view = (
        FRONTEND_ROOT / "src" / "components" / "report" / "ReportView.tsx"
    ).read_text(encoding="utf-8")
    assert "TableOfContents" in view
    assert "anchorFor" in view


def test_matrix_degrades_for_print():
    """Four coloured grades print as four indistinguishable greys, so the grade
    name — hidden on screen where colour carries it — must become visible."""
    css = (FRONTEND_ROOT / "src" / "components" / "report" / "report.css").read_text(encoding="utf-8")
    print_block = css[css.index("@media print") :]
    assert ".report-matrix-cell" in print_block
    assert ".report-matrix-level" in print_block
    # And the wide-grid escape must not leave the page scrolling sideways on paper.
    assert ".report-matrix-scroll" in print_block


def test_matrix_scrolls_inside_its_own_container():
    """A matrix may be wider than a phone; the page body must never scroll."""
    css = (FRONTEND_ROOT / "src" / "components" / "report" / "report.css").read_text(encoding="utf-8")
    scroll_rule = css[css.index(".report-matrix-scroll {") :][:200]
    assert "overflow-x: auto" in scroll_rule


def test_the_tool_documents_every_field_the_spec_accepts():
    """A field the validator accepts but the tool never names is a field no agent
    will ever fill — it ships as dead weight.

    `test_tool_schema_documents_the_block_catalog` checks block *ids*; this checks
    every field of every model, nested ones included. That gap is how `ref` on a
    source shipped undocumented in the first place,
    and this is the tripwire that closes the class rather than the instance.
    """
    import json

    from app.services import report_spec as mod

    definitions, _executor = _build_tools()
    generate = next(d for d in definitions if d.name == "generate_report")
    documented = generate.description + json.dumps(generate.input_schema)

    models = [
        getattr(mod, name)
        for name in dir(mod)
        if isinstance(getattr(mod, name), type)
        and issubclass(getattr(mod, name), mod.BaseModel)
        and getattr(mod, name) is not mod.BaseModel
    ]
    fields = {field for model in models for field in model.model_fields}
    undocumented = sorted(f for f in fields if f not in documented)
    assert undocumented == [], (
        "these ReportSpec fields are accepted by the validator but never named in "
        f"the tool description or schema, so no agent will use them: {undocumented}"
    )
