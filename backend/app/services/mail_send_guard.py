# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""One answer per conversation from a mailbox an agent sends through (layer 2).

Layer 1 (``event_run_dedup``) keeps one inbound event from starting two runs of
an agent. This is the last line behind it, on the send itself: before a mail
tool sends, it takes a short lock on the conversation for the sending agent,
looks for a message the agent (any of its runs) already sent in that
conversation within the window, and refuses the send naming that message
unless the caller passes ``force_duplicate=true``. Two runs answering the same
thread three seconds apart then send one reply, whatever let the second run
start.

A conversation is the provider's thread id when the message has one (a reply
or a draft of a thread); a new email has none before it is sent, so its thread
is the normalised subject (``Re:`` / ``Fwd:`` prefixes dropped) and its To / Cc
addresses, which is what its Sent Items copy repeats. The guard is scoped to
the sending mailbox (the integration) when an agent sends (an event
run and a chat run, or two agents, answering one thread from one mailbox
share the lock and the record); a send outside an agent's tools (no agent) is not
guarded, since a person repeating a mail on purpose is not two runs answering
one inbound mail.

Two sources say a message was already sent: the record this module writes
after each guarded send (one key per mailbox and thread, expiring with the
window) and the provider's Sent Items, which the mail tool reads when no
record is found so a record lost to a Redis outage never lets a duplicate out.

An earlier send is not a duplicate when the conversation moved on after it:
when a message from someone else (not this mailbox) arrived in the
conversation after the earlier send, the new send answers that message and
goes out (``inbound_after``; the mail tool reads the conversation once). Only
when nothing new arrived, or when the conversation cannot be read, is the
send refused. An automatic reply (an out-of-office answer to the earlier
send, :func:`is_automatic_reply`) is not something new to answer: it never
lets a second send through.

Redis itself is fail-open: an unreachable store neither blocks a send nor
holds a lock, and the Sent Items check still runs.

The window is ``MAIL_SEND_GUARD_WINDOW_SECONDS`` (default 300 s; 0 turns the
guard off).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.config import settings
from app.core.redis import redis_client
from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: Longest window a deployment may set (a day): past that, a guard is a block.
MAX_WINDOW_SECONDS = 86400
#: The default when the setting cannot be read as a whole number.
DEFAULT_WINDOW_SECONDS = 300
#: How long one send attempt may hold a thread's lock: a Graph send and its
#: Sent Items read take seconds; the lock is released as soon as the attempt
#: ends, this bound is for a worker that dies holding it.
LOCK_TTL_SECONDS = 60

_KEY_PREFIX = "mail_send_guard"
_REPLY_PREFIX_RE = re.compile(r"^\s*(?:(?:re|fw|fwd|aw|tr|sv|vs|wg)\s*:\s*)+", re.IGNORECASE)
#: The subject prefixes mail servers put on an out-of-office answer (Exchange
#: and Outlook in the languages they ship, plus the common generic ones).
_AUTO_REPLY_SUBJECT_RE = re.compile(
    r"^\s*(?:automatic reply|auto(?:matic)?[- ]?(?:reply|response)|out of (?:the )?office|réponse automatique"
    r"|absence du bureau|abwesenheitsnotiz|automatische antwort|respuesta automática|risposta automatica"
    r"|resposta automática|automatisch antwoord|autosvar|automaattinen vastaus|automatická odpověď"
    r"|odpowiedź automatyczna)\s*:",
    re.IGNORECASE,
)
#: Headers whose presence alone marks an automatic answer.
_AUTO_REPLY_FLAG_HEADERS = frozenset({"x-autoreply", "x-autorespond", "x-auto-reply"})


def window_seconds(value: object = None) -> int:
    """The guard's window in seconds, clamped to ``0..MAX_WINDOW_SECONDS``.

    *value* defaults to ``settings.mail_send_guard_window_seconds``; ``0``
    turns the guard off; a value that is not a whole number falls back to
    :data:`DEFAULT_WINDOW_SECONDS`.
    """
    raw = settings.mail_send_guard_window_seconds if value is None else value
    if isinstance(raw, bool):
        return DEFAULT_WINDOW_SECONDS
    try:
        return max(0, min(MAX_WINDOW_SECONDS, int(raw)))
    except (TypeError, ValueError):
        return DEFAULT_WINDOW_SECONDS


def normalize_subject(subject: object) -> str:
    """A subject without its reply / forward prefixes, whitespace collapsed, lower-cased."""
    text = " ".join(str(subject or "").split())
    return _REPLY_PREFIX_RE.sub("", text).strip().lower()


def normalize_addresses(recipients: Iterable[object]) -> tuple[str, ...]:
    """The distinct addresses of *recipients*, lower-cased and sorted."""
    return tuple(sorted({str(a).strip().lower() for a in recipients if str(a or "").strip()}))


def thread_fingerprint(subject: object, recipients: Iterable[object]) -> str:
    """The thread of a mail that has no conversation id yet: its subject and recipients."""
    payload = "\n".join([normalize_subject(subject), *normalize_addresses(recipients)])
    return "subject:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def thread_key(conversation_id: object, *, subject: object = None, recipients: Iterable[object] = ()) -> str:
    """The guard's thread: the provider's conversation id when given, else :func:`thread_fingerprint`."""
    conversation = str(conversation_id or "").strip()
    if conversation:
        return "conversation:" + hashlib.sha256(conversation.encode("utf-8")).hexdigest()
    return thread_fingerprint(subject, recipients)


def iso_instant(epoch: float) -> str:
    """*epoch* as the second-precision ISO-8601 UTC instant a provider's date filters take."""
    return datetime.fromtimestamp(max(0.0, epoch), tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def since_iso(now: float, window: int) -> str:
    """The start of the window as the ISO-8601 instant a provider's ``sentDateTime`` filter takes."""
    return iso_instant(now - max(0, int(window)))


def _sender_address(message: dict) -> str:
    sender = message.get("from") or message.get("sender") or {}
    address = (sender.get("emailAddress") or {}).get("address") if isinstance(sender, dict) else ""
    return str(address or "").strip().lower()


def is_automatic_reply(message: dict[str, object]) -> bool:
    """Whether *message* is an automatic answer (an out-of-office reply), not a person's.

    Read from its ``internetMessageHeaders`` when the listing carries them
    (RFC 3834 ``Auto-Submitted`` other than ``no``, ``X-Autoreply`` /
    ``X-Autorespond``, ``Precedence: auto_reply``) and from its subject (the
    "Automatic reply:" prefix Exchange and the common servers write, in their
    languages).
    """
    headers = message.get("internetMessageHeaders")
    if isinstance(headers, list):
        for header in headers:
            if not isinstance(header, dict):
                continue
            name = str(header.get("name") or "").strip().lower()
            value = str(header.get("value") or "").strip().lower()
            if name == "auto-submitted" and value and value != "no":
                return True
            if name in _AUTO_REPLY_FLAG_HEADERS:
                return True
            if name == "precedence" and value == "auto_reply":
                return True
    return bool(_AUTO_REPLY_SUBJECT_RE.match(str(message.get("subject") or "")))


def inbound_after(messages: Iterable[object], *, after: float, own_address: object) -> bool | None:
    """Whether a message from someone else arrived in the conversation strictly after *after*.

    *messages* are the conversation's messages as the provider lists them
    (``receivedDateTime``, ``from``, ``isDraft``, and ``subject`` /
    ``internetMessageHeaders`` to tell an automatic reply). Drafts, messages
    this mailbox sent (*own_address*) and automatic replies
    (:func:`is_automatic_reply`: an out-of-office answer to the earlier send
    is nothing to answer) do not count. ``None`` when a later message exists
    but the mailbox's own address is unknown, so whether it is an inbound one
    cannot be told: the caller keeps the refusal.
    """
    own = str(own_address or "").strip().lower()
    unknown = False
    for message in messages:
        if not isinstance(message, dict) or message.get("isDraft") or is_automatic_reply(message):
            continue
        received = _epoch(message.get("receivedDateTime"))
        if received is None or received <= after:
            continue
        sender = _sender_address(message)
        if not own or not sender:
            unknown = True
            continue
        if sender != own:
            return True
    return None if unknown else False


def _epoch(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


@dataclass(frozen=True, slots=True)
class EarlierSend:
    """A message already sent in the thread, as the refusal names it."""

    sent_at: float | None
    subject: str
    recipients: tuple[str, ...]
    message_id: str = ""
    web_link: str = ""
    source: str = "record"
    conversation_id: str = ""

    def describe(self, *, now: float | None = None) -> str:
        now = time.time() if now is None else now
        parts: list[str] = []
        if self.sent_at is not None:
            ago = max(0, int(now - self.sent_at))
            when = datetime.fromtimestamp(self.sent_at, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            parts.append(f"sent {ago} s ago at {when}")
        if self.subject:
            parts.append(f"subject '{self.subject}'")
        if self.recipients:
            parts.append("to " + ", ".join(self.recipients))
        if self.message_id:
            parts.append(f"message id {self.message_id}")
        if self.web_link:
            parts.append(self.web_link)
        origin = "recorded by an earlier send from this mailbox" if self.source == "record" else "found in the mailbox's Sent Items"
        return (", ".join(parts) or "an earlier message") + f" ({origin})"

    def to_json(self) -> str:
        return json.dumps(
            {
                "sent_at": self.sent_at,
                "subject": self.subject,
                "recipients": list(self.recipients),
                "message_id": self.message_id,
                "web_link": self.web_link,
                "conversation_id": self.conversation_id,
            }
        )

    @classmethod
    def from_json(cls, raw: object) -> EarlierSend | None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        if not isinstance(raw, str) or not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        recipients = data.get("recipients")
        return cls(
            sent_at=_epoch(data.get("sent_at")),
            subject=str(data.get("subject") or ""),
            recipients=tuple(str(r) for r in recipients) if isinstance(recipients, list) else (),
            message_id=str(data.get("message_id") or ""),
            web_link=str(data.get("web_link") or ""),
            source="record",
            conversation_id=str(data.get("conversation_id") or ""),
        )


def recent_send(
    *,
    sent_at: object,
    subject: object,
    recipients: Iterable[object],
    message_id: object = "",
    web_link: object = "",
    conversation_id: object = "",
    now: float,
    window: int,
) -> EarlierSend | None:
    """An :class:`EarlierSend` from a Sent Items message when it was sent within *window* seconds of *now*."""
    epoch = _epoch(sent_at)
    if epoch is None or window <= 0 or epoch < now - window or epoch > now + 60:
        return None
    return EarlierSend(
        sent_at=epoch,
        subject=" ".join(str(subject or "").split()),
        recipients=normalize_addresses(recipients),
        message_id=str(message_id or ""),
        web_link=str(web_link or ""),
        source="sent_items",
        conversation_id=str(conversation_id or ""),
    )


def refusal(
    earlier: EarlierSend,
    *,
    window: int,
    tool: str,
    now: float | None = None,
    inbound_checked: bool = True,
    forceable: bool = True,
) -> str:
    """The tool answer of a send refused as a duplicate.

    *inbound_checked* says whether the conversation was read for a newer
    message from someone else (and none was found); ``False`` when that could
    not be checked (no conversation id, a failed read), which the answer says.
    *forceable* is false for a sender with no ``force_duplicate`` (an agent
    account's automatic reply): the answer then names no way around it.
    """
    since = (
        "and no message from anyone else arrived in the conversation since"
        if inbound_checked
        else "and whether a message from someone else arrived in the conversation since could not be checked"
    )
    answer = (
        f"Not sent: a message was already sent in this conversation within the last {window} s, "
        f"{earlier.describe(now=now)}, {since}. A second answer to the same thread with nothing new to "
        f"answer is almost always a duplicate (two runs answering one inbound mail)."
    )
    if not forceable:
        return answer
    return f"{answer} If a second message is really wanted, call {tool} again with force_duplicate=true."


def contention_refusal(*, tool: str) -> str:
    """The tool answer when another send attempt holds the thread's lock."""
    return (
        "Not sent: another send in this conversation from this mailbox is in progress right now, which "
        "is what a duplicate run looks like. Do not retry blindly: read the conversation's Sent Items "
        f"first, and call {tool} again with force_duplicate=true only if a second message is really wanted."
    )


class SendGuard:
    """One guarded send attempt on a thread: ``acquire`` → ``recorded`` → send → ``sent`` or ``release``.

    *scope* is the sending mailbox (``mailbox:`` and the integration id);
    a guard without a scope does nothing (``active``
    is False). Every Redis failure is logged and read
    as "no lock, no record": the send proceeds.
    """

    def __init__(self, scope: object, thread: str, *, window: int) -> None:
        self.scope = str(scope or "").strip()
        self.thread = thread
        self.window = max(0, int(window))
        self.active = bool(self.scope) and self.window > 0
        self._held = False

    @property
    def lock_key(self) -> str:
        return f"{_KEY_PREFIX}:lock:{self.scope}:{self.thread}"

    @property
    def record_key(self) -> str:
        return f"{_KEY_PREFIX}:sent:{self.scope}:{self.thread}"

    async def acquire(self) -> bool:
        """Take the thread's lock. ``False`` when another attempt holds it; ``True`` when Redis cannot be asked."""
        if not self.active:
            return True
        try:
            redis = await redis_client()
            claimed = await redis.set(self.lock_key, "1", nx=True, ex=LOCK_TTL_SECONDS)
        except Exception as exc:
            logger.warning("Mail send guard: could not take the thread lock (sending unguarded): %s", format_exception_message(exc))
            return True
        self._held = bool(claimed)
        return self._held

    async def recorded(self) -> EarlierSend | None:
        """The send this scope recorded in the thread within the window, if any."""
        if not self.active:
            return None
        try:
            redis = await redis_client()
            raw = await redis.get(self.record_key)
        except Exception as exc:
            logger.warning("Mail send guard: could not read the thread's send record: %s", format_exception_message(exc))
            return None
        return EarlierSend.from_json(raw)

    async def sent(
        self,
        *,
        subject: object,
        recipients: Iterable[object],
        message_id: object = "",
        web_link: object = "",
        conversation_id: object = "",
        now: float | None = None,
    ) -> None:
        """Record the send that just succeeded (expiring with the window) and release the lock."""
        if not self.active:
            return
        record = EarlierSend(
            sent_at=time.time() if now is None else now,
            subject=" ".join(str(subject or "").split()),
            recipients=normalize_addresses(recipients),
            message_id=str(message_id or ""),
            web_link=str(web_link or ""),
            conversation_id=str(conversation_id or ""),
        )
        try:
            redis = await redis_client()
            await redis.set(self.record_key, record.to_json(), ex=self.window)
        except Exception as exc:
            logger.warning("Mail send guard: could not record a send (a duplicate may follow): %s", format_exception_message(exc))
        await self.release()

    async def release(self) -> None:
        """Give the lock back when this attempt took it; a lock held by another attempt is never touched."""
        if not self._held:
            return
        self._held = False
        try:
            redis = await redis_client()
            await redis.delete(self.lock_key)
        except Exception as exc:
            logger.debug("Mail send guard: could not release the thread lock (it expires): %s", format_exception_message(exc))
