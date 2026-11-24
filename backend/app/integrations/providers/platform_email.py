# SPDX-License-Identifier: Apache-2.0

"""Platform email: agents send email through the platform's own SMTP server.

A company-managed integration with no credential of its own: it sends through
the SMTP server the deployment configures (``SMTP_HOSTNAME`` and the other
``SMTP_*`` settings, :mod:`app.services.email`), From the platform sender, in
the platform's email layout. The catalog offers it only while SMTP is
configured, and only an administrator creates it, company-managed
(``requires_platform_smtp`` and ``company_managed_only`` in its catalog entry).

Its configuration (``extra_config``) holds the guards:

* ``allowed_domains``: the recipient domains, one per line (a JSON list reads
  the same); an address outside them is refused, and an empty list refuses
  every recipient.
* ``daily_limit``: the emails one agent may send in a UTC day (default 50).

Every send, refused by the SMTP server or accepted, writes one audit log entry
(entity ``platform_email``) naming the agent, the recipients and the subject;
the daily limit is counted from those entries. ``send_email`` declares
``always_ask``: for an agent set to ask, every call pauses whatever the
whitelist or a standing approval says; an agent not set to ask, or approvals
suspended for the platform (``tool_approvals_enabled`` off), pauses nothing
(``hitl_whitelist.build_approval_gate``).
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, time
from html import escape as html_escape
from typing import Any

from sqlalchemy import func, select

from app.integrations.connectivity import ProbeContext, ProbeResult, register_connectivity
from app.integrations.file_delivery import read_pipe_file
from app.tools.base import CallableToolExecutor, FileContext, SendGuard, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

PROVIDER = "platform_email"
_SOURCE = f"integration:{PROVIDER}"
#: The audit entity of a send, the record the daily limit counts.
AUDIT_ENTITY = "platform_email"
AUDIT_ACTION = "send"
DEFAULT_DAILY_LIMIT = 50
MAX_DAILY_LIMIT = 1000
#: The most addresses one email names (to, cc and bcc together).
MAX_RECIPIENTS = 50
_SUBJECT_MAX = 300
_ADDRESS = re.compile(r"^[^@\s,;<>\"]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)$")
_TOKEN_SEPARATOR = "||"  # noqa: S105 - a field separator, not a secret


def domain_entries(value: Any) -> list[str]:
    """The entries of a stored domain list, trimmed, empty ones dropped.

    The form stores one domain per line; an API or tool caller may send a JSON
    list instead (``["example.com"]``). Both read the same, so a list is never
    stringified into one entry that matches no address.
    """
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [str(item) for item in value if item is not None]
    else:
        items = re.split(r"[\s,;]+", "" if value is None else str(value))
    return [item.strip() for item in items if item and item.strip()]


def platform_email_token(extra: dict[str, Any] | None) -> str:
    """The compound credential the builder reads: ``domains||limit``.

    The integration has no secret; its guards live in ``extra_config`` and
    travel to the tool builder in this token (the MITRE ATT&CK shape).
    """
    extra = extra if isinstance(extra, dict) else {}
    domains = "\n".join(domain_entries(extra.get("allowed_domains")))
    limit = str(extra.get("daily_limit") or "").strip()
    return f"{domains}{_TOKEN_SEPARATOR}{limit}"


def parse_token(token: str | None) -> tuple[frozenset[str], int]:
    """``(allowed domains, daily limit)`` from :func:`platform_email_token`'s value."""
    domains_part, _, limit_part = str(token or "").partition(_TOKEN_SEPARATOR)
    domains = frozenset(
        d.strip().lower().lstrip("@").rstrip(".")
        for d in re.split(r"[\s,;]+", domains_part)
        if d.strip() and d.strip() != "__dummy__"
    )
    try:
        limit = int(limit_part.strip())
    except ValueError:
        limit = DEFAULT_DAILY_LIMIT
    return domains, max(0, min(limit, MAX_DAILY_LIMIT))


def split_addresses(value: str | None) -> list[str]:
    """The addresses of a comma or semicolon separated list, trimmed, empty ones dropped."""
    return [a.strip() for a in re.split(r"[,;]", value or "") if a.strip()]


def refused_addresses(addresses: list[str], allowed: frozenset[str]) -> list[str]:
    """The *addresses* that are malformed or outside the *allowed* domains (subdomains excluded)."""
    refused = []
    for address in addresses:
        match = _ADDRESS.match(address)
        if match is None or match.group(1).lower() not in allowed:
            refused.append(address)
    return refused


def _day_start() -> datetime:
    return datetime.combine(datetime.now(UTC).date(), time.min, tzinfo=UTC)


async def sends_today(agent_id: str | None) -> int:
    """The sends the audit log records for *agent_id* since 00:00 UTC."""
    from app.core.database import async_session
    from app.models.audit_log import AuditLog

    async with async_session() as db:
        query = (
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.entity_type == AUDIT_ENTITY,
                AuditLog.action == AUDIT_ACTION,
                AuditLog.created_at >= _day_start(),
            )
        )
        if agent_id:
            query = query.where(AuditLog.details["agent_id"].as_string() == str(agent_id))
        return int((await db.execute(query)).scalar_one())


async def record_send(
    *,
    user_id: str | None,
    agent_id: str | None,
    recipients: list[str],
    subject: str,
    attachments: int,
    error: str | None,
) -> None:
    """One audit entry for a send (accepted or failed); never raises."""
    if not user_id:
        logger.warning("Platform email: a send without a user to anchor its audit entry")
        return
    from app.core.database import async_session
    from app.services.audit import log_system_audit

    try:
        async with async_session() as db:
            await log_system_audit(
                db,
                actor=PROVIDER,
                anchor_user_id=user_id,
                action=AUDIT_ACTION,
                entity_type=AUDIT_ENTITY,
                entity_id=agent_id if agent_id and _is_uuid(agent_id) else None,
                entity_name=subject[:500],
                summary=(
                    f"Email {'not sent' if error else 'sent'} to {len(recipients)} "
                    f"recipient{'' if len(recipients) == 1 else 's'}"
                ),
                details={
                    "agent_id": str(agent_id or ""),
                    "recipients": recipients[:MAX_RECIPIENTS],
                    "subject": subject[:_SUBJECT_MAX],
                    "attachments": attachments,
                    "status": "failed" if error else "sent",
                    "error": (error or "")[:500] or None,
                },
            )
            await db.commit()
    except Exception:
        logger.warning("Platform email: could not write the audit entry of a send", exc_info=True)


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    *,
    agent_id: str | None = None,
    user_id: str | None = None,
    count_sends_fn: Callable[[str | None], Awaitable[int]] | None = None,
    record_send_fn: Callable[..., Awaitable[None]] | None = None,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """The ``send_email`` tool, guarded by the domains and daily limit of *token*."""
    allowed, daily_limit = parse_token(token)
    count_sends = count_sends_fn or sends_today
    record = record_send_fn or record_send

    async def send_email(
        to: str,
        subject: str,
        body: str,
        cc: str = "",
        bcc: str = "",
        reply_to: str = "",
        attachment_file_ids: list[str] | None = None,
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        """Send one email through the platform's SMTP server."""
        from app.services import email as email_service
        from app.services.email_html import markdown_to_email_html

        def _error(text: str) -> ToolResult:
            return ToolResult(tool_call_id="", content=f"Error: {text}", is_error=True)

        if not email_service.is_email_configured():
            return _error("the platform has no SMTP server configured; an administrator sets SMTP_HOSTNAME.")
        subject = " ".join(str(subject or "").split())[:_SUBJECT_MAX]
        if not subject:
            return _error("subject is required.")
        if not str(body or "").strip():
            return _error("body is required.")
        to_list, cc_list, bcc_list = split_addresses(to), split_addresses(cc), split_addresses(bcc)
        reply_list = split_addresses(reply_to)
        recipients = to_list + cc_list + bcc_list
        if not to_list:
            return _error("to names no address.")
        if len(recipients) > MAX_RECIPIENTS:
            return _error(f"{len(recipients)} recipients, over the limit of {MAX_RECIPIENTS} per email.")
        if len(reply_list) > 1:
            return _error("reply_to takes one address.")
        if not allowed:
            return _error(
                "this integration allows no recipient domain: an administrator lists them in its "
                "Allowed recipient domains setting."
            )
        refused = refused_addresses(recipients + reply_list, allowed)
        if refused:
            return _error(
                f"{', '.join(refused)} {'is' if len(refused) == 1 else 'are'} not an address of an allowed "
                f"domain ({', '.join(sorted(allowed))})."
            )
        if daily_limit <= 0:
            return _error("this integration's daily limit is 0: it sends no email.")
        sent = await count_sends(agent_id)
        if sent >= daily_limit:
            return _error(
                f"this agent has sent {sent} emails today (UTC), the daily limit of this integration; "
                "it resets at 00:00 UTC."
            )

        files: list[email_service.EmailAttachment] = []
        for file_id in attachment_file_ids or []:
            piped = await read_pipe_file(
                file_context, file_id, max_bytes=email_service.MAX_ATTACHMENT_BYTES, target="an email"
            )
            if isinstance(piped, str):
                return ToolResult(tool_call_id="", content=piped, is_error=True)
            data, filename, content_type = piped
            files.append(email_service.EmailAttachment(filename=filename, content_type=content_type, data=data))
        if sum(len(f.data) for f in files) > email_service.MAX_ATTACHMENT_BYTES:
            return _error(
                f"the attached files exceed {email_service.MAX_ATTACHMENT_BYTES // 1024 // 1024} MB together."
            )

        html = email_service.render_email(
            title=html_escape(subject),
            preheader=subject,
            body_html=markdown_to_email_html(body),
        )
        headers: dict[str, str] = {}
        if cc_list:
            headers["Cc"] = ", ".join(cc_list)
        if reply_list:
            headers["Reply-To"] = reply_list[0]
        error: str | None = None
        try:
            # Bcc never travels in a header: each recipient group gets its own envelope.
            await email_service.deliver_email(
                to=", ".join(to_list), subject=subject, html=html, text=body, headers=headers, attachments=files
            )
            # The SMTP envelope is read from To and Cc: a Bcc envelope carrying the Cc header
            # would send the Cc recipients one more copy per Bcc address.
            bcc_headers = {name: value for name, value in headers.items() if name != "Cc"}
            for address in bcc_list:
                await email_service.deliver_email(
                    to=address, subject=subject, html=html, text=body, headers=bcc_headers, attachments=files
                )
        except email_service.EmailDeliveryError as exc:
            error = str(exc) or "the SMTP server refused the email"
        await record(
            user_id=user_id,
            agent_id=agent_id,
            recipients=recipients,
            subject=subject,
            attachments=len(files),
            error=error,
        )
        if error:
            return _error(f"the email was not sent: {error}")
        return (
            f"Email sent to {len(recipients)} recipient{'' if len(recipients) == 1 else 's'} "
            f'({", ".join(recipients)}), subject "{subject}"'
            + (f", {len(files)} attachment{'' if len(files) == 1 else 's'}" if files else "")
            + f". Sends today for this agent: {sent + 1} of {daily_limit}."
        )

    executor.register("send_email", send_email)
    return [
        ToolDefinition(
            name="send_email",
            description=(
                "Send an email from the platform's own address through its SMTP server, in the platform "
                "layout: to, cc, bcc (comma-separated), reply_to, a subject and a Markdown body; attach "
                "files of this conversation with attachment_file_ids (20 MB together). Only the recipient "
                "domains the integration allows are accepted, within a daily limit per agent; every send "
                "is audited and asks for approval where tool approvals are in use."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Recipient addresses, comma-separated"},
                    "subject": {"type": "string", "description": "Subject, one line"},
                    "body": {"type": "string", "description": "Body in Markdown or plain text, sent as HTML"},
                    "cc": {"type": "string", "description": "CC addresses, comma-separated", "default": ""},
                    "bcc": {"type": "string", "description": "BCC addresses, comma-separated", "default": ""},
                    "reply_to": {"type": "string", "description": "One reply-to address", "default": ""},
                    "attachment_file_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "File ids of this conversation to attach",
                    },
                },
                "required": ["to", "subject", "body"],
            },
            # The same message to the same recipients twice is refused.
            send_guard=SendGuard(recipients=("to", "cc", "bcc"), body=("subject", "body")),
            source=_SOURCE,
            priority=0,
            always_ask=True,
        ),
    ]


async def check_connectivity(ctx: ProbeContext) -> ProbeResult:
    """SMTP configured, and the guards an administrator set."""
    from app.services.email import is_email_configured

    if not is_email_configured():
        return ProbeResult.failed(
            "The platform has no SMTP server configured (SMTP_HOSTNAME is empty): nothing can be sent."
        )
    allowed, limit = parse_token(platform_email_token(ctx.extra))
    if not allowed:
        return ProbeResult.degraded("SMTP is configured, but no recipient domain is allowed: every send is refused.")
    if limit <= 0:
        return ProbeResult.degraded("SMTP is configured, but the daily limit is 0: every send is refused.")
    return ProbeResult.ok(
        f"SMTP is configured. Allowed domains: {', '.join(sorted(allowed))}; up to {limit} emails per agent a day."
    )


register_connectivity(PROVIDER, check_connectivity)
