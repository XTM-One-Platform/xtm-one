# SPDX-License-Identifier: Apache-2.0

"""TemplateContainer model – non-vectorized template + asset containers.

A ``TemplateContainer`` stores the **structured metadata** of a reusable design
template (layout definitions, brand theme, fonts, colours, aspect ratio) plus
references to its binary assets (fonts, images) held in S3/MinIO through
``FileAttachment`` rows with ``source = "template_asset"``.

Why this is NOT a ``KnowledgeBase``
----------------------------------
``KnowledgeBase`` is structurally coupled to the embedding pipeline
(``embedding_model`` / ``chunk_size`` / ``chunk_overlap``, ``KBSyncSource``,
``store_knowledge`` / ``search_knowledge``): every document it holds is chunked
and embedded, and that invariant is load-bearing for sync sources, activity
logs, export packages and quotas.  Templates need the opposite contract —
retrieval by *exact structured filters* (name, tag, theme), never by semantic
similarity — so they live in their own container with **no chunking, no
embedding and no vector column anywhere in this module**.  Agents reach them
through the ``search_templates`` built-in tool, which is deliberately disjoint
from every ``search_knowledge`` code path.

Visibility follows the standard three-tier model (own + group-shared +
company-managed), like ``KnowledgeBase`` and ``Prompt``.
"""

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

#: ``FileAttachment.source`` value carried by every asset bound to a container.
#: Single source of truth — the API, the built-in tool and the cascade cleanup
#: all filter on this constant rather than re-spelling the string.
TEMPLATE_ASSET_SOURCE = "template_asset"

#: Recognised asset roles, used purely as a structured label so a consumer
#: feature can pick "the background image" or "the heading font" out of a
#: container without content sniffing. Free-form values are still accepted;
#: this tuple only drives validation hints and the tool's documentation.
#:
#: Two roles are LOAD-BEARING for the deck renderer: ``logo`` is the
#: brand mark the deck chrome draws on every slide, ``logo_light`` its
#: dark-on-light variant used by the light theme — both resolved by role when
#: the theme config names no file id (``presentations.resolve_template_theme``).
#: ``logo_mark`` (the symbol without the wordmark) and ``product_logo`` (a
#: product's own logo, one asset per product) are labels an agent picks from
#: ``search_templates`` results to place on a slide by file id.
#:
#: The predefined-asset roles tell the agent what KIND of picture it is
#: choosing, which is what a deck author asks for first ("the architecture
#: diagram", "a product screenshot", "our G2 badges", "the partner logos"):
#: ``diagram`` (architecture / flow / lifecycle figures), ``screenshot`` (a
#: product UI capture or mockup), ``badge`` (certifications, awards, marketplace
#: marks), ``partner_logo`` (a business partner's logo — reseller, MSSP,
#: alliance), ``integration_logo`` (the logo of a product the platform connects
#: to — SIEM, EDR, ticketing — for "integrations" slides) and
#: ``customer_logo`` (a customer's logo, for "trusted by" slides). The three
#: third-party logo roles are deliberately distinct from ``logo``, which the
#: deck chrome draws, and from each other: an agent asked for "the integrations
#: we support" must not be handed the reseller logos.
#:
#: ``people_photo`` is a team portrait - the ``photo_file_id`` of a
#: ``profiles`` block (founders, speakers, a leadership slide). Kept apart from
#: ``image`` so an agent asked for "the founders" is handed the portraits and
#: never a team-life photo, and so the brand deck can ship the official ones.
ASSET_ROLES: tuple[str, ...] = (
    "font",
    "image",
    "logo",
    "logo_light",
    "logo_mark",
    "product_logo",
    "partner_logo",
    "integration_logo",
    "customer_logo",
    "people_photo",
    "background",
    "icon",
    "illustration",
    "diagram",
    "screenshot",
    "badge",
    "other",
)

#: Maximum number of assets a single container may hold. Keeps one container
#: from becoming an unbounded object store (settled
#: at a generous cap rather than left unbounded). Raised from 100,
#: when the partner / integration / customer logo packs became predefined
#: assets of the brand deck itself (~180 seeded assets) instead of three
#: containers of their own; the cap still leaves room for a tenant's uploads.
MAX_ASSETS_PER_CONTAINER = 400

#: Ceiling on an asset's ``description``. It is read by an agent
#: choosing between assets, so it is a caption — "OpenAEV architecture: agents,
#: injectors and collectors around the platform" — not a document. One bound for
#: the REST, package-import, seed and authoring-tool paths.
MAX_ASSET_DESCRIPTION_CHARS = 500


class TemplateContainer(Base):
    """A reusable, non-vectorized template definition with linked assets."""

    __tablename__ = "template_containers"
    # ``user_id`` is indexed inline. The company-managed flag is filtered on by
    # every list endpoint's three-tier visibility query.
    __table_args__ = (
        Index("ix_template_containers_is_company_managed", "is_company_managed"),
        Index("ix_template_containers_kind", "kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # Company-managed containers are shared with all users; only admins can
    # edit them. ``user_id`` is NULL when True.
    is_company_managed: Mapped[bool] = mapped_column(Boolean, default=False)

    # Group sharing: list of Group UUIDs this entity is shared with.
    shared_group_ids: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    shared_group_permissions: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )

    # Per-user sharing + company-managed narrowing. Same shape as the
    # group columns above, keyed by User UUID; see ``app.models.agent`` for the
    # rationale and ``app.api.v1.helpers.user_visible_filter`` for enforcement.
    shared_user_ids: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    shared_user_permissions: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    access_restricted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Which consumer feature the template targets ("presentation", "report",
    # "dashboard", …). A free-form structured label rather than an enum: the
    # container is a generic building block and new consumers must not require
    # a migration. Indexed because it is the coarsest search filter.
    kind: Mapped[str] = mapped_column(String(50), nullable=False, default="presentation")

    # Free-form labels for structured filtering (the agent-facing search
    # matches on these, exact-match and case-insensitive).
    tags: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)

    # Brand/visual identity of the template: ``theme_config`` carries the actual
    # values (palette, font families) the consumer renders with. The colour
    # schemes a template can dress a deck in (``dark`` / ``light``) are DERIVED
    # from that palette on every read (``template_supported_schemes``), never
    # stored. ``theme`` is the LEGACY free-text search label: it renders
    # nothing, the UI and the agent tools no longer write it, and it survives
    # only so a saved ``?theme=<label>`` filter keeps matching exactly.
    theme: Mapped[str | None] = mapped_column(String(100), nullable=True)
    theme_config: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=dict)

    # e.g. "16:9", "4:3", "A4-portrait" — searchable because a consumer picks a
    # template by target surface before anything else.
    aspect_ratio: Mapped[str | None] = mapped_column(String(30), nullable=True)

    # Available layout definitions, e.g.
    #   [{"id": "title-slide", "name": "Title", "regions": [...]}, ...]
    # Structure is owned by the consumer feature; this model only stores it.
    layouts: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)

    # Escape hatch for consumer-specific structured attributes so a new
    # generative feature never needs a migration to carry its own fields.
    extra_metadata: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=dict
    )

    # Usage analytics, mirroring ``KnowledgeBase.query_count``. Bumped by the
    # structured search tool so "template search hit rate" is answerable
    # without a separate metrics pipeline.
    lookup_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=sa.text("0"), nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
