"""``render_deck`` retries a BROWSER failure and names its cause (issue #10639).

``capture_presentation_slides`` and ``export_presentation`` used to answer
"the platform's headless browser is unavailable" for any exception, with no
retry: a render page that refused the connection in the seconds after a
rollout, or one Chromium crash, failed the call for good. Pinned here:

* a transient browser failure is retried and the second attempt's deck is
  returned;
* a failure that persists raises :class:`HeadlessBrowserUnavailableError`
  naming the cause, the attempts and whether a later call can work;
* Chromium missing on the host is not retried (no retry can cure it);
* the deck's own failure (:class:`PresentationRenderError`) is never retried;
* both tools answer the cause instead of the old blanket sentence, and a
  failure that is not the browser's is not blamed on it.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager

import pytest

from app.services import presentation_export
from app.services.presentation_export import (
    RENDER_ATTEMPTS,
    HeadlessBrowserUnavailableError,
    PresentationRenderError,
    RenderedDeck,
    classify_browser_failure,
)

ORIGIN = "http://127.0.0.1:4000"


class _PlaywrightError(Exception):
    """Stands in for ``playwright.async_api.Error`` (classified by its message)."""


@pytest.fixture
def no_sleep(monkeypatch):
    pauses: list[float] = []

    async def _sleep(seconds: float) -> None:
        pauses.append(seconds)

    monkeypatch.setattr(presentation_export.asyncio, "sleep", _sleep)
    monkeypatch.setattr(presentation_export, "render_base_url", lambda: ORIGIN + "/xtm")
    return pauses


def _scripted(monkeypatch, outcomes: list[object]) -> list[int]:
    """Make ``_render_deck_once`` raise or return each outcome in turn."""
    calls: list[int] = []

    async def _once(*_a, **_k):
        calls.append(1)
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(presentation_export, "_render_deck_once", _once)
    return calls


def _render():
    return asyncio.run(presentation_export.render_deck(uuid.uuid4(), uuid.uuid4(), aspect_ratio="16:9"))


def test_a_refused_connection_after_a_rollout_is_retried_and_the_deck_returned(monkeypatch, no_sleep):
    deck = RenderedDeck(slide_count=10)
    calls = _scripted(
        monkeypatch,
        [_PlaywrightError(f"Page.goto: net::ERR_CONNECTION_REFUSED at {ORIGIN}/xtm/presentations/x/static"), deck],
    )

    assert _render() is deck
    assert len(calls) == 2
    # One pause, released from the render slot (the slot lives in the once call).
    assert no_sleep == [presentation_export.RENDER_RETRY_BACKOFF_S[0]]


def test_a_failure_that_persists_names_its_cause_the_attempts_and_says_a_retry_may_work(monkeypatch, no_sleep):
    crash = _PlaywrightError("Target page, context or browser has been closed")
    calls = _scripted(monkeypatch, [crash] * RENDER_ATTEMPTS)

    with pytest.raises(HeadlessBrowserUnavailableError) as raised:
        _render()

    assert len(calls) == RENDER_ATTEMPTS
    assert raised.value.retryable
    assert raised.value.attempts == RENDER_ATTEMPTS
    message = str(raised.value)
    assert "crashed" in message
    assert f"after {RENDER_ATTEMPTS} attempts" in message
    assert "a later call may work" in message
    assert raised.value.__cause__ is crash


def test_chromium_missing_on_the_host_is_not_retried(monkeypatch, no_sleep):
    calls = _scripted(
        monkeypatch,
        [_PlaywrightError("BrowserType.launch: Executable doesn't exist at /ms-playwright/chromium-1/chrome")],
    )

    with pytest.raises(HeadlessBrowserUnavailableError) as raised:
        _render()

    assert len(calls) == 1
    assert no_sleep == []
    assert not raised.value.retryable
    assert "Chromium is not installed" in str(raised.value)
    assert "retrying will not help" in str(raised.value)


def test_the_decks_own_failure_is_never_retried(monkeypatch, no_sleep):
    calls = _scripted(monkeypatch, [PresentationRenderError("an asset never loaded")])

    with pytest.raises(PresentationRenderError, match="an asset never loaded"):
        _render()

    assert len(calls) == 1
    assert no_sleep == []


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Page.goto: net::ERR_NAME_NOT_RESOLVED at http://x/", f"render page at {ORIGIN} could not be reached"),
        ("Page.goto: Timeout 30000ms exceeded.", "did not answer within 30 seconds"),
        ("Browser closed.", "closed or crashed"),
        ("something new", "_PlaywrightError: something new"),
    ],
)
def test_the_cause_is_read_from_the_browser_message(message, expected):
    exc: Exception = _PlaywrightError(message)
    if "Timeout" in message:
        exc = type("TimeoutError", (Exception,), {})(message)
    failure = classify_browser_failure(exc, origin=ORIGIN)
    assert expected in failure.cause
    # The render token never leaks into the cause: the origin only.
    assert "render_token" not in str(failure)


# ── The two tools ────────────────────────────────────────────────────────────


def _deck_row(user_id: uuid.UUID):
    from tests.test_presentation_export import _deck_row as deck_row

    deck = deck_row(user_id, title="Board deck")
    deck.spec = {**deck.spec, "slides": [{"layout": "content", "title": f"S{i}"} for i in range(3)]}
    return deck


def _wire(monkeypatch, deck, *, render=None, export=None):
    from app.services import presentations
    from app.services.builtin_tools import _shared
    from tests.test_presentation_export import _FakeSession

    @asynccontextmanager
    async def _own_session():
        yield _FakeSession([])

    async def _load(db, deck_id, *, user_id):
        return (deck, [])

    monkeypatch.setattr(_shared, "_own_session", _own_session)
    monkeypatch.setattr(presentations, "load_visible_presentation", _load)
    if render is not None:
        monkeypatch.setattr(presentation_export, "render_deck", render)
    if export is not None:
        monkeypatch.setattr(presentation_export, "export_presentation", export)


def _run(name: str, arguments: dict, user_id: uuid.UUID):
    from app.services.builtin_tools import build_presentation_tools
    from app.tools.base import ToolCall

    _defs, executor = build_presentation_tools(db=None, user_id=str(user_id), conversation_id=None)  # type: ignore[arg-type]
    return asyncio.run(executor.execute_tool(ToolCall(id="c1", name=name, arguments=arguments)))


def test_capture_answers_the_browser_cause_not_the_blanket_sentence(monkeypatch):
    user_id = uuid.uuid4()
    deck = _deck_row(user_id)

    async def _render(*_a, **_k):
        raise HeadlessBrowserUnavailableError(
            "the browser closed or crashed during the render", retryable=True, attempts=3
        )

    _wire(monkeypatch, deck, render=_render)
    result = _run("capture_presentation_slides", {"presentation_id": str(deck.id)}, user_id)

    assert result.is_error
    assert "closed or crashed" in result.content
    assert "after 3 attempts" in result.content
    assert "a later call may work" in result.content
    assert "headless browser is unavailable" not in result.content
    assert not result.files


def test_capture_names_an_unexpected_failure_by_its_type(monkeypatch):
    user_id = uuid.uuid4()
    deck = _deck_row(user_id)

    async def _render(*_a, **_k):
        raise KeyError("slide_links")

    _wire(monkeypatch, deck, render=_render)
    result = _run("capture_presentation_slides", {"presentation_id": str(deck.id)}, user_id)

    assert result.is_error
    assert "KeyError" in result.content
    assert "headless browser is unavailable" not in result.content


def test_export_answers_the_browser_cause(monkeypatch):
    user_id = uuid.uuid4()
    deck = _deck_row(user_id)

    async def _export(**_k):
        raise HeadlessBrowserUnavailableError("Chromium is not installed for Playwright on this host", retryable=False)

    _wire(monkeypatch, deck, export=_export)
    result = _run("export_presentation", {"presentation_id": str(deck.id), "format": "pdf"}, user_id)

    assert result.is_error
    assert "Chromium is not installed" in result.content
    assert "retrying will not help" in result.content
    assert f"/presentations/{deck.id}" in result.content


def test_export_does_not_blame_the_browser_for_a_storage_failure(monkeypatch):
    user_id = uuid.uuid4()
    deck = _deck_row(user_id)

    async def _export(**_k):
        raise OSError("bucket unreachable")

    _wire(monkeypatch, deck, export=_export)
    result = _run("export_presentation", {"presentation_id": str(deck.id), "format": "pdf"}, user_id)

    assert result.is_error
    assert "OSError: bucket unreachable" in result.content
    assert "headless browser" not in result.content
