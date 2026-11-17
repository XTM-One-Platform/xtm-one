# SPDX-License-Identifier: Apache-2.0

"""One person's own OAuth account on a ``per_user`` integration.

A token holder: its own tokens, status and account keys; every other column is
read from its integration. Contract: ``dev-docs/integration-user-credentials.md``.
"""

import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.integration import Integration


class IntegrationUserCredential(Base):
    __tablename__ = "integration_user_credentials"
    __table_args__ = (
        UniqueConstraint(
            "integration_id", "user_id", name="uq_integration_user_credentials_integration_user"
        ),
        Index("ix_integration_user_credentials_user_id", "user_id"),
        # The refresh sweep, as ``ix_integrations_status_auth_type_token_expires``.
        Index(
            "ix_integration_user_credentials_status_token_expires",
            "status",
            "token_expires_at",
            postgresql_where=text("refresh_token_enc IS NOT NULL"),
        ),
    )

    #: Read by the token lifecycle to tell a holder from an integration row.
    is_user_credential = True

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    integration_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("integrations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    # --- The account (Fernet) ---
    access_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The scopes the token was granted (app.integrations.granted_scopes).
    granted_scopes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Fernet JSON: the account-derived ``extra_config`` keys (see the module doc).
    account_config_enc: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ``connected`` / ``error`` / ``reauthorize``, as ``Integration.status``.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="connected")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    # Loaded with the row: the holder reads its configuration from it, and an
    # async session cannot lazy-load it later.
    integration: Mapped[Integration] = relationship(Integration, lazy="joined", innerjoin=True)

    # --- What differs from the integration's own columns ---

    @property
    def extra_config_enc(self) -> str | None:
        """The integration's configuration with this account's keys over it (Fernet JSON).

        Never the integration's own account keys (``configuration_extra``).
        Merged once per pair of stored ciphertexts: readers test then decrypt
        it, and the merge would otherwise decrypt and re-encrypt on each read.
        """
        from app.core.security import decrypt_json, encrypt_value
        from app.services.integration_user_credentials import configuration_extra

        base = self.integration.extra_config_enc
        if not base and not self.account_config_enc:
            return None
        cached = self.__dict__.get("_merged_extra")
        if cached is not None and cached[0] == (base, self.account_config_enc):
            return cached[1]
        merged = configuration_extra(self.integration)
        if self.account_config_enc:
            merged.update(decrypt_json(self.account_config_enc) or {})
        value = encrypt_value(json.dumps(merged))
        self.__dict__["_merged_extra"] = ((base, self.account_config_enc), value)
        return value

    #: The consent and the token-owner record describe the integration's own
    #: account; a person's own credential is neither lent nor transferred.
    oauth_share_acknowledged = False
    oauth_token_owner_id = None
    #: A person brings their own OAuth sign-in, never a key; the integration's
    #: key, if it has one, is not theirs to read.
    api_key_enc = None


#: The holder's own columns; every other ``Integration`` column is read from the
#: integration (``extra_config_enc`` merges both, above).
OWN_COLUMNS = frozenset(
    {
        "id",
        "user_id",
        "access_token_enc",
        "refresh_token_enc",
        "token_expires_at",
        "granted_scopes",
        "api_key_enc",
        "status",
        "created_at",
        "updated_at",
        "extra_config_enc",
        "oauth_share_acknowledged",
        "oauth_token_owner_id",
    }
)


def _integration_column(name: str) -> property:
    def _read(self: IntegrationUserCredential):
        return getattr(self.integration, name)

    _read.__name__ = name
    return property(_read, doc=f"The integration's ``{name}`` (read-only).")


for _column in Integration.__table__.columns:
    if _column.key not in OWN_COLUMNS:
        setattr(IntegrationUserCredential, _column.key, _integration_column(_column.key))
del _column
