# SPDX-License-Identifier: Apache-2.0

"""IntegrationOpenAPISpec model — the API an OpenAPI integration calls.

One row per ``provider == "openapi"`` integration, holding what was converted
from the imported Swagger 2.0 / OpenAPI 3.x document (``app.services.
openapi_import``). The document itself is NOT kept: a URL source is re-fetched
on refresh, a file source is replaced by uploading a new file.

Why a separate table rather than columns on ``integrations``: the tool
definitions of a large API weigh megabytes, and ``integrations`` rows are
loaded by every listing and visibility query. Here they are read only when
tools are built for this one integration — and even then only
``active_tools``, the selected subset.

Three JSON columns, one purpose each:

* ``operations`` — the picker's catalogue: every operation of the document,
  supported or not (with the reason), without tool schemas;
* ``tool_definitions`` — ``operation key → tool`` for every SUPPORTED
  operation, so the selection can change without re-importing;
* ``active_tools`` — the tools of ``selected_operation_keys``, in document
  order, with ``tool_overrides`` applied: exactly what the runtime registers.

``tool_overrides`` holds the tool names and descriptions the user set
(``operation key → {"name", "description"}``); they apply on top of the
converted definitions and survive a refresh of the document.

``tags`` keeps the document's tags (name, description, and the OpenAPI 3.2
``summary`` and ``parent``) so the operations are grouped as in the preview.

Credentials never live here — they stay Fernet-encrypted on the integration.
The row is deleted with its integration (``ON DELETE CASCADE``).
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class IntegrationOpenAPISpec(Base):
    __tablename__ = "integration_openapi_specs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # unique + index yields ONE unique index named ix_..., the same object the
    # migration builds, so create_all and Alembic converge.
    integration_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("integrations.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    # Where the document came from: "url" or "file".
    source_type: Mapped[str] = mapped_column(String(10), nullable=False)
    # The URL as entered (a refresh fetches it again); NULL for a file.
    source_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    source_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # "json" | "yaml"
    document_format: Mapped[str] = mapped_column(String(10), nullable=False)
    # As the document states it: "2.0", "3.0.3", "3.1.0"…
    spec_version: Mapped[str] = mapped_column(String(32), nullable=False)
    document_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    api_title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    api_version: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    api_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The spec's first server (informational: the base URL the integration
    # actually calls is its own ``extra_config.base_url``).
    default_server_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    tool_prefix: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    # The chosen authentication option (security.AuthOption.to_dict()) and
    # every option the document offered, for the detail page.
    auth_option: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    auth_options: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # The document's tags (name, description, summary, parent), for grouping.
    tags: Mapped[list | None] = mapped_column(JSON, nullable=True)
    operations: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    tool_definitions: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    selected_operation_keys: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list
    )
    active_tools: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    tool_overrides: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    warnings: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    # Last time the document was (re)imported or re-fetched from its URL.
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
