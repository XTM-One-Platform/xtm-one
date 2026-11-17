"""``PATCH /chat/conversations/{id}`` validates the title it stores.

``ConversationUpdate.title`` passed ``strip_whitespace=True`` to ``Field``, a
Pydantic v1 keyword v2 ignores: ``min_length=1`` ran on the raw value, so
``"   "`` was accepted and the handler's own ``strip()`` then stored an empty
title, while ``""`` was refused. The title is now stripped before its length
is checked, so a blank title is refused exactly like an empty one.

``ConversationCreate.title`` stored whatever it was given: a blank title now
falls back to the default title exactly as ``""`` always did.
"""

import pytest
from pydantic import ValidationError

from app.api.v1.chat import ConversationCreate, ConversationUpdate

# A NUL is stripped from every bound string by the engine, so a title made of
# NUL and whitespace would reach the column empty: it is blank too.
BLANKS = ["", "   ", "\t\n ", "\u00a0\u3000", "\x00", " \x00 "]


@pytest.mark.parametrize("blank", BLANKS)
def test_a_blank_title_is_refused_like_an_empty_one(blank):
    with pytest.raises(ValidationError):
        ConversationUpdate(title=blank)


@pytest.mark.parametrize("blank", BLANKS)
def test_a_blank_title_on_create_falls_back_like_an_empty_one(blank):
    # `title=""` on create has always meant "use the default title" (no 422).
    assert ConversationCreate(title=blank).title is None


def test_the_title_is_stripped_on_create():
    assert ConversationCreate(title="  Q3\x00 plan  ").title == "Q3 plan"
    assert ConversationCreate().title is None


def test_a_long_title_on_create_is_cut_to_the_column():
    # The column is VARCHAR(500): a longer title failed the insert with a 500.
    title = ConversationCreate(title="x" * 499 + " yyyy").title
    assert title == "x" * 499


def test_the_title_is_stripped():
    assert ConversationUpdate(title="  New title  ").title == "New title"
    assert ConversationUpdate(title="New\x00 title").title == "New title"


def test_the_length_limit_applies_to_the_stripped_title():
    assert len(ConversationUpdate(title="  " + "x" * 500 + "  ").title) == 500
    with pytest.raises(ValidationError):
        ConversationUpdate(title="x" * 501)
