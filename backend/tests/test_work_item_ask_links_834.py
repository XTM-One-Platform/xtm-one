"""Decision cards carry the sources the reader opens, filled without a model (#834)."""

from __future__ import annotations

import json
import logging
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import work_item_ask_backfill as backfill
from app.services.work_item_ask import (
    MAX_LINKS,
    ask_lines,
    normalize_ask,
    source_links,
    with_source_links,
)

JIRA = "https://filigran.atlassian.net/browse/FIN-798"
CARD = {
    "question": "Post Legal's review on the payroll request: approve with changes?",
    "context": "The People Team asks to sign a payroll provider.\nLegal approves with changes.",
    "options": [{"key": "go", "label": "Post it"}, {"key": "no", "label": "Don't post"}],
    "recommendation": "go",
}


# ── What the write path recognises ──────────────────────────────────────────


def test_jira_github_notion_and_concord_links_are_found_with_their_label() -> None:
    text = (
        f"Ticket: {JIRA}. PR https://github.com/XTM-One-Platform/xtm-one/pull/834, "
        "issue https://github.com/OpenCTI-Platform/opencti/issues/12 and spec "
        "https://www.notion.so/filigran/Spec-abc and https://secure.concordnow.com/doc/1 "
        "but not https://example.com/page"
    )
    assert source_links(text) == [
        {"label": "FIN-798", "url": JIRA},
        {"label": "xtm-one PR #834", "url": "https://github.com/XTM-One-Platform/xtm-one/pull/834"},
        {"label": "opencti Issue #12", "url": "https://github.com/OpenCTI-Platform/opencti/issues/12"},
        {"label": "Notion page", "url": "https://www.notion.so/filigran/Spec-abc"},
        {"label": "Concord document", "url": "https://secure.concordnow.com/doc/1"},
    ]


def test_links_are_deduplicated_bounded_and_read_from_a_payload() -> None:
    assert source_links(JIRA, f"again {JIRA}") == [{"label": "FIN-798", "url": JIRA}]
    assert source_links(None, {"ticket": JIRA}) == [{"label": "FIN-798", "url": JIRA}]
    many = " ".join(f"https://filigran.atlassian.net/browse/FIN-{n}" for n in range(20))
    assert len(source_links(many)) == MAX_LINKS


def test_a_host_is_parsed_never_matched_on_the_text_after_its_slashes() -> None:
    assert source_links("https://evil.example#x.atlassian.net") == []
    assert source_links("https://evil.example?q=.atlassian.net/browse/FIN-1") == []
    assert source_links("https://notnotion.so/page https://evilconcordnow.com/doc") == []
    assert source_links("https://user@filigran.atlassian.net:443/browse/FIN-2") == [
        {"label": "FIN-2", "url": "https://user@filigran.atlassian.net:443/browse/FIN-2"}
    ]
    assert source_links("https://notion.so/page") == [{"label": "Notion page", "url": "https://notion.so/page"}]


def test_a_payload_url_ends_at_its_string_never_at_a_json_escape() -> None:
    payload = {"note": f"{JIRA}\nnext line", "nested": [{"pr": "https://github.com/XTM-One-Platform/xtm-one/pull/9"}]}
    assert source_links(payload) == [
        {"label": "FIN-798", "url": JIRA},
        {"label": "xtm-one PR #9", "url": "https://github.com/XTM-One-Platform/xtm-one/pull/9"},
    ]


def test_the_agents_own_links_are_kept() -> None:
    mine = {**CARD, "links": [{"label": "Contract", "url": "https://example.com/c"}]}
    assert with_source_links(mine, JIRA) is mine
    assert with_source_links(CARD, "no link here") is CARD
    assert with_source_links(None, JIRA) is None
    assert with_source_links(CARD, JIRA)["links"] == [{"label": "FIN-798", "url": JIRA}]


# ── What is stored ──────────────────────────────────────────────────────────


def test_links_are_validated_and_labelled() -> None:
    ask = normalize_ask({**CARD, "links": [{"url": JIRA}, {"label": "Spec", "url": "https://x.io/s"}]})
    assert ask["links"] == [{"label": "FIN-798", "url": JIRA}, {"label": "Spec", "url": "https://x.io/s"}]


@pytest.mark.parametrize(
    ("links", "message"),
    [
        ("text", "must be a list"),
        ([{"url": "javascript:alert(1)"}], r"not an http\(s\) address"),
        ([{"label": "x"}], r"not an http\(s\) address"),
        ([{"url": JIRA}] * (MAX_LINKS + 1), "at most"),
    ],
)
def test_bad_links_are_refused(links: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        normalize_ask({**CARD, "links": links})


def test_the_tool_lines_name_the_sources() -> None:
    lines = ask_lines(normalize_ask({**CARD, "links": [{"url": JIRA}]}))
    assert f"    - source: FIN-798 {JIRA}" in lines


def test_the_tool_schema_shows_links() -> None:
    from app.services.builtin_tools.work_items import _BLOCKED_WHY_SCHEMA

    description = _BLOCKED_WHY_SCHEMA["ask"]["description"]
    assert "links:[{label,url}]" in description
    assert "needs_human: all but deadline, links; 2+ options" in description


# ── The read path: no bare title ────────────────────────────────────────────


def test_the_brief_skips_the_stamped_notes_and_is_cut_short() -> None:
    from app.services.work_item import CARD_FALLBACK_CHARS, card_brief

    description = (
        "2026-11-21 07:12Z: Assistant wrote the card.\n\n"
        "2026-11-20 19:14Z (run 68ddaa5a): CARD SENT.\n\n"
        f"The People Team asks Legal to review the payroll request. Ticket: {JIRA}"
    )
    assert card_brief(description) == f"The People Team asks Legal to review the payroll request. Ticket: {JIRA}"
    assert card_brief("2026-11-21 07:12Z: only a note") == "2026-11-21 07:12Z: only a note"
    long = card_brief("word " * 400)
    assert len(long) <= CARD_FALLBACK_CHARS + 3 and long.endswith("...")


def test_a_needs_human_item_without_a_card_gets_a_fallback() -> None:
    from app.services.work_item import _card_fallback

    item = _item(needs_human=True, description=f"2026-11-21 07:12Z: note\n\nReview the request. {JIRA}")
    fallback = _card_fallback(item)
    assert fallback == {
        "brief": f"Review the request. {JIRA}",
        "links": [{"label": "FIN-798", "url": JIRA}],
        "missing": ["ask.question", "ask.context", "ask.options (at least 2)", "ask.recommendation"],
    }
    assert _card_fallback(_item(needs_human=True, ask=dict(CARD))) is None
    assert _card_fallback(_item(needs_human=None)) is None


# ── The read path when the ask is missing ───────────────────────────────────


def test_a_no_ask_item_shows_its_brief_and_links_never_the_bare_title() -> None:
    from app.services.work_item import _card_fallback

    description = (
        "2026-11-21 07:12Z: Assistant rewrote the card.\n\n"
        "2026-11-20 19:14Z (run abc): CARD SENT.\n\n"
        f"The People Team asks Legal to review the payroll request. Ticket: {JIRA}"
    )
    item = SimpleNamespace(needs_human=True, ask=None, description=description, payload={})
    fallback = _card_fallback(item)
    assert fallback["brief"] == f"The People Team asks Legal to review the payroll request. Ticket: {JIRA}"
    assert fallback["links"] == [{"label": "FIN-798", "url": JIRA}]
    assert "ask.question" in fallback["missing"]


def test_a_complete_card_or_an_item_not_for_a_person_has_no_fallback() -> None:
    from app.services.work_item import _card_fallback

    assert _card_fallback(SimpleNamespace(needs_human=True, ask=CARD, description="x", payload={})) is None
    assert _card_fallback(SimpleNamespace(needs_human=None, ask=None, description="x", payload={})) is None


def test_a_long_brief_is_cut_on_a_word() -> None:
    from app.services.work_item import CARD_FALLBACK_CHARS, card_brief

    brief = card_brief("word " * 400)
    assert len(brief) <= CARD_FALLBACK_CHARS + 3 and brief.endswith("word...")


# ── The backfill ────────────────────────────────────────────────────────────


def _item(**kw: Any) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "title": "Procurement legal review",
        "description": f"Legal reviewed the payroll request. Ticket: {JIRA}",
        "blocked_reason": "waiting on the legal lead",
        "ask": None,
        "payload": {},
        "workspace_id": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _rewriter(answer: dict[str, Any]):
    async def rewrite(system: str, user: str) -> str | None:
        return json.dumps(answer)

    return rewrite


@pytest.mark.asyncio
async def test_a_rewritten_legacy_card_gets_its_jira_button() -> None:
    rewrite = await backfill.rewrite_item(_item(), _rewriter(CARD))
    assert rewrite is not None
    assert rewrite.ask["recommendation"] == "go"
    assert rewrite.ask["links"] == [{"label": "FIN-798", "url": JIRA}]


@pytest.mark.asyncio
async def test_a_card_without_recommendation_is_rewritten_whole() -> None:
    calls: list[str] = []

    async def rewrite(system: str, user: str) -> str | None:
        calls.append(system)
        return json.dumps(CARD)

    no_reco = {k: v for k, v in CARD.items() if k != "recommendation"}
    assert backfill.card_unreadable(no_reco)
    result = await backfill.rewrite_item(_item(ask=no_reco), rewrite)
    assert calls == [backfill.SYSTEM_PROMPT]
    assert result is not None and result.ask["recommendation"] == "go"


@pytest.mark.asyncio
async def test_the_backfill_links_readable_cards_without_a_model_and_is_idempotent(monkeypatch) -> None:
    readable = _item(ask=dict(CARD))
    plain = _item(ask=dict(CARD), description="no link")

    async def waiting(db: Any) -> list[Any]:
        return [readable, plain]

    async def store(db: Any, item_id: Any, values: dict[str, Any]) -> bool:
        for item in (readable, plain):
            if item.id == item_id:
                item.ask = values["ask"]
        return True

    async def no_model(system: str, user: str) -> str | None:
        raise AssertionError("no model call for a readable card")

    monkeypatch.setattr(backfill, "_waiting_items", waiting)
    monkeypatch.setattr(backfill, "_store", store)
    report = await backfill.backfill_readable_asks(None, no_model)
    assert report.linked == [str(readable.id)]
    assert readable.ask["links"] == [{"label": "FIN-798", "url": JIRA}]
    assert "links" not in plain.ask
    again = await backfill.backfill_readable_asks(None, no_model)
    assert again.linked == [] and again.rewritten == []


@pytest.mark.asyncio
async def test_the_outcome_is_logged_at_warning(monkeypatch, caplog) -> None:
    """Production keeps WARNING and above: the count must reach it (#834)."""

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def commit(self) -> None:
            return None

    async def allowed(db: Any) -> None:
        return None

    async def free(release: dict[str, Any]) -> bool:
        return False

    async def taken(release: dict[str, Any]) -> bool:
        return True

    async def report(db: Any, rewriter: Any) -> backfill.BackfillReport:
        return backfill.BackfillReport(rewritten=["a"], linked=["b", "c"])

    import app.core.database as database
    import app.services.agentic_gate as gate

    monkeypatch.setattr(database, "async_session", _Session)
    monkeypatch.setattr(gate, "agentic_refusal", allowed)
    monkeypatch.setattr(backfill, "_lease_held", free)
    monkeypatch.setattr(backfill, "_take_lease", taken)
    monkeypatch.setattr(backfill, "backfill_readable_asks", report)
    release = {"version": f"9.9.9-{uuid.uuid4().hex[:6]}", "git_sha": "abc"}
    with caplog.at_level(logging.WARNING, logger=backfill.__name__):
        await backfill.on_release_deployed(release, rewriter=_rewriter(CARD))
    assert any(
        r.levelno == logging.WARNING and "1 card(s) rewritten" in r.getMessage() and "2 given" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_a_lease_held_elsewhere_is_logged(monkeypatch, caplog) -> None:
    async def held(release: dict[str, Any]) -> bool:
        return True

    monkeypatch.setattr(backfill, "_lease_held", held)
    release = {"version": f"9.9.9-{uuid.uuid4().hex[:6]}", "git_sha": "abc"}
    with caplog.at_level(logging.WARNING, logger=backfill.__name__):
        assert await backfill.on_release_deployed(release, rewriter=_rewriter(CARD)) is None
    assert any("another process ran it" in r.getMessage() for r in caplog.records)
