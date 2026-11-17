# SPDX-License-Identifier: Apache-2.0

"""SSO configuration model – OIDC and SAML settings stored in DB.

A single row holds the full SSO configuration. Secrets (client_id,
client_secret) are Fernet-encrypted before storage.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class SSOConfig(Base):
    __tablename__ = "sso_config"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # ── OpenID Connect ──────────────────────────────────────────────
    oidc_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    oidc_display_name: Mapped[str] = mapped_column(String(100), default="Microsoft")
    oidc_client_id_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    oidc_client_secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    oidc_discovery_url: Mapped[str] = mapped_column(Text, default="")
    oidc_authorization_url: Mapped[str] = mapped_column(Text, default="")
    oidc_token_url: Mapped[str] = mapped_column(Text, default="")
    oidc_userinfo_url: Mapped[str] = mapped_column(Text, default="")
    oidc_scopes: Mapped[str] = mapped_column(Text, default="openid email profile")

    # ── SAML2 ───────────────────────────────────────────────────────
    saml_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    saml_display_name: Mapped[str] = mapped_column(String(100), default="Corporate SSO")
    saml_idp_metadata_url: Mapped[str] = mapped_column(Text, default="")
    saml_sp_entity_id: Mapped[str] = mapped_column(Text, default="")
    saml_requested_authn_context: Mapped[bool] = mapped_column(Boolean, default=False)
    saml_groups_mapping_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    saml_groups_auto_create: Mapped[bool] = mapped_column(Boolean, default=True)

    # ── Auto-admin at every SSO sign-in ──────────────────────────────
    # Despite "new_users" in the names, an existing account is promoted too
    # (``auth_sso._apply_auto_admin``, promote-only).
    oidc_auto_admin_new_users: Mapped[bool] = mapped_column(Boolean, default=False)
    saml_auto_admin_new_users: Mapped[bool] = mapped_column(Boolean, default=False)

    # ── On-the-fly user creation on first SSO login ──────────────────
    # Default True preserves the historical behavior: a user authenticated
    # by the IdP gets an account created on first login. When disabled,
    # only pre-provisioned accounts (matched by SSO subject or email) can
    # sign in via that SSO method.
    oidc_auto_create_users: Mapped[bool] = mapped_column(Boolean, default=True)
    saml_auto_create_users: Mapped[bool] = mapped_column(Boolean, default=True)

    # ── General auth ────────────────────────────────────────────────
    local_auth_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    registration_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Comma-separated; restricts self-registration and SSO account creation
    # (``app.core.email_domains``). Empty allows every domain.
    allowed_domains: Mapped[str] = mapped_column(Text, default="")

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
