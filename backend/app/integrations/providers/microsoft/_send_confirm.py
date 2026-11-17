"""Whether an Outlook send answered by a gateway error or a timeout went out.

A Graph send (``sendMail``, a draft's ``/send``) answered by a 5xx, a timeout
or a broken connection may still have been delivered: the failing tier is often
the gateway in front of a mailbox that took the message. Sending again risks a
duplicate in the recipient's inbox; giving up risks a mail that never left.

The send tools never retry such a send. They read the mailbox's Sent Items over
a short window instead and answer one of three states:

* sent: a copy is there (matched by its internet message id when the draft has
  one, else by subject and To / Cc), named in the answer;
* not sent: Sent Items was read and holds no such copy;
* unknown: Sent Items could not be read; the answer says to check it before
  any new send.

A refusal (4xx), a rate limit (429) or a connection never opened is not
ambiguous: Graph took nothing, the error is answered as it is.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from app.services import mail_send_guard

#: Seconds waited before each Sent Items read: the copy lands a few seconds
#: after Graph accepts the message, so the first read waits too.
READ_DELAYS_SECONDS: tuple[float, ...] = (2.0, 4.0, 8.0)
#: How far before the send the Sent Items listing starts (clock skew between
#: this server and Exchange); short, so an earlier copy of the same subject is
#: not taken for this one.
WINDOW_SKEW_SECONDS = 30
#: The Sent Items fields a match reads.
SENT_COPY_SELECT = "id,subject,toRecipients,ccRecipients,sentDateTime,conversationId,webLink,internetMessageId"

# The error strings ``format_http_error`` / ``format_network_error`` build for
# a SEND request whose outcome is unknown: a 5xx, a timeout, a transport
# failure after the connection opened. The send actions all start with "send"
# ("send email", "send draft", "send draft MIME"); a failed read before the
# send ("read draft MIME") is not one.
_AMBIGUOUS_RE = re.compile(r"(?:server error \(5\d\d\)|request timed out|request failed) while trying to send\b")


def is_ambiguous_send_error(err: object) -> bool:
    """Whether a send's error leaves open that Graph delivered the message."""
    return bool(err) and bool(_AMBIGUOUS_RE.search(str(err)))


def can_match(*, subject: object, recipients: Iterable[object], internet_message_id: object) -> bool:
    """Whether a Sent Items copy of the message can be told from other mail at all."""
    return bool(
        str(internet_message_id or "").strip()
        or mail_send_guard.normalize_subject(subject)
        or mail_send_guard.normalize_addresses(recipients)
    )


def find_sent_copy(
    messages: Iterable[Any],
    *,
    subject: object,
    recipients: Iterable[object],
    internet_message_id: object = "",
) -> dict[str, Any] | None:
    """The Sent Items copy of the message among *messages* (a listing already bounded to the window).

    By internet message id first; else the first copy with the same subject
    (reply / forward prefixes aside) and the same To / Cc addresses.
    """
    copies = [m for m in messages if isinstance(m, dict)]
    wanted_id = str(internet_message_id or "").strip()
    if wanted_id:
        for copy in copies:
            if str(copy.get("internetMessageId") or "").strip() == wanted_id:
                return copy
    if not can_match(subject=subject, recipients=recipients, internet_message_id=""):
        return None
    wanted_subject = mail_send_guard.normalize_subject(subject)
    wanted_recipients = mail_send_guard.normalize_addresses(recipients)
    for copy in copies:
        if mail_send_guard.normalize_subject(copy.get("subject")) != wanted_subject:
            continue
        found = [
            (r.get("emailAddress") or {}).get("address")
            for field in ("toRecipients", "ccRecipients")
            for r in copy.get(field) or []
            if isinstance(r, dict)
        ]
        if mail_send_guard.normalize_addresses(found) == wanted_recipients:
            return copy
    return None


def _first_line(err: object) -> str:
    return str(err or "").strip().splitlines()[0] if str(err or "").strip() else "an error"


def sent_note(err: object, copy: dict[str, Any]) -> str:
    """The line a send confirmed in Sent Items adds to the tool's answer."""
    parts = [f"id {copy.get('id')}"] if copy.get("id") else []
    if copy.get("sentDateTime"):
        parts.append(f"sent at {copy.get('sentDateTime')}")
    if copy.get("webLink"):
        parts.append(str(copy.get("webLink")))
    named = ", ".join(parts) or "a matching copy"
    return (
        f" Graph answered the send with an error ({_first_line(err)}), but the message is in Sent Items "
        f"({named}): it was sent, and was not sent again."
    )


def not_sent_answer(err: object) -> str:
    """The answer of a send whose error left the state open and whose Sent Items holds no copy."""
    return (
        f"{_first_line(err)}\nNOT sent: Sent Items, read after the error, holds no copy of this message. "
        "Nothing was retried; sending it again is safe."
    )


def unknown_answer(err: object, why: str) -> str:
    """The answer of a send whose error left the state open and that could not be confirmed either way."""
    return (
        f"{_first_line(err)}\nSend state UNKNOWN: {why}. Nothing was retried. Read Sent Items "
        "(list_microsoft_emails folder='sentitems') before sending this message again, or it may go twice."
    )
