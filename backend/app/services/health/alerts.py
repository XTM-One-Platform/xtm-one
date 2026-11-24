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

"""Platform Health alert emails (#101 S12, ``dev-docs/platform-health.md`` sections 7 and 9).

Off by default (``HealthSettings.alert_emails``). When an administrator turns
them on and names recipients:

* **Immediate**: one email per finding of critical severity once it has been
  open continuously for ``alert_after_minutes`` (default 15, #958), at most once
  per open episode: ``alerted_at`` is stamped on the row, and the Redis key
  ``health:alert:<finding id>:<opened_at>`` is set with ``NX`` before the send, so a
  second replica never sends it twice. A finding that resolves within the window
  never mails: a restart caught by a pass sends nothing.
* **Daily digest**: one email a day (UTC) listing the open findings at or above
  the minimum severity, sent only when there is at least one; the day is marked
  in ``health_schedule_marks`` under ``alert:digest``.

Sent with :func:`app.services.email.deliver_email` from the platform sender, not
through the agent tool of #759. No model call, not metered, nothing sent when
SMTP is not configured. A failed send is logged and never fails the pass.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from html import escape as html_escape

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.health_check import STATUS_OPEN, HealthCheckFinding
from app.services.health.settings import HealthAlertEmailSettings, load_health_settings

logger = logging.getLogger(__name__)

#: The schedule mark of the daily digest (``health_schedule_marks.schedule``, 16 characters at most).
DIGEST_MARK = "alert:digest"
#: How long the dedupe key of an immediate alert lives (the episode stamp ``alerted_at`` is the durable guard).
DEDUPE_TTL = timedelta(days=7)
#: The most findings one digest lists; the rest are counted.
DIGEST_MAX_ROWS = 50
#: Severities from the most to the least severe.
_RANK = {"critical": 0, "warning": 1, "info": 2}
_MODULE_LABELS = {
    "agents": "Agents",
    "fleets": "Fleets",
    "xtm_one": "XTM One",
    "opencti": "OpenCTI",
    "openaev": "OpenAEV",
    "opencrq": "OpenCRQ",
}


def at_or_above(severity: str, minimum: str) -> bool:
    """Whether *severity* is as severe as *minimum* or more."""
    return _RANK.get(severity, 99) <= _RANK.get(minimum, 0)


def dedupe_key(finding_id: str, opened_at: datetime | None = None) -> str:
    """The dedupe key of the immediate alert of one open episode of one finding."""
    episode = f":{opened_at.astimezone(UTC).isoformat()}" if opened_at is not None else ""
    return f"health:alert:{finding_id}{episode}"


def _health_link() -> str:
    from app.services.email import platform_url

    return f"{platform_url()}/health"


def _title(finding: HealthCheckFinding) -> str:
    return f"{_MODULE_LABELS.get(finding.module, finding.module)}: {finding.check_id}"


def finding_email(finding: HealthCheckFinding) -> tuple[str, str, str]:
    """``(subject, html, text)`` of the immediate alert of one critical finding."""
    from app.services.email import EmailPanelItem, email_button, email_heading, email_panel, render_email

    title = _title(finding)
    subject = f"Platform Health: critical finding, {title}"
    recommendation = finding.recommendation or "Open Platform Health for the evidence and the recommendation."
    link = _health_link()
    body = (
        email_heading("A critical finding opened")
        + email_panel(
            [
                EmailPanelItem(
                    eyebrow=f"{finding.severity} \u00b7 {finding.category}",
                    title=title,
                    detail=_MODULE_LABELS.get(finding.module, finding.module),
                    note=recommendation,
                )
            ]
        )
        + email_button("Open Platform Health", link)
    )
    html = render_email(
        title=subject,
        preheader=recommendation,
        body_html=body,
        fine_print_html=html_escape(
            "You get this email because an administrator listed you as a recipient of the Platform Health alert emails."
        ),
    )
    text = f"A critical finding opened: {title}\n\n{recommendation}\n\nOpen Platform Health: {link}\n"
    return subject, html, text


def digest_email(findings: Sequence[HealthCheckFinding], *, total: int, day: str) -> tuple[str, str, str]:
    """``(subject, html, text)`` of the daily digest of the open findings."""
    from app.services.email import (
        EmailPanelItem,
        email_button,
        email_heading,
        email_panel,
        email_paragraph,
        render_email,
    )

    subject = f"Platform Health: {total} open finding{'s' if total != 1 else ''} on {day}"
    link = _health_link()
    items = [
        EmailPanelItem(
            eyebrow=f"{row.severity} \u00b7 {_MODULE_LABELS.get(row.module, row.module)}",
            title=row.check_id,
            note=row.recommendation or None,
        )
        for row in findings
    ]
    more = total - len(findings)
    body = (
        email_heading("Open Platform Health findings")
        + email_panel(items)
        + (email_paragraph(f"And {more} more on the Platform Health page.") if more > 0 else "")
        + email_button("Open Platform Health", link)
    )
    html = render_email(
        title=subject,
        preheader=f"{total} open finding{'s' if total != 1 else ''}.",
        body_html=body,
        fine_print_html=html_escape(
            "You get this daily email because an administrator listed you as a recipient of the Platform Health alert emails."
        ),
    )
    lines = [f"- [{row.severity}] {_MODULE_LABELS.get(row.module, row.module)}: {row.check_id}" for row in findings]
    if more > 0:
        lines.append(f"And {more} more.")
    text = f"{subject}\n\n" + "\n".join(lines) + f"\n\nOpen Platform Health: {link}\n"
    return subject, html, text


async def _send_to_all(recipients: Sequence[str], subject: str, html: str, text: str) -> int:
    from app.services.email import EmailDeliveryError, deliver_email

    sent = 0
    for to in recipients:
        try:
            await deliver_email(to=to, subject=subject, html=html, text=text)
            sent += 1
        except EmailDeliveryError as exc:
            logger.warning("Platform Health alert email to %s failed: %s", to, exc)
    return sent


async def _claim(key: str) -> bool:
    """Set *key* once; False when another pass or replica already did."""
    from app.core.redis import redis_client

    client = await redis_client()
    return bool(await client.set(key, "1", nx=True, ex=int(DEDUPE_TTL.total_seconds())))


def alert_due(row: HealthCheckFinding, config: HealthAlertEmailSettings, now: datetime) -> bool:
    """Whether the immediate alert of *row* is due: critical, open for the whole window, not sent this episode."""
    if row.status != STATUS_OPEN or row.severity != "critical" or row.alerted_at is not None:
        return False
    opened = row.opened_at or row.first_seen
    return opened is not None and now - opened >= timedelta(minutes=config.alert_after_minutes)


async def _due_rows(db: AsyncSession, config: HealthAlertEmailSettings, now: datetime) -> list[HealthCheckFinding]:
    return [
        row
        for row in (
            await db.execute(
                select(HealthCheckFinding).where(
                    HealthCheckFinding.status == STATUS_OPEN,
                    HealthCheckFinding.severity == "critical",
                    HealthCheckFinding.alerted_at.is_(None),
                )
            )
        ).scalars()
        if alert_due(row, config, now)
    ]


async def stamp_due_alerts(db: AsyncSession, config: HealthAlertEmailSettings, *, now: datetime) -> int:
    """Stamp the due findings as alerted without sending, while the immediate path is skipped.

    Turning the emails on, adding recipients or configuring SMTP later then mails only the
    findings that open after it, never a burst of every critical finding already open (#958).
    """
    rows = await _due_rows(db, config, now)
    for row in rows:
        row.alerted_at = now
    if rows:
        await db.commit()
    return len(rows)


async def send_immediate_alerts(
    db: AsyncSession,
    config: HealthAlertEmailSettings,
    *,
    now: datetime,
    claim: Callable[[str], Awaitable[bool]] = _claim,
) -> int:
    """One email per critical finding open for the alert window and not yet alerted; the number alerted."""
    if not config.immediate:
        await stamp_due_alerts(db, config, now=now)
        return 0
    rows = await _due_rows(db, config, now)
    alerted = 0
    for row in rows:
        row.alerted_at = now
        if not await claim(dedupe_key(row.id, row.opened_at)):
            continue
        subject, html, text = finding_email(row)
        if await _send_to_all(config.recipients, subject, html, text):
            alerted += 1
    if rows:
        await db.commit()
    return alerted


async def send_daily_digest(db: AsyncSession, config: HealthAlertEmailSettings, *, now: datetime) -> bool:
    """The day's digest once per UTC day, when a finding at or above the minimum is open."""
    from app.models.health_cycle import HealthScheduleMark

    if not config.digest:
        return False
    mark = await db.get(HealthScheduleMark, DIGEST_MARK)
    if mark is not None and mark.fired_at.astimezone(UTC).date() >= now.astimezone(UTC).date():
        return False
    open_rows = [
        row
        for row in (
            await db.execute(select(HealthCheckFinding).where(HealthCheckFinding.status == STATUS_OPEN))
        ).scalars()
        if at_or_above(row.severity, config.min_severity)
    ]
    # The day is marked whether or not there is anything to send: one read a day.
    if mark is None:
        db.add(HealthScheduleMark(schedule=DIGEST_MARK, fired_at=now))
    else:
        mark.fired_at, mark.refusal = now, None
    await db.commit()
    if not open_rows:
        return False
    open_rows.sort(key=lambda r: (_RANK.get(r.severity, 99), r.module, r.check_id))
    subject, html, text = digest_email(
        open_rows[:DIGEST_MAX_ROWS], total=len(open_rows), day=now.astimezone(UTC).date().isoformat()
    )
    return bool(await _send_to_all(config.recipients, subject, html, text))


async def send_alerts(db: AsyncSession, *, now: datetime) -> dict[str, int]:
    """The pass's alert emails: nothing sent when off, without recipients or without SMTP.

    In that state the due findings are still stamped (``stamp_due_alerts``), so enabling the
    emails later never mails the findings that were already open.
    """
    from app.services.email import is_email_configured

    config = (await load_health_settings(db)).alert_emails
    if not config.enabled or not config.recipients or not is_email_configured():
        await stamp_due_alerts(db, config, now=now)
        return {"immediate": 0, "digest": 0}
    immediate = await send_immediate_alerts(db, config, now=now)
    digest = await send_daily_digest(db, config, now=now)
    return {"immediate": immediate, "digest": int(digest)}
