"""A reply an agent account posts carries links, never raw artifact markers (#11475).

Teams, Outlook, Gmail and WhatsApp draw none of the web chat's cards: the
deck, report, video and dashboard markers of the answer become links to
their web-app page, scoped to the account's owner, before the provider posts.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services.agent_accounts import pipeline

DECK = uuid.uuid4()
OTHER_DECK = uuid.uuid4()
OWNER = uuid.uuid4()
BASE = "https://one.example.com"


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _Session:
    """Answers each SELECT with the rows of the decks it knows, in the owner's scope."""

    def __init__(self, rows):
        self._rows = rows
        self.statements: list = []

    async def execute(self, statement, *args, **kwargs):
        self.statements.append(statement)
        return _Result(self._rows)


@pytest.fixture(autouse=True)
def _frontend_url(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "frontend_url", BASE)


def _account():
    return SimpleNamespace(id=uuid.uuid4(), user_id=OWNER)


async def test_a_deck_marker_becomes_a_link_with_its_title():
    db = _Session([SimpleNamespace(id=DECK, title="Q3 review")])
    out = await pipeline._markers_as_links(db, _account(), f"Here it is:\n\n[[PRESENTATION:{DECK}]]")
    assert out == f"Here it is:\n\n[Q3 review]({BASE}/presentations/{DECK})"
    # Scoped to the owner: the query names the account's user.
    assert str(OWNER).replace("-", "") in str(db.statements[0].compile(compile_kwargs={"literal_binds": True}))


async def test_the_slide_anchor_survives():
    db = _Session([SimpleNamespace(id=DECK, title="Defense deck")])
    out = await pipeline._markers_as_links(db, _account(), f"[[PRESENTATION:{DECK}#/8520ee3dc4ff]]")
    assert out == f"[Defense deck]({BASE}/presentations/{DECK}#/8520ee3dc4ff)"


async def test_an_unknown_deck_is_dropped_never_left_raw():
    db = _Session([])
    out = await pipeline._markers_as_links(db, _account(), f"See [[PRESENTATION:{DECK}#/abc]] now")
    assert "[[" not in out and str(DECK) not in out


async def test_two_markers_in_one_reply_are_both_linked():
    db = _Session([SimpleNamespace(id=DECK, title="One"), SimpleNamespace(id=OTHER_DECK, title=None)])
    out = await pipeline._markers_as_links(
        db, _account(), f"[[PRESENTATION:{DECK}]] and [[PRESENTATION:{OTHER_DECK}#/s2]]"
    )
    assert "[[" not in out
    assert f"[One]({BASE}/presentations/{DECK})" in out
    # No title: the catalog label, still a link on the slide.
    assert f"]({BASE}/presentations/{OTHER_DECK}#/s2)" in out


async def test_a_reply_without_a_marker_reads_nothing():
    db = _Session([])
    assert await pipeline._markers_as_links(db, _account(), "Plain [link](x)") == "Plain [link](x)"
    assert db.statements == []


async def test_post_sends_links_and_keeps_files_as_attachments(monkeypatch):
    """The text the provider posts has the deck link; a file marker still goes out as a file."""
    file_id = uuid.uuid4()
    sent: list[tuple[str, list]] = []

    class _Provider:
        capabilities = SimpleNamespace(triage=False)

        async def send_reply(self, session, route, text):
            sent.append((text, []))
            return "m-1"

        async def send_reply_files(self, session, route, text, files):
            sent.append((text, files))
            return "m-1"

    async def _files(db, account, ids):
        assert ids == [str(file_id)]
        return ["report.pdf"]

    async def _none(*args, **kwargs):
        return None

    monkeypatch.setattr(pipeline, "_reply_files", _files)
    monkeypatch.setattr(pipeline, "_sign", _none)
    monkeypatch.setattr(pipeline, "_owner_email", _none)
    monkeypatch.setattr(pipeline, "_follow_thread", _none)
    for name in ("mark_sending", "mark_sent", "mark_agent_post", "mark_replied"):
        monkeypatch.setattr(pipeline.state, name, _none)
    monkeypatch.setattr("app.services.agent_accounts.reactions.schedule_first_check", _none)

    account = SimpleNamespace(
        id=uuid.uuid4(), user_id=OWNER, provider="microsoft_teams", mailbox_actions=None, last_reply_at=None
    )
    reply = SimpleNamespace(
        ai_label=False, route={}, message_id="in-1", conversation_id="c-1", thread_id="t-1", origin=None
    )
    db = _Session([SimpleNamespace(id=DECK, title="Q3 review")])
    session = SimpleNamespace(delivery={})

    await pipeline._post(
        db, session, _Provider(), account, reply, f"Deck [[PRESENTATION:{DECK}#/s1]]\n\n[[FILE:{file_id}]]"
    )

    ((text, files),) = sent
    assert text == f"Deck [Q3 review]({BASE}/presentations/{DECK}#/s1)"
    assert files == ["report.pdf"]
    assert reply.sent_text == text
