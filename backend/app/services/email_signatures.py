# SPDX-License-Identifier: Apache-2.0

"""Email signatures read from a signature integration.

What an agent sends should carry the signature the person's own email does.
Microsoft Graph has no signature endpoint, so the platform copies the
signature block of the person's sent mail; a service that manages the
signatures of a company can hand out the current one instead - the person's
signature as the service composes it now, and its variant for replies.

A provider whose service holds signatures registers how it reads one
(:func:`register_signature_source`); a provider whose tools send email
registers as a sender (:func:`register_email_sender`). The mailbox of an agent
account (``AgentAccount.signature_source``) and the mail tools of every sender
read a signature through :func:`read_signature`, the one reader: it follows
the integration's relay route, bounds the call and keeps an answer for
:data:`CACHE_SECONDS`.

Which signature integration a sender's tools read, if any, is resolved when
the tools are built (``services/email_signature_settings``) into a
:class:`SignaturePlan`; the tools of one session sign through a
:class:`MailSigner`, and place what :func:`signature_markup` makes of the
service's HTML where their mail client puts a signature.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import logging
import re
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

#: How long the signature of one address, read through one integration, is reused.
CACHE_SECONDS = 600
#: A reply or a send is never held back longer by a slow signature service.
READ_TIMEOUT_SECONDS = 10
#: The largest signature kept, in characters of HTML.
MAX_SIGNATURE_CHARS = 200_000
_CACHE_ENTRIES = 1000


@dataclass(frozen=True)
class EmailSignature:
    """A person's signature as their signature service composes it, in HTML."""

    html: str
    #: The variant for replies, when the service defines one.
    reply_html: str = ""
    #: The signature's name in the service, for messages.
    name: str = ""

    def for_reply(self, reply: bool) -> str:
        """The HTML for a reply (its variant, when there is one) or a new email."""
        return self.reply_html if reply and self.reply_html else self.html


class SignatureUnavailableError(Exception):
    """The signature integration cannot be read; the message says why, in the owner's words."""


#: ``(credential, address)`` -> the signature, ``None`` when the service holds
#: none for the address. Raises :class:`SignatureUnavailableError` on a failure.
SignatureReader = Callable[[str, str], Awaitable[EmailSignature | None]]
#: ``address`` -> the signature, or ``None`` (none, or not readable: logged).
SignatureFn = Callable[[str], Awaitable[EmailSignature | None]]

_READERS: dict[str, SignatureReader] = {}
_SENDERS: set[str] = set()
_cache: OrderedDict[tuple[str, str], tuple[float, EmailSignature | None]] = OrderedDict()


def register_signature_source(provider: str, reader: SignatureReader) -> None:
    """Declare that *provider*'s integrations hold email signatures, read by *reader*. Idempotent."""
    _READERS[provider] = reader


def register_email_sender(provider: str) -> None:
    """Declare that *provider*'s tools send email, signed through a :class:`MailSigner`. Idempotent."""
    _SENDERS.add(provider)


def _load_providers() -> None:
    """A provider registers when imported, and the tool registry imports them all."""
    if not (_READERS and _SENDERS):
        import app.services.integration_tools  # noqa: F401


def _sources() -> dict[str, SignatureReader]:
    _load_providers()
    return _READERS


def signature_providers() -> frozenset[str]:
    """The integration providers that hold email signatures."""
    return frozenset(_sources())


def email_sender_providers() -> frozenset[str]:
    """The integration providers whose tools send email: each has an Email signature setting."""
    _load_providers()
    return frozenset(_SENDERS)


def bounded(signature: EmailSignature | None) -> EmailSignature | None:
    """*signature* with each variant within :data:`MAX_SIGNATURE_CHARS`; an over-long one is dropped."""
    if signature is None:
        return None
    html = signature.html if len(signature.html) <= MAX_SIGNATURE_CHARS else ""
    reply = signature.reply_html if len(signature.reply_html) <= MAX_SIGNATURE_CHARS else ""
    if not (html or reply):
        return None
    return EmailSignature(html=html or reply, reply_html=reply, name=signature.name)


async def read_signature(db: AsyncSession, integration_id: uuid.UUID | str, address: str) -> EmailSignature | None:
    """The signature the integration holds for *address*; ``None`` when it holds none.

    Raises :class:`SignatureUnavailableError` when it cannot be read. Who may use
    the integration is the caller's to decide, when it is chosen: this reads
    it as chosen.
    """
    from app.core.egress import egress_route
    from app.models.integration import Integration
    from app.services.relays import integration_egress_route
    from app.services.vault import reveal_field

    address = (address or "").strip().lower()
    if not address:
        raise SignatureUnavailableError("The account's email address is not known yet.")
    try:
        parsed = integration_id if isinstance(integration_id, uuid.UUID) else uuid.UUID(str(integration_id))
    except ValueError:
        raise SignatureUnavailableError("The signature integration is gone.") from None
    key = (str(parsed), address)
    hit = _cache.get(key)
    if hit is not None and hit[0] > time.monotonic():
        return hit[1]
    integration = (await db.execute(select(Integration).where(Integration.id == parsed))).scalar_one_or_none()
    if integration is None:
        raise SignatureUnavailableError("The signature integration is gone.")
    reader = _sources().get(integration.provider)
    if reader is None:
        raise SignatureUnavailableError(f"The integration '{integration.name}' does not hold email signatures.")
    credential = ((await reveal_field(integration, "api_key_enc")) or "").strip()
    if not credential:
        raise SignatureUnavailableError(f"The integration '{integration.name}' has no API key.")
    route = await integration_egress_route(db, integration)
    try:
        with egress_route(route):
            signature = await asyncio.wait_for(reader(credential, address), READ_TIMEOUT_SECONDS)
    except TimeoutError:
        raise SignatureUnavailableError(f"The integration '{integration.name}' did not answer in time.") from None
    signature = bounded(signature)
    _cache[key] = (time.monotonic() + CACHE_SECONDS, signature)
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_ENTRIES:
        _cache.popitem(last=False)
    return signature


def signature_fn_for(integration_id: str) -> SignatureFn:
    """The signature reader the mail tools of a run get for their signature integration.

    Reads on its own session (tools run concurrently, outside the request's);
    a failure is logged and answered ``None``, so the tool falls back on the
    signature it would have used without one.
    """
    from app.core.database import async_session

    async def signature_of(address: str) -> EmailSignature | None:
        try:
            async with async_session() as db:
                return await read_signature(db, integration_id, address)
        except SignatureUnavailableError as exc:
            logger.info("Signature integration %s not read: %s", integration_id, exc)
        return None

    return signature_of


# -- The plan of one sending integration --------------------------------------

#: Who turned the signature off: the agent's binding or the integration's setting.
OFF_BY_BINDING = "binding"
OFF_BY_INTEGRATION = "integration"


@dataclass(frozen=True)
class SignaturePlan:
    """How the mail tools of one sending integration sign, resolved when they are built.

    ``enabled`` is the on / off answer (the agent's binding, else the
    integration's setting, else on); ``integration_id`` the signature
    integration read when one resolves, ``missing`` why none does and how to
    set one.
    """

    enabled: bool = True
    integration_id: str | None = None
    integration_name: str = ""
    #: :data:`OFF_BY_BINDING` or :data:`OFF_BY_INTEGRATION` when ``enabled`` is false.
    off_by: str = ""
    missing: str = ""

    @property
    def signs(self) -> bool:
        """Whether a signature integration is read for what the tools write."""
        return self.enabled and bool(self.integration_id)

    def signature_fn(self) -> SignatureFn | None:
        """The reader the builder kwarg ``signature_fn`` carries; ``None`` when nothing is read."""
        return signature_fn_for(self.integration_id) if self.signs and self.integration_id else None

    def source(self) -> str:
        """Where the signature comes from, as the tools say it."""
        return f"the signature integration '{self.integration_name}'" if self.integration_name else "the signature integration"

    def off_reason(self) -> str:
        """Why nothing is signed, and where to turn it on."""
        if self.off_by == OFF_BY_BINDING:
            return (
                "email signatures are off on this agent's binding to the integration. "
                "Its owner turns them on in the agent's Email signatures settings "
                "(or update_agent integration_signatures)."
            )
        return (
            "email signatures are off in this integration's settings. Its owner turns "
            "them on in the integration's Email signature setting, or on the agent's binding."
        )


class MailSigner:
    """The signature the mail tools of one sending integration add, for one tool session.

    Built from the builder kwargs ``signature_fn`` and ``signature_plan``. A
    builder called without a plan signs whenever it is given a reader, as
    before the setting existed. Each address is read once per session: a run
    drafting many emails reads its signature once.
    """

    def __init__(self, signature_fn: SignatureFn | None, plan: SignaturePlan | None) -> None:
        self.plan = plan or SignaturePlan()
        self._fn = signature_fn if self.plan.enabled else None
        self._found: dict[str, EmailSignature | None] = {}

    @property
    def enabled(self) -> bool:
        """Whether what the tools send is signed at all (off: no signature from anywhere)."""
        return self.plan.enabled

    @property
    def signs(self) -> bool:
        """Whether a signature integration is read for what the tools write."""
        return self._fn is not None

    async def read(self, address: str) -> EmailSignature | None:
        """The person's signature for *address*; ``None`` without one or when none is held."""
        key = (address or "").strip().lower()
        if self._fn is None or not key:
            return None
        if key not in self._found:
            self._found[key] = await self._fn(key)
        return self._found[key]

    async def markup(self, address: str, *, reply: bool) -> str:
        """The signature for *address* as :func:`signature_markup` keeps it; empty without one."""
        found = await self.read(address)
        return signature_markup(found.for_reply(reply)) if found is not None else ""

    def tool_note(self) -> str:
        """What a send, draft or reply tool's description says about the signature."""
        if not self.signs:
            return ""
        return (
            f" The user's email signature, read from {self.plan.source()}, is added at the end "
            "automatically: do NOT write a signature in the body."
        )

    def report(self, address: str, markup: str) -> str:
        """What a ``get_<provider>_email_signature`` tool answers, *markup* being what was found."""
        if markup:
            return f"User's email signature (from {self.plan.source()}):\n{signature_text(markup)}"
        if not self.enabled:
            return f"No email signature: {self.plan.off_reason()}"
        if self.signs:
            return (
                f"No email signature: {self.plan.source()} holds none for "
                f"{address or 'this mailbox'} (or could not be read)."
            )
        return f"No email signature: {self.plan.missing or NO_SIGNATURE_INTEGRATION}"


#: What a sender without a signature integration says, when no resolution ran.
NO_SIGNATURE_INTEGRATION = (
    "no signature integration is set for this integration. Connect one and choose it "
    "in the integration's Email signature setting."
)


# -- Markup -------------------------------------------------------------------

_UNSAFE_TAGS = ["script", "iframe", "object", "embed", "frame", "frameset", "form", "base", "meta", "link", "title"]
_BLOCK_TAGS = ("p", "div", "tr", "table", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote")


def signature_markup(html: str) -> str:
    """A signature service's HTML made safe to put in an email; empty when there is nothing to show.

    A whole HTML document is reduced to its body, its head's styles kept;
    scripts, frames, forms and inline event handlers are dropped. Each mail
    client wraps the result in the block it recognises as a signature.
    """
    from bs4 import BeautifulSoup

    if not (html or "").strip():
        return ""
    soup = BeautifulSoup(html, "html.parser")
    styles: list[str] = []
    if soup.head is not None:
        styles = [str(style) for style in soup.head.find_all("style")]
        soup.head.decompose()
    for tag in soup.find_all(_UNSAFE_TAGS):
        tag.decompose()
    for tag in soup.find_all(True):
        for attr in [a for a in tag.attrs if a.lower().startswith("on")]:
            del tag.attrs[attr]
    root = soup.body or soup
    for wrapper in root.find_all(["html", "body"]):
        wrapper.unwrap()
    inner = root.decode_contents().strip()
    if not re.sub(r"<[^>]+>", "", inner).strip() and "<img" not in inner.lower():
        return ""
    return "".join(styles) + inner


def signature_text(html: str) -> str:
    """The text rendering of a signature's HTML, for the plain-text part of an email."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup.find_all(["style", "script"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")
    for cell in soup.find_all(["td", "th"]):
        cell.insert_after(" ")
    lines = [re.sub(r"[ \t\u00a0]+", " ", line).strip() for line in soup.get_text().splitlines()]
    return "\n".join(line for line in lines if line)


def text_to_html(text: str) -> str:
    """A plain-text body as HTML, line for line, for an email that gains an HTML part."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    return '<div dir="ltr">' + "<br>\n".join(html_lib.escape(line) for line in lines) + "</div>"


def append_to_body(html: str, block: str) -> str:
    """*html* (a whole email body or a fragment) with *block* after its content, inside its ``<body>``."""
    if not block:
        return html
    end = re.search(r"</body\s*>", html, re.IGNORECASE)
    if end:
        return html[: end.start()] + "\r\n" + block + "\r\n" + html[end.start():]
    return html + "\r\n" + block


def signed_text(text: str, markup: str) -> str:
    """A plain-text body with the text rendering of the signature *markup* after it."""
    rendered = signature_text(markup) if markup else ""
    if not rendered:
        return text
    return f"{text.rstrip()}\n\n{rendered}" if text.strip() else rendered
