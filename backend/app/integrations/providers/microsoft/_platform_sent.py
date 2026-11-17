"""The mark every message this platform composes carries, read back by the mail listings.

A Sent Items copy of a message the platform built (a send, a draft, a reply,
a forward) looks like any other: telling it from a message the user typed in
Outlook took a raw read of each message (its markup and the content ids of
its inline images), 20k to 40k tokens for one boolean.

Each message the mail tools compose is stamped once with an internet header
named property (``X-Xtm-One-Sent``, namespace ``PS_INTERNET_HEADERS``): set as
a ``singleValueExtendedProperties`` entry where Graph takes JSON (a send, a
draft's creation or PATCH), and as the MIME header where a draft goes out as
its MIME. Exchange keeps both as the same named property on the Sent Items
copy, so a listing reads it with one ``$expand`` in the request it already
makes: no extra Graph call per message. A message sent before the stamp
existed, or typed in Outlook, reads not platform-sent.
"""

from __future__ import annotations

from typing import Any

#: The MIME header a message sent as its MIME carries.
PLATFORM_SENT_HEADER = "X-Xtm-One-Sent"
#: Its value: the product that built the message.
PLATFORM_SENT_VALUE = "xtm-one"
#: The same mark as Graph names it: an internet header named property
#: (``PS_INTERNET_HEADERS``), whose name Exchange stores in lower case.
PLATFORM_SENT_PROPERTY_ID = "String {00020386-0000-0000-C000-000000000046} Name x-xtm-one-sent"
#: The ``$expand`` a message read adds to get the mark with the message.
PLATFORM_SENT_EXPAND = f"singleValueExtendedProperties($filter=id eq '{PLATFORM_SENT_PROPERTY_ID}')"
#: The state token of the compact listing line of a platform-sent message.
PLATFORM_SENT_STATE = "platform-sent"


def platform_sent_properties() -> list[dict[str, str]]:
    """The ``singleValueExtendedProperties`` a Graph message write carries to stamp it."""
    return [{"id": PLATFORM_SENT_PROPERTY_ID, "value": PLATFORM_SENT_VALUE}]


def stamp_platform_sent(message: dict[str, Any]) -> dict[str, Any]:
    """*message* (a Graph message resource or PATCH) with the mark added; it is changed in place and returned."""
    kept = [
        prop
        for prop in message.get("singleValueExtendedProperties") or []
        if str((prop or {}).get("id") or "").lower() != PLATFORM_SENT_PROPERTY_ID.lower()
    ]
    message["singleValueExtendedProperties"] = kept + platform_sent_properties()
    return message


def platform_sent_header() -> dict[str, str]:
    """The header a message sent as its MIME carries (``with_extra_headers`` form)."""
    return {"name": PLATFORM_SENT_HEADER, "value": PLATFORM_SENT_VALUE}


def is_platform_sent(message: dict[str, Any]) -> bool:
    """Whether *message* (read with :data:`PLATFORM_SENT_EXPAND`) carries the mark."""
    for prop in message.get("singleValueExtendedProperties") or []:
        if not isinstance(prop, dict):
            continue
        if str(prop.get("id") or "").lower() == PLATFORM_SENT_PROPERTY_ID.lower() and str(prop.get("value") or "").strip():
            return True
    return False
