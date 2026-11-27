# SPDX-License-Identifier: Apache-2.0

"""Browser profiles: a signed-in browser identity for one web application.

A profile names an application (its start URL, an optional sign-in URL),
how the platform's browser signs in to it (credentials, a TOTP secret,
interactive MFA) and the session that sign-in saved (cookies, local storage,
IndexedDB). Its credentials and its saved session are Fernet-encrypted
(invariant 2) and never served: the API answers ``has_password`` /
``has_totp`` / ``session_status`` instead. A profile is shareable: a team
uses the company's demo login without anyone seeing it.

Every browser of the platform can open a profile: a Video Studio recording
(``VideoTake.profile_id``), the agent ``browser`` tool for an agent that
binds it (``Agent.browser_profile_ids``) or a General Assistant the person
gave it to (``User.ga_browser_profile_ids``). The rules live in
``services/browser_profiles/``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.visibility import VisibilityColumns

#: Lifecycle of a browser profile's saved session. ``active`` and ``expired``
#: are the answers of a probe (``signin.check_authenticated`` on the start URL
#: in the saved session), never of the cookies a session holds; a
#: session saved and not probed yet is ``unverified``.
SESSION_NONE = "none"
SESSION_UNVERIFIED = "unverified"
SESSION_ACTIVE = "active"
SESSION_EXPIRED = "expired"
SESSION_AWAITING_MFA = "awaiting_mfa"
SESSION_SIGNING_IN = "signing_in"
SESSION_FAILED = "failed"
SESSION_STATUSES = (
    SESSION_NONE,
    SESSION_UNVERIFIED,
    SESSION_ACTIVE,
    SESSION_EXPIRED,
    SESSION_AWAITING_MFA,
    SESSION_SIGNING_IN,
    SESSION_FAILED,
)

#: Per-profile sign-in bounds (seconds): the flow's inactivity timeout
#: (counted from the last navigation), reading and writing the session, and
#: how long an MFA challenge keeps its browser open for a person.
SIGN_IN_TIMEOUT_DEFAULT = 120
SESSION_SAVE_TIMEOUT_DEFAULT = 30
MFA_TIMEOUT_DEFAULT = 300
#: How often the keep-alive probes and refreshes a new profile's saved session (minutes): every
#: profile renews by itself by default; 0 (never) is a choice a person makes.
KEEPALIVE_MINUTES_DEFAULT = 240


def _now() -> datetime:
    return datetime.now(UTC)


class BrowserProfile(VisibilityColumns, Base):
    """A signed-in browser identity for one web application."""

    __tablename__ = "browser_profiles"
    __table_args__ = (Index("ix_browser_profiles_is_company_managed", "is_company_managed"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The application's home page: where a session opens and is probed.
    start_url: Mapped[str] = mapped_column(Text, nullable=False)
    #: Where the sign-in happens, when it is not ``start_url``.
    login_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Host names (lowercase) the profile's browser may reach on a private
    #: network. Read only while the ``browser_private_targets`` security
    #: option is on; public addresses are always reachable, loopback and cloud
    #: metadata never.
    allowed_hosts: Mapped[list | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    #: The other applications the saved session signs in (``[{name, url}]``): an application that signs in
    #: through this one's identity provider opens signed in with this session. Their hosts are hosts of the
    #: profile (recordings, page snapshots, session checks per application, lookups by URL).
    apps: Mapped[list | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    #: Optional selectors the sign-in uses before its own heuristics
    #: (``services/browser_profiles/profiles.py``: ``LOGIN_CONFIG_KEYS``).
    login_config: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    #: Viewport, color scheme, locale and time zone of every context.
    browser_options: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    #: Fernet JSON ``{"username", "password", "totp_secret"}``; never served.
    credentials_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Fernet JSON of Playwright's storage state (cookies, local storage,
    #: IndexedDB); never served.
    storage_state_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=SESSION_NONE, server_default=SESSION_NONE
    )
    session_saved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    session_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The last sign-in's outcome, as the person reads it (never a secret).
    last_sign_in_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The open MFA challenge of a sign-in waiting for a code or an approval.
    mfa_challenge_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mfa_challenge_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    mfa_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Screenshot of the MFA prompt (a number to match on the phone), a file id.
    mfa_screenshot_file_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    #: Refresh the saved session this often (minutes) so it does not expire
    #: between uses; 0 = never.
    keepalive_minutes: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=KEEPALIVE_MINUTES_DEFAULT,
        server_default=sa.text(str(KEEPALIVE_MINUTES_DEFAULT)),
    )
    #: Whether the last sign-in with the stored credentials completed without a person: ``True`` when a
    #: sign-in, a dry run or the keep-alive reached the application asking for no code and no approval,
    #: ``False`` when one had to ask a person, ``None`` while none ran since the credentials were set.
    signs_in_unattended: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True, default=None)
    sign_in_timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=SIGN_IN_TIMEOUT_DEFAULT, server_default=sa.text(str(SIGN_IN_TIMEOUT_DEFAULT))
    )
    session_save_timeout_seconds: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=SESSION_SAVE_TIMEOUT_DEFAULT,
        server_default=sa.text(str(SESSION_SAVE_TIMEOUT_DEFAULT)),
    )
    mfa_timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=MFA_TIMEOUT_DEFAULT, server_default=sa.text(str(MFA_TIMEOUT_DEFAULT))
    )
    #: The last probe of the saved session: ``{authenticated, url, reason,
    #: checked_at}`` - where ``start_url`` landed with it and why it reads so.
    last_probe_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_probe_result: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
