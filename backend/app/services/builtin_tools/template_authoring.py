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

"""Built-in template authoring tools — agent-written templates.

Gated behind the ``template_authoring`` built-in tool id, this is the **write**
counterpart of the read-only ``template_search`` tools:

* ``create_template`` — write a new ``TemplateContainer`` from a conversation.
* ``update_template`` — patch a container the acting user may edit.
* ``delete_template`` — remove one, with its assets.
* ``attach_template_asset`` / ``detach_template_asset`` — bind or unbind a file
  that is **already in the platform** (something the user dropped in the
  conversation, or an artifact a tool produced).
* ``validate_presentation`` — dry-run a template definition, and check a deck
  against the template it claims to follow.

Why the id is separate from ``template_search``: reading templates is something
almost any agent wants; authoring them is not. An agent that renders a deck from
the brand template should not, by that fact alone, be able to rewrite it.

Three traps a human hits too, encoded here so the agent cannot ship a
silently-broken template:

1. **The ``theme_config`` whitelist.** Only ``THEME_CONFIG_KEYS`` reach the
   renderer; anything else is dropped at render time and the palette quietly
   does nothing. Here it is a rejection with the accepted key list, not a drop.
2. **The layout-id contract.** A slide claiming a layout outside
   ``SLIDE_LAYOUTS`` is refused by ``validate_spec``. A container advertising
   such an id is refused too (a retired v1 id is dropped silently - there is
   nothing left to advertise), and the ``fields`` of a valid one are
   derived from the spec models rather than taken from the agent.
3. **Built-in containers are full-synced on every startup.** Editing the seeded
   XTM One Brand Deck is not forbidden by RBAC — it is simply erased by the next
   deploy. Every write path refuses one and tells the agent to duplicate it.

Two rules the tools do **not** relax:

* An agent never makes a container **company-managed**. Publishing a template
  org-wide stays a human action in the Templates UI, whatever the acting user's
  role — a conversational turn is not the place to decide what the whole company
  sees. A non-admin's container is private to them, as the REST surface would
  also enforce.
* An asset can only be bound after the acting user is shown to be allowed to
  **read** the source file, through the same
  :func:`app.services.file_access.authorize_file_access` contract the download
  endpoint uses. Otherwise a file id could be laundered into a shared container.

Like every other tool closure, database work happens on a **dedicated
short-lived session** (never the caller's ``db``): tool calls run mid agentic
loop, after the caller released its pooled connection, and flushing on that
session would pin a connection across every remaining LLM call.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from app.core.utils import format_exception_message, safe_uuid
from app.models.template_container import (
    ASSET_ROLES,
    MAX_ASSETS_PER_CONTAINER,
    TEMPLATE_ASSET_SOURCE,
)
from app.services.image_geometry import image_geometry
from app.services.presentation_chrome import describe_template_chrome, template_chrome
from app.services.presentation_spec import (
    DEFAULT_SLIDE_CAP,
    MAX_SLIDE_CAP,
    PRESENTATION_ASPECT_RATIOS,
    SLIDE_LAYOUTS,
)
from app.services.presentations import (
    SUPPORTED_SCHEMES_KEY,
    THEME_CONFIG_KEYS,
    THEME_SCHEMES,
    derived_supported_schemes,
)
from app.services.report_spec import REPORT_BLOCKS
from app.services.reports import REPORT_THEME_CONFIG_KEYS
from app.services.template_containers import (
    PRESENTATION_KIND,
    TEMPLATE_KINDS,
    container_supported_schemes,
    has_color_schemes,
    kind_problem,
    normalize_kind,
    resolve_kind_update,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._shared import logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.template_container import TemplateContainer
    from app.models.user import User

#: Budget for the spec JSON echoed back by ``validate_presentation`` problems.
_PROBLEM_LIMIT = 25

#: What an agent is told to do instead of editing a code-managed container.
_BUILTIN_REFUSAL = (
    "`{name}` is a built-in template: it is re-written from code on every "
    "deployment, so any change here would be silently reverted. Call "
    "get_template to read it, then create_template with the values you want to "
    "keep — that gives the user their own copy, which they can edit for good."
)


def _coerce_json(value: object, *, expected: type) -> tuple[Any, str | None]:
    """Accept a JSON *string* where an object/array parameter is expected.

    LLMs regularly hand ``'{"accent": "#0f0"}'`` to an object-typed parameter.
    Parsing it here turns a guaranteed failure into a working call; anything
    that is neither the expected type nor a JSON encoding of it is an error.
    """
    if value is None or value == "":
        return None, None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None, f"expected a JSON {expected.__name__}, got a plain string"
    if not isinstance(value, expected):
        return None, f"expected a JSON {expected.__name__}, got {type(value).__name__}"
    return value, None


def _problem_block(problems: list[str], *, headline: str) -> str:
    shown = problems[:_PROBLEM_LIMIT]
    lines = [headline, *(f"  - {p}" for p in shown)]
    if len(problems) > _PROBLEM_LIMIT:
        lines.append(f"  - … and {len(problems) - _PROBLEM_LIMIT} more.")
    return "\n".join(lines)


def _parse_schemes(raw: object) -> tuple[list[str] | None, str | None]:
    """``"dark"`` / ``"dark,light"`` / ``["light"]`` -> canonical list, or a problem.

    ``None`` when nothing was passed (the caller then keeps or derives the
    declaration); a problem string for an unknown scheme, so the agent learns
    the vocabulary instead of storing a scheme no deck can be rendered in.
    """
    if raw is None or raw == "":
        return None, None
    items = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
    wanted = {str(item).strip().lower() for item in items if str(item).strip()}
    if not wanted:
        return None, None
    unknown = sorted(wanted - set(THEME_SCHEMES))
    if unknown:
        return None, (
            f"supported_schemes: unknown scheme(s) {', '.join(unknown)} - a deck theme "
            f"is one of {', '.join(THEME_SCHEMES)}"
        )
    return [scheme for scheme in THEME_SCHEMES if scheme in wanted], None


def _declare_schemes(
    metadata: dict | None,
    *,
    kind: str,
    theme_config: dict | None,
    requested: list[str] | None,
) -> dict | None:
    """Return *metadata* with an explicit ``supported_schemes`` declaration.

    A template SAYS which colour schemes it supports. *requested* (the
    tool's own parameter) wins over a value already in *metadata*; a
    presentation template that declares nothing is pinned to what its palettes
    currently support - or to both schemes when it has no palette yet - so
    every template an agent authors carries a declaration, and the luminance
    fallback is reserved for rows that predate it. A video template declares
    its schemes the same way. Other kinds keep *metadata* as it is
    (``validate_supported_schemes`` drops the key).
    """
    if not has_color_schemes(kind):
        return metadata
    result = dict(metadata or {})
    if requested is not None:
        result[SUPPORTED_SCHEMES_KEY] = requested
    elif SUPPORTED_SCHEMES_KEY not in result:
        result[SUPPORTED_SCHEMES_KEY] = derived_supported_schemes(theme_config or {}, result) or list(
            THEME_SCHEMES
        )
    return result


def _describe(container: TemplateContainer, *, asset_count: int | None = None) -> str:
    """Compact read-back so the agent can quote the result to the user."""
    bits = [
        f"**{container.name}** (template_id: `{container.id}`, "
        f"kind: {container.kind})"
    ]
    if container.aspect_ratio:
        bits.append(f"  - aspect ratio: {container.aspect_ratio}")
    if container.tags:
        bits.append(f"  - tags: {', '.join(container.tags)}")
    theme_config = container.theme_config or {}
    if theme_config:
        bits.append(
            "  - theme config: "
            + ", ".join(f"{k}={v}" for k, v in sorted(theme_config.items()))
        )
    # The deck themes this design can dress a deck in: the template's
    # own declaration, the same list the deck editor restricts its
    # theme picker to.
    schemes = container_supported_schemes(container)
    if schemes:
        only = " only" if len(schemes) == 1 else ""
        bits.append(f"  - deck themes supported: {' / '.join(schemes)}{only}")
    # The footer / header every deck it dresses draws, so the agent
    # can tell the user what the slides will read and which field fills what.
    bits.extend(describe_template_chrome(template_chrome(container.extra_metadata)))
    layouts = container.layouts or []
    if layouts:
        ids = [str(item.get("id")) for item in layouts if isinstance(item, dict)]
        bits.append(f"  - layouts ({len(ids)}): {', '.join(ids)}")
    if asset_count is not None:
        bits.append(f"  - assets: {asset_count}")
    bits.append(
        "  - visibility: "
        + (
            "company-managed (visible org-wide)"
            if container.is_company_managed
            else "private to its owner"
        )
    )
    return "\n".join(bits)


def build_template_authoring_tools(
    db: AsyncSession,
    user_id: str,
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """Build the template write tools for an agent.

    *user_id* is the acting user: every container is created owned by them, and
    every edit goes through the same ``can_manage`` check the REST surface uses.
    ``"anonymous"`` (an external-channel caller with no linked account) can
    author nothing — there would be no owner for the row, and a container with
    no owner is a company-managed one, which is exactly what this tool refuses
    to create.
    """
    del db  # every operation opens its own short-lived session
    acting_user_id = user_id if user_id and user_id != "anonymous" else None

    async def _load_actor(db_session: AsyncSession) -> User | None:
        from sqlalchemy import select

        from app.models.user import User as UserModel

        user_uuid = safe_uuid(acting_user_id) if acting_user_id else None
        if user_uuid is None:
            return None
        result = await db_session.execute(
            select(UserModel).where(UserModel.id == user_uuid)
        )
        return result.scalar_one_or_none()

    async def _writable(
        db_session: AsyncSession,
        actor: User,
        template_id: str,
        *,
        action: str,
    ) -> tuple[TemplateContainer | None, str | None]:
        """Resolve a container the actor may edit, or an agent-readable refusal."""
        from fastapi import HTTPException

        from app.api.v1.helpers import get_manageable_entity
        from app.models.template_container import TemplateContainer as Container
        from app.services.template_containers import is_builtin_container

        target = (template_id or "").strip()
        if safe_uuid(target) is None:
            return None, (
                "Error: template_id must be the UUID of a template container "
                "(use search_templates to find one)."
            )
        try:
            container = await get_manageable_entity(
                db_session,
                Container,
                actor,
                target,
                action=action,
                entity_label="Template container",
            )
        except HTTPException as exc:
            # 403 / 404 carry a message written for a human; it reads correctly
            # to the model too, and hiding it would leave the agent guessing
            # between "does not exist" and "not yours".
            return None, f"Error: {exc.detail}"
        if is_builtin_container(container):
            return None, _BUILTIN_REFUSAL.format(name=container.name)
        return container, None

    # ── create ───────────────────────────────────────────────────────────────

    async def _create_template(
        name: str,
        description: str = "",
        kind: str = "presentation",
        tags: str = "",
        theme_config: object = None,
        aspect_ratio: str = "",
        layouts: object = None,
        extra_metadata: object = None,
        supported_schemes: object = None,
        **_kw: object,
    ) -> str:
        # A legacy ``theme`` label argument lands in ``_kw`` and is ignored: a
        # template's deck themes are its DECLARED colour schemes, and
        # the free label rendered nothing.
        from app.core.database import async_session
        from app.models.template_container import TemplateContainer as Container
        from app.services.audit import log_audit
        from app.services.template_containers import (
            normalize_tags,
            validate_chrome,
            validate_layouts,
            validate_slide_cap,
            validate_supported_schemes,
            validate_template_heroes,
            validate_theme_config,
            validate_theme_variants,
            validate_video_style,
        )

        clean_name = (name or "").strip()
        if not clean_name:
            return "Error: name is required — templates are found by name."
        if acting_user_id is None:
            return (
                "Error: templates can only be authored for a signed-in user. "
                "An anonymous caller has no account to own the template."
            )

        clean_kind = normalize_kind(kind)
        if clean_kind is None:
            return f"Error: {kind_problem(kind)}"
        config_raw, config_err = _coerce_json(theme_config, expected=dict)
        if config_err:
            return f"Error: theme_config {config_err}."
        layouts_raw, layouts_err = _coerce_json(layouts, expected=list)
        if layouts_err:
            return f"Error: layouts {layouts_err}."
        metadata_raw, metadata_err = _coerce_json(extra_metadata, expected=dict)
        if metadata_err:
            return f"Error: extra_metadata {metadata_err}."
        requested_schemes, schemes_err = _parse_schemes(supported_schemes)
        if schemes_err:
            return f"Error: {schemes_err}."

        accepted_config, config_problems = validate_theme_config(
            config_raw, kind=clean_kind
        )
        clean_layouts, layout_problems = validate_layouts(
            layouts_raw, kind=clean_kind
        )
        # A palette for the other colour scheme rides in extra_metadata and is
        # held to the same contract as theme_config.
        metadata_raw, variant_problems = validate_theme_variants(
            metadata_raw, kind=clean_kind
        )
        # The schemes the template supports are DECLARED: the tool's
        # parameter, else what the metadata already says, else pinned from the
        # palettes so no new template relies on the legacy luminance fallback.
        metadata_raw = _declare_schemes(
            metadata_raw, kind=clean_kind, theme_config=accepted_config, requested=requested_schemes
        )
        metadata_raw, scheme_problems = validate_supported_schemes(metadata_raw, kind=clean_kind)
        # The footer / header pattern: an undeclared placeholder is a
        # refusal with the available ones, never a literal ``{{x}}`` on every slide.
        metadata_raw, chrome_problems = validate_chrome(metadata_raw, kind=clean_kind)
        # How many slides a deck of this template may hold.
        metadata_raw, cap_problems = validate_slide_cap(metadata_raw, kind=clean_kind)
        # The covers of the template's own.
        metadata_raw, hero_problems = validate_template_heroes(metadata_raw, kind=clean_kind)
        # What a video template presets, adds and locks.
        metadata_raw, style_problems = validate_video_style(metadata_raw, kind=clean_kind)
        problems = (
            config_problems
            + layout_problems
            + variant_problems
            + scheme_problems
            + chrome_problems
            + cap_problems
            + hero_problems
            + style_problems
        )
        if problems:
            return _problem_block(
                problems,
                headline=(
                    "The template was NOT created. Fix these and call the tool "
                    "again:"
                ),
            )

        try:
            async with async_session() as db_session:
                actor = await _load_actor(db_session)
                if actor is None:
                    return "Error: could not identify the acting user."
                container = Container(
                    user_id=actor.id,
                    name=clean_name[:200],
                    description=(description or "").strip() or None,
                    kind=clean_kind,
                    tags=normalize_tags(
                        [t.strip() for t in (tags or "").split(",") if t.strip()]
                    ),
                    theme_config=accepted_config,
                    aspect_ratio=(aspect_ratio or "").strip() or None,
                    layouts=clean_layouts,
                    extra_metadata=metadata_raw or {},
                    # Never org-wide from a conversation, whatever the actor's
                    # role: promoting a template is a deliberate human action in
                    # the Templates UI.
                    is_company_managed=False,
                )
                db_session.add(container)
                await db_session.flush()
                await log_audit(
                    db_session,
                    user=actor,
                    action="create",
                    entity_type="template_container",
                    entity_id=container.id,
                    entity_name=container.name,
                    summary=(
                        f"Agent created template container '{container.name}'"
                    ),
                    details={
                        "kind": container.kind,
                        "theme": container.theme,
                        "aspect_ratio": container.aspect_ratio,
                        "tags": container.tags or [],
                        "layout_count": len(container.layouts or []),
                        "is_company_managed": False,
                        "via": "builtin_tool:create_template",
                    },
                )
                await db_session.commit()
                await db_session.refresh(container)
                readback = _describe(container, asset_count=0)
        except Exception as exc:
            logger.error(
                "create_template tool failed: %s",
                format_exception_message(exc),
                exc_info=True,
            )
            return "Error: failed to create the template. Please try again."

        return (
            f"Created template {readback}\n\n"
            "Next: call generate_presentation with this template_id and a short "
            "sample deck so the user can SEE the palette instead of reading hex "
            "values. Keep the returned presentation_id and pass it back on the "
            "next iteration so the same deck updates in place."
        )

    # ── update ───────────────────────────────────────────────────────────────

    async def _update_template(
        template_id: str,
        name: str = "",
        description: str = "",
        kind: str = "",
        tags: str = "",
        theme_config: object = None,
        aspect_ratio: str = "",
        layouts: object = None,
        extra_metadata: object = None,
        supported_schemes: object = None,
        **_kw: object,
    ) -> str:
        # A legacy ``theme`` label argument lands in ``_kw`` and is ignored.
        from app.core.database import async_session
        from app.services.audit import log_audit
        from app.services.template_containers import (
            count_assets_by_container,
            normalize_tags,
            validate_chrome,
            validate_layouts,
            validate_slide_cap,
            validate_supported_schemes,
            validate_template_heroes,
            validate_theme_config,
            validate_theme_variants,
            validate_video_style,
        )

        config_raw, config_err = _coerce_json(theme_config, expected=dict)
        if config_err:
            return f"Error: theme_config {config_err}."
        layouts_raw, layouts_err = _coerce_json(layouts, expected=list)
        if layouts_err:
            return f"Error: layouts {layouts_err}."
        metadata_raw, metadata_err = _coerce_json(extra_metadata, expected=dict)
        if metadata_err:
            return f"Error: extra_metadata {metadata_err}."
        requested_schemes, schemes_err = _parse_schemes(supported_schemes)
        if schemes_err:
            return f"Error: {schemes_err}."

        try:
            async with async_session() as db_session:
                actor = await _load_actor(db_session)
                if actor is None:
                    return (
                        "Error: templates can only be edited by a signed-in user."
                    )
                container, refusal = await _writable(
                    db_session, actor, template_id, action="edit"
                )
                if container is None:
                    return refusal or "Error: template not found."

                # The closed choice applies to a CHANGE of kind; an echo of the
                # stored kind - a legacy ``asset-library`` included - is a
                # no-op, the same rule as the REST update
                # (``resolve_kind_update``). It needs the row, hence here.
                requested_kind: str | None = None
                if (kind or "").strip():
                    requested_kind = resolve_kind_update(container.kind, kind)
                    if requested_kind is None:
                        return f"Error: {kind_problem(kind)}"

                # ``kind`` decides whether the layout catalog is checked against
                # SLIDE_LAYOUTS, so resolve the post-update value before
                # validating the layouts against it.
                target_kind = requested_kind or container.kind
                problems: list[str] = []
                accepted_config = None
                if config_raw is not None:
                    accepted_config, config_problems = validate_theme_config(
                        config_raw, kind=target_kind
                    )
                    problems += config_problems
                clean_layouts = None
                if layouts_raw is not None:
                    clean_layouts, layout_problems = validate_layouts(
                        layouts_raw, kind=target_kind
                    )
                    problems += layout_problems
                if requested_schemes is not None and not has_color_schemes(target_kind):
                    problems.append(
                        "supported_schemes applies to presentation and video templates only: "
                        f"`{container.name}` dresses a {target_kind}, which has no colour scheme."
                    )
                # ``resolve_kind_update`` already folded an echo of the stored
                # kind into ``requested_kind``; this is the ONE reading of
                # "does the kind change" the rest of the call goes by.
                kind_changes = requested_kind is not None and requested_kind != container.kind
                authored_metadata = metadata_raw is not None
                if not authored_metadata and (requested_schemes is not None or kind_changes):
                    # A scheme change alone edits the stored metadata in place:
                    # the declaration lives there, next to the deck defaults. A
                    # change of KIND re-reads it too, against the kind the row
                    # ends up with - the REST update's rule: the deck-only
                    # declarations (the supported schemes, the header / footer
                    # pattern) are dropped from a template that stops dressing
                    # decks, instead of surviving on a report template whose
                    # read-back would keep describing a footer no deck draws.
                    metadata_raw = dict(container.extra_metadata or {})
                dropped_keys: list[str] = []
                if metadata_raw is not None:
                    keys_before = set(metadata_raw)
                    if authored_metadata or requested_schemes is not None:
                        # The palette variants are held to the whitelist when
                        # this call wrote metadata or a scheme; a kind change
                        # alone keeps them as the REST update does - the two
                        # kind-dependent keys below drop, they never refuse.
                        metadata_raw, variant_problems = validate_theme_variants(
                            metadata_raw, kind=target_kind
                        )
                        problems += variant_problems
                    if requested_schemes is not None:
                        metadata_raw = {**metadata_raw, SUPPORTED_SCHEMES_KEY: requested_schemes}
                    metadata_raw, scheme_problems = validate_supported_schemes(
                        metadata_raw, kind=target_kind
                    )
                    problems += scheme_problems
                    metadata_raw, chrome_problems = validate_chrome(metadata_raw, kind=target_kind)
                    problems += chrome_problems
                    metadata_raw, cap_problems = validate_slide_cap(metadata_raw, kind=target_kind)
                    problems += cap_problems
                    metadata_raw, hero_problems = validate_template_heroes(metadata_raw, kind=target_kind)
                    problems += hero_problems
                    metadata_raw, style_problems = validate_video_style(metadata_raw, kind=target_kind)
                    problems += style_problems
                    if target_kind != PRESENTATION_KIND:
                        # The validators drop a deck-only key from a template
                        # that dresses no deck WITHOUT a problem (a kind change
                        # must not make the row unsaveable) - so the result
                        # says which keys went, instead of the agent finding
                        # `extra_metadata` in the changed list of a call that
                        # passed none, or a footer gone from the read-back with
                        # no word on why.
                        dropped_keys = sorted(keys_before - set(metadata_raw))
                if problems:
                    return _problem_block(
                        problems,
                        headline=(
                            f"`{container.name}` was NOT modified. Fix these and "
                            "call the tool again:"
                        ),
                    )

                changed: list[str] = []
                if (name or "").strip():
                    container.name = name.strip()[:200]
                    changed.append("name")
                if (description or "").strip():
                    container.description = description.strip()
                    changed.append("description")
                if kind_changes:
                    container.kind = target_kind
                    changed.append("kind")
                if (tags or "").strip():
                    container.tags = normalize_tags(
                        [t.strip() for t in tags.split(",") if t.strip()]
                    )
                    changed.append("tags")
                if accepted_config is not None:
                    container.theme_config = accepted_config
                    changed.append("theme_config")
                if (aspect_ratio or "").strip():
                    container.aspect_ratio = aspect_ratio.strip()
                    changed.append("aspect_ratio")
                if clean_layouts is not None:
                    container.layouts = clean_layouts
                    changed.append("layouts")
                if metadata_raw is not None and metadata_raw != (container.extra_metadata or {}):
                    # Written only when the bytes differ, whichever way they
                    # got here: metadata re-read for a kind change and left
                    # as it was (nothing deck-only to drop), a PATCH that
                    # echoes the stored metadata, or a scheme the template
                    # already declared is NOT a change - so the account and
                    # the audit say "Updated kind", never "kind, extra_metadata"
                    # for bytes that did not move.
                    container.extra_metadata = metadata_raw
                    changed.append("extra_metadata")

                if not changed:
                    echoed = [
                        field
                        for field, supplied in (
                            ("kind", requested_kind is not None),
                            ("extra_metadata", authored_metadata),
                            ("supported_schemes", requested_schemes is not None),
                        )
                        if supplied
                    ]
                    if echoed:
                        # Something WAS passed - it just already read that way.
                        # Said as such: "every field was left empty" would send
                        # the agent looking for a missing argument.
                        return (
                            f"No change on `{container.name}`: {', '.join(echoed)} "
                            "already read that way, so nothing was written."
                        )
                    return (
                        f"Nothing to change on `{container.name}` — every field "
                        "was left empty. Pass the field(s) you want to set."
                    )

                await log_audit(
                    db_session,
                    user=actor,
                    action="update",
                    entity_type="template_container",
                    entity_id=container.id,
                    entity_name=container.name,
                    summary=(
                        f"Agent updated template container '{container.name}'"
                    ),
                    details={
                        "fields": changed,
                        "kind": container.kind,
                        "theme": container.theme,
                        "aspect_ratio": container.aspect_ratio,
                        "tags": container.tags or [],
                        "layout_count": len(container.layouts or []),
                        "is_company_managed": container.is_company_managed,
                        "via": "builtin_tool:update_template",
                    },
                )
                await db_session.commit()
                await db_session.refresh(container)
                counts = await count_assets_by_container(db_session, [container.id])
                readback = _describe(
                    container, asset_count=counts.get(str(container.id), 0)
                )
                shared = container.is_company_managed
        except Exception as exc:
            logger.error(
                "update_template tool failed: %s",
                format_exception_message(exc),
                exc_info=True,
            )
            return "Error: failed to update the template. Please try again."

        dropped = (
            f"\n\nDropped from extra_metadata: {', '.join(dropped_keys)} - "
            f"deck-only declaration(s) a {target_kind} template never reads."
            if dropped_keys
            else ""
        )
        warning = (
            "\n\nNOTE: this template is company-managed — the change is visible "
            "to everyone in the organisation. Say so in your reply."
            if shared
            else ""
        )
        return (
            f"Updated {', '.join(changed)} on {readback}{dropped}{warning}\n\n"
            "Regenerate the preview deck (generate_presentation with the same "
            "presentation_id and this template_id) so the user sees the change."
        )

    # ── delete ───────────────────────────────────────────────────────────────

    async def _delete_template(template_id: str, **_kw: object) -> str:
        from app.core.database import async_session
        from app.services.audit import log_audit
        from app.services.file_cleanup import delete_file_attachments

        try:
            async with async_session() as db_session:
                actor = await _load_actor(db_session)
                if actor is None:
                    return (
                        "Error: templates can only be deleted by a signed-in user."
                    )
                container, refusal = await _writable(
                    db_session, actor, template_id, action="delete"
                )
                if container is None:
                    return refusal or "Error: template not found."

                name = container.name
                removed = await delete_file_attachments(
                    db_session, template_container_id=container.id
                )
                await log_audit(
                    db_session,
                    user=actor,
                    action="delete",
                    entity_type="template_container",
                    entity_id=container.id,
                    entity_name=name,
                    summary=f"Agent deleted template container '{name}'",
                    details={
                        "kind": container.kind,
                        "is_company_managed": container.is_company_managed,
                        "assets_deleted": removed,
                        "via": "builtin_tool:delete_template",
                    },
                )
                await db_session.delete(container)
                await db_session.commit()
        except Exception as exc:
            logger.error(
                "delete_template tool failed: %s",
                format_exception_message(exc),
                exc_info=True,
            )
            return "Error: failed to delete the template. Please try again."

        return (
            f"Deleted template **{name}** and its {removed} asset(s). Decks "
            "already generated from it keep rendering — they carry their own "
            "spec, and only the template's palette stops being applied."
        )

    # ── assets ───────────────────────────────────────────────────────────────

    async def _attach_template_asset(
        template_id: str,
        file_id: str,
        asset_role: str = "",
        description: str = "",
        **_kw: object,
    ) -> str:
        from fastapi import HTTPException
        from sqlalchemy import select

        from app.core.database import async_session
        from app.models.file_attachment import FileAttachment
        from app.services.audit import log_audit
        from app.services.file_access import authorize_file_access
        from app.services.file_storage import file_storage
        from app.services.svg_sanitizer import SvgSanitizeError
        from app.services.template_containers import (
            load_assets,
            normalize_asset_description,
            store_template_asset_bytes,
            template_asset_sha256,
        )

        source_id = safe_uuid((file_id or "").strip())
        if source_id is None:
            return (
                "Error: file_id must be the UUID of a file already in the "
                "platform — one the user attached to this conversation (the "
                "Available Files block lists their ids) or one a tool produced."
            )
        role = (asset_role or "").strip().lower()[:50] or None
        caption = normalize_asset_description(description)

        try:
            async with async_session() as db_session:
                actor = await _load_actor(db_session)
                if actor is None:
                    return (
                        "Error: template assets can only be bound by a "
                        "signed-in user."
                    )
                container, refusal = await _writable(
                    db_session, actor, template_id, action="edit"
                )
                if container is None:
                    return refusal or "Error: template not found."

                result = await db_session.execute(
                    select(FileAttachment).where(FileAttachment.id == source_id)
                )
                source = result.scalar_one_or_none()

                # The download-authorization contract, enforced verbatim: a file
                # the acting user could not fetch over HTTP must not become
                # readable to a container's whole audience by way of this tool.
                # A missing file and an unreadable one get the same answer, so
                # the tool never confirms which ids exist.
                readable = source is not None
                if source is not None:
                    try:
                        await authorize_file_access(source, actor, db_session)
                    except HTTPException:
                        readable = False
                if not readable:
                    return (
                        f"Error: `{file_id}` is not a platform file you can "
                        "read, so it cannot be attached to a template. Ask the "
                        "user to upload it in this conversation."
                    )

                existing = await load_assets(db_session, container.id)
                if any(a.id == source.id for a in existing):
                    return (
                        f"File `{file_id}` is already an asset of "
                        f"**{container.name}**."
                    )
                if len(existing) >= MAX_ASSETS_PER_CONTAINER:
                    return (
                        f"Error: **{container.name}** already holds the maximum "
                        f"of {MAX_ASSETS_PER_CONTAINER} assets. Detach one first."
                    )

                # The bytes are COPIED to a container-owned object rather than
                # the source row being re-pointed: re-pointing would strip the
                # file out of the conversation (or run) that owns it, and hand a
                # chat attachment the container's visibility. The container then
                # owns its own copy, which is what makes deleting a template
                # able to delete its binaries. The copy goes through the one
                # template-asset write path, so a chat-uploaded SVG is
                # sanitized on its way in like every other asset.
                raw = await file_storage.download(source.s3_object_name)
                try:
                    raw, s3_object_name = await store_template_asset_bytes(
                        container.id,
                        raw,
                        filename=source.filename,
                        content_type=source.content_type,
                    )
                except SvgSanitizeError as exc:
                    return (
                        f"Error: file `{file_id}` ({source.filename}) is not a "
                        f"valid SVG document and cannot become a template asset: "
                        f"{exc}"
                    )
                asset = FileAttachment(
                    user_id=actor.id,
                    template_container_id=container.id,
                    asset_role=role,
                    description=caption,
                    filename=source.filename,
                    content_type=source.content_type,
                    size=len(raw),
                    s3_object_name=s3_object_name,
                    sha256=await template_asset_sha256(raw),
                    source=TEMPLATE_ASSET_SOURCE,
                    # The copy's geometry, read from the bytes just
                    # stored - never the source row's: the sanitizer may have
                    # changed an SVG on its way in.
                    image_meta=await image_geometry(raw, source.content_type),
                )
                db_session.add(asset)
                await db_session.flush()
                await log_audit(
                    db_session,
                    user=actor,
                    action="update",
                    entity_type="template_container",
                    entity_id=container.id,
                    entity_name=container.name,
                    summary=(
                        f"Agent attached asset '{source.filename}' to template "
                        f"container '{container.name}'"
                    ),
                    details={
                        "asset_id": str(asset.id),
                        "asset_role": role,
                        "description": caption,
                        "filename": source.filename,
                        "size": len(raw),
                        "source_file_id": str(source.id),
                        "via": "builtin_tool:attach_template_asset",
                    },
                )
                asset_id = str(asset.id)
                container_name = container.name
                filename = source.filename
                await db_session.commit()
        except Exception as exc:
            logger.error(
                "attach_template_asset tool failed: %s",
                format_exception_message(exc),
                exc_info=True,
            )
            return "Error: failed to attach the asset. Please try again."

        return (
            f"Attached **{filename}** to **{container_name}** as "
            f"[{role or 'unlabelled'}] (asset id: `{asset_id}`). Use that asset "
            "id as image_file_id / background_image_file_id when you generate a "
            "deck from this template."
        )

    async def _detach_template_asset(
        template_id: str,
        asset_id: str,
        **_kw: object,
    ) -> str:
        from app.core.database import async_session
        from app.services.audit import log_audit
        from app.services.file_cleanup import delete_file_attachments
        from app.services.template_containers import load_assets

        target = safe_uuid((asset_id or "").strip())
        if target is None:
            return "Error: asset_id must be a valid UUID (from get_template)."

        try:
            async with async_session() as db_session:
                actor = await _load_actor(db_session)
                if actor is None:
                    return (
                        "Error: template assets can only be removed by a "
                        "signed-in user."
                    )
                container, refusal = await _writable(
                    db_session, actor, template_id, action="edit"
                )
                if container is None:
                    return refusal or "Error: template not found."

                # Scoped to the container on purpose: an asset id from some
                # other container must read as "not found" here, never as a
                # cross-container delete.
                assets = await load_assets(db_session, container.id)
                asset = next((a for a in assets if a.id == target), None)
                if asset is None:
                    return (
                        f"Error: **{container.name}** has no asset "
                        f"`{asset_id}`. Call get_template to list its assets."
                    )

                filename = asset.filename
                await delete_file_attachments(db_session, id=asset.id)
                await log_audit(
                    db_session,
                    user=actor,
                    action="update",
                    entity_type="template_container",
                    entity_id=container.id,
                    entity_name=container.name,
                    summary=(
                        f"Agent detached asset '{filename}' from template "
                        f"container '{container.name}'"
                    ),
                    details={
                        "asset_id": str(target),
                        "filename": filename,
                        "via": "builtin_tool:detach_template_asset",
                    },
                )
                await db_session.commit()
                container_name = container.name
        except Exception as exc:
            logger.error(
                "detach_template_asset tool failed: %s",
                format_exception_message(exc),
                exc_info=True,
            )
            return "Error: failed to detach the asset. Please try again."

        return f"Removed **{filename}** from **{container_name}**."

    # ── validate ─────────────────────────────────────────────────────────────

    async def _validate_presentation(
        template_id: str,
        presentation_id: str = "",
        spec: object = None,
        **_kw: object,
    ) -> str:
        from sqlalchemy import select

        from app.api.v1.helpers import caller_visible_filter, get_user_group_ids
        from app.core.database import async_session
        from app.models.template_container import TemplateContainer as Container
        from app.services.presentation_spec import (
            PresentationSpecError,
            validate_spec,
        )
        from app.services.presentations import load_visible_presentation, template_slide_cap
        from app.services.template_containers import (
            validate_chrome,
            validate_layouts,
            validate_slide_cap,
            validate_supported_schemes,
            validate_template_heroes,
            validate_theme_config,
            validate_theme_variants,
            validate_video_style,
        )

        container_id = safe_uuid((template_id or "").strip())
        if container_id is None:
            return (
                "Error: template_id must be the UUID of a template container "
                "(use search_templates to find one)."
            )
        deck_id = None
        if (presentation_id or "").strip():
            deck_id = safe_uuid(presentation_id.strip())
            if deck_id is None:
                return (
                    "Error: presentation_id must be the UUID of a deck you "
                    "generated (omit it to check the template alone)."
                )
        spec_raw, spec_err = _coerce_json(spec, expected=dict)
        if spec_err:
            return f"Error: spec {spec_err}."
        if deck_id is not None and spec_raw is not None:
            return (
                "Error: pass either presentation_id (check a saved deck) or "
                "spec (check a draft you have not saved yet), not both."
            )

        try:
            async with async_session() as db_session:
                user_uuid = safe_uuid(acting_user_id) if acting_user_id else None
                group_ids = (
                    await get_user_group_ids(db_session, user_uuid)
                    if user_uuid is not None
                    else []
                )
                query = select(Container).where(
                    caller_visible_filter(Container, user_uuid, group_ids)
                )
                result = await db_session.execute(
                    query.where(Container.id == container_id)
                )
                container = result.scalar_one_or_none()
                if container is None:
                    return (
                        f"No template `{template_id}` is visible to you. Use "
                        "search_templates to list the ones you can use."
                    )

                deck_spec: dict | None = spec_raw
                deck_label = "the draft spec"
                deck_template_id = None
                if deck_id is not None:
                    if user_uuid is None:
                        return (
                            "Error: decks are only readable by a signed-in user."
                        )
                    # Checking a deck against a template is a READ: a deck
                    # shared with the user (even read-only) qualifies.
                    loaded_deck = await load_visible_presentation(
                        db_session, deck_id, user_id=user_uuid
                    )
                    if loaded_deck is None:
                        return (
                            f"No presentation `{presentation_id}` you can access "
                            "was found."
                        )
                    deck, _deck_group_ids = loaded_deck
                    deck_spec = deck.spec or {}
                    deck_label = f"deck **{deck.title}**"
                    deck_template_id = (
                        str(deck.template_container_id)
                        if deck.template_container_id
                        else None
                    )

                container_name = container.name
                container_kind = container.kind
                container_aspect = container.aspect_ratio
                container_config = container.theme_config or {}
                container_layouts = container.layouts or []
                container_metadata = dict(container.extra_metadata or {})
        except Exception as exc:
            logger.error(
                "validate_presentation tool failed: %s",
                format_exception_message(exc),
                exc_info=True,
            )
            return "Error: failed to run the validation. Please try again."

        # ── the template's own definition ────────────────────────────────────
        problems: list[str] = []
        _, config_problems = validate_theme_config(
            container_config, kind=container_kind
        )
        problems += config_problems
        _, layout_problems = validate_layouts(
            container_layouts, kind=container_kind
        )
        problems += layout_problems
        # The second palette and the scheme declaration are held to the
        # same contract the write paths enforce, so a row that predates them
        # (or was written through the REST API) is checked here too.
        _, variant_problems = validate_theme_variants(container_metadata, kind=container_kind)
        problems += variant_problems
        _, scheme_problems = validate_supported_schemes(container_metadata, kind=container_kind)
        problems += scheme_problems
        _, chrome_problems = validate_chrome(container_metadata, kind=container_kind)
        problems += chrome_problems
        _, cap_problems = validate_slide_cap(container_metadata, kind=container_kind)
        problems += cap_problems
        _, hero_problems = validate_template_heroes(container_metadata, kind=container_kind)
        problems += hero_problems
        _, style_problems = validate_video_style(container_metadata, kind=container_kind)
        problems += style_problems
        if (
            container_kind == PRESENTATION_KIND
            and container_aspect
            and container_aspect not in PRESENTATION_ASPECT_RATIOS
        ):
            problems.append(
                f"aspect_ratio '{container_aspect}' is not one a deck can "
                f"render — use one of: {', '.join(PRESENTATION_ASPECT_RATIOS)}."
            )
        if container_kind == PRESENTATION_KIND and not container_config:
            problems.append(
                "theme_config is empty, so a deck built from this template "
                "renders with the stock palette — the template contributes "
                "nothing. Set at least background, foreground and accent."
            )
        template_ok = not problems

        # ── the deck against the template ────────────────────────────────────
        if deck_spec is not None:
            try:
                # Held to THIS template's cap: the check answers "would
                # this deck be accepted under this template".
                deck_spec = validate_spec(deck_spec, slide_cap=template_slide_cap(container_metadata))
            except PresentationSpecError as exc:
                preamble = (
                    "The template definition itself is valid."
                    if template_ok
                    else _problem_block(problems, headline="Template problems:")
                )
                return (
                    f"{preamble}\n\n"
                    f"{deck_label} is not a valid presentation spec:\n{exc}"
                )

            advertised = {
                str(item.get("id"))
                for item in container_layouts
                if isinstance(item, dict) and item.get("id")
            }
            # validate_spec refused every layout outside SLIDE_LAYOUTS and
            # upgraded the retired v1 ones, so each slide below wears
            # a layout of the catalog; the only question left is the template's.
            for idx, slide in enumerate(deck_spec.get("slides") or []):
                layout = slide.get("layout")
                if advertised and layout not in advertised:
                    problems.append(
                        f"slide {idx + 1} uses layout '{layout}', which "
                        f"**{container_name}** does not advertise "
                        f"({', '.join(sorted(advertised))}). It still renders, "
                        "but the deck is off-template."
                    )
            deck_aspect = deck_spec.get("aspect_ratio")
            if container_aspect and deck_aspect and deck_aspect != container_aspect:
                problems.append(
                    f"the deck is {deck_aspect} but the template is "
                    f"{container_aspect} — pass aspect_ratio='{container_aspect}' "
                    "to generate_presentation."
                )
            if deck_id is not None and deck_template_id != str(container_id):
                problems.append(
                    "the deck is not linked to this template "
                    f"(template_container_id = {deck_template_id or 'none'}), so "
                    "none of its palette is applied. Re-run "
                    "generate_presentation with the same presentation_id and "
                    f"template_id='{template_id}'."
                )

        if not problems:
            scope = (
                f"Template **{container_name}** and {deck_label} are consistent"
                if deck_spec is not None
                else f"Template **{container_name}** is valid"
            )
            return (
                f"{scope}: every theme_config key is honoured by the renderer "
                "and every layout id exists."
            )
        return _problem_block(
            problems,
            headline=(
                f"Checked **{container_name}**"
                + (f" against {deck_label}" if deck_spec is not None else "")
                + " — problems found:"
            ),
        )

    executor = CallableToolExecutor()
    executor.register("create_template", _create_template)
    executor.register("update_template", _update_template)
    executor.register("delete_template", _delete_template)
    executor.register("attach_template_asset", _attach_template_asset)
    executor.register("detach_template_asset", _detach_template_asset)
    executor.register("validate_presentation", _validate_presentation)

    # The two constants below are interpolated, never re-typed: the tool
    # description is the only place the model learns what a valid theme key or
    # layout id is, so it has to come from the code that enforces them.
    theme_keys = ", ".join(THEME_CONFIG_KEYS)
    report_only_keys = ", ".join(
        k for k in REPORT_THEME_CONFIG_KEYS if k not in THEME_CONFIG_KEYS
    )
    layout_ids = ", ".join(SLIDE_LAYOUTS)
    block_ids = ", ".join(REPORT_BLOCKS)
    roles_hint = ", ".join(ASSET_ROLES)
    field_docs = {
        "name": {"type": "string", "description": "Template name, how it is found."},
        "description": {
            "type": "string",
            "description": "What this template is for, in one or two sentences.",
        },
        "kind": {
            "type": "string",
            "enum": list(TEMPLATE_KINDS),
            "description": (
                "What the template dresses: 'presentation' (default - slide "
                "decks and pages), 'report', or 'video' (Video Studio: the deck "
                "palette keys, and extra_metadata.video - defaults, "
                "scene_defaults, overlay_defaults, intro, outro, brand_name "
                "(the name its logo spells, when not the template's own), "
                "locked, aspect_ratios - for what every video wearing it "
                "presets, adds and keeps). Each has its own honoured theme keys; anything "
                "else is refused."
            ),
        },
        "supported_schemes": {
            "type": "string",
            "description": (
                "The colour schemes the template supports, comma-separated: "
                "'dark', 'light' or 'dark,light' (presentation templates only). "
                "A deck linked to the template can only be rendered in a scheme "
                "listed here, and the deck editor offers only these. Omit to "
                "keep the current declaration on update; on create it is then "
                "read off the palettes ('background' of theme_config plus the "
                "'theme_variants' schemes), or both when there is no palette yet."
            ),
        },
        "tags": {
            "type": "string",
            "description": (
                "Comma-separated tags used to find the template later, e.g. "
                "'acme,brand,executive'. Lower-cased on save."
            ),
        },
        "theme_config": {
            "type": "object",
            "description": (
                "The palette the template renders with. For a presentation "
                f"ONLY these keys are honoured: {theme_keys}. A report "
                f"(kind='report') also honours: {report_only_keys}. Anything "
                "else is refused rather than stored, because the renderer would "
                "silently drop it. Values are bare CSS tokens — "
                "'hsl(220 54% 8%)', '#0b1020', "
                "\"'Inter', system-ui, sans-serif\" — with no braces, "
                "semicolons, url() or comments. Its 'background' says which "
                "scheme (dark / light) this palette is drawn for; a "
                "'theme_variants' palette in extra_metadata carries the other "
                "scheme's colours. Which schemes the template SUPPORTS is the "
                "supported_schemes declaration, not a theme label. The three "
                "*_file_id keys name this template's image assets by UUID - the "
                "wordmark (logo_file_id), its dark-on-light variant "
                "(logo_light_file_id) and the bare mark the gradient cover "
                "watermarks (mark_file_id); leave them out to use the assets "
                "tagged logo / logo_light / logo_mark."
            ),
        },
        "aspect_ratio": {
            "type": "string",
            "description": (
                "Slide aspect ratio. A presentation template must use one of: "
                f"{', '.join(PRESENTATION_ASPECT_RATIOS)}."
            ),
        },
        "layouts": {
            "type": "array",
            "description": (
                "The layout catalog this template advertises. Each entry is "
                "{id, name, use_for}; for a presentation the id MUST be one of: "
                f"{layout_ids}. For a report it MUST be one of: {block_ids}. An "
                "invented id is refused — content claiming it could not be "
                "rendered as intended. The 'fields' of each layout are filled "
                "in for you from the renderer."
            ),
            "items": {"type": "object"},
        },
        "extra_metadata": {
            "type": "object",
            "description": (
                "Free-form JSON. Seven keys are READ by the presentation tooling: "
                f"'max_slides' (a whole number, 1 to {MAX_SLIDE_CAP}; how many slides a "
                f"deck of this template may hold - {DEFAULT_SLIDE_CAP} when unset; a board "
                "pack or a training deck sets more); "
                "'supported_schemes' (['dark'], ['light'] or both - the "
                "supported_schemes parameter writes it for you); 'heroes' (the brand's "
                "own covers, offered on top of plain / gradient / split / spotlight: any "
                "of mosaic, cascade, horizon, pillar - only for a brand whose design kit "
                "draws those pixel tiles); 'deck_defaults' "
                "{theme ('dark' or 'light'), transition, aspect_ratio} fills "
                "whatever a deck generated from this template leaves unset, and "
                "its per-cover objects keyed by 'title' / 'closing' fill the "
                "opening and closing slides the same way: 'hero_defaults' (one of "
                "the covers the template offers), "
                "'hero_tones' (brand, accent, "
                "gradient_from, gradient_to, info, success, warning, error, muted) "
                "and 'slide_themes' ('dark' / 'light' - the slide's own theme, so a "
                "brand opens dark on a light deck); "
                "'guidance' (3-5 sentences on how to compose a deck in this brand) "
                "is returned to whoever generates from it; 'theme_variants' "
                "{light: {...}} or {dark: {...}} is a second palette (same keys as "
                "theme_config) laid over the base when a deck's theme is of that "
                "scheme, so a template supporting both schemes brands a light deck "
                "light; 'chrome' {footer, header, variables} is the footer / header "
                "pattern every deck of this template draws on each slide, e.g. "
                "'{{event}} - {{title}} - {{classification}}': {{title}}, "
                "{{subtitle}} and {{event}} come from the deck, any other "
                "placeholder needs a variable {key, label, hint?, default?} the "
                "deck fills in chrome.fields; chrome 'watermark': true draws the "
                "logo_mark asset faint on every body slide. Anything else is "
                "authoring notes."
            ),
        },
    }

    definitions = [
        ToolDefinition(
            name="create_template",
            description=(
                "Create a reusable design template (a brand deck design - "
                "palette, fonts, logos - or a report skin) from what the user "
                "described, and get back its template_id. Use this when the "
                "user wants a TEMPLATE they can "
                "reuse — not when they want one deck, which is "
                "generate_presentation on its own.\n"
                f"theme_config accepts ONLY these keys: {theme_keys}. Layout "
                f"ids must exist: {layout_ids}. Both are refused rather than "
                "silently dropped, so a template can never be born broken.\n"
                "The template is PRIVATE to the user. Making one visible to the "
                "whole organisation is a human action in the Templates UI and "
                "cannot be done from here — say so if the user asks.\n"
                "To copy a template that already exists (including a built-in "
                "one), read it with get_template and pass the values you want "
                "to keep — never edit a built-in.\n"
                "After creating, generate a short preview deck from it so the "
                "user judges the palette by looking at it."
            ),
            input_schema={
                "type": "object",
                "properties": dict(field_docs),
                "required": ["name"],
            },
            source="builtin:design",
        ),
        ToolDefinition(
            name="update_template",
            description=(
                "Change fields on a template the user owns (or an admin may "
                "manage). Only the fields you pass are touched — leave the rest "
                "out. Use it to iterate on a palette across turns: adjust "
                "theme_config, then regenerate the preview deck with the same "
                "presentation_id so the user sees the difference in place.\n"
                f"Same guardrails as create_template: theme_config keys are "
                f"limited to {theme_keys} and layout ids to {layout_ids}; an "
                "invalid value fails the whole call and nothing is written.\n"
                "Built-in templates cannot be edited — they are rewritten from "
                "code on every deployment. Duplicate one with create_template "
                "instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "template_id": {
                        "type": "string",
                        "description": "UUID of the template to change.",
                    },
                    **field_docs,
                },
                "required": ["template_id"],
            },
            source="builtin:design",
        ),
        ToolDefinition(
            name="delete_template",
            description=(
                "Delete a template and its assets. Decks already generated from "
                "it keep working — they carry their own copy of the slides — "
                "they just stop being themed by it. Ask the user before calling "
                "this; it cannot be undone."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "template_id": {
                        "type": "string",
                        "description": "UUID of the template to delete.",
                    }
                },
                "required": ["template_id"],
            },
            source="builtin:design",
        ),
        ToolDefinition(
            name="attach_template_asset",
            description=(
                "Bind a file that is ALREADY in the platform — one the user "
                "attached to this conversation (its id is in the Available "
                "Files block) or one a tool produced — to a template as a "
                "logo, font or background. This tool does not upload anything: "
                "if the user has not sent the file yet, ask them to.\n"
                f"asset_role labels the file's purpose; conventional values: "
                f"{roles_hint}.\n"
                "The returned asset id is what you pass as image_file_id or "
                "background_image_file_id to generate_presentation."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "template_id": {
                        "type": "string",
                        "description": "UUID of the template to bind the file to.",
                    },
                    "file_id": {
                        "type": "string",
                        "description": (
                            "UUID of an existing platform file. You must have "
                            "seen this id — never guess one."
                        ),
                    },
                    "asset_role": {
                        "type": "string",
                        "description": f"What the file is for, e.g. {roles_hint}.",
                    },
                    "description": {
                        "type": "string",
                        "description": (
                            "One line saying what the asset DEPICTS ('OpenAEV "
                            "architecture: agents, injectors and collectors around "
                            "the platform'). It is how a future deck picks this "
                            "asset over the others, so write it whenever you know."
                        ),
                    },
                },
                "required": ["template_id", "file_id"],
            },
            source="builtin:design",
        ),
        ToolDefinition(
            name="detach_template_asset",
            description=(
                "Remove one asset from a template (and delete its stored copy). "
                "Asset ids come from get_template. The user's original file in "
                "the conversation is untouched."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "template_id": {
                        "type": "string",
                        "description": "UUID of the template.",
                    },
                    "asset_id": {
                        "type": "string",
                        "description": "UUID of the asset, from get_template.",
                    },
                },
                "required": ["template_id", "asset_id"],
            },
            source="builtin:design",
        ),
        ToolDefinition(
            name="validate_presentation",
            description=(
                "Check a template, and optionally check that a deck really "
                "follows it. Two ways to call it:\n"
                "- template_id alone: audit the stored template — theme_config "
                "keys the renderer ignores, layout ids that do not exist, an "
                "unrenderable aspect ratio, an empty palette.\n"
                "- template_id + presentation_id (or + spec for a draft you "
                "have not saved): additionally check the deck against the "
                "template — slide layouts outside the template's catalog, a "
                "mismatched aspect ratio, and whether the deck is actually "
                "LINKED to the template (an unlinked deck gets none of its "
                "palette, which looks like the template not working).\n"
                "Nothing is written. Run it after generating a preview deck, "
                "before telling the user the template is ready."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "template_id": {
                        "type": "string",
                        "description": "UUID of the template to check.",
                    },
                    "presentation_id": {
                        "type": "string",
                        "description": (
                            "Optional UUID of a saved deck to check against the "
                            "template."
                        ),
                    },
                    "spec": {
                        "type": "object",
                        "description": (
                            "Optional unsaved deck spec ({title, slides, …}) to "
                            "check instead of a saved deck."
                        ),
                    },
                },
                "required": ["template_id"],
            },
            source="builtin:design",
        ),
    ]

    return definitions, executor
