# SPDX-License-Identifier: Apache-2.0

"""Minimal SMTP email sender for transactional emails (e.g. password reset).

Uses the stdlib ``smtplib`` run in a worker thread so it never blocks the event
loop. When SMTP is not configured (``smtp_hostname`` empty) or the send fails,
:func:`send_email` logs a warning and returns ``False``. The fallback behaviour
is owned by each caller, not by this module:

* Admin-driven flows (``POST /api/v1/invitations`` with ``send_email=True``,
  ``POST /api/v1/users/{id}/password-reset``) refuse with ``503`` *before*
  minting a token when SMTP is unconfigured, so a misconfigured deployment
  cannot silently leak orphan single-use secrets into Redis.
* The self-service ``/auth/forgot-password`` route short-circuits silently in
  production builds (timing-safety: it always returns ``200``) and only mints
  the token + surfaces the reset link via ``logger.warning`` when
  ``settings.debug`` is set, so a developer running locally without SMTP can
  still complete the loop.
* Seed / automation callers (``POST /api/v1/invitations`` with
  ``send_email=False``) bypass this module entirely and return the freshly
  minted link in the response body — that's the explicit contract used by
  ``scripts/invite-user.sh`` and any external orchestrator.

A caller that must tell a refused recipient from a transient SMTP failure
(the model catalog announcements) uses :func:`deliver_email`, which
raises :class:`EmailDeliveryError` instead of returning ``False``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import smtplib
from collections.abc import Sequence
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr
from html import escape as html_escape

from app.core.config import settings
from app.core.utils import format_exception_message
from app.services.branding import branding_defaults, get_cached_branding

logger = logging.getLogger(__name__)

# RFC 3834: every email the platform sends is machine-generated, so vacation
# responders and Exchange auto-replies must not answer it. ``_build_message``
# puts them on every message; a caller's ``headers`` are added on top.
AUTOMATED_EMAIL_HEADERS: dict[str, str] = {
    "Auto-Submitted": "auto-generated",
    "X-Auto-Response-Suppress": "All",
}


def is_email_configured() -> bool:
    """True when an SMTP host is configured."""
    return bool(settings.smtp_hostname)


class EmailDeliveryError(Exception):
    """One email could not be delivered.

    ``permanent`` is True only when sending it again would fail again: every
    recipient refused with a 5xx, or a message that cannot be built (a CR / LF
    in the address). Every other failure (connection, authentication, a sending
    limit, a 4xx) may pass on a later attempt.

    ``uncertain`` is True when the connection failed or timed out while the
    message itself was being sent: the server may have accepted it before the
    link broke, so a caller that must never email anyone twice cannot retry it.

    ``recipient_refused`` is True when the server refused the recipient itself
    (its reply to the address), having accepted the connection and the sender:
    another address may still go through.

    ``address_specific`` is True when that refusal is about the address and
    nothing else, by its enhanced status code (:func:`reply_names_the_address`):
    no such domain or mailbox, a full one. Any other refusal of a recipient (a
    policy, a sending limit, no enhanced code) may be the server refusing the
    platform under a recipient's reply.
    """

    def __init__(
        self,
        message: str,
        *,
        permanent: bool,
        uncertain: bool = False,
        recipient_refused: bool = False,
        address_specific: bool = False,
    ) -> None:
        super().__init__(message)
        self.permanent = permanent
        self.uncertain = uncertain
        self.recipient_refused = recipient_refused
        self.address_specific = address_specific


class _UncertainDeliveryError(Exception):
    """The link broke inside ``send_message``, after the data may have gone."""


def _from_header() -> str:
    addr = settings.smtp_from or settings.smtp_username or "no-reply@localhost"
    name = settings.smtp_from_name.strip()
    return formataddr((name, addr)) if name else addr


def _send_sync(msg: EmailMessage) -> None:
    if settings.smtp_use_tls:
        server: smtplib.SMTP = smtplib.SMTP_SSL(settings.smtp_hostname, settings.smtp_port, timeout=15)
    else:
        server = smtplib.SMTP(settings.smtp_hostname, settings.smtp_port, timeout=15)
    try:
        server.ehlo()
        if settings.smtp_starttls and not settings.smtp_use_tls:
            server.starttls()
            server.ehlo()
        if settings.smtp_username:
            server.login(settings.smtp_username, settings.smtp_password)
        try:
            server.send_message(msg)
        except smtplib.SMTPServerDisconnected as exc:
            raise _UncertainDeliveryError(format_exception_message(exc)) from exc
        except smtplib.SMTPException:
            # A reply the server gave (a refused recipient, a 4xx / 5xx on
            # DATA): the message was not taken. ``SMTPException`` is an
            # ``OSError``, so it is told apart before the clause below.
            raise
        except OSError as exc:
            # A broken link or a timeout: says nothing either way.
            raise _UncertainDeliveryError(format_exception_message(exc)) from exc
    finally:
        # ``server.quit()`` is best-effort teardown — the message has either
        # been accepted by ``send_message`` (in which case the SMTP exchange
        # is complete) or already raised, so a connection error during QUIT
        # cannot affect delivery. We log at ``debug`` rather than swallow
        # silently so an empty handler smell doesn't hide a server-side
        # misbehaviour (e.g. SMTP server closing the socket before QUIT).
        try:
            server.quit()
        except Exception as quit_exc:
            logger.debug(
                "SMTP server.quit() raised during teardown (non-fatal): %s",
                format_exception_message(quit_exc),
            )


@dataclass(frozen=True)
class EmailAttachment:
    """A file sent with an email: its name, its media type (``type/subtype``) and its bytes."""

    filename: str
    content_type: str
    data: bytes


#: An email carries at most this many bytes of files: a relay refuses larger ones.
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024


def _build_message(
    *,
    to: str,
    subject: str,
    html: str,
    text: str | None,
    headers: dict[str, str] | None,
    attachments: Sequence[EmailAttachment] = (),
) -> EmailMessage:
    msg = EmailMessage()
    # A subject carries names the platform does not control (the platform
    # name, a flow's text): folded to one line, never a header break.
    msg["Subject"] = " ".join(subject.split())
    msg["From"] = _from_header()
    msg["To"] = to
    for name, value in {**AUTOMATED_EMAIL_HEADERS, **(headers or {})}.items():
        msg[name] = value
    msg.set_content(text or "Open this email in an HTML-capable client.")
    msg.add_alternative(html, subtype="html")
    if sum(len(attachment.data) for attachment in attachments) > MAX_ATTACHMENT_BYTES:
        raise ValueError(f"the attached files exceed {MAX_ATTACHMENT_BYTES} bytes")
    for attachment in attachments:
        maintype, _, subtype = attachment.content_type.partition("/")
        # A name folded to one line: never a header break.
        name = " ".join(attachment.filename.split()) or "attachment"
        msg.add_attachment(attachment.data, maintype=maintype or "application", subtype=subtype or "octet-stream", filename=name)
    return msg


def _refusal_is_permanent(exc: smtplib.SMTPRecipientsRefused) -> bool:
    """Every recipient refused with a 5xx: a 4xx (greylisting, a full mailbox,
    a rate limit) may pass later."""
    codes = [reply[0] for reply in (exc.recipients or {}).values() if isinstance(reply, tuple) and reply]
    return bool(codes) and all(isinstance(code, int) and code >= 500 for code in codes)


# An RFC 3463 enhanced status code opening a reply (``4.1.2 ...``), after the
# basic code when the reply still carries it (``450 4.1.2 ...``).
_ENHANCED_STATUS = re.compile(r"\s*(?:[245]\d\d[ -]+)?[245]\.(\d{1,3})\.(\d{1,3})(?!\d)")
# Addressing status of the destination address (X.1.Y): no such mailbox or
# domain, bad syntax, ambiguous, moved, a null MX. X.1.0 and X.1.5 say nothing
# of it, and X.1.7 / X.1.8 refuse the SENDER's address (Postfix answers them at
# RCPT time, to every recipient).
_DESTINATION_ADDRESS_DETAILS = frozenset({1, 2, 3, 4, 6, 10})
# Mailbox status (X.2.Y) of the recipient's mailbox: undefined, disabled, full.
# X.2.3 (a message over the size limit) and X.2.4 (a mailing list expansion
# problem) are about the message or the server, not the address.
_MAILBOX_STATUS = 2
_MAILBOX_STATE_DETAILS = frozenset({0, 1, 2})


def reply_names_the_address(reply: bytes | str) -> bool:
    """The server's reply to a recipient refuses that address, not the platform.

    Read off its enhanced status code: a reply without one, or with a policy,
    system or protocol code (X.7.Y: a sending limit, a blocked sender), may be
    the server refusing every address the platform sends to.
    """
    text = reply.decode("utf-8", "replace") if isinstance(reply, bytes) else str(reply)
    match = _ENHANCED_STATUS.match(text)
    if match is None:
        return False
    subject, detail = int(match.group(1)), int(match.group(2))
    if subject == _MAILBOX_STATUS:
        return detail in _MAILBOX_STATE_DETAILS
    return subject == 1 and detail in _DESTINATION_ADDRESS_DETAILS


def _refusal_is_address_specific(exc: smtplib.SMTPRecipientsRefused) -> bool:
    """Every recipient was refused for its own address."""
    replies = list((exc.recipients or {}).values())
    return bool(replies) and all(
        isinstance(reply, tuple) and len(reply) >= 2 and reply_names_the_address(reply[1]) for reply in replies
    )


async def deliver_email(
    *,
    to: str,
    subject: str,
    html: str,
    text: str | None = None,
    headers: dict[str, str] | None = None,
    attachments: Sequence[EmailAttachment] = (),
) -> None:
    """Send one email (with its *attachments*) or raise :class:`EmailDeliveryError`."""
    if not is_email_configured():
        raise EmailDeliveryError("SMTP is not configured (SMTP_HOSTNAME empty)", permanent=False)
    try:
        msg = _build_message(to=to, subject=subject, html=html, text=text, headers=headers, attachments=attachments)
    except ValueError as exc:
        # A CR / LF in the address, files too large: the same failure on every attempt.
        raise EmailDeliveryError(format_exception_message(exc), permanent=True) from exc
    except Exception as exc:
        raise EmailDeliveryError(format_exception_message(exc), permanent=False) from exc
    try:
        await asyncio.to_thread(_send_sync, msg)
    except smtplib.SMTPRecipientsRefused as exc:
        raise EmailDeliveryError(
            format_exception_message(exc),
            permanent=_refusal_is_permanent(exc),
            recipient_refused=True,
            address_specific=_refusal_is_address_specific(exc),
        ) from exc
    except _UncertainDeliveryError as exc:
        raise EmailDeliveryError(str(exc), permanent=False, uncertain=True) from exc
    except Exception as exc:
        raise EmailDeliveryError(format_exception_message(exc), permanent=False) from exc
    logger.info("Sent email to %s (subject=%r)", to, subject)


async def send_email(
    *,
    to: str,
    subject: str,
    html: str,
    text: str | None = None,
    headers: dict[str, str] | None = None,
    attachments: Sequence[EmailAttachment] = (),
) -> bool:
    """Send an email (with its *attachments*). Returns True on success, False if not configured / failed."""
    if not is_email_configured():
        logger.warning("SMTP not configured (SMTP_HOSTNAME empty); cannot send email to %s", to)
        return False
    try:
        await deliver_email(to=to, subject=subject, html=html, text=text, headers=headers, attachments=attachments)
        return True
    except EmailDeliveryError:
        logger.exception("Failed to send email to %s", to)
        return False


# --- Filigran email layout ------------------------------------------------------
# Every email the platform writes follows the Filigran email guideline, the
# Maizzle design system in ``filigran-ui/packages/filigran-email`` that the XTM
# Hub and Filigran marketing emails are built from: a #F1F5F9 page, a 552 px
# white card, a 24 px regular-weight heading, the #001BDB button, the "Best
# regards" sign-off above a divider, and a footer with the Filigran social
# links, logo and offices. A product email: no unsubscribe or preferences link.
#
# Real tables with inline styles and PNG images only. Gmail drops CSS attribute
# selectors and never renders SVG, which is what breaks a layout drawn with
# ``display: table`` divs or an SVG logo. The ``<style>`` rules are the
# class-based mobile paddings and, in a block of their own so a client that
# drops the attribute selector keeps the paddings, the Outlook dark mode logo
# swap (:func:`_logo`); every client that ignores them can do without. The
# images are the platform's own ``frontend/public/email-assets`` files,
# addressed through the public frontend URL.

EMAIL_ASSETS_PATH = "/email-assets"

_PAGE_BG = "#F1F5F9"
_CARD_BG = "#FFFFFF"
_TEXT = "#00020C"
_MUTED = "#475569"
_RULE = "#CBD5E1"
_PANEL_BG = "#F8FAFC"
_PANEL_BORDER = "#E2E8F0"
_BUTTON_BG = "#001BDB"
_BUTTON_TEXT = "#F4F4F6"
_FONT = "ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif"
_OFFICES = "New York, Paris, London, Sydney, Singapore, Tokyo"
_SOCIAL_LINKS: tuple[tuple[str, str, str], ...] = (
    ("website.png", "https://filigran.io/", "Filigran website"),
    (
        "slack.png",
        "https://filigran-community.slack.com/join/shared_invite/zt-2pp43qkqc-EY7qC7Sk30zZfsNjgXwF5w",
        "Filigran community on Slack",
    ),
    ("linkedin.png", "https://www.linkedin.com/company/filigran", "Filigran on LinkedIn"),
    ("youtube.png", "https://www.youtube.com/@Filigran", "Filigran on YouTube"),
    ("x.png", "https://x.com/FiligranHQ", "Filigran on X"),
)
# Invisible characters padding the preheader so an inbox preview never runs on
# into the body text (the Maizzle layout's filler).
_PREHEADER_FILLER = "&#8199;&#65279;&#847; " * 90


def platform_url() -> str:
    """The public URL of the web app, the one every email link points at."""
    return (settings.frontend_url or settings.base_url).rstrip("/")


def email_asset_url(name: str) -> str:
    """Absolute URL of a ``frontend/public/email-assets`` file."""
    return f"{platform_url()}{EMAIL_ASSETS_PATH}/{name}"


@dataclass(frozen=True)
class _EmailBrand:
    name: str
    logo_url: str
    logo_width: int | None
    logo_height: int
    # The platform is not rebranded: sign as Filigran and draw the Filigran
    # footer. A rebranded platform keeps its own name in both.
    filigran: bool
    # The logo drawn for a dark background (a configured logo has none).
    logo_dark_url: str | None = None


def _email_brand() -> _EmailBrand:
    b = get_cached_branding()
    d = branding_defaults(is_xtm=settings.is_xtm_one)
    base = settings.base_url.rstrip("/")
    # A configured logo is used when a mail client can render it (a raster data
    # URL served by the branding route, or an absolute URL); the default is the
    # product logo as a PNG, since no SVG renders in Gmail.
    custom_logo: str | None = None
    if b.logo:
        if b.logo.startswith("data:"):
            custom_logo = f"{base}/api/v1/platform/branding/asset/logo"
        elif b.logo.startswith(("http://", "https://")):
            custom_logo = b.logo
    rebranded = custom_logo is not None or b.name != d.name
    if custom_logo:
        return _EmailBrand(b.name, custom_logo, None, 40, filigran=False)
    if settings.is_xtm_one:
        return _EmailBrand(
            b.name,
            email_asset_url("xtm-one.png"),
            206,
            44,
            filigran=not rebranded,
            logo_dark_url=email_asset_url("xtm-one-dark.png"),
        )
    return _EmailBrand(
        b.name,
        email_asset_url("filigran.png"),
        168,
        48,
        filigran=not rebranded,
        logo_dark_url=email_asset_url("filigran-dark.png"),
    )


def email_greeting_name(full_name: str | None) -> str | None:
    """The first name to greet, or ``None`` when the account carries no real one."""
    first = (full_name or "").strip().split(" ")[0]
    if not first or "@" in first or first.lower() in {"admin", "administrator", "user"}:
        return None
    return first


def email_heading(text: str) -> str:
    return (
        '<h1 class="email-heading" style="margin:0 0 24px;font-family:'
        f"{_FONT};font-size:24px;line-height:32px;font-weight:400;color:{_TEXT};"
        f'">{html_escape(text)}</h1>'
    )


def email_paragraph(text: str, *, small: bool = False, muted: bool = False, margin: str = "0 0 24px") -> str:
    """A body paragraph of plain text, escaped here."""
    return email_paragraph_html(html_escape(text), small=small, muted=muted, margin=margin)


def email_paragraph_html(markup: str, *, small: bool = False, muted: bool = False, margin: str = "0 0 24px") -> str:
    """A body paragraph of trusted markup: every value in ``markup`` must already
    be escaped (``html_escape``, :func:`email_link`)."""
    size, line = (14, 20) if small else (16, 24)
    color = _MUTED if muted else _TEXT
    return (
        f'<p style="margin:{margin};font-family:{_FONT};font-size:{size}px;'
        f'line-height:{line}px;color:{color};">{markup}</p>'
    )


def email_button(label: str, href: str) -> str:
    """The guideline button, as a table cell so Outlook draws its background."""
    return (
        '<table role="presentation" align="center" cellpadding="0" cellspacing="0" border="0" '
        'style="margin:8px auto 32px;"><tr>'
        f'<td align="center" bgcolor="{_BUTTON_BG}" style="border-radius:4px;background-color:{_BUTTON_BG};">'
        f'<a href="{html_escape(href)}" target="_blank" style="display:inline-block;padding:16px 24px;'
        f"font-family:{_FONT};font-size:16px;line-height:16px;font-weight:600;color:{_BUTTON_TEXT};"
        f'text-decoration:none;border-radius:4px;">{html_escape(label)}</a>'
        "</td></tr></table>"
    )


@dataclass(frozen=True)
class EmailPanelItem:
    """One entry of :func:`email_panel`. Every field is plain text."""

    eyebrow: str
    title: str
    detail: str | None = None
    # Rendered in a monospace face after ``detail`` (an identifier).
    code: str | None = None
    note: str | None = None
    # Drawn in the muted text colour: something that went away.
    muted: bool = False


def email_panel(items: list[EmailPanelItem]) -> str:
    """A light, bordered panel listing ``items`` one under the other."""
    rows = []
    for index, item in enumerate(items):
        border = f"border-top:1px solid {_PANEL_BORDER};" if index else ""
        meta = " &middot; ".join(
            part
            for part in (
                html_escape(item.detail) if item.detail else "",
                (
                    '<span style="font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;">'
                    f"{html_escape(item.code)}</span>"
                )
                if item.code
                else "",
            )
            if part
        )
        eyebrow_color, title_color = (_MUTED, _MUTED) if item.muted else (_BUTTON_BG, _TEXT)
        rows.append(
            f'<tr><td style="padding:20px 24px;{border}">'
            f'<p style="margin:0 0 6px;font-family:{_FONT};font-size:12px;line-height:16px;font-weight:600;'
            f'letter-spacing:0.08em;text-transform:uppercase;color:{eyebrow_color};">{html_escape(item.eyebrow)}</p>'
            f'<p style="margin:0;font-family:{_FONT};font-size:20px;line-height:28px;font-weight:600;'
            f'color:{title_color};">{html_escape(item.title)}</p>'
            + (
                f'<p style="margin:4px 0 0;font-family:{_FONT};font-size:14px;line-height:20px;'
                f'color:{_MUTED};">{meta}</p>'
                if meta
                else ""
            )
            + (
                f'<p style="margin:10px 0 0;font-family:{_FONT};font-size:14px;line-height:20px;'
                f'color:{_MUTED};">{html_escape(item.note)}</p>'
                if item.note
                else ""
            )
            + "</td></tr>"
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="margin:0 0 24px;border:1px solid {_PANEL_BORDER};border-radius:6px;'
        f'background-color:{_PANEL_BG};">{"".join(rows)}</table>'
    )


def email_link(label: str, href: str) -> str:
    return (
        f'<a href="{html_escape(href)}" target="_blank" '
        f'style="color:{_BUTTON_BG};text-decoration:underline;word-break:break-all;">{html_escape(label)}</a>'
    )


def _divider() -> str:
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="margin:24px 0;"><tr><td style="height:1px;line-height:1px;font-size:0;'
        f'background-color:{_RULE};">&zwj;</td></tr></table>'
    )


def _logo(*, href: str, src: str, dark_src: str | None, width: int | None, height: int, alt: str) -> str:
    """A linked logo, swapped for its dark variant where a mail client darkens the email.

    Both PNGs are transparent, with no background at all: a client that darkens
    the email without telling CSS (Gmail's apps, Outlook for Windows) shows the
    default one on its dark background as it is. Outlook.com and the Outlook
    apps mark what they darkened with ``data-ogsc`` / ``data-ogsb``, and there
    ``dark_src``, drawn for a dark background, takes its place. It is hidden
    inline, and from Outlook for Windows (``mso``), which never shows it.
    """
    size = (f'width="{width}" ' if width else "") + f'height="{height}"'
    size_style = (f"width:{width}px;" if width else "") + f"height:{height}px;"
    target = html_escape(href)

    def image(url: str) -> str:
        return (
            f'<img src="{html_escape(url)}" alt="{html_escape(alt)}" '
            f'style="display:inline-block;{size_style}border:0;outline:none;text-decoration:none;" {size}>'
        )

    if not dark_src:
        return f'<a href="{target}" target="_blank">{image(src)}</a>'
    return (
        f'<a class="email-logo" href="{target}" target="_blank">{image(src)}</a>'
        '<!--[if !mso]><!--><div class="email-logo-dark" style="display:none;max-height:0;overflow:hidden;">'
        f'<a href="{target}" target="_blank">{image(dark_src)}</a></div><!--<![endif]-->'
    )


def _team(brand: _EmailBrand) -> str:
    return "The Filigran Team" if brand.filigran else f"The {brand.name} team"


def email_signoff_team() -> str:
    """Who signs the email: Filigran, unless the platform is rebranded."""
    return _team(_email_brand())


def _signoff(brand: _EmailBrand) -> str:
    return (
        f'<p style="margin:0;padding-top:8px;font-family:{_FONT};font-size:16px;line-height:24px;'
        f'color:{_TEXT};">Best regards,<br><strong>{html_escape(_team(brand))}</strong></p>'
    )


def _footer(brand: _EmailBrand) -> str:
    if not brand.filigran:
        return (
            f'<p style="margin:16px 0 40px;font-family:{_FONT};font-size:12px;line-height:16px;'
            f'color:{_MUTED};">{html_escape(brand.name)}</p>'
        )
    icons = "".join(
        f'<td style="padding:4px;"><a href="{href}" target="_blank">'
        f'<img src="{email_asset_url(image)}" width="24" height="24" alt="{alt}" '
        'style="display:block;width:24px;height:24px;border:0;outline:none;text-decoration:none;">'
        "</a></td>"
        for image, href, alt in _SOCIAL_LINKS
    )
    return (
        '<table role="presentation" align="center" cellpadding="0" cellspacing="0" border="0" '
        f'style="margin:16px auto 8px;"><tr>{icons}</tr></table>'
        + _logo(
            href="https://filigran.io",
            src=email_asset_url("filigran.png"),
            dark_src=email_asset_url("filigran-dark.png"),
            width=105,
            height=30,
            alt="Filigran",
        )
        + f'<p style="margin:8px 0 40px;font-family:{_FONT};font-size:12px;line-height:16px;'
        f'color:{_TEXT};">{_OFFICES}</p>'
    )


def render_email(*, title: str, preheader: str, body_html: str, fine_print_html: str | None = None) -> str:
    """Wrap ``body_html`` (built from the ``email_*`` helpers) in the Filigran layout.

    The logo, the sign-off and the footer are added here: a body holds the
    heading, the paragraphs and the button only. ``fine_print_html`` (trusted
    markup) is the small print the guideline puts under the sign-off, after a
    divider: why the reader got this email, what to ignore.
    """
    brand = _email_brand()
    fine_print = (
        _divider() + email_paragraph_html(fine_print_html, small=True, muted=True, margin="0")
        if fine_print_html
        else ""
    )
    logo = _logo(
        href=platform_url(),
        src=brand.logo_url,
        dark_src=brand.logo_dark_url,
        width=brand.logo_width,
        height=brand.logo_height,
        alt=brand.name,
    )
    title_esc = html_escape(title)
    return f"""\
<!doctype html>
<html lang="en" xmlns:v="urn:schemas-microsoft-com:vml">
<head>
<meta charset="utf-8">
<meta name="x-apple-disable-message-reformatting">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="format-detection" content="telephone=no, date=no, address=no, email=no, url=no">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<!--[if mso]>
<noscript><xml><o:OfficeDocumentSettings xmlns:o="urn:schemas-microsoft-com:office:office"><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml></noscript>
<style>td,th,div,p,a,h1,h2,h3 {{font-family: "Segoe UI", sans-serif; mso-line-height-rule: exactly;}}</style>
<![endif]-->
<title>{title_esc}</title>
<style>
@media (max-width: 600px) {{
  .email-outer {{ padding: 24px 12px !important; }}
  .email-card {{ padding: 32px 24px !important; }}
  .email-heading {{ font-size: 22px !important; line-height: 30px !important; }}
}}
</style>
<style>
[data-ogsc] .email-logo, [data-ogsb] .email-logo {{ display: none !important; }}
[data-ogsc] .email-logo-dark, [data-ogsb] .email-logo-dark {{ display: block !important; max-height: none !important; overflow: visible !important; }}
</style>
</head>
<body style="margin:0;padding:0;width:100%;background-color:{_PAGE_BG};-webkit-font-smoothing:antialiased;word-break:break-word;">
<div style="display:none;max-height:0;max-width:0;overflow:hidden;opacity:0;mso-hide:all;">{html_escape(preheader)}{_PREHEADER_FILLER}</div>
<div role="article" aria-roledescription="email" aria-label="{title_esc}" lang="en">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:{_PAGE_BG};">
<tr><td align="center" class="email-outer" style="padding:40px 16px;">
<table role="presentation" width="552" cellpadding="0" cellspacing="0" border="0" style="width:552px;max-width:100%;">
<tr><td class="email-card" style="padding:48px;background-color:{_CARD_BG};border-radius:4px;box-shadow:0 1px 2px 0 rgba(0,0,0,0.05);font-family:{_FONT};font-size:16px;line-height:24px;color:{_TEXT};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr><td align="center" style="padding:0 0 40px;">
{logo}
</td></tr></table>
{body_html}
{_signoff(brand)}
{fine_print}
</td></tr>
<tr><td align="center" style="padding:0 24px;font-family:{_FONT};text-align:center;">
{_footer(brand)}
</td></tr>
</table>
</td></tr>
</table>
</div>
</body>
</html>"""


def _action_email(
    *,
    title: str,
    preheader: str,
    lead_html: str,
    button_label: str,
    link: str,
    valid_for: str,
    disclaimer: str,
) -> str:
    body = (
        email_heading(title)
        + email_paragraph_html(lead_html)
        + email_button(button_label, link)
        + email_paragraph_html(
            f"This link is valid for {html_escape(valid_for)}. If the button doesn't work, "
            f"copy this URL into your browser:<br>{email_link(link, link)}",
            small=True,
            muted=True,
        )
    )
    return render_email(
        title=title,
        preheader=preheader,
        body_html=body,
        fine_print_html=html_escape(disclaimer),
    )


def _invitation_valid_for(expire_hours: int) -> str:
    return f"{expire_hours} hours" if expire_hours < 48 else f"{max(1, expire_hours // 24)} days"


def invitation_email(link: str, role: str, expire_hours: int) -> tuple[str, str]:
    """Return (html, text) bodies for an invitation email."""
    name = get_cached_branding().name
    name_esc = html_escape(name)
    role_label = "an administrator" if role == "admin" else "a user"
    valid_for = _invitation_valid_for(expire_hours)
    html = _action_email(
        title="You've been invited",
        preheader=f"Create your {name} account.",
        lead_html=(
            f"You've been invited to join <strong>{name_esc}</strong> as {role_label}. "
            "Create your account and choose your password to get started."
        ),
        button_label="Create your account",
        link=link,
        valid_for=valid_for,
        disclaimer="If you weren't expecting this invitation, you can safely ignore this email.",
    )
    text = (
        f"You've been invited to join {name} as {role_label}.\n\n"
        f"Create your account here (link valid for {valid_for}):\n{link}\n\n"
        "If you weren't expecting this invitation, you can ignore this email."
    )
    return html, text


#: The longest name a third party puts in an email (an access notice's place, its sender).
SUPPLIED_NAME_MAX_LENGTH = 120

#: The characters a link or an address is detected around: ``.`` (a host),
#: ``:`` and ``/`` (a scheme), ``\\`` (a network path), ``@`` (an address),
#: with their full-width forms.
_LINK_PUNCTUATION = frozenset(".:/\\@\u3002\uff0e\uff61\uff1a\uff0f\uff20")
_ZERO_WIDTH_SPACE = "\u200b"


def unlinkable_text(value: str, *, max_length: int = SUPPLIED_NAME_MAX_LENGTH) -> str:
    """*value*, cut to *max_length* characters, as text no mail client turns into a link.

    Mail clients link what looks like a URL, a domain or an address in plain
    text, ``html_escape`` or not. A zero-width space after each of
    :data:`_LINK_PUNCTUATION` breaks what they match while the reader sees the
    same text. For a value a third party supplies; still to be escaped as HTML.
    """
    value = value.strip()
    if len(value) > max_length:
        value = value[: max_length - 1].rstrip() + "\u2026"
    return "".join(char + _ZERO_WIDTH_SPACE if char in _LINK_PUNCTUATION else char for char in value)


def access_granted_email(*, link: str, platform_name: str, scope_name: str, granted_by: str) -> tuple[str, str]:
    """Return (html, text) bodies telling a user that a registered platform gave them access.

    *scope_name* and *granted_by* come from the platform: shown as
    :func:`unlinkable_text`, never as markup nor as a link. *link* is the only link.
    """
    brand = get_cached_branding().name
    scope = unlinkable_text(scope_name)
    sender = unlinkable_text(granted_by)
    title = f"You have access to {platform_name}"
    disclaimer = "If you weren't expecting this access, you can ignore this email."
    body = (
        email_heading(title)
        + email_paragraph_html(
            f"{html_escape(sender)} gave you access to <strong>{html_escape(scope)}</strong> "
            f"on {html_escape(platform_name)}. Sign in with your {html_escape(brand)} account."
        )
        + email_button(f"Open {platform_name}", link)
        + email_paragraph_html(
            f"If the button doesn't work, copy this URL into your browser:<br>{email_link(link, link)}",
            small=True,
            muted=True,
        )
    )
    html = render_email(
        title=title,
        preheader=f"{sender} gave you access to {scope}.",
        body_html=body,
        fine_print_html=html_escape(disclaimer),
    )
    text = (
        f"{sender} gave you access to {scope} on {platform_name}.\n\n"
        f"Sign in with your {brand} account:\n{link}\n\n"
        f"{disclaimer}"
    )
    return html, text


def platform_invitation_email(
    *, link: str, platform_name: str, scope_name: str, invited_by: str, expire_hours: int
) -> tuple[str, str]:
    """Return (html, text) bodies inviting someone a registered platform added to create an account.

    *scope_name* and *invited_by* come from the platform: shown as
    :func:`unlinkable_text`, never as markup nor as a link. *link* is the only link.
    """
    brand = get_cached_branding().name
    scope = unlinkable_text(scope_name)
    sender = unlinkable_text(invited_by)
    valid_for = _invitation_valid_for(expire_hours)
    disclaimer = "If you weren't expecting this invitation, you can ignore this email."
    html = _action_email(
        title=f"You're invited to {platform_name}",
        preheader=f"{sender} invited you to {scope}.",
        lead_html=(
            f"{html_escape(sender)} invited you to <strong>{html_escape(scope)}</strong> "
            f"on {html_escape(platform_name)}. Create your {html_escape(brand)} account to sign in."
        ),
        button_label="Create your account",
        link=link,
        valid_for=valid_for,
        disclaimer=disclaimer,
    )
    text = (
        f"{sender} invited you to {scope} on {platform_name}. Create your {brand} account to sign in.\n\n"
        f"Create your account here (link valid for {valid_for}):\n{link}\n\n"
        f"{disclaimer}"
    )
    return html, text


def email_changed_email(*, old_email: str, new_email: str) -> tuple[str, str]:
    """Return (html, text) bodies for the notice of an account's email change.

    Sent to the old and the new address. It carries no link to act on: the
    owner who did not expect it contacts an administrator."""
    name = get_cached_branding().name
    title = "Your email address changed"
    old_esc, new_esc = html_escape(old_email), html_escape(new_email)
    body = email_heading(title) + email_paragraph_html(
        f"An administrator changed the email address of your {html_escape(name)} account "
        f"from <strong>{old_esc}</strong> to <strong>{new_esc}</strong>. "
        "You now sign in and receive account emails at the new address."
    )
    html = render_email(
        title=title,
        preheader=f"The email address of your {name} account changed.",
        body_html=body,
        fine_print_html=html_escape(
            "If you didn't expect this change, contact your administrator."
        ),
    )
    text = (
        f"An administrator changed the email address of your {name} account "
        f"from {old_email} to {new_email}.\n\n"
        "If you didn't expect this change, contact your administrator."
    )
    return html, text


def password_reset_email(link: str, expire_hours: int) -> tuple[str, str]:
    """Return (html, text) bodies for a password reset email."""
    name = get_cached_branding().name
    valid_for = f"{expire_hours} hours"
    html = _action_email(
        title="Reset your password",
        preheader=f"Choose a new {name} password.",
        lead_html=(
            "We received a request to reset your password. Choose a new one with "
            "the button below. The link can be used once."
        ),
        button_label="Reset password",
        link=link,
        valid_for=valid_for,
        disclaimer=("If you didn't request this, you can safely ignore this email: your password won't change."),
    )
    text = (
        f"We received a request to reset your {name} password.\n\n"
        f"Reset it here (link valid for {valid_for}):\n{link}\n\n"
        "If you didn't request this, you can ignore this email."
    )
    return html, text
