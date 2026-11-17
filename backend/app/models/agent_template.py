# SPDX-License-Identifier: Apache-2.0

"""AgentTemplate model – reusable agent configurations stored in the DB.

Templates are managed data, not hardcoded. Admins can create, edit, import,
and share templates via the UI. Built-in templates are seeded at first startup
and are fully editable afterward.

A template provides sensible defaults that are copied into real Agent + Assignment
records when a user creates an agent from it. After creation, the agent and its
assignments are independent — editing the template does not affect existing agents.
"""

import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.assignment import assignment_text_length_error

logger = logging.getLogger(__name__)


class AgentTemplate(Base):
    """A reusable agent configuration template."""

    __tablename__ = "agent_templates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Unique slug for referencing (e.g. "email-triage", "github-ops")
    slug: Mapped[str] = mapped_column(
        String(100), nullable=False, unique=True, index=True
    )

    # Display
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    icon: Mapped[str] = mapped_column(String(50), nullable=False, default="Bot")
    category: Mapped[str] = mapped_column(String(50), nullable=False, default="general")

    # Default persona that gets copied to the agent
    default_persona: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Default output format for agents created from this template.
    # Valid values: "markdown", "html", "json", "plain_text".
    default_output_format: Mapped[str] = mapped_column(
        String(20), nullable=False, default="markdown"
    )

    # Integration hints (informational — tells the UI what integrations are useful)
    required_integrations: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )
    required_integration_types: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )
    recommended_mcp: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )

    # What the user still has to configure once an agent is created from this
    # template — each entry drives one mandatory step of the creation wizard.
    # Entry shape: { "kind": "integration" | "knowledge_base" | "persona_field",
    #                "target": <type/name the entry points at>,
    #                "instruction": <short text shown in the wizard step> }
    must_be_defined: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )

    # Skills an agent created from this template starts with, by built-in skill
    # slug (the ``slug:<x>`` tag of a company-managed skill). A slug no skill
    # carries in this deployment is skipped. Resolved per caller by
    # ``app.services.agent_template_skills``.
    default_skills: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )

    # Default assignments — JSON array of assignment definitions that get created
    # when a user instantiates this template.
    # Each entry: { name, description, trigger_type, trigger_config, prompt, delay, ai_label }
    default_assignments: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )

    # Whether agents created from this template should have chat disabled by default
    disable_chat: Mapped[bool] = mapped_column(Boolean, default=False)

    # Built-in templates are seeded at startup; user-created ones are not
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)

    # Admins can disable templates so users can't create new agents from them
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


def default_assignment_text(
    template_slug: str, entry: Mapping[str, Any], field: str, default: str | None
) -> Any:
    """One text field of a ``default_assignments`` entry, as its column can hold it.

    The entries are admin-saved JSON, and one broken entry must not fail agent
    creation for every user of the template: a value longer than its column is
    read as absent - *default* - as a malformed ``trigger_config`` already is
    (:func:`default_assignment_trigger_config`), and logged so the template can
    be fixed. A lone UTF-16 surrogate needs no such rule: ``default_assignments``
    is a JSON column, and the engine's decoder reads one as U+FFFD.
    """
    value = entry.get(field, default)
    error = assignment_text_length_error(field, value)
    if error is None:
        return value
    logger.warning(
        "Agent template %r, default assignment: %s Using %r instead.",
        template_slug,
        error,
        default,
    )
    return default


def default_assignment_trigger_config(entry: Mapping[str, Any]) -> dict:
    """The ``trigger_config`` of a ``default_assignments`` entry, ``{}`` when it is not an object.

    The one reader of both create-from-template paths (REST and the agent
    builder). Stored as sent, a ``null`` is the JSON literal ``null`` in a NOT
    NULL object column, a row every read fails to serialize.
    """
    value = entry.get("trigger_config", {})
    return value if isinstance(value, dict) else {}


def default_assignment_manual_parameters(entry: Mapping[str, Any]) -> list[dict] | None:
    """The ``manual_trigger_parameters`` of a ``default_assignments`` entry.

    Read by both create-from-template paths, like the readers above. Only
    objects carrying a non-blank string ``key`` are kept - the trigger dialog
    names each input by it and the prompt reads ``trigger.<key>`` - so a
    malformed admin-saved entry drops the bad input instead of failing agent
    creation. ``None`` when nothing usable is left (no dialog).
    """
    value = entry.get("manual_trigger_parameters")
    if not isinstance(value, list):
        return None
    kept = [
        dict(param)
        for param in value
        if isinstance(param, dict)
        and isinstance(param.get("key"), str)
        and param["key"].strip()
    ]
    return kept or None
