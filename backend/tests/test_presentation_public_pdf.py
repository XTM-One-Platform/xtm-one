"""The PDF a public link may offer.

What these pin, with the no-DB fakes of ``test_presentations.py``:

* the option belongs to the link: off on a new link, kept by a regenerate,
  set by its managers (``set_pdf_download``), read back by
  ``get_presentation`` and the tool's result;
* the ``set_presentation_public_link`` tool's ``pdf_download``: read as a
  choice (a guess is refused before any query), one audit row per change,
  ignored by ``disable``;
* the render is bounded: one render per deck content per worker (cache +
  single flight), a new render for any visible change, the cache capped in
  bytes, the render budget counted on misses only, and the render itself
  printed from the PUBLIC static page in the worker's one public slot.

``tests/test_presentation_public_links_postgres.py`` drives the routes end to
end, the headless browser stubbed.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.models.presentation import PresentationPublicLink
from app.services import presentation_export as export
from app.services import presentation_public_links as links
from app.services.presentation_public_links import (
    OUTCOME_CREATED,
    OUTCOME_PDF_ON,
    OUTCOME_REGENERATED,
    format_public_link_result,
    normalize_download_flag,
    pdf_fingerprint,
    public_link_status_line,
    set_pdf_download,
)
from tests.test_presentations import (
    _account,
    _audit_rows,
    _build_tools,
    _compiled,
    _deck_lookup,
    _FakeSession,
    _q3_deck,
    _Result,
    _run_tool,
    _session_factory,
)


@pytest.fixture
def fake_session(monkeypatch):
    import app.core.database as database

    def _install(results) -> _FakeSession:
        session = _FakeSession(results=results)
        monkeypatch.setattr(database, "async_session", _session_factory(session))
        return session

    return _install


@pytest.fixture(autouse=True)
def _fresh_cache():
    links.clear_pdf_cache()
    yield
    links.clear_pdf_cache()


def _link_row(deck, *, allow: bool = False) -> PresentationPublicLink:
    return PresentationPublicLink(
        id=uuid.uuid4(), presentation_id=deck.id, token_hash="h", token_enc="e", allow_pdf_download=allow
    )


def _set_link(executor, deck, **arguments):
    return _run_tool(executor, "set_presentation_public_link", {"presentation_id": str(deck.id), **arguments})


# ── The option ───────────────────────────────────────────────────────────────


def test_the_option_belongs_to_the_link_and_changes_only_when_it_differs():
    owner = _account("samuel@filigran.io")
    deck = _q3_deck(owner)

    assert asyncio.run(set_pdf_download(_FakeSession(results=[_Result(one=None)]), deck.id, True)) is None

    row = _link_row(deck, allow=True)
    assert asyncio.run(set_pdf_download(_FakeSession(results=[_Result(one=row)]), deck.id, True)) is False

    session = _FakeSession(results=[_Result(one=row)])
    assert asyncio.run(set_pdf_download(session, deck.id, False)) is True
    assert row.allow_pdf_download is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), ("", None), (True, True), (False, False), ("true", True), ("Off", False), ("allow", True)],
)
def test_pdf_download_is_read_as_a_choice(value, expected):
    assert normalize_download_flag(value, field="pdf_download") is expected


@pytest.mark.parametrize("value", ["maybe", "sometimes", 2, ["yes"]])
def test_a_pdf_download_guess_is_refused(value):
    with pytest.raises(ValueError):
        normalize_download_flag(value, field="pdf_download")


def test_the_status_line_and_the_result_say_whether_visitors_may_download():
    assert "and download it as a PDF" in public_link_status_line(has_link=True, allow_pdf_download=True)
    assert "(no PDF download)" in public_link_status_line(has_link=True, allow_pdf_download=False)
    assert public_link_status_line(has_link=False, allow_pdf_download=True) == ""
    token = links.mint_token()
    assert "Visitors can now also download it as a PDF" in format_public_link_result(
        "Q3", OUTCOME_CREATED, token, allow_pdf_download=True, pdf_changed=True
    )
    assert "Visitors cannot download it" in format_public_link_result("Q3", OUTCOME_CREATED, token)


# ── The tool ─────────────────────────────────────────────────────────────────


def test_the_tool_turns_the_link_on_with_its_pdf_and_audits_both(fake_session):
    owner = _account("samuel@filigran.io")
    deck = _q3_deck(owner)
    row = _link_row(deck)
    session = fake_session([*_deck_lookup(deck), _Result(one=owner), _Result(one=uuid.uuid4()), _Result(one=row)])

    _defs, executor = _build_tools(str(owner.id))
    result = _set_link(executor, deck, pdf_download=True)

    assert not result.is_error, result.content
    assert row.allow_pdf_download is True
    assert "Visitors can now also download it as a PDF" in result.content
    assert [audit.details["public_link"] for audit in _audit_rows(session)] == [OUTCOME_CREATED, OUTCOME_PDF_ON]
    assert all(audit.details["via"] == "agent" for audit in _audit_rows(session))
    assert session.committed


def test_a_regenerate_keeps_the_pdf_option(fake_session):
    owner = _account("samuel@filigran.io")
    deck = _q3_deck(owner)
    # The rotating UPDATE returns the option it kept.
    session = fake_session([*_deck_lookup(deck), _Result(one=owner), _Result(one=True)])

    _defs, executor = _build_tools(str(owner.id))
    result = _set_link(executor, deck, action="regenerate")

    assert not result.is_error, result.content
    assert "allow_pdf_download" in _compiled(session.statements[3])
    assert "Visitors can also download it as a PDF" in result.content
    assert [audit.details["public_link"] for audit in _audit_rows(session)] == [OUTCOME_REGENERATED]


def test_a_pdf_download_guess_is_refused_before_any_query(fake_session):
    owner = _account("samuel@filigran.io")
    deck = _q3_deck(owner)
    session = fake_session([])
    _defs, executor = _build_tools(str(owner.id))
    result = _set_link(executor, deck, pdf_download="maybe")
    assert "pdf_download must be true or false" in result.content
    assert session.statements == []


def test_the_tool_documents_the_option():
    defs, _executor = _build_tools()
    tool = next(d for d in defs if d.name == "set_presentation_public_link")
    assert tool.input_schema["properties"]["pdf_download"]["type"] == "boolean"
    assert "a new link offers no download" in tool.description
    assert "Set it only when the user asks" in tool.description


# ── The render, bounded ──────────────────────────────────────────────────────


class _Renders:
    """A stand-in for the headless render: counts, and lets concurrent asks overlap."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, token: str, *, aspect_ratio: str) -> bytes:
        self.calls.append((token, aspect_ratio))
        await asyncio.sleep(0.05)
        return f"%PDF-1.7 render {len(self.calls)}".encode()


@pytest.fixture
def renders(monkeypatch):
    stand_in = _Renders()
    budget: list[str] = []

    async def _no_release(_db):
        return None

    async def _budget(_request, name, *, bucket=None, **_kw):
        budget.append(f"{name}:{bucket}")

    import app.core.database as database
    import app.core.rate_limit as rate_limit

    monkeypatch.setattr(export, "render_public_deck_pdf", stand_in)
    monkeypatch.setattr(database, "release_db_connection_before_loop", _no_release)
    monkeypatch.setattr(rate_limit, "enforce_rate_limit", _budget)
    stand_in.budget = budget  # type: ignore[attr-defined]
    return stand_in


def _payload_for(monkeypatch, payloads: dict):
    async def _payload(_db, presentation, _link=None):
        return payloads[presentation.id]

    monkeypatch.setattr(links, "public_presentation_payload", _payload)


def _deck(title: str = "Q3 flash"):
    return SimpleNamespace(id=uuid.uuid4(), title=title)


def test_a_room_downloading_at_once_costs_one_render(monkeypatch, renders):
    deck = _deck()
    link = SimpleNamespace(id=uuid.uuid4(), allow_pdf_download=True)
    _payload_for(monkeypatch, {deck.id: {"id": str(deck.id), "revision": 3, "aspect_ratio": "4:3", "spec": {"a": 1}}})

    async def _room():
        return await asyncio.gather(*(links.public_pdf(_FakeSession(), deck, link, "tok") for _ in range(12)))

    served = asyncio.run(_room())
    assert len(renders.calls) == 1
    assert renders.calls[0] == ("tok", "4:3")
    assert {pdf.data for pdf in served} == {b"%PDF-1.7 render 1"}
    assert sum(not pdf.cached for pdf in served) == 1
    assert served[0].filename == "q3-flash.pdf"
    # The link's render budget is counted on the miss only.
    assert renders.budget == [f"presentation_public_pdf_render:{link.id}"]

    again = asyncio.run(links.public_pdf(_FakeSession(), deck, link, "tok"))
    assert again.cached and len(renders.calls) == 1


def test_any_visible_change_is_a_new_render(monkeypatch, renders):
    deck = _deck()
    link = SimpleNamespace(id=uuid.uuid4(), allow_pdf_download=True)
    payloads = {deck.id: {"id": str(deck.id), "revision": 3, "spec": {"slides": [{"title": "Q3"}]}}}
    _payload_for(monkeypatch, payloads)

    asyncio.run(links.public_pdf(_FakeSession(), deck, link, "tok"))
    payloads[deck.id] = {**payloads[deck.id], "revision": 4, "spec": {"slides": [{"title": "Q3, revised"}]}}
    revised = asyncio.run(links.public_pdf(_FakeSession(), deck, link, "tok"))

    assert len(renders.calls) == 2
    assert revised.data == b"%PDF-1.7 render 2" and not revised.cached


def test_the_fingerprint_follows_what_the_page_shows_not_the_offer():
    shown = {"id": "d", "revision": 2, "spec": {"slides": [1]}, "theme_config": {"accent": "#0ff"}}
    assert pdf_fingerprint({**shown, "pdf_download": True}) == pdf_fingerprint({**shown, "pdf_download": False})
    assert pdf_fingerprint(shown) != pdf_fingerprint({**shown, "theme_config": {"accent": "#f0f"}})


def test_the_cache_is_capped_in_bytes_and_skips_an_oversized_pdf(monkeypatch):
    monkeypatch.setattr(links, "PUBLIC_PDF_CACHE_MAX_BYTES", 10)
    monkeypatch.setattr(links, "PUBLIC_PDF_MAX_CACHED_BYTES", 6)
    links._pdf_cache_put("a", b"12345")
    links._pdf_cache_put("b", b"12345")
    assert links._pdf_cache_get("a") == b"12345"  # a is now the most recent
    links._pdf_cache_put("c", b"12345")
    assert links._pdf_cache_get("b") is None  # evicted, least recently used
    assert links._pdf_cache_get("a") == b"12345"
    links._pdf_cache_put("big", b"1234567")
    assert links._pdf_cache_get("big") is None


def test_an_expired_render_is_rendered_again(monkeypatch, renders):
    deck = _deck()
    link = SimpleNamespace(id=uuid.uuid4(), allow_pdf_download=True)
    _payload_for(monkeypatch, {deck.id: {"id": str(deck.id), "revision": 1}})
    monkeypatch.setattr(links, "PUBLIC_PDF_CACHE_SECONDS", 0)

    asyncio.run(links.public_pdf(_FakeSession(), deck, link, "tok"))
    asyncio.run(links.public_pdf(_FakeSession(), deck, link, "tok"))
    assert len(renders.calls) == 2


def test_the_render_is_printed_from_the_public_page_in_one_public_slot(monkeypatch):
    monkeypatch.setattr(settings, "presentation_render_base_url", "http://render.internal:4000")
    monkeypatch.setattr(settings, "base_path", "")
    opened: list[str] = []
    printed: list[dict] = []
    active = {"now": 0, "max": 0}

    class _Slides:
        async def count(self):
            return 2

    class _Page:
        def locator(self, selector):
            assert selector == ".deck-static-slide"
            return _Slides()

        async def pdf(self, **kwargs):
            printed.append(kwargs)
            return b"%PDF-1.7"

    @asynccontextmanager
    async def _open(url, **_kw):
        opened.append(url)
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        try:
            await asyncio.sleep(0.02)
            yield _Page()
        finally:
            active["now"] -= 1

    monkeypatch.setattr(export, "open_render_page", _open)
    export._public_render_slots.clear()

    async def _two():
        return await asyncio.gather(
            export.render_public_deck_pdf("Ab-_9", aspect_ratio="A4"),
            export.render_public_deck_pdf("Ab-_9", aspect_ratio="16:9"),
        )

    assert asyncio.run(_two()) == [b"%PDF-1.7", b"%PDF-1.7"]
    assert opened == ["http://render.internal:4000/public/presentations/Ab-_9/static"] * 2
    # One public render at a time per worker, the shared slots left for accounts.
    assert export.MAX_CONCURRENT_PUBLIC_RENDERS == 1 < export.MAX_CONCURRENT_RENDERS
    assert active["max"] == 1
    assert (printed[0]["width"], printed[0]["height"]) == export.PDF_PAGE_SIZES_IN["A4"]
    assert printed[0]["scale"] == export.PDF_PRINT_SCALES["A4"]
    assert printed[1]["scale"] == export.PDF_PRINT_SCALES["16:9"]


def test_a_page_with_no_slides_fails_the_render(monkeypatch):
    class _Page:
        def locator(self, _selector):
            return SimpleNamespace(count=lambda: asyncio.sleep(0, result=0))

    @asynccontextmanager
    async def _open(_url, **_kw):
        yield _Page()

    monkeypatch.setattr(export, "open_render_page", _open)
    export._public_render_slots.clear()
    with pytest.raises(export.PresentationRenderError):
        asyncio.run(export.render_public_deck_pdf("tok", aspect_ratio="16:9"))
