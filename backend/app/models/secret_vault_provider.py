# SPDX-License-Identifier: Apache-2.0

"""A secret vault provider an administrator connected in Settings > Access and security > Secret vaults.

The database twin of a ``SECRET_VAULT_<NAME>_*`` declaration
(``app/services/vault/config.py``): references name it by :attr:`name`
(``vault://<name>/<path>#<key>``). What opens the vault (a token, an AppRole
secret id, a cloud key) is in :attr:`credentials_enc`, Fernet-encrypted, and is
never served; every other setting is in :attr:`settings`.

Who may use it in a secret field is :attr:`grants`: an empty list is
administrators only, a grant names everyone, a group or a user with the path
prefixes it covers. Administrators always have every path of an enabled
provider.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class SecretVaultProvider(Base):
    __tablename__ = "secret_vault_providers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: The ``<provider>`` of the references to it; immutable once a secret field uses it.
    name: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    #: ``hashicorp``, ``aws``, ``gcp`` or ``azure``.
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    #: The settings that open nothing (an address, a region, an auth method), by lowercase setting name.
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    #: The settings that open the vault, a Fernet-encrypted JSON object; never served, logged or traced.
    credentials_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: ``[{"principal": "everyone" | "group" | "user", "id": <uuid or null>, "paths": [<prefix>, ...]}]``.
    grants: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    #: Whether a non-administrator holding a grant may list secret names in the picker.
    browsable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: How long a value read from it stays in memory, instead of ``SECRET_VAULT_CACHE_SECONDS``.
    cache_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: The last connection test: when, whether it passed, and its reason code and detail when not.
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_check_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_check_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_check_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )
    # No foreign key: a provider outlives the account of the administrator who changed it.
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
