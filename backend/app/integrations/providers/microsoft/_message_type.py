"""The kind of an Outlook message: a plain email or a meeting message.

A meeting request reads like any email when its body holds no Teams join
block (an in-person invite): only its Graph type tells it apart. Graph names
that type in ``@odata.type`` on every item of a ``/messages`` read (a derived
type: ``eventMessageRequest``, ``eventMessageResponse``, ``eventMessage``;
the plain ``message`` type is left out), with no ``$select`` of its own, so a
listing reads it from the request it already makes. ``meetingMessageType``,
a property of the derived type, gives the precise kind (an acceptance, a
decline) when the message was read whole.
"""

from __future__ import annotations

from typing import Any

#: The kind of a message that is not a meeting message.
PLAIN_MESSAGE = "message"

#: The kind each Graph type names, when ``meetingMessageType`` was not read.
_KIND_OF_TYPE = {
    "eventmessagerequest": "meetingRequest",
    "eventmessageresponse": "meetingResponse",
    # Exchange writes a cancellation as the base event message type.
    "eventmessage": "meetingCancelled",
}

#: The ``meetingMessageType`` values Graph documents, as it spells them.
_MEETING_KINDS = {
    kind.lower(): kind
    for kind in (
        "meetingRequest",
        "meetingCancelled",
        "meetingAccepted",
        "meetingTentativelyAccepted",
        "meetingTenativelyAccepted",
        "meetingDeclined",
    )
}


def message_type(message: dict[str, Any]) -> str:
    """``message``, or the meeting kind of *message*: ``meetingRequest``, ``meetingCancelled``, ...

    ``meetingMessageType`` wins when the read carried it; otherwise the
    ``@odata.type`` names the kind (``meetingResponse`` for an answer whose
    verdict was not read).
    """
    graph_type = str(message.get("@odata.type") or "").rsplit(".", 1)[-1].lower()
    kind = _KIND_OF_TYPE.get(graph_type)
    if kind is None:
        return PLAIN_MESSAGE
    precise = _MEETING_KINDS.get(str(message.get("meetingMessageType") or "").lower())
    if precise == "meetingTenativelyAccepted":
        # Graph's own misspelling of the value, read under its documented name.
        return "meetingTentativelyAccepted"
    return precise or kind
