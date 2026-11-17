"""The REST chat turn commits before it answers.

``get_db`` commits on dependency teardown, which FastAPI runs AFTER the
response is sent. A handler whose response the caller reads back on its next
request must therefore commit itself, or that request can arrive before the
transaction lands: the A2A e2e specs read a conversation 14 ms after a 200
that carried the reply and got the user message alone, and read the
delegation rows a turn recorded on the same session and found none.

``send_message`` is far too large to drive through a fake session, so the
contract is pinned on its source: the assistant message is stored, the
session is committed, and only then is the ``SendMessageResponse`` built.
"""

from __future__ import annotations

import inspect

from app.api.v1 import chat


def _send_message_source() -> str:
    return inspect.getsource(chat.send_message)


def test_send_message_commits_after_storing_the_reply_and_before_responding():
    source = _send_message_source()
    stored = source.index("assistant_msg = await store_message(")
    responded = source.rindex("return SendMessageResponse(")
    assert stored < responded

    between = source[stored:responded]
    assert "await db.commit()" in between, (
        "send_message must commit the request session after storing the "
        "assistant message and before building its response: the get_db "
        "teardown commit runs after the response is sent"
    )


def test_the_final_response_is_the_only_one_after_the_reply_is_stored():
    """The commit guards THE return the reply rides on, not an earlier exit."""
    source = _send_message_source()
    stored = source.index("assistant_msg = await store_message(")
    assert source[stored:].count("return SendMessageResponse(") == 1
